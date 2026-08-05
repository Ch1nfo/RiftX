"""Authenticated durable command channel between Control Plane and remote Runners."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import secrets
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Protocol

from riftx.application.errors import (
    ApplicationConflictError,
    AuthenticationError,
    EntityNotFoundError,
    RepositoryConflictError,
    RepositoryIntegrityError,
    ServiceUnavailableError,
)
from riftx.application.ports import (
    ExecutionRepository,
    RunEventRepository,
    RunnerCommandRepository,
    RunnerCredentialRepository,
    RunRepository,
    TerminalRepository,
    ToolCallIntentRepository,
)
from riftx.domain import (
    RUNNER_COMMAND_OWNERSHIP_CAPABILITY,
    RUNNER_STOP_ACK_BROWSER_SCHEMA,
    RUNNER_STOP_ACK_EXECUTION_SCHEMA,
    RUNNER_STOP_ACK_TARGET_HTTP_SCHEMA,
    RUNNER_STOP_ACK_TERMINAL_SCHEMA,
    BrowserSession,
    Execution,
    ExecutionStatus,
    ExecutorType,
    Node,
    NodeStatus,
    Run,
    RunKind,
    RunnerCommand,
    RunnerCommandKind,
    RunnerCommandOrigin,
    RunnerCommandOwnership,
    RunnerCommandOwnershipState,
    RunnerCommandStatus,
    RunnerCredential,
    RunnerEffectBinding,
    RunnerOperationFamily,
    RunnerOutputContract,
    RunnerPrincipal,
    RunnerResourceKind,
    RunnerStopReceipt,
    TerminalSession,
    TerminalStatus,
    runner_command_payload_binding_invalid_fields,
    runner_command_protocol,
    runner_payload_digest,
    runner_stop_ack_digest,
    runner_success_result_invalid_fields,
)
from riftx.domain.base import new_id, utc_now
from riftx.runner.paths import RunnerPaths
from riftx.runtime.types import ToolCallStatus
from riftx.security import validate_runner_registration_credential

from .nodes import NodeApplicationService, NodeHeartbeat, NodeRegistration
from .runs import require_general_run_operation

_MAX_OUTPUT_CHUNK_BYTES = 256 * 1024
_STOP_ACK_EXECUTION = RUNNER_STOP_ACK_EXECUTION_SCHEMA
_STOP_ACK_TARGET_HTTP = RUNNER_STOP_ACK_TARGET_HTTP_SCHEMA
_STOP_ACK_BROWSER = RUNNER_STOP_ACK_BROWSER_SCHEMA
_STOP_ACK_TERMINAL = RUNNER_STOP_ACK_TERMINAL_SCHEMA
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
_NORMAL_COMPLETION_REPORT_STATUSES = frozenset(
    {
        ExecutionStatus.COMPLETED,
        ExecutionStatus.EXITED,
        ExecutionStatus.FAILED,
        ExecutionStatus.HARD_TIMEOUT,
        ExecutionStatus.LOST,
    }
)
_LEGACY_TARGET_HTTP_TOOL_IDS = frozenset({"request_target_url"})
_LEGACY_STOP_ACK_MAX_RESULT_BYTES = 64 * 1024
_LEGACY_STOP_KINDS = frozenset(
    {
        RunnerCommandKind.CANCEL,
        RunnerCommandKind.TERMINAL_CLOSE,
        RunnerCommandKind.TARGET_HTTP_CANCEL,
        RunnerCommandKind.BROWSER_CLOSE,
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


@dataclass(frozen=True, slots=True)
class _LegacyReplacementStop:
    run_id: str
    node_id: str
    target: RunnerPrincipal
    kind: RunnerCommandKind
    resource_kind: RunnerResourceKind
    resource_id: str
    execution_id: str
    idempotency_key: str
    payload: dict[str, object]
    output_contract: RunnerOutputContract


class BrowserOwnershipRepository(Protocol):
    """Minimal authoritative browser identity surface used by command admission."""

    async def get_session(self, session_id: str) -> BrowserSession | None: ...

    async def list_active_sessions(self, *, node_id: str) -> Sequence[BrowserSession]: ...

    async def close_session_if_active(
        self,
        session_id: str,
        *,
        run_id: str,
        node_id: str,
        closed_at: datetime,
    ) -> tuple[BrowserSession, bool]: ...


class RunnerControlService:
    """Issues scoped credentials and persists reconnect-safe Runner commands."""

    def __init__(
        self,
        *,
        credentials: RunnerCredentialRepository,
        commands: RunnerCommandRepository,
        nodes: NodeApplicationService,
        executions: ExecutionRepository,
        stop_projection_executions: ExecutionRepository | None = None,
        runs: RunRepository,
        paths: RunnerPaths,
        registration_token: str | None,
        terminals: TerminalRepository | None = None,
        browser_sessions: BrowserOwnershipRepository | None = None,
        tool_call_intents: ToolCallIntentRepository | None = None,
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
        # Safety writes never fall back to the ordinary Execution repository:
        # that repository may stage normal ``execution_completed`` intents.
        # Missing or incorrectly configured projection wiring is rejected at
        # the mutation boundary instead of silently widening authority.
        self._stop_projection_executions = stop_projection_executions
        self._runs = runs
        self._paths = paths
        self._registration_token = validate_runner_registration_credential(registration_token)
        self._terminals = terminals
        self._browser_sessions = browser_sessions
        self._tool_call_intents = tool_call_intents
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
            protocol_capabilities=tuple(sorted(set(registration.capabilities))),
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
        run_id: str,
        origin: RunnerCommandOrigin,
        operation_family: RunnerOperationFamily,
        resource_kind: RunnerResourceKind,
        resource_id: str,
        execution_id: str | None = None,
        output_contract: RunnerOutputContract | None = None,
        target: RunnerPrincipal | None = None,
    ) -> tuple[RunnerCommand, bool]:
        command = await self._prepare_command(
            node_id,
            kind=kind,
            idempotency_key=idempotency_key,
            payload=payload,
            run_id=run_id,
            origin=origin,
            operation_family=operation_family,
            resource_kind=resource_kind,
            resource_id=resource_id,
            execution_id=execution_id,
            output_contract=output_contract,
            target=target,
        )
        return await self._commands.enqueue(command)

    async def _prepare_command(
        self,
        node_id: str,
        *,
        kind: RunnerCommandKind,
        idempotency_key: str,
        payload: dict[str, object],
        run_id: str,
        origin: RunnerCommandOrigin,
        operation_family: RunnerOperationFamily,
        resource_kind: RunnerResourceKind,
        resource_id: str,
        execution_id: str | None,
        output_contract: RunnerOutputContract | None,
        target: RunnerPrincipal | None,
    ) -> RunnerCommand:
        run = await self._runs.get(run_id)
        if run is None:
            raise EntityNotFoundError("Run", run_id)
        if run.kind is RunKind.CODE_AUDIT:
            # M1 has no authoritative Audit effect plan.  Resolve this fence
            # before Runner availability or credential details so admission is
            # deterministically deny-all with zero enqueue side effects.
            raise ApplicationConflictError(
                "code_audit_runner_admission_denied",
                "Code Audit Runner effects are not enabled in the M1 ownership protocol",
                details={"run_id": run_id, "operation": kind.value},
            )
        node = await self._nodes.get(node_id)
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
        command_id = _runner_command_id(node_id, idempotency_key)
        binding = RunnerEffectBinding(
            id=_runner_effect_binding_id(command_id),
            run_id=run.id,
            run_kind=run.kind,
            node_id=node_id,
            target=target,
            origin=origin,
            operation_family=operation_family,
            execution_id=execution_id,
            resource_kind=resource_kind,
            resource_id=resource_id,
        )
        ownership = RunnerCommandOwnership(
            command_id=command_id,
            effect_binding=binding,
            operation=kind,
            operation_family=operation_family,
            payload_digest=runner_payload_digest(payload),
            output_contract=output_contract or RunnerOutputContract(),
        )
        await self._validate_ownership_authority(
            ownership,
            kind=kind,
            payload=payload,
        )
        _require_runner_effect_policy(
            ownership,
            operation="service.runner.enqueue",
            origin="application_service",
            effect="host_execution",
            mode="normal",
        )
        if (
            node.status not in {NodeStatus.ONLINE, NodeStatus.DEGRADED}
            and operation_family is not RunnerOperationFamily.SAFETY_STOP
        ):
            raise ServiceUnavailableError(
                "runner_unavailable",
                f"Runner node {node_id!r} is not connected",
                details={"node_id": node_id, "status": node.status.value},
            )
        if (
            target_credential.revoked_at is not None
            and operation_family is not RunnerOperationFamily.SAFETY_STOP
        ):
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
        return RunnerCommand(
            id=command_id,
            node_id=node_id,
            kind=kind,
            idempotency_key=idempotency_key,
            target=target,
            ownership=ownership,
            ownership_state=RunnerCommandOwnershipState.VERIFIED,
            quarantine_reason="",
            payload=payload,
            created_at=now,
            updated_at=now,
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
        self._require_ownership_protocol(credential)
        deadline = asyncio.get_running_loop().time() + min(max(wait_seconds, 0), 30)
        while True:
            now = self._clock()
            command = await self._commands.lease_next(
                node_id,
                principal=credential.principal,
                lease_id=new_id(),
                leased_until=now + self._lease_duration,
                now=now,
                validate_candidate=self._poll_candidate_quarantine_reason,
                safety_only=safety_only,
            )
            if command is not None:
                return command
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return None
            await asyncio.sleep(min(0.25, remaining))

    async def _poll_candidate_quarantine_reason(
        self,
        command: RunnerCommand,
    ) -> str | None:
        try:
            await self._validate_command_binding(command)
            _require_runner_effect_policy(
                _require_verified_command_ownership(command),
                operation="service.runner.poll",
                origin="application_service",
                effect="runner_callback",
                mode="ownership_callback",
            )
        except (
            ApplicationConflictError,
            EntityNotFoundError,
            RepositoryIntegrityError,
        ) as exc:
            return _command_quarantine_reason(exc)
        return None

    async def finish_command(
        self,
        node_id: str,
        token: str,
        command_id: str,
        *,
        lease_id: str,
        state_version: int,
        envelope_digest: str,
        binding_digest: str,
        succeeded: bool,
        result: dict[str, object] | None = None,
        error: str = "",
    ) -> RunnerCommand:
        credential = await self.authenticate(node_id, token)
        self._require_ownership_protocol(credential)
        command = await self._require_command(command_id)
        self._require_command_scope(command, node_id, credential.principal)
        ownership = await self._validate_command_callback_identity(
            command,
            envelope_digest=envelope_digest,
            binding_digest=binding_digest,
        )
        stop_ack = succeeded and ownership.output_contract.stop_ack_schema is not None
        _require_runner_effect_policy(
            ownership,
            operation=(
                "service.runner.stop_ack"
                if stop_ack
                else "service.runner.finish"
            ),
            origin="application_service",
            effect="runner_callback",
            mode=("stop_proof" if stop_ack else "ownership_callback"),
        )
        if command.status in {
            RunnerCommandStatus.COMPLETED,
            RunnerCommandStatus.FAILED,
        }:
            self._require_finished_command_callback_state(
                command,
                lease_id=lease_id,
                state_version=state_version,
            )
        else:
            self._require_active_command_lease(
                command,
                lease_id=lease_id,
                state_version=state_version,
            )
        bounded_result = result if result is not None else {}
        result_limit = ownership.output_contract.max_result_bytes
        result_size = (
            len(json.dumps(bounded_result, ensure_ascii=False).encode())
            if bounded_result
            else 0
        )
        if result_size > result_limit:
            raise ApplicationConflictError(
                "runner_result_too_large",
                f"Runner command results must not exceed {result_limit} bytes",
            )
        stop_receipt: RunnerStopReceipt | None = None
        if succeeded and ownership.output_contract.stop_ack_schema is not None:
            await self._validate_stop_ack(command, bounded_result)
            binding = ownership.effect_binding
            stop_receipt = RunnerStopReceipt(
                id=_runner_stop_receipt_id(command.id),
                command_id=command.id,
                effect_binding_id=binding.id,
                envelope_digest=ownership.envelope_digest,
                binding_digest=binding.binding_digest,
                operation=command.kind,
                operation_family=ownership.operation_family,
                resource_kind=binding.resource_kind,
                resource_id=binding.resource_id,
                execution_id=binding.execution_id,
                node_id=command.node_id,
                principal=credential.principal,
                ack_digest=runner_stop_ack_digest(bounded_result),
                received_at=self._clock(),
            )
        elif succeeded:
            invalid_fields = runner_success_result_invalid_fields(
                command.kind,
                ownership.effect_binding,
                command.payload,
                bounded_result,
            )
            if invalid_fields:
                raise ApplicationConflictError(
                    "runner_result_invalid",
                    "Runner command result does not match its registered operation",
                    details={
                        "command_id": command.id,
                        "invalid_fields": list(invalid_fields),
                    },
                )
        finished = await self._commands.finish(
            command_id,
            principal=credential.principal,
            lease_id=lease_id,
            state_version=state_version,
            envelope_digest=envelope_digest,
            binding_digest=binding_digest,
            status=(RunnerCommandStatus.COMPLETED if succeeded else RunnerCommandStatus.FAILED),
            result=bounded_result,
            error=error[:8192],
            completed_at=self._clock(),
            stop_receipt=stop_receipt,
        )
        if stop_receipt is not None:
            await self._project_stop_receipt(stop_receipt)
        return finished

    async def record_legacy_stop_ack(
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
        """Accept only isolated physical-stop proof from a legacy lease owner."""

        credential = await self.authenticate(node_id, token)
        command = await self._require_command(command_id)
        self._require_command_scope(command, node_id, credential.principal)
        if (
            command.ownership_state is not RunnerCommandOwnershipState.QUARANTINED
            or command.ownership is not None
            or command.quarantine_reason != "legacy_ownership_missing"
        ):
            raise ApplicationConflictError(
                "runner_legacy_stop_ack_not_allowed",
                "Runner command is not an ownership-missing legacy quarantine",
            )
        if command.kind not in _LEGACY_STOP_KINDS:
            raise ApplicationConflictError(
                "runner_legacy_stop_ack_not_allowed",
                "Legacy acknowledgement sink only accepts safety-stop commands",
            )
        if (
            command.status is not RunnerCommandStatus.LEASED
            or command.lease_id != lease_id
            or command.lease_expires_at is None
        ):
            raise ApplicationConflictError(
                "runner_command_lease_mismatch",
                "Legacy Runner command lease is missing or no longer current",
            )
        _require_legacy_stop_ack_effect_policy(
            command,
            principal=credential.principal,
            lease_id=lease_id,
        )
        if not succeeded or error:
            raise ApplicationConflictError(
                "runner_legacy_stop_ack_not_affirmative",
                "Legacy acknowledgement sink only accepts affirmative stop proof",
            )
        bounded_result = result if result is not None else {}
        result_size = (
            len(json.dumps(bounded_result, ensure_ascii=False).encode())
            if bounded_result
            else 0
        )
        if result_size > _LEGACY_STOP_ACK_MAX_RESULT_BYTES:
            raise ApplicationConflictError(
                "runner_result_too_large",
                "Legacy Runner stop acknowledgements must not exceed 65536 bytes",
            )
        invalid_fields = _legacy_stop_ack_invalid_fields(command, bounded_result)
        if invalid_fields:
            _raise_stop_ack_invalid(command, invalid_fields)
        return await self._commands.record_legacy_stop_ack(
            command_id,
            principal=credential.principal,
            lease_id=lease_id,
            expected_state_version=command.state_version,
            ack=bounded_result,
            received_at=self._clock(),
        )

    async def renew_command_lease(
        self,
        node_id: str,
        token: str,
        command_id: str,
        *,
        lease_id: str,
        state_version: int,
        envelope_digest: str,
        binding_digest: str,
    ) -> RunnerCommand:
        credential = await self.authenticate(node_id, token)
        self._require_ownership_protocol(credential)
        command = await self._require_command(command_id)
        self._require_command_scope(command, node_id, credential.principal)
        ownership = await self._validate_command_callback_identity(
            command,
            envelope_digest=envelope_digest,
            binding_digest=binding_digest,
        )
        _require_runner_effect_policy(
            ownership,
            operation="service.runner.renew_lease",
            origin="application_service",
            effect="runner_callback",
            mode="ownership_callback",
        )
        self._require_active_command_lease(
            command,
            lease_id=lease_id,
            state_version=state_version,
        )
        now = self._clock()
        return await self._commands.renew_lease(
            command_id,
            principal=credential.principal,
            lease_id=lease_id,
            state_version=state_version,
            envelope_digest=envelope_digest,
            binding_digest=binding_digest,
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
        state_version: int,
        envelope_digest: str,
        binding_digest: str,
        offset: int,
        data: bytes,
    ) -> int:
        credential = await self.authenticate(node_id, token)
        self._require_ownership_protocol(credential)
        command = await self._require_command(command_id)
        self._require_command_scope(command, node_id, credential.principal)
        ownership = await self._validate_command_callback_identity(
            command,
            envelope_digest=envelope_digest,
            binding_digest=binding_digest,
        )
        _require_runner_effect_policy(
            ownership,
            operation="service.runner.command_output",
            origin="application_service",
            effect="runner_callback",
            mode="ownership_callback",
        )
        self._require_active_command_lease(
            command,
            lease_id=lease_id,
            state_version=state_version,
        )
        contract = ownership.output_contract
        if (
            "command" not in contract.allowed_streams
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
        if offset + len(data) > contract.max_output_bytes:
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
        *,
        command_id: str,
        effect_binding_id: str,
        envelope_digest: str,
        binding_digest: str,
    ) -> Execution:
        credential = await self.authenticate(node_id, token)
        self._require_ownership_protocol(credential)
        execution = await self._require_execution(execution_id)
        self._require_execution_scope(execution, node_id, credential.principal)
        _validate_execution_status_report(report)
        ownership = await self._validate_execution_callback_binding(
            execution,
            credential=credential,
            command_id=command_id,
            effect_binding_id=effect_binding_id,
            envelope_digest=envelope_digest,
            binding_digest=binding_digest,
        )
        _require_runner_effect_policy(
            ownership,
            operation="service.runner.execution_status",
            origin="application_service",
            effect="runner_callback",
            mode="ownership_callback",
        )
        verified_cancel_report = _is_verified_cancel_status_report(ownership, report)
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
            # Select the repository from both the verified incoming contract
            # and the durable state participating in this CAS. A late natural
            # terminal retry may fill provenance on an already safety-projected
            # CANCELLED row; that update must remain non-emitting even though
            # the retry itself says COMPLETED/EXITED.
            execution_updates = self._execution_status_updates(
                report,
                durable_status=expected_status,
                verified_cancel_report=verified_cancel_report,
            )
            execution, saved = await execution_updates.save_if_status(
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

    async def _validate_ownership_authority(
        self,
        ownership: RunnerCommandOwnership,
        *,
        kind: RunnerCommandKind,
        payload: dict[str, object],
    ) -> None:
        binding = ownership.effect_binding
        protocol = runner_command_protocol(kind)
        if ownership.operation is not kind:
            raise ApplicationConflictError(
                "runner_command_operation_mismatch",
                "Runner command operation does not match its immutable envelope",
            )
        if ownership.operation_family is not protocol.operation_family:
            raise ApplicationConflictError(
                "runner_command_family_mismatch",
                "Runner command operation is not registered for this effect family",
            )
        if binding.resource_kind is not protocol.resource_kind:
            raise ApplicationConflictError(
                "runner_command_resource_kind_mismatch",
                "Runner command operation is not registered for this resource kind",
            )
        allowed_origins = (
            {
                RunnerCommandOrigin.APPLICATION_SERVICE,
                RunnerCommandOrigin.SAFETY_RECONCILER,
            }
            if ownership.operation_family is RunnerOperationFamily.SAFETY_STOP
            else {RunnerCommandOrigin.APPLICATION_SERVICE}
        )
        if binding.origin not in allowed_origins:
            raise ApplicationConflictError(
                "runner_command_origin_mismatch",
                "Runner command origin is not registered for this effect family",
            )
        contract = ownership.output_contract
        if contract.result_schema != protocol.result_schema:
            raise ApplicationConflictError(
                "runner_result_contract_mismatch",
                "Runner command result schema is not registered for this operation",
            )
        if ownership.operation_family is RunnerOperationFamily.SAFETY_STOP:
            if contract.stop_ack_schema != protocol.stop_ack_schema:
                raise ApplicationConflictError(
                    "runner_stop_ack_contract_mismatch",
                    "Safety Runner command requires its resource-family stop ACK schema",
                )
        elif contract.stop_ack_schema is not None:
            raise ApplicationConflictError(
                "runner_stop_ack_contract_invalid",
                "Non-safety Runner command cannot request a physical-stop acknowledgement",
            )
        if protocol.output_mode == "command":
            valid_output_contract = (
                contract.max_output_bytes > 0
                and contract.allowed_streams == ("command",)
            )
        elif protocol.output_mode == "execution":
            valid_output_contract = (
                contract.max_output_bytes > 0
                and contract.allowed_streams == ("stderr", "stdout")
            )
        else:
            valid_output_contract = (
                contract.max_output_bytes == 0 and contract.allowed_streams == ()
            )
        if not valid_output_contract:
            raise ApplicationConflictError(
                "runner_output_contract_invalid",
                "Runner command output is not registered for this operation",
            )
        await self._validate_binding_authority(binding, kind=kind, payload=payload)

    async def _validate_binding_authority(
        self,
        binding: RunnerEffectBinding,
        *,
        kind: RunnerCommandKind,
        payload: dict[str, object],
    ) -> None:
        run = await self._runs.get(binding.run_id)
        if run is None:
            raise EntityNotFoundError("Run", binding.run_id)
        if run.kind is not binding.run_kind or run.node_id != binding.node_id:
            raise ApplicationConflictError(
                "runner_effect_run_mismatch",
                "Runner effect binding does not match its authoritative Run",
            )
        if run.kind is RunKind.CODE_AUDIT:
            raise ApplicationConflictError(
                "code_audit_runner_admission_denied",
                "Code Audit Runner effects are not enabled in the M1 ownership protocol",
            )
        execution: Execution | None = None
        if binding.execution_id is not None:
            execution = await self._require_execution(binding.execution_id)
            if (
                execution.run_id != binding.run_id
                or execution.node_id != binding.node_id
                or execution.owner != binding.target
                or execution.audit_id != binding.audit_id
                or execution.plan_digest != binding.plan_digest
            ):
                raise ApplicationConflictError(
                    "runner_effect_execution_mismatch",
                    "Runner effect binding does not match its authoritative Execution",
                )

        if binding.resource_kind is RunnerResourceKind.EXECUTION:
            if execution is None or binding.resource_id != execution.id:
                raise ApplicationConflictError(
                    "runner_effect_execution_identity_mismatch",
                    "Runner execution resource identity is invalid",
                )
        elif binding.resource_kind is RunnerResourceKind.TERMINAL_SESSION:
            if self._terminals is None:
                raise ServiceUnavailableError(
                    "runner_terminal_authority_unavailable",
                    "Terminal ownership repository is unavailable",
                )
            terminal = await self._terminals.get(binding.resource_id)
            if (
                terminal is None
                or execution is None
                or terminal.run_id != binding.run_id
                or terminal.execution_id != execution.id
            ):
                raise ApplicationConflictError(
                    "runner_terminal_authority_mismatch",
                    "Runner terminal binding does not match its durable session",
                )
        elif binding.resource_kind is RunnerResourceKind.BROWSER_SESSION:
            if self._browser_sessions is None:
                raise ServiceUnavailableError(
                    "runner_browser_authority_unavailable",
                    "Browser ownership repository is unavailable",
                )
            session = await self._browser_sessions.get_session(binding.resource_id)
            if (
                session is None
                or getattr(session, "run_id", None) != binding.run_id
                or getattr(session, "node_id", None) != binding.node_id
            ):
                raise ApplicationConflictError(
                    "runner_browser_authority_mismatch",
                    "Runner browser binding does not match its durable session",
                )
        elif binding.resource_kind is RunnerResourceKind.TARGET_HTTP_INTENT:
            if self._tool_call_intents is None:
                raise ServiceUnavailableError(
                    "runner_target_http_authority_unavailable",
                    "Target HTTP ownership repository is unavailable",
                )
            intent = await self._tool_call_intents.get(binding.resource_id)
            if intent is None or intent.run_id != binding.run_id:
                raise ApplicationConflictError(
                    "runner_target_http_authority_mismatch",
                    "Runner Target HTTP binding does not match its durable intent",
                )
        else:
            raise ApplicationConflictError(
                "runner_resource_kind_unknown",
                "Runner effect resource kind is not registered",
            )
        invalid_fields = runner_command_payload_binding_invalid_fields(
            kind,
            binding,
            payload,
            authoritative_execution_key=(
                execution.execution_key if execution is not None else None
            ),
        )
        if invalid_fields:
            raise ApplicationConflictError(
                "runner_command_payload_binding_mismatch",
                "Runner command payload does not match its immutable effect binding",
                details={"invalid_fields": list(invalid_fields)},
            )

    async def _validate_command_binding(self, command: RunnerCommand) -> None:
        ownership = _require_verified_command_ownership(command)
        if command.kind is not ownership.operation:
            raise ApplicationConflictError(
                "runner_command_operation_mismatch",
                "Runner command operation does not match its immutable envelope",
            )
        if runner_payload_digest(command.payload) != ownership.payload_digest:
            raise ApplicationConflictError(
                "runner_command_payload_digest_mismatch",
                "Runner command payload does not match its immutable envelope",
            )
        await self._validate_ownership_authority(
            ownership,
            kind=command.kind,
            payload=command.payload,
        )

    async def _validate_command_callback_identity(
        self,
        command: RunnerCommand,
        *,
        envelope_digest: str,
        binding_digest: str,
    ) -> RunnerCommandOwnership:
        await self._validate_command_binding(command)
        ownership = _require_verified_command_ownership(command)
        if not hmac.compare_digest(ownership.envelope_digest, envelope_digest):
            raise ApplicationConflictError(
                "runner_command_envelope_digest_mismatch",
                "Runner command envelope digest does not match",
            )
        if not hmac.compare_digest(
            ownership.effect_binding.binding_digest,
            binding_digest,
        ):
            raise ApplicationConflictError(
                "runner_effect_binding_digest_mismatch",
                "Runner effect binding digest does not match",
            )
        return ownership

    def _require_active_command_lease(
        self,
        command: RunnerCommand,
        *,
        lease_id: str,
        state_version: int,
    ) -> None:
        if (
            command.status is not RunnerCommandStatus.LEASED
            or command.lease_id != lease_id
            or command.lease_expires_at is None
            or command.lease_expires_at <= self._clock()
        ):
            raise ApplicationConflictError(
                "runner_command_lease_mismatch",
                "Runner command lease is missing, expired, or no longer current",
            )
        if command.state_version != state_version:
            raise ApplicationConflictError(
                "runner_command_state_version_mismatch",
                "Runner command state version is no longer current",
            )

    @staticmethod
    def _require_finished_command_callback_state(
        command: RunnerCommand,
        *,
        lease_id: str,
        state_version: int,
    ) -> None:
        """Admit only a retry of the lease generation that already finished."""

        if command.lease_id != lease_id or command.state_version != state_version + 1:
            raise ApplicationConflictError(
                "runner_command_lease_mismatch",
                "Runner command completion retry does not match its final lease generation",
            )

    async def _validate_stop_ack(
        self,
        command: RunnerCommand,
        result: dict[str, object],
    ) -> None:
        ownership = _require_verified_command_ownership(command)
        binding = ownership.effect_binding
        schema = ownership.output_contract.stop_ack_schema
        if schema == _STOP_ACK_EXECUTION:
            execution = await self._require_execution(binding.resource_id)
            invalid_fields = _execution_stop_ack_invalid_fields(
                command,
                result,
                execution=execution,
            )
        elif schema == _STOP_ACK_TERMINAL:
            if binding.execution_id is None:
                invalid_fields = ["binding.execution_id"]
            else:
                execution = await self._require_execution(binding.execution_id)
                invalid_fields = _execution_stop_ack_invalid_fields(
                    command,
                    result,
                    execution=execution,
                )
                if result.get("session_id") != binding.resource_id:
                    invalid_fields.append("session_id")
        elif schema == _STOP_ACK_TARGET_HTTP:
            invalid_fields = _target_http_stop_ack_invalid_fields(binding, result)
        elif schema == _STOP_ACK_BROWSER:
            invalid_fields = _browser_stop_ack_invalid_fields(binding, result)
        else:
            invalid_fields = ["output_contract.stop_ack_schema"]
        if invalid_fields:
            _raise_stop_ack_invalid(command, invalid_fields)

    async def reconcile_stop_receipts(self, *, limit: int = 100) -> int:
        projected = 0
        for receipt in await self._commands.list_pending_stop_receipts(limit=limit):
            if await self._project_stop_receipt(receipt):
                projected += 1
        return projected

    async def reconcile_quarantined_commands(self, *, limit: int = 100) -> int:
        """Converge provable legacy effects without deriving owners from payloads."""

        reconciled = 0
        for command in await self._commands.list_quarantined(limit=limit):
            replacements: list[RunnerCommand] = []
            failed = False
            targets = await self._legacy_replacement_stops(command.node_id)
            for target in targets:
                try:
                    replacement = await self._enqueue_legacy_replacement(target)
                except (
                    ApplicationConflictError,
                    EntityNotFoundError,
                    RepositoryConflictError,
                    ServiceUnavailableError,
                ):
                    failed = True
                    break
                replacements.append(replacement)
            if failed:
                # Authority or transport state may become available later. Do
                # not convert a retryable safety convergence gap into a manual
                # terminal decision.
                continue
            await self._commands.mark_quarantine_reconciled(
                command.id,
                replacement_command_id=(replacements[0].id if replacements else None),
                reconciled_at=self._clock(),
            )
            reconciled += 1
        return reconciled

    async def _enqueue_legacy_replacement(
        self,
        target: _LegacyReplacementStop,
    ) -> RunnerCommand:
        command = await self._prepare_command(
            target.node_id,
            kind=target.kind,
            idempotency_key=target.idempotency_key,
            payload=target.payload,
            run_id=target.run_id,
            origin=RunnerCommandOrigin.SAFETY_RECONCILER,
            operation_family=RunnerOperationFamily.SAFETY_STOP,
            resource_kind=target.resource_kind,
            resource_id=target.resource_id,
            execution_id=target.execution_id,
            output_contract=target.output_contract,
            target=target.target,
        )
        ownership = _require_verified_command_ownership(command)
        _require_runner_effect_policy(
            ownership,
            operation="service.runner.reconcile_quarantine",
            origin="safety_reconciler",
            effect="durable_write",
            mode="safety_reduce_only",
        )
        persisted, _ = await self._commands.enqueue(command)
        return persisted

    async def _legacy_replacement_stops(
        self,
        node_id: str,
    ) -> tuple[_LegacyReplacementStop, ...]:
        """Resolve legacy stop targets exclusively from durable authority ledgers.

        Quarantined command payload, kind, path and idempotency text are never
        consulted. Executions with a complete verified launch binding belong
        to the v1 protocol and are intentionally excluded.
        """

        runs = await self._general_runs_for_node(node_id)
        active_terminals = (
            tuple(await self._terminals.list_active()) if self._terminals is not None else ()
        )
        terminals_by_execution: dict[str, list[TerminalSession]] = {}
        for active_terminal in active_terminals:
            terminals_by_execution.setdefault(active_terminal.execution_id, []).append(
                active_terminal
            )
        targets: list[_LegacyReplacementStop] = []
        seen_executions: set[str] = set()
        for run in runs:
            offset = 0
            while True:
                page = tuple(await self._executions.list(run.id, limit=1000, offset=offset))
                for execution in page:
                    if execution.id in seen_executions:
                        continue
                    seen_executions.add(execution.id)
                    if not _legacy_execution_requires_stop(execution, node_id=node_id):
                        continue
                    terminals = terminals_by_execution.get(execution.id, [])
                    matched_terminal = terminals[0] if len(terminals) == 1 else None
                    if (
                        execution.executor_type is ExecutorType.PTY
                        and matched_terminal is not None
                        and matched_terminal.execution_id == execution.id
                        and matched_terminal.run_id == execution.run_id
                        and matched_terminal.runner_id == execution.node_id
                    ):
                        targets.append(
                            _legacy_terminal_stop(execution, matched_terminal.id)
                        )
                    else:
                        targets.append(_legacy_execution_stop(execution))
                if len(page) < 1000:
                    break
                offset += len(page)

        # Browser and Target HTTP ledgers are still inspected. Their M1 rows
        # do not persist a Runner principal, so they cannot yield a verified
        # replacement envelope; they remain manual/unconfirmed instead of
        # borrowing a principal from the quarantined payload or current node.
        if self._browser_sessions is not None:
            await self._browser_sessions.list_active_sessions(node_id=node_id)
        if self._tool_call_intents is not None:
            for run in runs:
                await self._tool_call_intents.active_for_run(
                    run.id,
                    tool_ids=_LEGACY_TARGET_HTTP_TOOL_IDS,
                )

        return tuple(
            sorted(
                targets,
                key=lambda item: (
                    item.run_id,
                    item.resource_kind.value,
                    item.resource_id,
                ),
            )
        )

    async def _general_runs_for_node(self, node_id: str) -> tuple[Run, ...]:
        runs: list[Run] = []
        offset = 0
        while True:
            page = tuple(
                await self._runs.list(
                    kind=RunKind.GENERAL,
                    limit=1000,
                    offset=offset,
                )
            )
            runs.extend(run for run in page if run.node_id == node_id)
            if len(page) < 1000:
                break
            offset += len(page)
        return tuple(runs)

    async def _project_stop_receipt(self, receipt: RunnerStopReceipt) -> bool:
        try:
            command = await self._require_command(receipt.command_id)
            run_id = await self._receipt_run_id(receipt, command=command)
            await self._validate_command_binding(command)
            ownership = _require_verified_command_ownership(command)
            await self._validate_stop_receipt_contract(
                command,
                receipt=receipt,
                ownership=ownership,
            )
            _require_runner_effect_policy(
                ownership,
                operation="service.runner.reconcile_stop_receipts",
                origin="safety_reconciler",
                effect="durable_write",
                mode="safety_reduce_only",
            )
        except (
            ApplicationConflictError,
            AuthenticationError,
            EntityNotFoundError,
            RepositoryConflictError,
            RepositoryIntegrityError,
            ServiceUnavailableError,
        ):
            # A receipt is durable evidence, not authority by itself. Corrupt
            # or temporarily unverifiable evidence remains pending so a later
            # repair/reconciliation pass can retry without projecting it onto
            # any resource selected by receipt-controlled fields.
            return False
        binding = ownership.effect_binding
        if receipt.resource_kind is RunnerResourceKind.BROWSER_SESSION:
            if self._browser_sessions is None:
                return False
            try:
                await self._browser_sessions.close_session_if_active(
                    receipt.resource_id,
                    run_id=run_id,
                    node_id=receipt.node_id,
                    closed_at=receipt.received_at,
                )
            except (
                ApplicationConflictError,
                EntityNotFoundError,
                RepositoryConflictError,
            ):
                return False
            return await self._commands.mark_stop_receipt_projected(
                receipt.id,
                projected_at=self._clock(),
                expected_state_version=0,
            )
        if receipt.resource_kind is RunnerResourceKind.TARGET_HTTP_INTENT:
            if self._tool_call_intents is None:
                return False
            try:
                intent = await self._tool_call_intents.get(receipt.resource_id)
                if intent is None or intent.run_id != run_id:
                    return False
                if intent.status is ToolCallStatus.EXECUTING:
                    intent, _ = await self._tool_call_intents.compare_and_set_status(
                        intent.id,
                        expected={ToolCallStatus.EXECUTING},
                        target=ToolCallStatus.CANCELLED,
                    )
                if intent.status not in {
                    ToolCallStatus.CANCELLED,
                    ToolCallStatus.COMPLETED,
                    ToolCallStatus.REJECTED,
                    ToolCallStatus.FAILED,
                }:
                    return False
            except (
                ApplicationConflictError,
                EntityNotFoundError,
                RepositoryConflictError,
            ):
                return False
            return await self._commands.mark_stop_receipt_projected(
                receipt.id,
                projected_at=self._clock(),
                expected_state_version=0,
            )
        if receipt.execution_id is None:
            # Unknown/future resource families remain pending until a typed
            # projector is registered; never consume proof without projection.
            return False
        try:
            stop_projection_executions = self._require_stop_projection_executions()
            execution = await stop_projection_executions.get(receipt.execution_id)
            if execution is None:
                raise EntityNotFoundError("Execution", receipt.execution_id)
            if not _execution_matches_stop_binding(
                execution,
                binding=binding,
                receipt=receipt,
            ):
                return False
            received_at = receipt.received_at
            for _ in range(8):
                expected_status = execution.status
                candidate = execution.model_copy(deep=True)
                changed = False
                if candidate.status not in _PHYSICAL_STOP_PROOF_REQUIRED_STATUSES:
                    if not candidate.can_transition_to(ExecutionStatus.CANCELLED):
                        return False
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
                execution, saved = await stop_projection_executions.save_if_status(
                    candidate,
                    expected={expected_status},
                )
                if saved:
                    break
                if not _execution_matches_stop_binding(
                    execution,
                    binding=binding,
                    receipt=receipt,
                ):
                    return False
            else:
                return False
            await self._sync_terminal_status(execution, execution.status)
            return await self._commands.mark_stop_receipt_projected(
                receipt.id,
                projected_at=self._clock(),
                expected_state_version=0,
            )
        except (
            ApplicationConflictError,
            AuthenticationError,
            EntityNotFoundError,
            RepositoryConflictError,
            RepositoryIntegrityError,
            ServiceUnavailableError,
        ):
            return False

    async def _receipt_run_id(
        self,
        receipt: RunnerStopReceipt,
        *,
        command: RunnerCommand | None = None,
    ) -> str:
        command = command or await self._require_command(receipt.command_id)
        ownership = _require_verified_command_ownership(command)
        binding = ownership.effect_binding
        if (
            ownership.operation_family is not RunnerOperationFamily.SAFETY_STOP
            or command.kind is not receipt.operation
            or ownership.operation is not receipt.operation
            or ownership.operation_family is not receipt.operation_family
            or binding.operation_family is not receipt.operation_family
            or binding.execution_id != receipt.execution_id
            or binding.id != receipt.effect_binding_id
            or not hmac.compare_digest(binding.binding_digest, receipt.binding_digest)
            or not hmac.compare_digest(ownership.envelope_digest, receipt.envelope_digest)
            or binding.resource_kind is not receipt.resource_kind
            or binding.resource_id != receipt.resource_id
            or binding.node_id != receipt.node_id
            or binding.target != receipt.principal
        ):
            raise ApplicationConflictError(
                "runner_stop_receipt_binding_mismatch",
                "Runner stop receipt no longer matches its immutable command binding",
            )
        return binding.run_id

    async def _validate_stop_receipt_contract(
        self,
        command: RunnerCommand,
        *,
        receipt: RunnerStopReceipt,
        ownership: RunnerCommandOwnership,
    ) -> None:
        binding = ownership.effect_binding
        if (
            command.status is not RunnerCommandStatus.COMPLETED
            or ownership.operation_family is not RunnerOperationFamily.SAFETY_STOP
            or ownership.output_contract.stop_ack_schema
            != _expected_stop_ack_schema(binding)
            or not hmac.compare_digest(
                receipt.ack_digest,
                runner_stop_ack_digest(command.result),
            )
        ):
            raise ApplicationConflictError(
                "runner_stop_receipt_contract_mismatch",
                "Runner stop receipt does not match its completed safety command",
            )
        # Re-validate typed ACK semantics after restart; persistence of a
        # digest is not sufficient if either the receipt or result row was
        # corrupted below the service boundary.
        await self._validate_stop_ack(command, command.result)

    def _require_stop_projection_executions(self) -> ExecutionRepository:
        repository = self._stop_projection_executions
        if (
            repository is None
            or getattr(repository, "emits_workflow_signal_intents", None) is not False
        ):
            raise ServiceUnavailableError(
                "runner_stop_projection_unavailable",
                "Runner safety projection requires a verified non-emitting repository",
            )
        return repository

    def _require_completion_executions(self) -> ExecutionRepository:
        if getattr(self._executions, "emits_workflow_signal_intents", None) is not True:
            raise ServiceUnavailableError(
                "runner_completion_projection_unavailable",
                "Runner terminal completion requires a verified intent-emitting repository",
            )
        return self._executions

    def _execution_status_updates(
        self,
        report: ExecutionStatusReport,
        *,
        durable_status: ExecutionStatus,
        verified_cancel_report: bool,
    ) -> ExecutionRepository:
        if verified_cancel_report or durable_status is ExecutionStatus.CANCELLED:
            return self._require_stop_projection_executions()
        if report.status in _NORMAL_COMPLETION_REPORT_STATUSES:
            return self._require_completion_executions()
        return self._executions

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
        command_id: str,
        effect_binding_id: str,
        envelope_digest: str,
        binding_digest: str,
        stream: str,
        offset: int,
        data: bytes,
    ) -> int:
        credential = await self.authenticate(node_id, token)
        self._require_ownership_protocol(credential)
        execution = await self._require_execution(execution_id)
        self._require_execution_scope(execution, node_id, credential.principal)
        ownership = await self._validate_execution_callback_binding(
            execution,
            credential=credential,
            command_id=command_id,
            effect_binding_id=effect_binding_id,
            envelope_digest=envelope_digest,
            binding_digest=binding_digest,
        )
        _require_runner_effect_policy(
            ownership,
            operation="service.runner.execution_output",
            origin="application_service",
            effect="runner_callback",
            mode="ownership_callback",
        )
        if stream not in {"stdout", "stderr"}:
            raise ValueError("stream must be stdout or stderr")
        contract = ownership.output_contract
        if stream not in contract.allowed_streams:
            raise AuthenticationError(
                "runner_execution_output_scope_mismatch",
                "Runner launch command does not authorize this output stream",
            )
        if len(data) > _MAX_OUTPUT_CHUNK_BYTES:
            raise ApplicationConflictError(
                "runner_output_chunk_too_large",
                f"Runner output chunks must not exceed {_MAX_OUTPUT_CHUNK_BYTES} bytes",
            )
        if offset + len(data) > contract.max_output_bytes:
            raise ApplicationConflictError(
                "runner_execution_output_too_large",
                "Runner execution output exceeds its immutable launch contract",
                details={
                    "max_output_bytes": contract.max_output_bytes,
                    "attempted_offset": offset,
                    "attempted_bytes": len(data),
                },
            )
        path = Path(execution.stdout_path if stream == "stdout" else execution.stderr_path)
        key = (execution_id, stream)
        lock = self._output_locks.setdefault(key, asyncio.Lock())
        async with lock:
            next_offset = await asyncio.to_thread(_append_exact, path, offset, data)
        return next_offset

    async def _validate_execution_callback_binding(
        self,
        execution: Execution,
        *,
        credential: RunnerCredential,
        command_id: str,
        effect_binding_id: str,
        envelope_digest: str,
        binding_digest: str,
    ) -> RunnerCommandOwnership:
        """Validate the exact launch envelope before callback mutation or file I/O."""

        # Principal precedence is intentional: a foreign authenticated Runner
        # must not learn whether a supplied command/digest happens to exist.
        self._require_execution_scope(
            execution,
            credential.node_id,
            credential.principal,
        )
        persisted_binding = (
            execution.runner_command_id,
            execution.runner_effect_binding_id,
            execution.runner_envelope_digest,
            execution.runner_binding_digest,
        )
        if any(item is None for item in persisted_binding):
            raise ApplicationConflictError(
                "runner_execution_callback_binding_missing",
                "Execution has no verified Runner launch command binding",
                details={"execution_id": execution.id},
            )
        persisted_envelope_digest = execution.runner_envelope_digest
        persisted_effect_digest = execution.runner_binding_digest
        assert persisted_envelope_digest is not None
        assert persisted_effect_digest is not None
        if command_id != execution.runner_command_id:
            raise ApplicationConflictError(
                "runner_execution_command_mismatch",
                "Execution callback does not match its launch command",
            )
        if effect_binding_id != execution.runner_effect_binding_id:
            raise ApplicationConflictError(
                "runner_execution_effect_binding_mismatch",
                "Execution callback does not match its launch effect binding",
            )

        command = await self._require_command(execution.runner_command_id)
        self._require_command_scope(
            command,
            credential.node_id,
            credential.principal,
        )
        await self._validate_command_binding(command)
        ownership = _require_verified_command_ownership(command)
        binding = ownership.effect_binding
        expected_kind = (
            RunnerCommandKind.TERMINAL_START
            if execution.executor_type is ExecutorType.PTY
            else RunnerCommandKind.EXECUTE
        )
        if (
            command.kind is not expected_kind
            or binding.id != execution.runner_effect_binding_id
            or binding.execution_id != execution.id
            or binding.run_id != execution.run_id
            or binding.node_id != execution.node_id
            or binding.target != execution.owner
        ):
            raise ApplicationConflictError(
                "runner_execution_launch_binding_invalid",
                "Execution does not match its immutable Runner launch envelope",
            )
        if not hmac.compare_digest(
            ownership.envelope_digest,
            persisted_envelope_digest,
        ) or not hmac.compare_digest(
            binding.binding_digest,
            persisted_effect_digest,
        ):
            raise ApplicationConflictError(
                "runner_execution_persisted_digest_mismatch",
                "Execution launch digests do not match the persisted command envelope",
            )
        if not hmac.compare_digest(ownership.envelope_digest, envelope_digest):
            raise ApplicationConflictError(
                "runner_command_envelope_digest_mismatch",
                "Runner command envelope digest does not match",
            )
        if not hmac.compare_digest(binding.binding_digest, binding_digest):
            raise ApplicationConflictError(
                "runner_effect_binding_digest_mismatch",
                "Runner effect binding digest does not match",
            )
        return ownership

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

    @staticmethod
    def _require_ownership_protocol(credential: RunnerCredential) -> None:
        if RUNNER_COMMAND_OWNERSHIP_CAPABILITY not in credential.protocol_capabilities:
            raise ApplicationConflictError(
                "runner_protocol_capability_missing",
                "Runner does not support immutable command ownership envelopes",
                details={"required_capability": RUNNER_COMMAND_OWNERSHIP_CAPABILITY},
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


def _require_runner_effect_policy(
    ownership: RunnerCommandOwnership,
    *,
    operation: str,
    origin: str,
    effect: str,
    mode: str,
) -> None:
    """Apply the catalog to one fully verified immutable Runner envelope."""

    from riftx.application.run_kind_effects import (
        RunEffectOwnership,
        RunKindEffectPolicyDenied,
        require_run_kind_effect_policy,
    )

    binding = ownership.effect_binding
    try:
        require_run_kind_effect_policy(
            operation,
            origin,
            ownership=RunEffectOwnership(
                run_id=binding.run_id,
                run_kind=binding.run_kind,
                audit_id=binding.audit_id,
                plan_digest=binding.plan_digest,
                execution_id=binding.execution_id,
                resource_kind=binding.resource_kind.value,
                resource_id=binding.resource_id,
                node_id=binding.node_id,
                runner_principal=binding.target,
                runner_command_id=ownership.command_id,
            ),
            effect=effect,
            mode=mode,
        )
    except (RunKindEffectPolicyDenied, TypeError, ValueError):
        raise ApplicationConflictError(
            "run_kind_effect_policy_denied",
            "The requested Runner effect is not admitted for this immutable owner",
        ) from None


def _require_legacy_stop_ack_effect_policy(
    command: RunnerCommand,
    *,
    principal: RunnerPrincipal,
    lease_id: str,
) -> None:
    """Apply the dedicated non-Run legacy stop-proof policy."""

    from riftx.application.run_kind_effects import (
        EffectMode,
        EffectOrigin,
        LegacyRunnerCommandEffectOwnership,
        OperationEffect,
        RunEffectOperation,
        RunKindEffectPolicyDenied,
        require_run_kind_effect_policy,
    )

    try:
        require_run_kind_effect_policy(
            RunEffectOperation.SERVICE_RUNNER_LEGACY_STOP_ACK,
            EffectOrigin.APPLICATION_SERVICE,
            ownership=LegacyRunnerCommandEffectOwnership(
                node_id=command.node_id,
                runner_principal=principal,
                runner_command_id=command.id,
                lease_identity=lease_id,
                quarantine_state="quarantined:legacy_ownership_missing",
            ),
            effect=OperationEffect.RUNNER_CALLBACK,
            mode=EffectMode.STOP_PROOF,
        )
    except (RunKindEffectPolicyDenied, TypeError, ValueError):
        raise ApplicationConflictError(
            "run_kind_effect_policy_denied",
            "The legacy Runner stop acknowledgement is not admitted",
        ) from None


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


def _runner_stop_receipt_id(command_id: str) -> str:
    digest = hashlib.sha256(
        f"riftx.runner-stop-receipt/v1\0{command_id}".encode()
    ).hexdigest()
    return f"runner-stop-{digest[:52]}"


def _runner_command_id(node_id: str, idempotency_key: str) -> str:
    digest = hashlib.sha256(
        f"riftx.runner-command-id/v1\0{node_id}\0{idempotency_key}".encode()
    ).hexdigest()
    return f"runner-command-{digest[:49]}"


def _runner_effect_binding_id(command_id: str) -> str:
    digest = hashlib.sha256(
        f"riftx.runner-effect-binding-id/v1\0{command_id}".encode()
    ).hexdigest()
    return f"runner-effect-{digest[:50]}"


def _legacy_execution_requires_stop(
    execution: Execution,
    *,
    node_id: str,
) -> bool:
    runner_binding = (
        execution.runner_command_id,
        execution.runner_effect_binding_id,
        execution.runner_binding_digest,
        execution.runner_envelope_digest,
    )
    return (
        execution.node_id == node_id
        and execution.owner is not None
        and all(item is None for item in runner_binding)
        and execution.audit_id is None
        and execution.plan_digest is None
        and execution.physical_stop_confirmed_at is None
    )


def _legacy_execution_stop(execution: Execution) -> _LegacyReplacementStop:
    owner = execution.owner
    if owner is None:  # pragma: no cover - guarded by the authority resolver
        raise ValueError("legacy Execution replacement requires an authoritative owner")
    return _LegacyReplacementStop(
        run_id=execution.run_id,
        node_id=execution.node_id,
        target=owner,
        kind=RunnerCommandKind.CANCEL,
        resource_kind=RunnerResourceKind.EXECUTION,
        resource_id=execution.id,
        execution_id=execution.id,
        idempotency_key=_legacy_stop_idempotency_key(
            RunnerResourceKind.EXECUTION,
            execution.id,
        ),
        payload={
            "execution_id": execution.id,
            "execution_key": execution.execution_key,
        },
        output_contract=RunnerOutputContract(
            result_schema="riftx.runner-result/execution-stop/v1",
            stop_ack_schema=RUNNER_STOP_ACK_EXECUTION_SCHEMA,
        ),
    )


def _legacy_terminal_stop(
    execution: Execution,
    terminal_id: str,
) -> _LegacyReplacementStop:
    owner = execution.owner
    if owner is None:  # pragma: no cover - guarded by the authority resolver
        raise ValueError("legacy Terminal replacement requires an authoritative owner")
    return _LegacyReplacementStop(
        run_id=execution.run_id,
        node_id=execution.node_id,
        target=owner,
        kind=RunnerCommandKind.TERMINAL_CLOSE,
        resource_kind=RunnerResourceKind.TERMINAL_SESSION,
        resource_id=terminal_id,
        execution_id=execution.id,
        idempotency_key=_legacy_stop_idempotency_key(
            RunnerResourceKind.TERMINAL_SESSION,
            terminal_id,
        ),
        payload={
            "session_id": terminal_id,
            "execution_id": execution.id,
            "execution_key": execution.execution_key,
        },
        output_contract=RunnerOutputContract(
            result_schema="riftx.runner-result/terminal-stop/v1",
            stop_ack_schema=RUNNER_STOP_ACK_TERMINAL_SCHEMA,
        ),
    )


def _legacy_stop_idempotency_key(
    resource_kind: RunnerResourceKind,
    resource_id: str,
) -> str:
    digest = hashlib.sha256(
        (
            "riftx.runner-legacy-replacement-stop/v1\0"
            f"{resource_kind.value}\0{resource_id}"
        ).encode()
    ).hexdigest()
    return f"legacy-stop-v1:{digest}"


def _require_verified_command_ownership(
    command: RunnerCommand,
) -> RunnerCommandOwnership:
    if (
        command.ownership_state is not RunnerCommandOwnershipState.VERIFIED
        or command.ownership is None
    ):
        raise ApplicationConflictError(
            "runner_command_ownership_invalid",
            "Runner command does not have a verified immutable ownership envelope",
        )
    return command.ownership


def _expected_stop_ack_schema(binding: RunnerEffectBinding) -> str:
    if binding.resource_kind is RunnerResourceKind.EXECUTION:
        return _STOP_ACK_EXECUTION
    if binding.resource_kind is RunnerResourceKind.TERMINAL_SESSION:
        return _STOP_ACK_TERMINAL
    if binding.resource_kind is RunnerResourceKind.TARGET_HTTP_INTENT:
        return _STOP_ACK_TARGET_HTTP
    if binding.resource_kind is RunnerResourceKind.BROWSER_SESSION:
        return _STOP_ACK_BROWSER
    raise ApplicationConflictError(
        "runner_stop_ack_resource_unknown",
        "Runner stop acknowledgement resource kind is not registered",
    )


def _execution_stop_ack_invalid_fields(
    command: RunnerCommand,
    result: dict[str, object],
    *,
    execution: Execution,
) -> list[str]:
    target = command.target
    expected_owner = target.model_dump(mode="json") if target is not None else None
    invalid_fields: list[str] = []
    if result.get("execution_id") != execution.id:
        invalid_fields.append("execution_id")
    if result.get("local_execution_id") != execution.id:
        invalid_fields.append("local_execution_id")
    if result.get("execution_key") != execution.execution_key:
        invalid_fields.append("execution_key")
    if result.get("owner") != expected_owner:
        invalid_fields.append("owner")
    if result.get("status") != ExecutionStatus.CANCELLED.value:
        invalid_fields.append("status")
    if result.get("physical_stop_confirmed") is not True:
        invalid_fields.append("physical_stop_confirmed")
    return invalid_fields


def _legacy_stop_ack_invalid_fields(
    command: RunnerCommand,
    result: dict[str, object],
) -> list[str]:
    """Validate proof shape without deriving any authoritative resource owner."""

    if command.kind in {RunnerCommandKind.CANCEL, RunnerCommandKind.TERMINAL_CLOSE}:
        execution_id = result.get("execution_id")
        local_execution_id = result.get("local_execution_id")
        invalid_fields: list[str] = []
        if not isinstance(execution_id, str) or not execution_id:
            invalid_fields.append("execution_id")
        if local_execution_id != execution_id or not isinstance(local_execution_id, str):
            invalid_fields.append("local_execution_id")
        execution_key = result.get("execution_key")
        if not isinstance(execution_key, str) or not execution_key:
            invalid_fields.append("execution_key")
        expected_owner = (
            command.target.model_dump(mode="json") if command.target is not None else None
        )
        if result.get("owner") != expected_owner:
            invalid_fields.append("owner")
        if result.get("status") != ExecutionStatus.CANCELLED.value:
            invalid_fields.append("status")
        if result.get("physical_stop_confirmed") is not True:
            invalid_fields.append("physical_stop_confirmed")
        if command.kind is RunnerCommandKind.TERMINAL_CLOSE:
            session_id = result.get("session_id")
            if not isinstance(session_id, str) or not session_id:
                invalid_fields.append("session_id")
        return invalid_fields
    if command.kind is RunnerCommandKind.TARGET_HTTP_CANCEL:
        raw_outcomes = result.get("outcomes")
        if not isinstance(raw_outcomes, list) or len(raw_outcomes) != 1:
            return ["outcomes"]
        outcome = raw_outcomes[0]
        if not isinstance(outcome, dict):
            return ["outcomes[0]"]
        invalid_fields = []
        tool_call_id = outcome.get("tool_call_id")
        if not isinstance(tool_call_id, str) or not tool_call_id:
            invalid_fields.append("outcomes[0].tool_call_id")
        if outcome.get("confirmed") is not True:
            invalid_fields.append("outcomes[0].confirmed")
        return invalid_fields
    if command.kind is RunnerCommandKind.BROWSER_CLOSE:
        raw_result = result.get("result")
        raw_session = raw_result.get("session") if isinstance(raw_result, dict) else None
        if not isinstance(raw_session, dict):
            return ["result.session"]
        invalid_fields = []
        for field_name in ("id", "run_id"):
            value = raw_session.get(field_name)
            if not isinstance(value, str) or not value:
                invalid_fields.append(f"result.session.{field_name}")
        if raw_session.get("node_id") != command.node_id:
            invalid_fields.append("result.session.node_id")
        if raw_session.get("status") != "closed":
            invalid_fields.append("result.session.status")
        return invalid_fields
    return ["operation"]


def _target_http_stop_ack_invalid_fields(
    binding: RunnerEffectBinding,
    result: dict[str, object],
) -> list[str]:
    raw_outcomes = result.get("outcomes")
    if not isinstance(raw_outcomes, list) or len(raw_outcomes) != 1:
        return ["outcomes"]
    outcome = raw_outcomes[0]
    if not isinstance(outcome, dict):
        return ["outcomes[0]"]
    invalid_fields: list[str] = []
    if outcome.get("tool_call_id") != binding.resource_id:
        invalid_fields.append("outcomes[0].tool_call_id")
    if outcome.get("confirmed") is not True:
        invalid_fields.append("outcomes[0].confirmed")
    return invalid_fields


def _browser_stop_ack_invalid_fields(
    binding: RunnerEffectBinding,
    result: dict[str, object],
) -> list[str]:
    raw_result = result.get("result")
    raw_session = raw_result.get("session") if isinstance(raw_result, dict) else None
    if not isinstance(raw_session, dict):
        return ["result.session"]
    invalid_fields: list[str] = []
    if raw_session.get("id") != binding.resource_id:
        invalid_fields.append("result.session.id")
    if raw_session.get("run_id") != binding.run_id:
        invalid_fields.append("result.session.run_id")
    if raw_session.get("node_id") != binding.node_id:
        invalid_fields.append("result.session.node_id")
    if raw_session.get("status") != "closed":
        invalid_fields.append("result.session.status")
    return invalid_fields


def _raise_stop_ack_invalid(
    command: RunnerCommand,
    invalid_fields: list[str],
) -> None:
    raise ApplicationConflictError(
        "runner_stop_ack_invalid",
        "Runner stop acknowledgement did not prove the owning resource stopped",
        details={
            "command_id": command.id,
            "invalid_fields": invalid_fields,
        },
    )


def _command_quarantine_reason(exc: Exception) -> str:
    code = getattr(exc, "code", "runner_command_ownership_invalid")
    normalized = str(code).strip().lower().replace(" ", "_")
    return normalized[:255] or "runner_command_ownership_invalid"


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


def _is_verified_cancel_status_report(
    ownership: RunnerCommandOwnership,
    report: ExecutionStatusReport,
) -> bool:
    """Route only a verified launch callback's CANCELLED proof as safety state.

    ``physical_stop_confirmed`` is Runner-supplied and therefore cannot select
    a privileged repository on its own.  The callback has already been bound
    to the immutable launch command; require that registered operation/status
    contract as well.  Natural COMPLETED/EXITED reports intentionally remain
    on the ordinary completion-emitting repository.
    """

    if report.status is not ExecutionStatus.CANCELLED:
        return False
    binding = ownership.effect_binding
    contract = (
        ownership.operation,
        ownership.operation_family,
        binding.operation_family,
        binding.resource_kind,
    )
    if contract not in {
        (
            RunnerCommandKind.EXECUTE,
            RunnerOperationFamily.EXECUTION,
            RunnerOperationFamily.EXECUTION,
            RunnerResourceKind.EXECUTION,
        ),
        (
            RunnerCommandKind.TERMINAL_START,
            RunnerOperationFamily.TERMINAL,
            RunnerOperationFamily.TERMINAL,
            RunnerResourceKind.TERMINAL_SESSION,
        ),
    } or not _is_physical_stop_report(report):
        raise ApplicationConflictError(
            "runner_execution_cancel_contract_mismatch",
            "Runner cancellation status does not match its verified launch contract",
        )
    return True


def _execution_matches_stop_binding(
    execution: Execution,
    *,
    binding: RunnerEffectBinding,
    receipt: RunnerStopReceipt,
) -> bool:
    """Compare every immutable Execution owner fact before safety projection."""

    return (
        receipt.execution_id is not None
        and binding.execution_id == receipt.execution_id
        and execution.id == receipt.execution_id
        and execution.run_id == binding.run_id
        and execution.node_id == binding.node_id
        and execution.owner == binding.target
        and execution.audit_id == binding.audit_id
        and execution.plan_digest == binding.plan_digest
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
