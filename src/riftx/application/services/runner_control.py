"""Authenticated durable command channel between Control Plane and remote Runners."""

from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from riftx.application.errors import (
    ApplicationConflictError,
    AuthenticationError,
    EntityNotFoundError,
    ServiceUnavailableError,
)
from riftx.application.ports import (
    ExecutionRepository,
    RunEventRepository,
    RunnerCommandRepository,
    RunnerCredentialRepository,
    RunRepository,
    TerminalRepository,
)
from riftx.domain import (
    Execution,
    ExecutionStatus,
    ExecutorType,
    Node,
    NodeStatus,
    RunnerCommand,
    RunnerCommandKind,
    RunnerCommandStatus,
    RunnerCredential,
    RunnerPrincipal,
    TerminalStatus,
)
from riftx.domain.base import new_id, utc_now
from riftx.runner.paths import RunnerPaths
from riftx.security import validate_runner_registration_credential

from .nodes import NodeApplicationService, NodeHeartbeat, NodeRegistration
from .runs import require_general_run_operation

_MAX_RESULT_BYTES = 64 * 1024
_MAX_BROWSER_RESULT_BYTES = 512 * 1024
_MAX_OUTPUT_CHUNK_BYTES = 256 * 1024
_OFFLINE_SAFE_COMMANDS = {
    RunnerCommandKind.CANCEL,
    RunnerCommandKind.TARGET_HTTP_CANCEL,
    RunnerCommandKind.BROWSER_CLOSE,
    RunnerCommandKind.TERMINAL_CLOSE,
}
_RUNNER_REPORTED_EXECUTION_STATUSES = frozenset(
    {
        ExecutionStatus.RUNNING,
        ExecutionStatus.COMPLETED,
        ExecutionStatus.EXITED,
        ExecutionStatus.FAILED,
        ExecutionStatus.CANCELLED,
        ExecutionStatus.HARD_TIMEOUT,
        ExecutionStatus.LOST,
    }
)
_PHYSICAL_STOP_PROOF_REQUIRED_STATUSES = frozenset(
    {
        ExecutionStatus.COMPLETED,
        ExecutionStatus.EXITED,
        ExecutionStatus.CANCELLED,
        ExecutionStatus.HARD_TIMEOUT,
    }
)


@dataclass(frozen=True, slots=True)
class RunnerRegistrationResult:
    node: Node
    created: bool
    token: str
    principal: RunnerPrincipal


@dataclass(frozen=True, slots=True)
class ExecutionStatusReport:
    status: ExecutionStatus
    pid: int | None = None
    process_group_id: int | None = None
    exit_code: int | None = None
    executable_path: str | None = None
    tool_id: str | None = None
    tool_version: str | None = None
    platform_system: str = ""
    platform_release: str = ""
    platform_architecture: str = ""
    process_created_at: datetime | None = None
    physical_stop_confirmed: bool = False


