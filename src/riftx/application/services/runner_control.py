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
    RunnerCommandRepository,
    RunnerCredentialRepository,
)
from riftx.domain import (
    Execution,
    ExecutionStatus,
    Node,
    NodeStatus,
    RunnerCommand,
    RunnerCommandKind,
    RunnerCommandStatus,
    RunnerCredential,
)
from riftx.domain.base import new_id, utc_now
from riftx.runner.paths import RunnerPaths

from .nodes import NodeApplicationService, NodeHeartbeat, NodeRegistration

_MAX_RESULT_BYTES = 64 * 1024
_MAX_OUTPUT_CHUNK_BYTES = 256 * 1024


@dataclass(frozen=True, slots=True)
class RunnerRegistrationResult:
    node: Node
    created: bool
    token: str


@dataclass(frozen=True, slots=True)
class ExecutionStatusReport:
    status: ExecutionStatus
    pid: int | None = None
    process_group_id: int | None = None
    exit_code: int | None = None


class RunnerControlService:
    """Issues scoped credentials and persists reconnect-safe Runner commands."""

    def __init__(
        self,
        *,
        credentials: RunnerCredentialRepository,
        commands: RunnerCommandRepository,
        nodes: NodeApplicationService,
        executions: ExecutionRepository,
        paths: RunnerPaths,
        registration_token: str | None,
        lease_duration: timedelta = timedelta(seconds=30),
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        if lease_duration <= timedelta(0):
            raise ValueError("lease_duration must be positive")
        self._credentials = credentials
        self._commands = commands
        self._nodes = nodes
        self._executions = executions
        self._paths = paths
        self._registration_token = registration_token
        self._lease_duration = lease_duration
        self._clock = clock
        self._output_locks: dict[tuple[str, str], asyncio.Lock] = {}

    async def register(
        self,
        registration: NodeRegistration,
        *,
        bootstrap_token: str,
    ) -> RunnerRegistrationResult:
        self._authenticate_bootstrap(bootstrap_token)
        node, created = await self._nodes.register(registration)
        token = secrets.token_urlsafe(32)
        now = self._clock()
        existing = await self._credentials.get(node.id)
        credential = RunnerCredential(
            node_id=node.id,
            token_hash=_token_hash(token),
            token_prefix=token[:8],
            created_at=existing.created_at if existing else now,
            rotated_at=now,
        )
        await self._credentials.save(credential)
        return RunnerRegistrationResult(node=node, created=created, token=token)

    async def authenticate(self, node_id: str, token: str) -> RunnerCredential:
        credential = await self._credentials.get(node_id)
        if (
            credential is None
            or credential.revoked_at is not None
            or not secrets.compare_digest(credential.token_hash, _token_hash(token))
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
        await self.authenticate(node_id, token)
        return await self._nodes.heartbeat(node_id, heartbeat)

    async def enqueue(
        self,
        node_id: str,
        *,
        kind: RunnerCommandKind,
        idempotency_key: str,
        payload: dict[str, object],
    ) -> tuple[RunnerCommand, bool]:
        node = await self._nodes.get(node_id)
        if node.status not in {NodeStatus.ONLINE, NodeStatus.DEGRADED}:
            raise ServiceUnavailableError(
                "runner_unavailable",
                f"Runner node {node_id!r} is not connected",
                details={"node_id": node_id, "status": node.status.value},
            )
        now = self._clock()
        return await self._commands.enqueue(
            RunnerCommand(
                node_id=node_id,
                kind=kind,
                idempotency_key=idempotency_key,
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
    ) -> RunnerCommand | None:
        await self.authenticate(node_id, token)
        await self._nodes.heartbeat(node_id, NodeHeartbeat())
        deadline = asyncio.get_running_loop().time() + min(max(wait_seconds, 0), 30)
        while True:
            now = self._clock()
            command = await self._commands.lease_next(
                node_id,
                lease_id=new_id(),
                leased_until=now + self._lease_duration,
                now=now,
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
        await self.authenticate(node_id, token)
        command = await self._require_command(command_id)
        if command.node_id != node_id:
            raise AuthenticationError(
                "runner_command_scope_mismatch",
                "Runner cannot complete a command assigned to another node",
            )
        bounded_result = result or {}
        if len(json.dumps(bounded_result, ensure_ascii=False).encode()) > _MAX_RESULT_BYTES:
            raise ApplicationConflictError(
                "runner_result_too_large",
                f"Runner command results must not exceed {_MAX_RESULT_BYTES} bytes",
            )
        return await self._commands.finish(
            command_id,
            lease_id=lease_id,
            status=(RunnerCommandStatus.COMPLETED if succeeded else RunnerCommandStatus.FAILED),
            result=bounded_result,
            error=error[:8192],
            completed_at=self._clock(),
        )

    async def report_execution(
        self,
        node_id: str,
        token: str,
        execution_id: str,
        report: ExecutionStatusReport,
    ) -> Execution:
        await self.authenticate(node_id, token)
        execution = await self._require_execution(execution_id)
        self._require_execution_scope(execution, node_id)
        if execution.status is report.status:
            return execution
        if report.status not in {
            ExecutionStatus.RUNNING,
            ExecutionStatus.EXITED,
            ExecutionStatus.FAILED,
            ExecutionStatus.CANCELLED,
            ExecutionStatus.LOST,
        }:
            raise ApplicationConflictError(
                "invalid_runner_execution_status",
                f"Runner cannot report execution status {report.status.value!r}",
            )
        if not execution.can_transition_to(report.status):
            raise ApplicationConflictError(
                "invalid_runner_execution_transition",
                f"Execution cannot transition from {execution.status.value} "
                f"to {report.status.value}",
            )
        if report.status is ExecutionStatus.RUNNING:
            execution.pid = report.pid
            execution.process_group_id = report.process_group_id
        execution.transition_to(report.status, exit_code=report.exit_code)
        return await self._executions.save(execution)

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
        await self.authenticate(node_id, token)
        execution = await self._require_execution(execution_id)
        self._require_execution_scope(execution, node_id)
        if stream not in {"stdout", "stderr"}:
            raise ValueError("stream must be stdout or stderr")
        if len(data) > _MAX_OUTPUT_CHUNK_BYTES:
            raise ApplicationConflictError(
                "runner_output_chunk_too_large",
                f"Runner output chunks must not exceed {_MAX_OUTPUT_CHUNK_BYTES} bytes",
            )
        output_paths = self._paths.execution(execution.run_id, execution.id)
        path = output_paths.stdout if stream == "stdout" else output_paths.stderr
        key = (execution_id, stream)
        lock = self._output_locks.setdefault(key, asyncio.Lock())
        async with lock:
            next_offset = await asyncio.to_thread(_append_exact, path, offset, data)
        return next_offset

    def _authenticate_bootstrap(self, token: str) -> None:
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

    @staticmethod
    def _require_execution_scope(execution: Execution, node_id: str) -> None:
        if execution.node_id != node_id:
            raise AuthenticationError(
                "runner_execution_scope_mismatch",
                "Runner cannot update an execution assigned to another node",
            )


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


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
