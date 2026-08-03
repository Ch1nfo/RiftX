"""Remote Runner command handler for native PTY and ConPTY sessions."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from riftx.application.errors import EntityNotFoundError
from riftx.application.ports import ExecutionRepository, TerminalRepository
from riftx.domain import (
    Execution,
    ExecutionStatus,
    RunEvent,
    RunnerCommandKind,
    RunnerPrincipal,
    TerminalSession,
    TerminalStatus,
)
from riftx.domain.base import new_id

from ._durable_file import atomic_write_json, locked_file
from .control_client import OutputOffsetMismatch, RunnerControlClientError
from .models import TerminalLaunchRequest
from .protocols import EffectGuard
from .supervisor import ProcessTerminationError
from .terminal import TerminalSupervisor
from .terminal_identity import require_terminal_start_replay_matches

logger = logging.getLogger(__name__)

_FINAL_STATUSES = {
    ExecutionStatus.COMPLETED,
    ExecutionStatus.EXITED,
    ExecutionStatus.FAILED,
    ExecutionStatus.CANCELLED,
    ExecutionStatus.HARD_TIMEOUT,
    ExecutionStatus.LOST,
}
_PHYSICAL_STOP_PROOF_STATUSES = {
    ExecutionStatus.COMPLETED,
    ExecutionStatus.EXITED,
    ExecutionStatus.CANCELLED,
    ExecutionStatus.HARD_TIMEOUT,
}
_OPERATION_JOURNAL_SCHEMA_VERSION = "riftx.runner-operation-journal/v1"
_LEGACY_UNBOUND_OUTCOME = {"state": "legacy_unbound"}
_OPERATION_CLAIMED_OUTCOME = {"state": "effect_claimed"}


class OperationJournalConflict(RuntimeError):
    """A durable operation key is already bound to another immutable command."""

    def __init__(self, operation_key: str) -> None:
        super().__init__(
            f"Runner operation {operation_key!r} conflicts with its durable journal record"
        )
        self.operation_key = operation_key


@dataclass(frozen=True, slots=True)
class OperationJournalIdentity:
    """Immutable Runner ownership identity attached to one local operation fact."""

    command_id: str
    binding_digest: str
    envelope_digest: str

    def __post_init__(self) -> None:
        if not self.command_id or len(self.command_id) > 64:
            raise ValueError("Runner operation journal command_id is invalid")
        _validate_journal_digest(self.binding_digest, "binding_digest")
        _validate_journal_digest(self.envelope_digest, "envelope_digest")


@dataclass(frozen=True, slots=True)
class OperationJournalRecord:
    """Versioned durable fact used for exact replay and fail-closed divergence."""

    operation_key: str
    command_id: str | None
    binding_digest: str | None
    envelope_digest: str | None
    outcome: dict[str, object]
    schema_version: str = _OPERATION_JOURNAL_SCHEMA_VERSION

    @property
    def is_legacy_unbound(self) -> bool:
        return (
            self.command_id is None
            and self.binding_digest is None
            and self.envelope_digest is None
        )


@dataclass(frozen=True, slots=True)
class ResourceTombstone:
    """Digest-independent, monotonic safety state for one local resource."""

    resource_key: str
    outcome: dict[str, object]
    schema_version: str = _OPERATION_JOURNAL_SCHEMA_VERSION


@dataclass(slots=True)
class _OperationJournalState:
    records: dict[str, OperationJournalRecord]
    resource_tombstones: dict[str, ResourceTombstone]
    legacy_resource_tombstones: dict[str, ResourceTombstone]


class TerminalControlClient(Protocol):
    @property
    def principal(self) -> RunnerPrincipal | None: ...

    async def report_status(
        self,
        execution_id: str,
        status: ExecutionStatus,
        *,
        runner_command_id: str,
        runner_effect_binding_id: str,
        runner_envelope_digest: str,
        runner_binding_digest: str,
        pid: int | None = None,
        process_group_id: int | None = None,
        exit_code: int | None = None,
        physical_stop_confirmed: bool = False,
    ) -> None: ...

    async def report_output(
        self,
        execution_id: str,
        *,
        runner_command_id: str,
        runner_effect_binding_id: str,
        runner_envelope_digest: str,
        runner_binding_digest: str,
        stream: str,
        offset: int,
        data: bytes,
    ) -> int: ...


class NullRunEventRepository:
    """Runner-local terminal events are authoritative only in the Control Plane."""

    def __init__(self) -> None:
        self._sequence = 0

    async def append(
        self,
        run_id: str,
        event_type: str,
        payload: dict[str, object] | None = None,
        *,
        event_id: str | None = None,
    ) -> RunEvent:
        self._sequence += 1
        return RunEvent(
            id=event_id or new_id(),
            run_id=run_id,
            sequence=self._sequence,
            event_type=event_type,
            payload=payload or {},
        )

    async def get(self, event_id: str) -> RunEvent | None:
        del event_id
        return None

    async def append_user_message(
        self,
        run_id: str,
        message: str,
        *,
        event_id: str | None = None,
    ) -> RunEvent:
        return await self.append(
            run_id,
            "user.message",
            {"message": message},
            event_id=event_id,
        )

    async def append_terminal_projection_if_current(
        self,
        run_id: str,
        event_type: str,
        payload: dict[str, object] | None = None,
        *,
        event_id: str,
        session_id: str,
        expected_terminal_status: TerminalStatus,
        expected_execution_status: ExecutionStatus,
    ) -> RunEvent | None:
        # Runner-local instances do not own the Control Plane projection.  The
        # method exists only to preserve the repository protocol for call sites
        # that intentionally discard these events.
        del session_id, expected_terminal_status, expected_execution_status
        return await self.append(
            run_id,
            event_type,
            payload,
            event_id=event_id,
        )

    async def list_after(
        self,
        run_id: str,
        *,
        after_sequence: int = 0,
        limit: int = 100,
    ) -> Sequence[RunEvent]:
        return []


class OperationJournal:
    """Persist digest-bound Runner operation facts across command re-leases.

    ``contains`` retains the historical mixed record/resource lookup used by
    operation journals. Safety fences must use ``get_resource`` (or the explicit
    legacy lookup) so a command-attempt record can never masquerade as a resource
    tombstone.

    Legacy V2 journals were bare lists. Execution cancellation used caller-chosen
    execution keys in that format, so those entries must be structurally isolated
    from modern typed Execution-ID resource keys. ``legacy_list_resources`` opts a
    journal into that one-way migration without changing legacy semantics for the
    terminal-operation and delivery journals that stored exact operation keys.
    """

    def __init__(self, path: Path, *, legacy_list_resources: bool = False) -> None:
        self.path = path
        self._legacy_list_resources = legacy_list_resources
        self._lock = asyncio.Lock()

    async def contains(self, operation_id: str) -> bool:
        async with self._lock:
            return await asyncio.to_thread(self._contains_locked, operation_id)

    async def get_exact(
        self,
        operation_id: str,
        identity: OperationJournalIdentity,
    ) -> OperationJournalRecord | None:
        async with self._lock:
            return await asyncio.to_thread(
                self._get_exact_locked,
                operation_id,
                identity,
            )

    async def add(
        self,
        operation_id: str,
        identity: OperationJournalIdentity,
        *,
        outcome: dict[str, object],
    ) -> None:
        await self.claim(operation_id, identity, outcome=outcome)

    async def claim(
        self,
        operation_id: str,
        identity: OperationJournalIdentity,
        *,
        outcome: dict[str, object],
    ) -> bool:
        """Persist a bound fact and report whether this exact caller won.

        An existing key is idempotent only when both immutable digests and the
        outcome are equal. Any divergent reuse raises before the journal is
        changed.
        """

        async with self._lock:
            return await asyncio.to_thread(
                self._claim_locked,
                operation_id,
                identity,
                outcome,
            )

    async def transition(
        self,
        operation_id: str,
        identity: OperationJournalIdentity,
        *,
        expected_outcome: dict[str, object],
        outcome: dict[str, object],
    ) -> OperationJournalRecord:
        """CAS one exact command fact from an admitted to a durable outcome."""

        async with self._lock:
            return await asyncio.to_thread(
                self._transition_locked,
                operation_id,
                identity,
                expected_outcome,
                outcome,
            )

    async def get_resource(
        self,
        resource_key: str,
    ) -> ResourceTombstone | None:
        async with self._lock:
            return await asyncio.to_thread(self._get_resource_locked, resource_key)

    async def get_legacy_resource(
        self,
        resource_key: str,
    ) -> ResourceTombstone | None:
        """Read a pre-typed resource tombstone from its isolated namespace."""

        async with self._lock:
            return await asyncio.to_thread(
                self._get_legacy_resource_locked,
                resource_key,
            )

    async def get_resource_attempt_exact(
        self,
        resource_key: str,
        identity: OperationJournalIdentity,
    ) -> OperationJournalRecord | None:
        return await self.get_exact(_resource_attempt_key(resource_key, identity), identity)

    async def claim_resource(
        self,
        resource_key: str,
        identity: OperationJournalIdentity,
        *,
        outcome: dict[str, object],
    ) -> tuple[bool, ResourceTombstone]:
        """Atomically bind one command attempt and raise a no-restart tombstone."""

        async with self._lock:
            return await asyncio.to_thread(
                self._claim_resource_locked,
                resource_key,
                identity,
                outcome,
            )

    async def transition_resource(
        self,
        resource_key: str,
        identity: OperationJournalIdentity,
        *,
        expected_outcome: dict[str, object],
        outcome: dict[str, object],
        resource_outcome: dict[str, object],
    ) -> OperationJournalRecord:
        """CAS an exact attempt and monotonically confirm its resource stopped."""

        async with self._lock:
            return await asyncio.to_thread(
                self._transition_resource_locked,
                resource_key,
                identity,
                expected_outcome,
                outcome,
                resource_outcome,
            )

    def _contains_locked(self, operation_id: str) -> bool:
        with locked_file(self.path):
            state = self._read()
            return (
                operation_id in state.records
                or operation_id in state.resource_tombstones
            )

    def _get_exact_locked(
        self,
        operation_id: str,
        identity: OperationJournalIdentity,
    ) -> OperationJournalRecord | None:
        with locked_file(self.path):
            record = self._read().records.get(operation_id)
            if record is None:
                return None
            self._require_exact_identity(record, identity)
            return record

    def _claim_locked(
        self,
        operation_id: str,
        identity: OperationJournalIdentity,
        outcome: dict[str, object],
    ) -> bool:
        # The read/merge/replace transaction must happen under the OS lock.
        # Per-instance asyncio locks cannot protect two Runner processes (or
        # two independently constructed journal objects) sharing state_path.
        with locked_file(self.path):
            state = self._read()
            existing = state.records.get(operation_id)
            if existing is not None:
                self._require_exact_identity(existing, identity)
                if existing.outcome != outcome:
                    raise OperationJournalConflict(operation_id)
                return False
            state.records[operation_id] = OperationJournalRecord(
                operation_key=operation_id,
                command_id=identity.command_id,
                binding_digest=identity.binding_digest,
                envelope_digest=identity.envelope_digest,
                outcome=_copy_journal_outcome(outcome),
            )
            self._write(state)
            return True

    def _transition_locked(
        self,
        operation_id: str,
        identity: OperationJournalIdentity,
        expected_outcome: dict[str, object],
        outcome: dict[str, object],
    ) -> OperationJournalRecord:
        with locked_file(self.path):
            state = self._read()
            existing = state.records.get(operation_id)
            if existing is None:
                raise OperationJournalConflict(operation_id)
            self._require_exact_identity(existing, identity)
            if existing.outcome == outcome:
                return existing
            if existing.outcome != expected_outcome:
                raise OperationJournalConflict(operation_id)
            transitioned = OperationJournalRecord(
                operation_key=operation_id,
                command_id=identity.command_id,
                binding_digest=identity.binding_digest,
                envelope_digest=identity.envelope_digest,
                outcome=_copy_journal_outcome(outcome),
            )
            state.records[operation_id] = transitioned
            self._write(state)
            return transitioned

    def _get_resource_locked(self, resource_key: str) -> ResourceTombstone | None:
        with locked_file(self.path):
            return self._read().resource_tombstones.get(resource_key)

    def _get_legacy_resource_locked(
        self,
        resource_key: str,
    ) -> ResourceTombstone | None:
        with locked_file(self.path):
            return self._read().legacy_resource_tombstones.get(resource_key)

    def _claim_resource_locked(
        self,
        resource_key: str,
        identity: OperationJournalIdentity,
        outcome: dict[str, object],
    ) -> tuple[bool, ResourceTombstone]:
        attempt_key = _resource_attempt_key(resource_key, identity)
        with locked_file(self.path):
            state = self._read()
            existing = state.records.get(attempt_key)
            claimed = existing is None
            if existing is not None:
                self._require_exact_identity(existing, identity)
                if existing.outcome != outcome:
                    raise OperationJournalConflict(resource_key)
            else:
                state.records[attempt_key] = OperationJournalRecord(
                    operation_key=attempt_key,
                    command_id=identity.command_id,
                    binding_digest=identity.binding_digest,
                    envelope_digest=identity.envelope_digest,
                    outcome=_copy_journal_outcome(outcome),
                )
            tombstone = state.resource_tombstones.get(resource_key)
            if tombstone is None:
                tombstone = ResourceTombstone(
                    resource_key=resource_key,
                    outcome={"state": "cancellation_requested"},
                )
                state.resource_tombstones[resource_key] = tombstone
            elif tombstone.outcome.get("state") not in {
                "cancellation_requested",
                "physical_stop_confirmed",
                "legacy_unbound",
            }:
                raise RuntimeError(
                    f"Runner resource tombstone has an invalid outcome: {self.path}"
                )
            self._write(state)
            return claimed, tombstone

    def _transition_resource_locked(
        self,
        resource_key: str,
        identity: OperationJournalIdentity,
        expected_outcome: dict[str, object],
        outcome: dict[str, object],
        resource_outcome: dict[str, object],
    ) -> OperationJournalRecord:
        attempt_key = _resource_attempt_key(resource_key, identity)
        with locked_file(self.path):
            state = self._read()
            existing = state.records.get(attempt_key)
            if existing is None:
                raise OperationJournalConflict(resource_key)
            self._require_exact_identity(existing, identity)
            if existing.outcome != outcome:
                if existing.outcome != expected_outcome:
                    raise OperationJournalConflict(resource_key)
                existing = OperationJournalRecord(
                    operation_key=attempt_key,
                    command_id=identity.command_id,
                    binding_digest=identity.binding_digest,
                    envelope_digest=identity.envelope_digest,
                    outcome=_copy_journal_outcome(outcome),
                )
                state.records[attempt_key] = existing
            tombstone = state.resource_tombstones.get(resource_key)
            if tombstone is None:
                raise OperationJournalConflict(resource_key)
            if tombstone.outcome.get("state") == "physical_stop_confirmed":
                # A resource-level stop is monotonic. A fresh verified command
                # may bind its own attempt to the already-confirmed outcome.
                pass
            elif tombstone.outcome.get("state") in {
                "cancellation_requested",
                "legacy_unbound",
            }:
                state.resource_tombstones[resource_key] = ResourceTombstone(
                    resource_key=resource_key,
                    outcome=_copy_journal_outcome(resource_outcome),
                )
            else:
                raise RuntimeError(
                    f"Runner resource tombstone has an invalid outcome: {self.path}"
                )
            self._write(state)
            return existing

    @staticmethod
    def _require_exact_identity(
        record: OperationJournalRecord,
        identity: OperationJournalIdentity,
    ) -> None:
        if (
            record.command_id is None
            or record.binding_digest is None
            or record.envelope_digest is None
            or not hmac.compare_digest(record.command_id, identity.command_id)
            or not hmac.compare_digest(record.binding_digest, identity.binding_digest)
            or not hmac.compare_digest(record.envelope_digest, identity.envelope_digest)
        ):
            raise OperationJournalConflict(record.operation_key)

    def _read(self) -> _OperationJournalState:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return _OperationJournalState(
                records={},
                resource_tombstones={},
                legacy_resource_tombstones={},
            )
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Runner operation journal is corrupted: {self.path}") from exc
        if isinstance(raw, list):
            # V2 journals stored only string keys. Preserve them as monotonic,
            # unbound tombstones, but never treat them as an exact replay.
            if any(not isinstance(item, str) or not item for item in raw):
                raise RuntimeError(
                    f"Runner operation journal has an invalid legacy shape: {self.path}"
                )
            tombstones = {
                operation_key: ResourceTombstone(
                    resource_key=operation_key,
                    outcome=dict(_LEGACY_UNBOUND_OUTCOME),
                )
                for operation_key in raw
            }
            if self._legacy_list_resources:
                return _OperationJournalState(
                    records={},
                    resource_tombstones={},
                    legacy_resource_tombstones=tombstones,
                )
            records = {
                operation_key: OperationJournalRecord(
                    operation_key=operation_key,
                    command_id=None,
                    binding_digest=None,
                    envelope_digest=None,
                    outcome=dict(_LEGACY_UNBOUND_OUTCOME),
                )
                for operation_key in raw
            }
            return _OperationJournalState(
                records=records,
                resource_tombstones=tombstones,
                legacy_resource_tombstones={},
            )
        if (
            not isinstance(raw, dict)
            or raw.get("schema_version") != _OPERATION_JOURNAL_SCHEMA_VERSION
            or not isinstance(raw.get("records"), list)
            or not isinstance(raw.get("resource_tombstones", []), list)
            or not isinstance(raw.get("legacy_resource_tombstones", []), list)
        ):
            raise RuntimeError(f"Runner operation journal has an invalid shape: {self.path}")
        records: dict[str, OperationJournalRecord] = {}
        for item in raw["records"]:
            record = _parse_journal_record(item, path=self.path)
            if record.operation_key in records:
                raise RuntimeError(
                    f"Runner operation journal contains duplicate keys: {self.path}"
                )
            records[record.operation_key] = record
        tombstones: dict[str, ResourceTombstone] = {}
        for item in raw.get("resource_tombstones", []):
            tombstone = _parse_resource_tombstone(item, path=self.path)
            if tombstone.resource_key in tombstones:
                raise RuntimeError(
                    f"Runner operation journal contains duplicate resource tombstones: "
                    f"{self.path}"
                )
            tombstones[tombstone.resource_key] = tombstone
        legacy_tombstones: dict[str, ResourceTombstone] = {}
        for item in raw.get("legacy_resource_tombstones", []):
            tombstone = _parse_resource_tombstone(item, path=self.path)
            if tombstone.resource_key in legacy_tombstones:
                raise RuntimeError(
                    f"Runner operation journal contains duplicate legacy resource "
                    f"tombstones: {self.path}"
                )
            legacy_tombstones[tombstone.resource_key] = tombstone
        return _OperationJournalState(
            records=records,
            resource_tombstones=tombstones,
            legacy_resource_tombstones=legacy_tombstones,
        )

    def _write(self, state: _OperationJournalState) -> None:
        atomic_write_json(
            self.path,
            {
                "schema_version": _OPERATION_JOURNAL_SCHEMA_VERSION,
                "records": [
                    {
                        "schema_version": record.schema_version,
                        "operation_key": record.operation_key,
                        "command_id": record.command_id,
                        "binding_digest": record.binding_digest,
                        "envelope_digest": record.envelope_digest,
                        "outcome": record.outcome,
                    }
                    for record in sorted(
                        state.records.values(),
                        key=lambda item: item.operation_key,
                    )
                ],
                "resource_tombstones": [
                    {
                        "schema_version": tombstone.schema_version,
                        "resource_key": tombstone.resource_key,
                        "outcome": tombstone.outcome,
                    }
                    for tombstone in sorted(
                        state.resource_tombstones.values(),
                        key=lambda item: item.resource_key,
                    )
                ],
                "legacy_resource_tombstones": [
                    {
                        "schema_version": tombstone.schema_version,
                        "resource_key": tombstone.resource_key,
                        "outcome": tombstone.outcome,
                    }
                    for tombstone in sorted(
                        state.legacy_resource_tombstones.values(),
                        key=lambda item: item.resource_key,
                    )
                ],
            },
        )


def _parse_journal_record(item: object, *, path: Path) -> OperationJournalRecord:
    if not isinstance(item, dict):
        raise RuntimeError(f"Runner operation journal has an invalid record: {path}")
    expected_keys = {
        "schema_version",
        "operation_key",
        "command_id",
        "binding_digest",
        "envelope_digest",
        "outcome",
    }
    if set(item) != expected_keys:
        raise RuntimeError(f"Runner operation journal has an invalid record: {path}")
    schema_version = item.get("schema_version")
    operation_key = item.get("operation_key")
    command_id = item.get("command_id")
    binding_digest = item.get("binding_digest")
    envelope_digest = item.get("envelope_digest")
    outcome = item.get("outcome")
    if (
        schema_version != _OPERATION_JOURNAL_SCHEMA_VERSION
        or not isinstance(operation_key, str)
        or not operation_key
        or not isinstance(outcome, dict)
        or any(not isinstance(key, str) for key in outcome)
    ):
        raise RuntimeError(f"Runner operation journal has an invalid record: {path}")
    if command_id is None or binding_digest is None or envelope_digest is None:
        if not (
            command_id is None
            and binding_digest is None
            and envelope_digest is None
            and outcome == _LEGACY_UNBOUND_OUTCOME
        ):
            raise RuntimeError(f"Runner operation journal has an invalid record: {path}")
    else:
        try:
            if not isinstance(command_id, str) or not command_id or len(command_id) > 64:
                raise ValueError("invalid command_id")
            _validate_journal_digest(binding_digest, "binding_digest")
            _validate_journal_digest(envelope_digest, "envelope_digest")
        except ValueError as exc:
            raise RuntimeError(
                f"Runner operation journal has an invalid record: {path}"
            ) from exc
    return OperationJournalRecord(
        operation_key=operation_key,
        command_id=command_id,
        binding_digest=binding_digest,
        envelope_digest=envelope_digest,
        outcome=_copy_journal_outcome(outcome),
    )


def _parse_resource_tombstone(item: object, *, path: Path) -> ResourceTombstone:
    if not isinstance(item, dict) or set(item) != {
        "schema_version",
        "resource_key",
        "outcome",
    }:
        raise RuntimeError(f"Runner operation journal has an invalid tombstone: {path}")
    resource_key = item.get("resource_key")
    outcome = item.get("outcome")
    if (
        item.get("schema_version") != _OPERATION_JOURNAL_SCHEMA_VERSION
        or not isinstance(resource_key, str)
        or not resource_key
        or not isinstance(outcome, dict)
        or outcome.get("state")
        not in {"cancellation_requested", "physical_stop_confirmed", "legacy_unbound"}
    ):
        raise RuntimeError(f"Runner operation journal has an invalid tombstone: {path}")
    return ResourceTombstone(
        resource_key=resource_key,
        outcome=_copy_journal_outcome(outcome),
    )


def _resource_attempt_key(
    resource_key: str,
    identity: OperationJournalIdentity,
) -> str:
    command_hash = hashlib.sha256(identity.command_id.encode("utf-8")).hexdigest()
    return f"{resource_key}:command:{command_hash}"


def _validate_journal_digest(value: object, field_name: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"Runner operation journal {field_name} must be a SHA-256 digest")


def _copy_journal_outcome(outcome: dict[str, object]) -> dict[str, object]:
    try:
        encoded = json.dumps(
            outcome,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )
        copied = json.loads(encoded)
    except (TypeError, ValueError) as exc:
        raise ValueError("Runner operation journal outcome must be canonical JSON") from exc
    if not isinstance(copied, dict):  # pragma: no cover - input is statically a dict
        raise ValueError("Runner operation journal outcome must be an object")
    return copied


def _completed_operation_result(record: OperationJournalRecord) -> dict[str, object]:
    if record.outcome.get("state") != "effect_completed":
        raise RuntimeError(
            f"Runner operation {record.operation_key!r} has an unconfirmed physical outcome"
        )
    result = record.outcome.get("result")
    if not isinstance(result, dict) or any(not isinstance(key, str) for key in result):
        raise RuntimeError(
            f"Runner operation {record.operation_key!r} has an invalid durable outcome"
        )
    return _copy_journal_outcome(result)


class RemoteTerminalManager:
    """Execute durable terminal commands and stream transcripts back to the server."""

    def __init__(
        self,
        *,
        node_id: str,
        supervisor: TerminalSupervisor,
        terminals: TerminalRepository,
        executions: ExecutionRepository,
        client: TerminalControlClient,
        operation_journal: OperationJournal,
        output_poll_seconds: float = 0.1,
    ) -> None:
        self._node_id = node_id
        self._supervisor = supervisor
        self._terminals = terminals
        self._executions = executions
        self._client = client
        self._journal = operation_journal
        self._output_poll_seconds = output_poll_seconds
        self._monitors: dict[str, asyncio.Task[None]] = {}
        self._command_lock = asyncio.Lock()
        self._recovered = False
        self._closed = False

    async def handle(
        self,
        kind: RunnerCommandKind,
        payload: dict[str, object],
        *,
        journal_identity: OperationJournalIdentity | None = None,
        effect_guard: EffectGuard | None = None,
        on_admitted: Callable[[], None] | None = None,
    ) -> object:
        if kind is RunnerCommandKind.TERMINAL_START:
            return await self._start(
                payload,
                effect_guard=effect_guard,
                on_admitted=on_admitted,
            )
        if kind not in {
            RunnerCommandKind.TERMINAL_WRITE,
            RunnerCommandKind.TERMINAL_RESIZE,
            RunnerCommandKind.TERMINAL_INTERRUPT,
            RunnerCommandKind.TERMINAL_CLOSE,
        }:
            raise ValueError(f"unsupported terminal command: {kind.value}")

        operation_id = _required_string(payload, "operation_id")
        if journal_identity is None:
            raise RuntimeError(
                f"Terminal operation {operation_id!r} omitted its Runner journal identity"
            )
        async with self._command_lock:
            existing = await self._journal.get_exact(operation_id, journal_identity)
            if existing is not None:
                result = _completed_operation_result(existing)
                return {**result, "operation_id": operation_id, "duplicate": True}
            claimed = await self._journal.claim(
                operation_id,
                journal_identity,
                outcome=_OPERATION_CLAIMED_OUTCOME,
            )
            if not claimed:  # pragma: no cover - serialized by _command_lock
                raise RuntimeError(
                    f"Terminal operation {operation_id!r} has an unconfirmed physical outcome"
                )
            result = await self._apply_operation(kind, payload)
            await self._journal.transition(
                operation_id,
                journal_identity,
                expected_outcome=_OPERATION_CLAIMED_OUTCOME,
                outcome={"state": "effect_completed", "result": result},
            )
            return {**result, "operation_id": operation_id, "duplicate": False}

    async def resume_active(self) -> None:
        if self._recovered:
            return
        self._recovered = True
        owner = self._client_principal()
        for terminal in await self._supervisor.recover(owner=owner):
            execution = await self._require_execution(terminal.execution_id)
            self._require_execution_owner(execution, owner)
            await self._report_with_retry(execution)

    async def cancel_execution(self, execution_id: str) -> Execution:
        """Stop a PTY/ConPTY through its native supervisor and return observed state."""
        owner = self._client_principal()
        before = await self._require_execution(execution_id)
        self._require_execution_owner(before, owner)
        execution = await self._supervisor.close_execution(execution_id)
        self._require_execution_owner(execution, owner)
        terminal = await self._terminals.get_by_execution(execution_id)
        if (
            execution.status not in _PHYSICAL_STOP_PROOF_STATUSES
            or execution.physical_stop_confirmed_at is None
        ):
            raise ProcessTerminationError(
                f"Terminal execution {execution_id!r} did not provide affirmative "
                "physical-stop confirmation"
            )
        if terminal is not None and terminal.status is not TerminalStatus.CLOSED:
            terminal.transition_to(TerminalStatus.CLOSED)
            await self._terminals.save(terminal)
        return execution

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        tasks = list(self._monitors.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        owner = self._client_principal()
        terminal_stops = [
            asyncio.create_task(
                self._close_terminal_on_shutdown(terminal, owner),
                name=f"riftx-runner-close-terminal-{terminal.id}",
            )
            for terminal in await self._terminals.list_active()
        ]
        results = await asyncio.gather(*terminal_stops, return_exceptions=True)
        errors = [result for result in results if isinstance(result, BaseException)]
        if errors:
            for error in errors[1:]:
                logger.error("Additional terminal shutdown failure: %r", error)
            raise errors[0]

    async def _close_terminal_on_shutdown(
        self,
        terminal: TerminalSession,
        owner: RunnerPrincipal,
    ) -> None:
        execution = await self._require_execution(terminal.execution_id)
        if execution.owner != owner:
            logger.error(
                "Refusing to close terminal %s owned by another Runner principal",
                terminal.id,
            )
            return
        await self._supervisor.close(terminal.id)
        execution = await self._require_execution(terminal.execution_id)
        self._require_execution_owner(execution, owner)
        try:
            await self._forward_once(execution.id, 0)
            await self._report_execution(execution)
        except RunnerControlClientError:
            logger.warning(
                "Unable to report terminal %s during Runner shutdown",
                terminal.id,
            )

    async def _start(
        self,
        payload: dict[str, object],
        *,
        effect_guard: EffectGuard | None,
        on_admitted: Callable[[], None] | None,
    ) -> dict[str, object]:
        session_id = _required_string(payload, "session_id")
        execution_id = _required_string(payload, "execution_id")
        raw_request = payload.get("request")
        if not isinstance(raw_request, dict):
            raise ValueError("terminal_start command is missing request")
        request = TerminalLaunchRequest.model_validate(raw_request)
        owner = self._client_principal()
        if request.runner_principal != owner:
            raise ValueError("terminal_start request owner does not match this Runner")
        if request.session_id not in {None, session_id}:
            raise ValueError("terminal_start session IDs do not match")
        if request.execution_id not in {None, execution_id}:
            raise ValueError("terminal_start execution IDs do not match")
        if request.node_id != self._node_id:
            raise ValueError("terminal_start command targets a different Runner node")
        request = request.model_copy(
            update={"session_id": session_id, "execution_id": execution_id}
        )

        existing = await self._terminals.get(session_id)
        if existing is not None:
            if existing.execution_id != execution_id:
                raise ValueError("terminal_start conflicts with an existing session")
            execution = await self._require_execution(execution_id)
            require_terminal_start_replay_matches(existing, execution, request)
            if existing.status is TerminalStatus.CREATED and execution.status in {
                ExecutionStatus.CREATED,
                ExecutionStatus.STARTING,
            }:
                # Do not turn a crash between durable admission phases into a
                # successful duplicate.  The supervisor can safely converge
                # CREATED, while STARTING remains fail-closed and is never
                # replayed without an attached native handle.
                try:
                    existing = await self._supervisor.start(
                        request,
                        effect_guard=effect_guard,
                    )
                except Exception:
                    if on_admitted is not None:
                        on_admitted()
                    recovered = await self._executions.get(execution_id)
                    if recovered is not None and recovered.status in _FINAL_STATUSES:
                        await self._report_execution(recovered)
                    raise
                if on_admitted is not None:
                    on_admitted()
                execution = await self._require_execution(existing.execution_id)
                await self._report_execution(execution)
                if execution.status not in _FINAL_STATUSES:
                    self._start_monitor(execution.id)
                return {
                    "session_id": existing.id,
                    "execution_id": execution.id,
                    "status": execution.status.value,
                    "duplicate": True,
                }
            # A foreign payload must be rejected before invoking any callback,
            # reporting durable state, or starting a monitor. The complete
            # replay identity above includes the authenticated Runner owner.
            if effect_guard is not None:
                await effect_guard()
            if on_admitted is not None:
                on_admitted()
            await self._report_execution(execution)
            if execution.status not in _FINAL_STATUSES:
                self._start_monitor(execution_id)
            return {
                "session_id": session_id,
                "execution_id": execution_id,
                "status": execution.status.value,
                "duplicate": True,
            }

        try:
            terminal = await self._supervisor.start(
                request,
                effect_guard=effect_guard,
            )
        except Exception:
            # Local admission has resolved (including guarded pre/post-spawn
            # cleanup). Release the daemon's execution lock before any status
            # upload so Control Plane I/O cannot delay a safety stop.
            if on_admitted is not None:
                on_admitted()
            recovered = await self._executions.get(execution_id)
            if recovered is not None and recovered.status in _FINAL_STATUSES:
                await self._report_execution(recovered)
            raise
        if on_admitted is not None:
            on_admitted()
        execution = await self._require_execution(terminal.execution_id)
        self._require_execution_owner(execution, owner)
        await self._report_execution(execution)
        self._start_monitor(execution.id)
        return {
            "session_id": terminal.id,
            "execution_id": execution.id,
            "status": execution.status.value,
            "duplicate": False,
        }

    async def _apply_operation(
        self,
        kind: RunnerCommandKind,
        payload: dict[str, object],
    ) -> dict[str, object]:
        terminal = await self._require_terminal(payload)
        execution = await self._require_execution(terminal.execution_id)
        self._require_execution_owner(execution, self._client_principal())
        if kind is RunnerCommandKind.TERMINAL_WRITE:
            encoded = _required_string(payload, "data")
            try:
                data = base64.b64decode(encoded, validate=True)
            except ValueError as exc:
                raise ValueError("terminal_write data is not valid base64") from exc
            if len(data) > 64 * 1024:
                raise ValueError("terminal input exceeds 65536 bytes")
            await self._supervisor.write(terminal.id, data, actor=terminal.owner)
            return {"session_id": terminal.id, "bytes_written": len(data)}
        if kind is RunnerCommandKind.TERMINAL_RESIZE:
            cols = _required_positive_int(payload, "cols")
            rows = _required_positive_int(payload, "rows")
            resized = await self._supervisor.resize(terminal.id, cols=cols, rows=rows)
            return {"session_id": terminal.id, "cols": resized.cols, "rows": resized.rows}
        if kind is RunnerCommandKind.TERMINAL_INTERRUPT:
            await self._supervisor.interrupt(terminal.id, actor=terminal.owner)
            return {"session_id": terminal.id, "interrupted": True}
        closed = await self._supervisor.close(terminal.id)
        execution = await self._require_execution(closed.execution_id)
        await self._forward_once(execution.id, 0)
        await self._report_execution(execution)
        return {"session_id": terminal.id, "status": closed.status.value}

    async def _require_terminal(self, payload: dict[str, object]) -> TerminalSession:
        session_id = _required_string(payload, "session_id")
        execution_id = _required_string(payload, "execution_id")
        terminal = await self._terminals.get(session_id)
        if terminal is None:
            raise EntityNotFoundError("Terminal Session", session_id)
        if terminal.execution_id != execution_id:
            raise ValueError("terminal command execution does not match the session")
        return terminal

    async def _require_execution(self, execution_id: str) -> Execution:
        execution = await self._executions.get(execution_id)
        if execution is None:
            raise EntityNotFoundError("Execution", execution_id)
        return execution

    def _start_monitor(self, execution_id: str) -> None:
        current = self._monitors.get(execution_id)
        if current is not None and not current.done():
            return
        task = asyncio.create_task(
            self._monitor(execution_id),
            name=f"riftx-remote-terminal-monitor-{execution_id}",
        )
        self._monitors[execution_id] = task
        task.add_done_callback(lambda _: self._monitors.pop(execution_id, None))

    async def _monitor(self, execution_id: str) -> None:
        cursor = 0
        while not self._closed:
            try:
                cursor = await self._forward_once(execution_id, cursor)
                execution = await self._require_execution(execution_id)
                if execution.status in _FINAL_STATUSES:
                    cursor = await self._forward_once(execution_id, cursor)
                    await self._report_with_retry(execution)
                    return
            except RunnerControlClientError:
                logger.warning("Terminal output forwarding failed for %s; retrying", execution_id)
            await asyncio.sleep(self._output_poll_seconds)

    async def _forward_once(self, execution_id: str, cursor: int) -> int:
        execution = await self._require_execution(execution_id)
        self._require_execution_owner(execution, self._client_principal())
        terminal = await self._terminals.get_by_execution(execution_id)
        if terminal is None:
            return cursor
        output = await self._supervisor.read(terminal.id, cursor=cursor, max_bytes=256 * 1024)
        if not output.data:
            return cursor
        try:
            return await self._client.report_output(
                execution_id,
                **_execution_callback_kwargs(execution),
                stream="stdout",
                offset=output.cursor,
                data=output.data,
            )
        except OutputOffsetMismatch as exc:
            return exc.expected_offset

    async def _report_with_retry(self, execution: Execution) -> None:
        while not self._closed:
            try:
                await self._report_execution(execution)
                return
            except RunnerControlClientError:
                await asyncio.sleep(self._output_poll_seconds)

    async def _report_execution(self, execution: Execution) -> None:
        self._require_execution_owner(execution, self._client_principal())
        await self._client.report_status(
            execution.id,
            execution.status,
            **_execution_callback_kwargs(execution),
            pid=execution.pid if execution.status is ExecutionStatus.RUNNING else None,
            process_group_id=(
                execution.process_group_id if execution.status is ExecutionStatus.RUNNING else None
            ),
            exit_code=execution.exit_code if execution.status in _FINAL_STATUSES else None,
            physical_stop_confirmed=(execution.physical_stop_confirmed_at is not None),
        )

    def _client_principal(self) -> RunnerPrincipal:
        principal = self._client.principal
        if principal is None:
            raise RuntimeError("Runner terminal client has no authenticated principal")
        return principal

    @staticmethod
    def _require_execution_owner(
        execution: Execution,
        owner: RunnerPrincipal,
    ) -> None:
        if execution.owner != owner:
            raise RuntimeError(
                f"Terminal execution {execution.id!r} belongs to another Runner principal"
            )


def _execution_callback_kwargs(execution: Execution) -> dict[str, str]:
    values = {
        "runner_command_id": execution.runner_command_id,
        "runner_effect_binding_id": execution.runner_effect_binding_id,
        "runner_binding_digest": execution.runner_binding_digest,
        "runner_envelope_digest": execution.runner_envelope_digest,
    }
    if any(value is None for value in values.values()):
        raise RuntimeError(
            f"Terminal execution {execution.id!r} has no verified launch callback binding"
        )
    return {name: value for name, value in values.items() if value is not None}


def _required_string(payload: dict[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"terminal command is missing {key}")
    return value


def _required_positive_int(payload: dict[str, object], key: str) -> int:
    value = payload.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError(f"terminal command {key} must be a positive integer")
    return value