class RunnerControlService:
    """Issues scoped credentials and persists reconnect-safe Runner commands."""

    def __init__(
        self,
        *,
        credentials: RunnerCredentialRepository,
        commands: RunnerCommandRepository,
        nodes: NodeApplicationService,
        executions: ExecutionRepository,
        runs: RunRepository,
        paths: RunnerPaths,
        registration_token: str | None,
        terminals: TerminalRepository | None = None,
        events: RunEventRepository | None = None,
        lease_duration: timedelta = timedelta(seconds=30),
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        if lease_duration <= timedelta(0):
            raise ValueError("lease_duration must be positive")
        self._credentials = credentials
        self._commands = commands
        self._nodes = nodes
        self._executions = executions
        self._runs = runs
        self._paths = paths
        self._registration_token = validate_runner_registration_credential(registration_token)
        self._terminals = terminals
        self._events = events
        self._lease_duration = lease_duration
        self._clock = clock
        self._output_locks: dict[tuple[str, str], asyncio.Lock] = {}

    async def register(
        self,
        registration: NodeRegistration,
        *,
        bootstrap_token: str,
    ) -> RunnerRegistrationResult:
        self.authenticate_bootstrap(bootstrap_token)
        node, created = await self._nodes.register(registration)
        token = secrets.token_urlsafe(32)
        now = self._clock()
        credential = await self._credentials.issue(
            node.id,
            token_hash=_token_hash(token),
            token_prefix=token[:8],
            issued_at=now,
        )
        # Credential issuance also advances the node's current owner in the
        # same transaction. Re-read so callers never observe the pre-issue node.
        node = await self._nodes.get(node.id)
        return RunnerRegistrationResult(
            node=node,
            created=created,
            token=token,
            principal=credential.principal,
        )

    async def authenticate(self, node_id: str, token: str) -> RunnerCredential:
        token_hash = _token_hash(token)
        credential = await self._credentials.get_by_token_hash(node_id, token_hash)
        if (
            credential is None
            or credential.revoked_at is not None
            or not secrets.compare_digest(credential.token_hash, token_hash)
        ):
            raise AuthenticationError(
                "runner_authentication_failed",
                "Runner credentials are missing or invalid",
            )
        return credential

    async def heartbeat(
        self,
        node_id: str,
        token: str,
        heartbeat: NodeHeartbeat,
    ) -> Node:
        credential = await self.authenticate(node_id, token)
        node = await self._nodes.get(node_id)
        if node.current_owner != credential.principal:
            # Superseded owners remain authenticated so they can receive and
            # acknowledge commands for effects they still own. They must not
            # refresh the logical node's current-owner liveness metadata.
            return node
        return await self._nodes.heartbeat(node_id, heartbeat)

    async def current_principal(self, node_id: str) -> RunnerPrincipal:
        credential = await self._credentials.get_current(node_id)
        if credential is None or credential.revoked_at is not None:
            raise ServiceUnavailableError(
                "runner_owner_unavailable",
                f"Runner node {node_id!r} has no active owner credential",
                details={"node_id": node_id},
            )
        return credential.principal

    async def enqueue(
        self,
        node_id: str,
        *,
        kind: RunnerCommandKind,
        idempotency_key: str,
        payload: dict[str, object],
        target: RunnerPrincipal | None = None,
    ) -> tuple[RunnerCommand, bool]:
        node = await self._nodes.get(node_id)
        if (
            node.status not in {NodeStatus.ONLINE, NodeStatus.DEGRADED}
            and kind not in _OFFLINE_SAFE_COMMANDS
        ):
            raise ServiceUnavailableError(
                "runner_unavailable",
                f"Runner node {node_id!r} is not connected",
                details={"node_id": node_id, "status": node.status.value},
            )
        target = target or await self.current_principal(node_id)
        target_credential = await self._credentials.get_by_principal(node_id, target)
        if target_credential is None:
            raise ServiceUnavailableError(
                "runner_owner_unavailable",
                "Runner command target is not registered for this node",
                details={
                    "node_id": node_id,
                    "runner_instance_id": target.instance_id,
                    "runner_epoch": target.epoch,
                },
            )
        if target_credential.revoked_at is not None and kind not in _OFFLINE_SAFE_COMMANDS:
            raise ServiceUnavailableError(
                "runner_owner_revoked",
                "Runner command target credential has been revoked",
                details={
                    "node_id": node_id,
                    "runner_instance_id": target.instance_id,
                    "runner_epoch": target.epoch,
                },
            )
        now = self._clock()
        return await self._commands.enqueue(
            RunnerCommand(
                node_id=node_id,
                kind=kind,
                idempotency_key=idempotency_key,
                target=target,
                payload=payload,
                created_at=now,
                updated_at=now,
            )
        )

    async def poll(
        self,
        node_id: str,
        token: str,
        *,
        wait_seconds: float = 0,
        safety_only: bool = False,
    ) -> RunnerCommand | None:
        credential = await self.authenticate(node_id, token)
        await self._heartbeat_current_owner(node_id, credential)
        deadline = asyncio.get_running_loop().time() + min(max(wait_seconds, 0), 30)
        while True:
            now = self._clock()
            command = await self._commands.lease_next(
                node_id,
                principal=credential.principal,
                lease_id=new_id(),
                leased_until=now + self._lease_duration,
                now=now,
                safety_only=safety_only,
            )
            if command is not None:
                return command
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return None
            await asyncio.sleep(min(0.25, remaining))

    async def finish_command(
        self,
        node_id: str,
        token: str,
        command_id: str,
        *,
        lease_id: str,
        succeeded: bool,
        result: dict[str, object] | None = None,
        error: str = "",
    ) -> RunnerCommand:
        credential = await self.authenticate(node_id, token)
        command = await self._require_command(command_id)
        self._require_command_scope(command, node_id, credential.principal)
        bounded_result = result or {}
        result_limit = (
            _MAX_BROWSER_RESULT_BYTES
            if command.kind is RunnerCommandKind.BROWSER
            else _MAX_RESULT_BYTES
        )
        if len(json.dumps(bounded_result, ensure_ascii=False).encode()) > result_limit:
            raise ApplicationConflictError(
                "runner_result_too_large",
                f"Runner command results must not exceed {result_limit} bytes",
            )
        if succeeded and command.kind is RunnerCommandKind.CANCEL:
            _validate_cancel_ack(command, bounded_result)
        finished = await self._commands.finish(
            command_id,
            principal=credential.principal,
            lease_id=lease_id,
            status=(RunnerCommandStatus.COMPLETED if succeeded else RunnerCommandStatus.FAILED),
            result=bounded_result,
            error=error[:8192],
            completed_at=self._clock(),
        )
        if (
            finished.kind is RunnerCommandKind.CANCEL
            and finished.status is RunnerCommandStatus.COMPLETED
        ):
            cancel_identity = _validate_cancel_ack(finished, finished.result)
            # Persist only after the repository has authenticated the exact
            # command lease. If the proof write fails, retrying finish with the
            # same lease is idempotent and will attempt this write again.
            await self._record_cancel_ack_stop_proof(
                finished,
                node_id=node_id,
                principal=credential.principal,
                execution_id=cancel_identity[0],
                execution_key=cancel_identity[1],
            )
        return finished

    async def renew_command_lease(
        self,
        node_id: str,
        token: str,
        command_id: str,
        *,
        lease_id: str,
    ) -> RunnerCommand:
        credential = await self.authenticate(node_id, token)
        command = await self._require_command(command_id)
        self._require_command_scope(command, node_id, credential.principal)
        now = self._clock()
        return await self._commands.renew_lease(
            command_id,
            principal=credential.principal,
            lease_id=lease_id,
            leased_until=now + self._lease_duration,
            now=now,
        )

    async def wait_command(
        self,
        command_id: str,
        *,
        timeout_seconds: float,
        poll_interval_seconds: float = 0.1,
    ) -> RunnerCommand:
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        while True:
            command = await self._require_command(command_id)
            if command.status in {RunnerCommandStatus.COMPLETED, RunnerCommandStatus.FAILED}:
                return command
            if asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError(f"Runner command {command_id!r} did not complete in time")
            await asyncio.sleep(poll_interval_seconds)

    async def append_command_output(
        self,
        node_id: str,
        token: str,
        command_id: str,
        *,
        lease_id: str,
        offset: int,
        data: bytes,
    ) -> int:
        credential = await self.authenticate(node_id, token)
        command = await self._require_command(command_id)
        self._require_command_scope(command, node_id, credential.principal)
        if (
            command.kind not in {RunnerCommandKind.TARGET_HTTP, RunnerCommandKind.BROWSER}
            or command.status is not RunnerCommandStatus.LEASED
            or command.lease_id != lease_id
        ):
            raise AuthenticationError(
                "runner_command_output_scope_mismatch",
                "Runner cannot upload output for this command lease",
            )
        if len(data) > _MAX_OUTPUT_CHUNK_BYTES:
            raise ApplicationConflictError(
                "runner_output_chunk_too_large",
                f"Runner output chunks must not exceed {_MAX_OUTPUT_CHUNK_BYTES} bytes",
            )
        raw_limit = command.payload.get(
            "max_response_bytes",
            command.payload.get("max_attachment_bytes", 10_000_000),
        )
        try:
            limit = int(raw_limit) if not isinstance(raw_limit, bool) else 10_000_000
        except (TypeError, ValueError):
            limit = 10_000_000
        if offset + len(data) > min(max(limit, 1), 100_000_000):
            raise ApplicationConflictError(
                "runner_command_output_too_large",
                "Runner command output exceeds its declared response limit",
            )
        path = self._paths.command_output(command_id)
        lock = self._output_locks.setdefault((command_id, "command"), asyncio.Lock())
        async with lock:
            return await asyncio.to_thread(_append_exact, path, offset, data)

    async def read_command_output(self, command_id: str) -> bytes:
        command = await self._require_command(command_id)
        if command.kind not in {RunnerCommandKind.TARGET_HTTP, RunnerCommandKind.BROWSER}:
            raise ApplicationConflictError(
                "runner_command_output_unavailable",
                "Runner command does not carry binary output",
            )
        path = self._paths.command_output(command_id)
        try:
            return await asyncio.to_thread(path.read_bytes)
        except FileNotFoundError:
            return b""

    async def report_execution(
        self,
        node_id: str,
        token: str,
        execution_id: str,
        report: ExecutionStatusReport,
    ) -> Execution:
        credential = await self.authenticate(node_id, token)
        execution = await self._require_execution(execution_id)
        self._require_execution_scope(execution, node_id, credential.principal)
        _validate_execution_status_report(report)
        await self._require_execution_callback_kind(
            execution,
            allow_safety_stop=_is_physical_stop_report(report),
        )
        received_at = self._clock()
        for _ in range(8):
            expected_status = execution.status
            candidate = execution.model_copy(deep=True)
            proof_compatible_retry = (
                candidate.status is not report.status
                and candidate.status in _PHYSICAL_STOP_PROOF_REQUIRED_STATUSES
                and report.status in _PHYSICAL_STOP_PROOF_REQUIRED_STATUSES
                and report.physical_stop_confirmed is True
            )
            if proof_compatible_retry:
                changed = _apply_missing_execution_provenance(candidate, report)
            else:
                changed = _apply_execution_provenance(candidate, report)
            if candidate.status is report.status:
                if report.status is ExecutionStatus.RUNNING:
                    if report.pid is not None and candidate.pid != report.pid:
                        candidate.pid = report.pid
                        changed = True
                    if (
                        report.process_group_id is not None
                        and candidate.process_group_id != report.process_group_id
                    ):
                        candidate.process_group_id = report.process_group_id
                        changed = True
                elif report.exit_code is not None and candidate.exit_code is None:
                    candidate.exit_code = report.exit_code
                    changed = True
            elif proof_compatible_retry:
                # The owning Runner may retry a natural-stop report after the
                # Control Plane durably accepted it but its HTTP response was
                # lost. Preserve the first proof-backed terminal state while
                # allowing the retry to fill fields that were still unknown.
                if report.exit_code is not None and candidate.exit_code is None:
                    candidate.exit_code = report.exit_code
                    changed = True
            else:
                target_status = report.status
                if (
                    candidate.status
                    in {
                        ExecutionStatus.STARTING,
                        ExecutionStatus.LOST,
                        ExecutionStatus.FAILED,
                    }
                    and report.status
                    in {
                        ExecutionStatus.COMPLETED,
                        ExecutionStatus.EXITED,
                        ExecutionStatus.HARD_TIMEOUT,
                    }
                    and report.physical_stop_confirmed is True
                ):
                    # The Runner may naturally stop before its first RUNNING
                    # upload commits, or reconciliation may record LOST/FAILED
                    # while that upload is disconnected. The proof is still
                    # valid, but these states may only converge to CANCELLED in
                    # the domain state machine.
                    target_status = ExecutionStatus.CANCELLED
                if not candidate.can_transition_to(target_status):
                    raise ApplicationConflictError(
                        "invalid_runner_execution_transition",
                        f"Execution cannot transition from {candidate.status.value} "
                        f"to {report.status.value}",
                    )
                if target_status is ExecutionStatus.RUNNING:
                    candidate.pid = report.pid
                    candidate.process_group_id = report.process_group_id
                candidate.transition_to(
                    target_status,
                    at=received_at,
                    exit_code=report.exit_code,
                )
                changed = True
            if report.physical_stop_confirmed and candidate.physical_stop_confirmed_at is None:
                # Runner clocks are not trusted for durable safety evidence.
                # Record when the owning callback reached the Control Plane.
                candidate.physical_stop_confirmed_at = received_at
                changed = True
            if not changed:
                execution = candidate
                break
            execution, saved = await self._executions.save_if_status(
                candidate,
                expected={expected_status},
            )
            if saved:
                break
            self._require_execution_scope(execution, node_id, credential.principal)
        else:
            raise ApplicationConflictError(
                "runner_execution_update_conflict",
                "Runner execution status changed repeatedly while applying its report",
                details={"execution_id": execution_id},
            )
        await self._sync_terminal_status(execution, execution.status)
        return execution

    async def _record_cancel_ack_stop_proof(
        self,
        command: RunnerCommand,
        *,
        node_id: str,
        principal: RunnerPrincipal,
        execution_id: str,
        execution_key: str,
    ) -> None:
        execution = await self._require_execution(execution_id)
        self._require_execution_scope(execution, node_id, principal)
        if execution.execution_key != execution_key:
            _raise_cancel_ack_invalid(command, ["execution.execution_key"])
        received_at = self._clock()
        for _ in range(8):
            expected_status = execution.status
            candidate = execution.model_copy(deep=True)
            changed = False
            if candidate.status not in _PHYSICAL_STOP_PROOF_REQUIRED_STATUSES:
                if not candidate.can_transition_to(ExecutionStatus.CANCELLED):
                    _raise_cancel_ack_invalid(command, ["execution.status"])
                candidate.transition_to(
                    ExecutionStatus.CANCELLED,
                    at=received_at,
                    exit_code=candidate.exit_code,
                )
                changed = True
            if candidate.physical_stop_confirmed_at is None:
                candidate.physical_stop_confirmed_at = received_at
                changed = True
            if not changed:
                execution = candidate
                break
            execution, saved = await self._executions.save_if_status(
                candidate,
                expected={expected_status},
            )
            if saved:
                break
            self._require_execution_scope(execution, node_id, principal)
            if execution.execution_key != execution_key:
                _raise_cancel_ack_invalid(command, ["execution.execution_key"])
        else:
            raise ApplicationConflictError(
                "runner_execution_update_conflict",
                "Execution changed repeatedly while recording cancellation proof",
                details={"execution_id": execution_id, "command_id": command.id},
            )
        await self._sync_terminal_status(execution, execution.status)

    async def _sync_terminal_status(
        self,
        execution: Execution,
        status: ExecutionStatus,
    ) -> None:
        if self._terminals is None or execution.executor_type is not ExecutorType.PTY:
            return
        for _ in range(8):
            latest_execution = await self._executions.get(execution.id)
            if latest_execution is not None:
                execution = latest_execution
                status = execution.status
            terminal = await self._terminals.get_by_execution(execution.id)
            if terminal is None:
                return
            target: TerminalStatus | None = None
            event_type: str | None = None
            if status is ExecutionStatus.RUNNING and terminal.status is TerminalStatus.CREATED:
                target = TerminalStatus.OPEN
                event_type = "terminal.opened"
            elif status is ExecutionStatus.LOST and terminal.status in {
                TerminalStatus.CREATED,
                TerminalStatus.OPEN,
            }:
                target = TerminalStatus.LOST
                event_type = "terminal.lost"
            elif (
                status
                in {
                    ExecutionStatus.COMPLETED,
                    ExecutionStatus.EXITED,
                    ExecutionStatus.CANCELLED,
                    ExecutionStatus.HARD_TIMEOUT,
                }
                and execution.physical_stop_confirmed_at is not None
                and terminal.status
                in {
                    TerminalStatus.CREATED,
                    TerminalStatus.OPEN,
                    TerminalStatus.LOST,
                }
            ):
                target = TerminalStatus.CLOSED
                event_type = "terminal.closed"
            if target is None:
                return
            expected_terminal_status = terminal.status
            candidate = terminal.model_copy(deep=True)
            candidate.transition_to(target)
            terminal, saved = await self._terminals.save_if_status(
                candidate,
                expected={expected_terminal_status},
            )
            if not saved:
                continue
            if target is not TerminalStatus.CLOSED:
                newest_execution = await self._executions.get(execution.id)
                if newest_execution is not None and newest_execution.status is not status:
                    execution = newest_execution
                    status = newest_execution.status
                    continue
            if self._events is not None and event_type is not None:
                projected = await self._events.append_terminal_projection_if_current(
                    terminal.run_id,
                    event_type,
                    {"backend": "remote"},
                    event_id=_terminal_projection_event_id(
                        terminal.run_id,
                        terminal.id,
                        target,
                    ),
                    session_id=terminal.id,
                    expected_terminal_status=target,
                    expected_execution_status=status,
                )
                if projected is None:
                    # A higher Execution/Terminal state committed after the
                    # service-level recheck.  Re-read and converge instead of
                    # appending a stale lower-state event.
                    continue
            return
        raise ApplicationConflictError(
            "terminal_projection_update_conflict",
            "Terminal projection changed repeatedly while applying execution status",
            details={"execution_id": execution.id},
        )

    async def append_output(
        self,
        node_id: str,
        token: str,
        execution_id: str,
        *,
        stream: str,
        offset: int,
        data: bytes,
    ) -> int:
        credential = await self.authenticate(node_id, token)
        execution = await self._require_execution(execution_id)
        self._require_execution_scope(execution, node_id, credential.principal)
        await self._require_execution_callback_kind(execution)
        if stream not in {"stdout", "stderr"}:
            raise ValueError("stream must be stdout or stderr")
        if len(data) > _MAX_OUTPUT_CHUNK_BYTES:
            raise ApplicationConflictError(
                "runner_output_chunk_too_large",
                f"Runner output chunks must not exceed {_MAX_OUTPUT_CHUNK_BYTES} bytes",
            )
        path = Path(execution.stdout_path if stream == "stdout" else execution.stderr_path)
        key = (execution_id, stream)
        lock = self._output_locks.setdefault(key, asyncio.Lock())
        async with lock:
            next_offset = await asyncio.to_thread(_append_exact, path, offset, data)
        return next_offset

    async def require_execution_callback_kind(
        self,
        *,
        node_id: str,
        principal: RunnerPrincipal,
        execution_id: str,
        allow_safety_stop: bool = False,
    ) -> Execution:
        """Outer callback admission after API authentication and owner proof."""

        execution = await self._require_execution(execution_id)
        self._require_execution_scope(execution, node_id, principal)
        await self._require_execution_callback_kind(
            execution,
            allow_safety_stop=allow_safety_stop,
        )
        return execution

    async def _require_execution_callback_kind(
        self,
        execution: Execution,
        *,
        allow_safety_stop: bool = False,
    ) -> None:
        run = await self._runs.get(execution.run_id)
        if run is None:
            raise EntityNotFoundError("Run", execution.run_id)
        if allow_safety_stop:
            return
        require_general_run_operation(run)

    def authenticate_bootstrap(self, token: str) -> None:
        """Validate the shared token used to provision a Runner credential."""

        if not self._registration_token:
            raise ServiceUnavailableError(
                "runner_registration_disabled",
                "Remote Runner registration is disabled",
            )
        if not secrets.compare_digest(self._registration_token, token):
            raise AuthenticationError(
                "runner_registration_denied",
                "Runner registration token is invalid",
            )

    async def _require_command(self, command_id: str) -> RunnerCommand:
        command = await self._commands.get(command_id)
        if command is None:
            raise EntityNotFoundError("RunnerCommand", command_id)
        return command

    async def _require_execution(self, execution_id: str) -> Execution:
        execution = await self._executions.get(execution_id)
        if execution is None:
            raise EntityNotFoundError("Execution", execution_id)
        return execution

    async def _heartbeat_current_owner(
        self,
        node_id: str,
        credential: RunnerCredential,
    ) -> None:
        node = await self._nodes.get(node_id)
        if node.current_owner == credential.principal:
            await self._nodes.heartbeat(node_id, NodeHeartbeat())

    @staticmethod
    def _require_command_scope(
        command: RunnerCommand,
        node_id: str,
        principal: RunnerPrincipal,
    ) -> None:
        if command.node_id != node_id or command.target != principal:
            raise AuthenticationError(
                "runner_command_scope_mismatch",
                "Runner cannot access a command assigned to another owner",
            )

    @staticmethod
    def _require_execution_scope(
        execution: Execution,
        node_id: str,
        principal: RunnerPrincipal,
    ) -> None:
        if execution.node_id != node_id:
            raise AuthenticationError(
                "runner_execution_scope_mismatch",
                "Runner cannot update an execution assigned to another node",
            )
        if execution.owner is None:
            raise AuthenticationError(
                "runner_execution_owner_missing",
                "Runner callbacks require an execution owner",
            )
        if execution.owner != principal:
            raise AuthenticationError(
                "runner_execution_owner_mismatch",
                "Runner cannot update an execution assigned to another owner",
            )


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _terminal_projection_event_id(
    run_id: str,
    session_id: str,
    status: TerminalStatus,
) -> str:
    digest = hashlib.sha256(f"{run_id}\0{session_id}\0{status.value}".encode()).hexdigest()
    # Persistence IDs are capped at 64 characters.  The prefix identifies the
    # reserved namespace while 176 digest bits keep collisions negligible.
    return f"terminal-projection-{digest[:44]}"


def _validate_cancel_ack(
    command: RunnerCommand,
    result: dict[str, object],
) -> tuple[str, str]:
    target = command.target
    expected_execution_id = command.payload.get("execution_id")
    expected_execution_key = command.payload.get("execution_key")
    expected_owner = target.model_dump(mode="json") if target is not None else None
    invalid_fields: list[str] = []
    if not isinstance(expected_execution_id, str) or not expected_execution_id:
        invalid_fields.append("command.execution_id")
    if not isinstance(expected_execution_key, str) or not expected_execution_key:
        invalid_fields.append("command.execution_key")
    if expected_owner is None:
        invalid_fields.append("command.owner")
    if result.get("execution_id") != expected_execution_id:
        invalid_fields.append("execution_id")
    if result.get("local_execution_id") != expected_execution_id:
        invalid_fields.append("local_execution_id")
    if result.get("execution_key") != expected_execution_key:
        invalid_fields.append("execution_key")
    if result.get("owner") != expected_owner:
        invalid_fields.append("owner")
    if result.get("status") != ExecutionStatus.CANCELLED.value:
        invalid_fields.append("status")
    if result.get("physical_stop_confirmed") is not True:
        invalid_fields.append("physical_stop_confirmed")
    if invalid_fields:
        _raise_cancel_ack_invalid(command, invalid_fields)
    assert isinstance(expected_execution_id, str)
    assert isinstance(expected_execution_key, str)
    return expected_execution_id, expected_execution_key


def _raise_cancel_ack_invalid(
    command: RunnerCommand,
    invalid_fields: list[str],
) -> None:
    raise ApplicationConflictError(
        "runner_cancel_ack_invalid",
        "Runner cancellation acknowledgement did not prove the owning process stopped",
        details={
            "command_id": command.id,
            "invalid_fields": invalid_fields,
        },
    )


def _validate_execution_status_report(report: ExecutionStatusReport) -> None:
    if report.status not in _RUNNER_REPORTED_EXECUTION_STATUSES:
        raise ApplicationConflictError(
            "invalid_runner_execution_status",
            f"Runner cannot report execution status {report.status.value!r}",
        )
    if report.status in _PHYSICAL_STOP_PROOF_REQUIRED_STATUSES:
        if report.physical_stop_confirmed is not True:
            raise ApplicationConflictError(
                "runner_execution_stop_proof_required",
                "Runner stopped-status reports require affirmative physical-stop proof",
                details={"status": report.status.value},
            )
        return
    if report.physical_stop_confirmed is not False:
        raise ApplicationConflictError(
            "runner_execution_stop_proof_invalid",
            "Runner cannot attach physical-stop proof to this execution status",
            details={"status": report.status.value},
        )


def _is_physical_stop_report(report: ExecutionStatusReport) -> bool:
    return (
        report.status in _PHYSICAL_STOP_PROOF_REQUIRED_STATUSES
        and report.physical_stop_confirmed is True
    )


def _apply_execution_provenance(
    execution: Execution,
    report: ExecutionStatusReport,
) -> bool:
    changed = False
    for name in (
        "executable_path",
        "tool_id",
        "tool_version",
        "platform_system",
        "platform_release",
        "platform_architecture",
        "process_created_at",
    ):
        value = getattr(report, name)
        if value not in {None, ""} and getattr(execution, name) != value:
            setattr(execution, name, value)
            changed = True
    return changed


def _apply_missing_execution_provenance(
    execution: Execution,
    report: ExecutionStatusReport,
) -> bool:
    changed = False
    for name in (
        "executable_path",
        "tool_id",
        "tool_version",
        "platform_system",
        "platform_release",
        "platform_architecture",
        "process_created_at",
    ):
        value = getattr(report, name)
        if value not in {None, ""} and getattr(execution, name) in {None, ""}:
            setattr(execution, name, value)
            changed = True
    return changed


def _append_exact(path: Path, offset: int, data: bytes) -> int:
    if offset < 0:
        raise ValueError("output offset must not be negative")
    path.parent.mkdir(parents=True, exist_ok=True)
    current_size = path.stat().st_size if path.exists() else 0
    if offset != current_size:
        raise ApplicationConflictError(
            "runner_output_offset_mismatch",
            f"Expected output offset {current_size}, received {offset}",
            details={"expected_offset": current_size, "received_offset": offset},
        )
    with path.open("ab") as stream:
        stream.write(data)
    return current_size + len(data)
