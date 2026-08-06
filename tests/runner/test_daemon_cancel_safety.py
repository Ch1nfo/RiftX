from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import riftx.runner.daemon as daemon_module
from riftx.application.services.run_safety import RunSafetyStopService
from riftx.browser import (
    BrowserOperation,
    BrowserRuntimeExchange,
    BrowserRuntimeResult,
    BrowserSessionCommand,
)
from riftx.domain import (
    RUNNER_STOP_ACK_BROWSER_SCHEMA,
    RUNNER_STOP_ACK_EXECUTION_SCHEMA,
    RUNNER_STOP_ACK_TARGET_HTTP_SCHEMA,
    RUNNER_STOP_ACK_TERMINAL_SCHEMA,
    BrowserMode,
    BrowserSession,
    BrowserSessionStatus,
    Execution,
    ExecutionStatus,
    ExecutorType,
    RunKind,
    RunnerCommandKind,
    RunnerCommandOrigin,
    RunnerCommandOwnership,
    RunnerEffectBinding,
    RunnerOperationFamily,
    RunnerOutputContract,
    RunnerPrincipal,
    RunnerResourceKind,
    Scope,
    runner_payload_digest,
)
from riftx.runner.control_client import (
    LeasedRunnerCommand,
    OutputLimitExceeded,
    OutputOffsetMismatch,
    RunnerControlClientError,
)
from riftx.runner.daemon import RunnerDaemon, RunnerDaemonConfig
from riftx.runner.models import ExecutionLaunchRequest, ExecutionOutput, OutputSlice
from riftx.runner.paths import RunnerPaths
from riftx.runner.state import FileExecutionRepository
from riftx.runner.supervisor import ProcessSupervisor
from riftx.runner.terminal_manager import (
    OperationJournal,
    OperationJournalConflict,
    OperationJournalIdentity,
    OperationJournalRecord,
    ResourceTombstone,
)
from riftx.target_http.models import (
    TargetHttpExchange,
    TargetHttpRequest,
    TargetHttpRunnerRequest,
    TargetHttpRunnerStopOutcome,
)

_OWNER = RunnerPrincipal(instance_id="runner-instance-a", epoch=1)
_EXECUTION_CALLBACK_FIELDS = (
    "runner_command_id",
    "runner_effect_binding_id",
    "runner_binding_digest",
    "runner_envelope_digest",
)


def _bind_execution_from_launch_request(
    execution: Execution,
    request: object,
) -> None:
    assert isinstance(request, ExecutionLaunchRequest)
    for field_name in _EXECUTION_CALLBACK_FIELDS:
        value = getattr(request, field_name)
        assert isinstance(value, str) and value
        # These fakes stand in for a supervisor constructing a new Execution
        # from the already-bound launch request. Update the prepared fixture in
        # place so concurrent test tasks retain the same object reference.
        object.__setattr__(execution, field_name, value)


def _bind_execution_from_terminal_payload(
    execution: Execution,
    payload: dict[str, object],
) -> None:
    raw_request = payload.get("request")
    assert isinstance(raw_request, dict)
    for field_name in _EXECUTION_CALLBACK_FIELDS:
        value = raw_request.get(field_name)
        assert isinstance(value, str) and value
        object.__setattr__(execution, field_name, value)


class _FailingCancellationJournal:
    async def get_exact(
        self,
        operation_id: str,
        identity: OperationJournalIdentity,
    ) -> OperationJournalRecord | None:
        del operation_id, identity
        return None

    async def add(
        self,
        operation_id: str,
        identity: OperationJournalIdentity,
        *,
        outcome: dict[str, object],
    ) -> None:
        del identity, outcome
        raise OSError(f"cannot persist {operation_id}")

    async def contains(self, operation_id: str) -> bool:
        return False

    async def get_resource(self, resource_key: str) -> ResourceTombstone | None:
        del resource_key
        return None

    async def get_legacy_resource(self, resource_key: str) -> ResourceTombstone | None:
        del resource_key
        return None

    async def get_resource_attempt_exact(
        self,
        resource_key: str,
        identity: OperationJournalIdentity,
    ) -> OperationJournalRecord | None:
        del resource_key, identity
        return None

    async def claim_resource(
        self,
        resource_key: str,
        identity: OperationJournalIdentity,
        *,
        outcome: dict[str, object],
    ) -> tuple[bool, ResourceTombstone]:
        del identity, outcome
        raise OSError(f"cannot persist {resource_key}")


class _FailingConfirmationJournal:
    async def get_exact(
        self,
        operation_id: str,
        identity: OperationJournalIdentity,
    ) -> OperationJournalRecord | None:
        del identity
        raise OSError(f"cannot read confirmation {operation_id}")

    async def add(
        self,
        operation_id: str,
        identity: OperationJournalIdentity,
        *,
        outcome: dict[str, object],
    ) -> None:
        del identity, outcome
        raise OSError(f"cannot persist confirmation {operation_id}")

    async def contains(self, operation_id: str) -> bool:
        raise OSError(f"cannot read confirmation {operation_id}")

    async def get_resource(self, resource_key: str) -> ResourceTombstone | None:
        raise OSError(f"cannot read confirmation {resource_key}")

    async def claim_resource(
        self,
        resource_key: str,
        identity: OperationJournalIdentity,
        *,
        outcome: dict[str, object],
    ) -> tuple[bool, ResourceTombstone]:
        del identity, outcome
        raise OSError(f"cannot persist confirmation {resource_key}")


class _CancellationJournal:
    def __init__(self, legacy_operations: set[str] | None = None) -> None:
        self.operations: set[str] = set()
        self.legacy_operations = legacy_operations or set()
        self.records: dict[
            str,
            tuple[OperationJournalIdentity, dict[str, object]],
        ] = {}
        self.resource_outcomes: dict[str, dict[str, object]] = {}
        self.legacy_resource_outcomes: dict[str, dict[str, object]] = {
            operation: {"state": "legacy_unbound"}
            for operation in self.legacy_operations
        }

    async def get_exact(
        self,
        operation_id: str,
        identity: OperationJournalIdentity,
    ) -> OperationJournalRecord | None:
        stored = self.records.get(operation_id)
        if stored is None:
            if operation_id in self.operations:
                raise OperationJournalConflict(operation_id)
            return None
        stored_identity, outcome = stored
        if stored_identity != identity:
            raise OperationJournalConflict(operation_id)
        return OperationJournalRecord(
            operation_key=operation_id,
            command_id=identity.command_id,
            binding_digest=identity.binding_digest,
            envelope_digest=identity.envelope_digest,
            outcome=dict(outcome),
        )

    async def add(
        self,
        operation_id: str,
        identity: OperationJournalIdentity,
        *,
        outcome: dict[str, object],
    ) -> None:
        existing = await self.get_exact(operation_id, identity)
        if existing is not None:
            if existing.outcome != outcome:
                raise OperationJournalConflict(operation_id)
            return
        self.operations.add(operation_id)
        self.records[operation_id] = (identity, dict(outcome))

    async def transition(
        self,
        operation_id: str,
        identity: OperationJournalIdentity,
        *,
        expected_outcome: dict[str, object],
        outcome: dict[str, object],
    ) -> OperationJournalRecord:
        existing = await self.get_exact(operation_id, identity)
        if existing is None or (
            existing.outcome != expected_outcome and existing.outcome != outcome
        ):
            raise OperationJournalConflict(operation_id)
        self.records[operation_id] = (identity, dict(outcome))
        return OperationJournalRecord(
            operation_key=operation_id,
            command_id=identity.command_id,
            binding_digest=identity.binding_digest,
            envelope_digest=identity.envelope_digest,
            outcome=dict(outcome),
        )

    async def contains(self, operation_id: str) -> bool:
        return operation_id in self.operations

    async def get_resource(
        self,
        resource_key: str,
    ) -> ResourceTombstone | None:
        outcome = self.resource_outcomes.get(resource_key)
        if outcome is None:
            return None
        return ResourceTombstone(resource_key=resource_key, outcome=dict(outcome))

    async def get_legacy_resource(
        self,
        resource_key: str,
    ) -> ResourceTombstone | None:
        outcome = self.legacy_resource_outcomes.get(resource_key)
        if outcome is None:
            return None
        return ResourceTombstone(resource_key=resource_key, outcome=dict(outcome))

    async def get_resource_attempt_exact(
        self,
        resource_key: str,
        identity: OperationJournalIdentity,
    ) -> OperationJournalRecord | None:
        return await self.get_exact(
            f"{resource_key}:command:{identity.command_id}",
            identity,
        )

    async def claim_resource(
        self,
        resource_key: str,
        identity: OperationJournalIdentity,
        *,
        outcome: dict[str, object],
    ) -> tuple[bool, ResourceTombstone]:
        attempt_key = f"{resource_key}:command:{identity.command_id}"
        existing = await self.get_exact(attempt_key, identity)
        claimed = existing is None
        if existing is None:
            self.records[attempt_key] = (identity, dict(outcome))
        elif existing.outcome != outcome:
            raise OperationJournalConflict(resource_key)
        self.operations.add(resource_key)
        self.resource_outcomes.setdefault(
            resource_key,
            {"state": "cancellation_requested"},
        )
        tombstone = await self.get_resource(resource_key)
        assert tombstone is not None
        return claimed, tombstone

    async def transition_resource(
        self,
        resource_key: str,
        identity: OperationJournalIdentity,
        *,
        expected_outcome: dict[str, object],
        outcome: dict[str, object],
        resource_outcome: dict[str, object],
    ) -> OperationJournalRecord:
        attempt_key = f"{resource_key}:command:{identity.command_id}"
        existing = await self.get_exact(attempt_key, identity)
        if existing is None or (
            existing.outcome != expected_outcome and existing.outcome != outcome
        ):
            raise OperationJournalConflict(resource_key)
        self.records[attempt_key] = (identity, dict(outcome))
        if self.resource_outcomes.get(resource_key, {}).get("state") != (
            "physical_stop_confirmed"
        ):
            self.resource_outcomes[resource_key] = dict(resource_outcome)
        record = await self.get_exact(attempt_key, identity)
        assert record is not None
        return record


class _SignallingCancellationJournal(_CancellationJournal):
    def __init__(self) -> None:
        super().__init__()
        self.added = asyncio.Event()

    async def add(
        self,
        operation_id: str,
        identity: OperationJournalIdentity,
        *,
        outcome: dict[str, object],
    ) -> None:
        await super().add(operation_id, identity, outcome=outcome)
        self.added.set()

    async def claim_resource(
        self,
        resource_key: str,
        identity: OperationJournalIdentity,
        *,
        outcome: dict[str, object],
    ) -> tuple[bool, ResourceTombstone]:
        result = await super().claim_resource(
            resource_key,
            identity,
            outcome=outcome,
        )
        self.added.set()
        return result


class _PreSpawnGuardJournal(_SignallingCancellationJournal):
    """Pause the supervisor's guard after the daemon's initial tombstone read."""

    def __init__(self) -> None:
        super().__init__()
        self.resource_reads = 0
        self.guard_entered = asyncio.Event()
        self.release_guard = asyncio.Event()

    async def get_resource(self, resource_key: str) -> ResourceTombstone | None:
        self.resource_reads += 1
        if self.resource_reads == 2:
            self.guard_entered.set()
            await self.release_guard.wait()
        return await super().get_resource(resource_key)


class _ExecutionRepository:
    def __init__(self, execution: Execution | None) -> None:
        self.execution = execution

    async def get_by_key(self, execution_key: str) -> Execution | None:
        if self.execution is None or self.execution.execution_key != execution_key:
            return None
        return self.execution

    async def get(self, execution_id: str) -> Execution | None:
        if self.execution is None or self.execution.id != execution_id:
            return None
        return self.execution

    async def save(self, execution: Execution) -> Execution:
        self.execution = execution
        return execution

    async def list_active(self) -> list[Execution]:
        return []


class _SequencedExecutionRepository(_ExecutionRepository):
    def __init__(self, first: Execution, second: Execution) -> None:
        super().__init__(first)
        self._first = first
        self._second = second
        self.get_by_key_calls = 0

    async def get_by_key(self, execution_key: str) -> Execution | None:
        self.get_by_key_calls += 1
        execution = self._first if self.get_by_key_calls == 1 else self._second
        if execution.execution_key != execution_key:
            return None
        return execution


class _ActiveExecutionRepository(_ExecutionRepository):
    async def list_active(self) -> list[Execution]:
        if self.execution is None:
            return []
        return [self.execution]


class _MultipleActiveExecutionRepository:
    def __init__(self, executions: list[Execution]) -> None:
        self.executions = executions

    async def get_by_key(self, execution_key: str) -> Execution | None:
        return next(
            (item for item in self.executions if item.execution_key == execution_key),
            None,
        )

    async def get(self, execution_id: str) -> Execution | None:
        return next((item for item in self.executions if item.id == execution_id), None)

    async def save(self, execution: Execution) -> Execution:
        for index, existing in enumerate(self.executions):
            if existing.id == execution.id:
                self.executions[index] = execution
                return execution
        self.executions.append(execution)
        return execution

    async def list_active(self) -> list[Execution]:
        return list(self.executions)


class _Supervisor:
    def __init__(self, execution: Execution) -> None:
        self.execution = execution
        self.cancel_calls: list[str] = []
        self.cancelled = asyncio.Event()

    async def cancel(self, execution_id: str) -> Execution:
        self.cancel_calls.append(execution_id)
        if self.execution.status in {
            ExecutionStatus.STARTING,
            ExecutionStatus.RUNNING,
        }:
            self.execution.transition_to(ExecutionStatus.CANCELLED)
        if self.execution.status is ExecutionStatus.CANCELLED:
            self.execution.physical_stop_confirmed_at = datetime.now(UTC)
        self.cancelled.set()
        return self.execution

    async def close(self, *, cancel_running: bool = False) -> None:
        return None


class _OutputSupervisor(_Supervisor):
    def __init__(
        self,
        execution: Execution,
        *,
        stdout: bytes,
        stderr: bytes,
    ) -> None:
        super().__init__(execution)
        self.stdout = stdout
        self.stderr = stderr

    async def reconcile(self, execution_id: str) -> Execution:
        assert execution_id == self.execution.id
        return self.execution

    async def read_output(
        self,
        execution_id: str,
        *,
        stdout_cursor: int,
        stderr_cursor: int,
        max_bytes: int,
    ) -> ExecutionOutput:
        assert execution_id == self.execution.id

        def output_slice(content: bytes, cursor: int) -> OutputSlice:
            data = content[cursor : cursor + max_bytes]
            next_cursor = cursor + len(data)
            return OutputSlice(
                data=data,
                cursor=cursor,
                next_cursor=next_cursor,
                eof=next_cursor >= len(content),
            )

        return ExecutionOutput(
            stdout=output_slice(self.stdout, stdout_cursor),
            stderr=output_slice(self.stderr, stderr_cursor),
        )


class _FailingSupervisor(_Supervisor):
    async def cancel(self, execution_id: str) -> Execution:
        self.cancel_calls.append(execution_id)
        raise RuntimeError("process termination failed")


class _BarrierStartSupervisor(_Supervisor):
    def __init__(self, execution: Execution, repository: _ExecutionRepository) -> None:
        super().__init__(execution)
        self.repository = repository
        self.start_entered = asyncio.Event()
        self.release_start = asyncio.Event()
        self.start_cancelled = asyncio.Event()
        self.spawned = False

    async def start(self, request: object, *, effect_guard=None) -> Execution:
        _bind_execution_from_launch_request(self.execution, request)
        self.repository.execution = self.execution
        if effect_guard is not None:
            await effect_guard()
        self.start_entered.set()
        try:
            await self.release_start.wait()
        except asyncio.CancelledError:
            self.start_cancelled.set()
            raise
        self.spawned = True
        if effect_guard is not None:
            await effect_guard()
        return self.execution


class _PreSpawnGuardSupervisor(_Supervisor):
    def __init__(self, execution: Execution, repository: _ExecutionRepository) -> None:
        super().__init__(execution)
        self.repository = repository
        self.spawned = False

    async def start(self, request: object, *, effect_guard=None) -> Execution:
        _bind_execution_from_launch_request(self.execution, request)
        self.repository.execution = self.execution
        if effect_guard is not None:
            await effect_guard()
        self.spawned = True
        return self.execution


class _ImmediateStartBlockingCancelSupervisor(_Supervisor):
    def __init__(self, execution: Execution, repository: _ExecutionRepository) -> None:
        super().__init__(execution)
        self.repository = repository
        self.cancel_entered = asyncio.Event()
        self.release_cancel = asyncio.Event()

    async def start(self, request: object, *, effect_guard=None) -> Execution:
        _bind_execution_from_launch_request(self.execution, request)
        self.repository.execution = self.execution
        if effect_guard is not None:
            await effect_guard()
            await effect_guard()
        return self.execution

    async def cancel(self, execution_id: str) -> Execution:
        self.cancel_calls.append(execution_id)
        self.cancel_entered.set()
        await self.release_cancel.wait()
        if self.execution.status in {
            ExecutionStatus.STARTING,
            ExecutionStatus.RUNNING,
        }:
            self.execution.transition_to(ExecutionStatus.CANCELLED)
        if self.execution.status is ExecutionStatus.CANCELLED:
            self.execution.physical_stop_confirmed_at = datetime.now(UTC)
        self.cancelled.set()
        return self.execution


class _RunnerClient:
    def __init__(self) -> None:
        self.finished: list[tuple[str, bool, dict[str, object], str]] = []
        self.statuses: list[tuple[str, ExecutionStatus]] = []
        self.status_details: list[dict[str, object]] = []

    @property
    def principal(self) -> RunnerPrincipal:
        return _OWNER

    async def finish(
        self,
        command: LeasedRunnerCommand,
        *,
        succeeded: bool,
        result: dict[str, object] | None = None,
        error: str = "",
    ) -> None:
        self.finished.append((command.id, succeeded, result or {}, error))

    async def report_status(
        self,
        execution_id: str,
        status: ExecutionStatus,
        **details: object,
    ) -> None:
        self.statuses.append((execution_id, status))
        self.status_details.append(details)

    async def close(self) -> None:
        return None


class _CappedOutputRunnerClient(_RunnerClient):
    def __init__(self, *, max_output_bytes: int) -> None:
        super().__init__()
        self.max_output_bytes = max_output_bytes
        self.output = {"stdout": bytearray(b"ab"), "stderr": bytearray()}
        self.output_attempts: list[tuple[str, int, bytes]] = []

    async def report_output(
        self,
        execution_id: str,
        *,
        stream: str,
        offset: int,
        data: bytes,
        **callback: object,
    ) -> int:
        del execution_id, callback
        self.output_attempts.append((stream, offset, data))
        if offset + len(data) > self.max_output_bytes:
            raise OutputLimitExceeded(
                409,
                "runner_execution_output_too_large",
                "output exceeds immutable contract",
                details={"max_output_bytes": self.max_output_bytes},
            )
        expected_offset = len(self.output[stream])
        if offset != expected_offset:
            raise OutputOffsetMismatch(
                409,
                "runner_output_offset_mismatch",
                "output offset mismatch",
                details={"expected_offset": expected_offset},
            )
        self.output[stream].extend(data)
        return len(self.output[stream])


class _FailFirstStatusRunnerClient(_RunnerClient):
    def __init__(self) -> None:
        super().__init__()
        self.status_attempts: list[tuple[str, ExecutionStatus]] = []

    async def report_status(
        self,
        execution_id: str,
        status: ExecutionStatus,
        **details: object,
    ) -> None:
        self.status_attempts.append((execution_id, status))
        if len(self.status_attempts) == 1:
            raise RunnerControlClientError(
                503,
                "temporal_unavailable",
                "first terminal status upload failed",
            )
        await super().report_status(execution_id, status, **details)


class _PollingRunnerClient(_RunnerClient):
    def __init__(self, commands: list[LeasedRunnerCommand]) -> None:
        super().__init__()
        self.commands = commands
        self.poll_modes: list[bool] = []
        self.close_acknowledged = asyncio.Event()
        self.poll_released = asyncio.Event()

    async def connect(self, registration: object) -> str:
        return "connected"

    async def poll(
        self,
        *,
        wait_seconds: float,
        safety_only: bool = False,
    ) -> LeasedRunnerCommand | None:
        self.poll_modes.append(safety_only)
        if self.commands:
            return self.commands.pop(0)
        await self.poll_released.wait()
        return None

    async def finish(
        self,
        command: LeasedRunnerCommand,
        *,
        succeeded: bool,
        result: dict[str, object] | None = None,
        error: str = "",
    ) -> None:
        await super().finish(command, succeeded=succeeded, result=result, error=error)
        if command.kind is RunnerCommandKind.BROWSER_CLOSE:
            self.close_acknowledged.set()

    async def close(self) -> None:
        self.poll_released.set()


class _RenewingRunnerClient(_RunnerClient):
    def __init__(self) -> None:
        super().__init__()
        self.renewed = asyncio.Event()
        self.renew_calls = 0

    async def renew(self, command: LeasedRunnerCommand) -> datetime:
        self.renew_calls += 1
        self.renewed.set()
        return datetime.now(UTC) + timedelta(seconds=1)


class _BlockingRenewRunnerClient(_RunnerClient):
    def __init__(self) -> None:
        super().__init__()
        self.renew_entered = asyncio.Event()
        self.renew_cancelled = asyncio.Event()
        self.release_renew = asyncio.Event()

    async def renew(self, command: LeasedRunnerCommand) -> datetime:
        self.renew_entered.set()
        try:
            await self.release_renew.wait()
        except asyncio.CancelledError:
            self.renew_cancelled.set()
            raise
        raise AssertionError(f"blocked renewal unexpectedly released for {command.id}")


class _FailingRenewRunnerClient(_RunnerClient):
    def __init__(self) -> None:
        super().__init__()
        self.renew_calls = 0

    async def renew(self, command: LeasedRunnerCommand) -> datetime:
        self.renew_calls += 1
        raise RuntimeError(f"lease renewal failed for {command.id}")


class _RejectedRenewRunnerClient(_RunnerClient):
    def __init__(self, status_code: int) -> None:
        super().__init__()
        self.status_code = status_code
        self.renew_entered = asyncio.Event()
        self.rejected_at: float | None = None
        self.renew_calls = 0

    async def renew(self, command: LeasedRunnerCommand) -> datetime:
        self.renew_calls += 1
        self.rejected_at = asyncio.get_running_loop().time()
        self.renew_entered.set()
        raise RunnerControlClientError(
            self.status_code,
            "runner_command_lease_conflict",
            f"lease was reclaimed for {command.id}",
        )


class _LeaseFailBlockingStatusRunnerClient(_FailingRenewRunnerClient):
    def __init__(self) -> None:
        super().__init__()
        self.running_report_entered = asyncio.Event()
        self.release_running_report = asyncio.Event()
        self.cancelled_reported = asyncio.Event()

    async def report_status(
        self,
        execution_id: str,
        status: ExecutionStatus,
        **details: object,
    ) -> None:
        if status is ExecutionStatus.RUNNING:
            self.running_report_entered.set()
            await self.release_running_report.wait()
        await super().report_status(execution_id, status, **details)
        if status is ExecutionStatus.CANCELLED:
            self.cancelled_reported.set()


class _SlowPreemptionRunnerClient(_RunnerClient):
    def __init__(self, slow_command_id: str) -> None:
        super().__init__()
        self.slow_command_id = slow_command_id
        self.preemption_finish_entered = asyncio.Event()
        self.release_preemption_finish = asyncio.Event()

    async def finish(
        self,
        command: LeasedRunnerCommand,
        *,
        succeeded: bool,
        result: dict[str, object] | None = None,
        error: str = "",
    ) -> None:
        if command.id == self.slow_command_id:
            self.preemption_finish_entered.set()
            await self.release_preemption_finish.wait()
        await super().finish(command, succeeded=succeeded, result=result, error=error)


class _DelayedFinishRunnerClient(_RunnerClient):
    def __init__(self, delayed_command_id: str) -> None:
        super().__init__()
        self.delayed_command_id = delayed_command_id
        self.finish_entered = asyncio.Event()
        self.release_finish = asyncio.Event()

    async def finish(
        self,
        command: LeasedRunnerCommand,
        *,
        succeeded: bool,
        result: dict[str, object] | None = None,
        error: str = "",
    ) -> None:
        if command.id == self.delayed_command_id:
            self.finish_entered.set()
            await self.release_finish.wait()
        await super().finish(command, succeeded=succeeded, result=result, error=error)


class _TerminalHandler:
    def __init__(
        self,
        execution: Execution | None = None,
        journal: _CancellationJournal | None = None,
    ) -> None:
        self.execution = execution
        self.journal = journal
        self.calls: list[tuple[RunnerCommandKind, dict[str, object]]] = []
        self.cancel_calls: list[str] = []

    async def handle(
        self,
        kind: RunnerCommandKind,
        payload: dict[str, object],
        *,
        journal_identity=None,
        effect_guard=None,
        on_admitted=None,
    ) -> dict[str, object]:
        del journal_identity
        if effect_guard is not None:
            await effect_guard()
        self.calls.append((kind, payload))
        if on_admitted is not None:
            on_admitted()
        return {"status": "started"}

    async def cancel_execution(self, execution_id: str) -> Execution:
        self.cancel_calls.append(execution_id)
        if self.execution is None:
            raise RuntimeError("no terminal execution configured")
        if self.journal is not None:
            assert await self.journal.contains(
                f"execution:{self.execution.id}"
            ) or await self.journal.contains(self.execution.execution_key)
        if self.execution.status is ExecutionStatus.RUNNING:
            self.execution.transition_to(ExecutionStatus.CANCELLED)
        if self.execution.status is ExecutionStatus.CANCELLED:
            self.execution.physical_stop_confirmed_at = datetime.now(UTC)
        return self.execution


class _BlockingTerminalHandler(_TerminalHandler):
    def __init__(self) -> None:
        super().__init__()
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def handle(
        self,
        kind: RunnerCommandKind,
        payload: dict[str, object],
        *,
        journal_identity=None,
        effect_guard=None,
        on_admitted=None,
    ) -> dict[str, object]:
        del journal_identity
        if effect_guard is not None:
            await effect_guard()
        self.entered.set()
        if on_admitted is not None:
            on_admitted()
        await self.release.wait()
        return {"status": "written"}


class _CancellationAwareBlockingTerminalHandler(_BlockingTerminalHandler):
    def __init__(self) -> None:
        super().__init__()
        self.cancelled = asyncio.Event()
        self.completed = asyncio.Event()

    async def handle(
        self,
        kind: RunnerCommandKind,
        payload: dict[str, object],
        *,
        journal_identity=None,
        effect_guard=None,
        on_admitted=None,
    ) -> dict[str, object]:
        try:
            result = await super().handle(
                kind,
                payload,
                journal_identity=journal_identity,
                effect_guard=effect_guard,
                on_admitted=on_admitted,
            )
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        self.completed.set()
        return result


class _CancellationTransformingTerminalHandler(_BlockingTerminalHandler):
    def __init__(self, *, raise_after_cancel: bool) -> None:
        super().__init__()
        self.raise_after_cancel = raise_after_cancel
        self.transformed = asyncio.Event()

    async def handle(
        self,
        kind: RunnerCommandKind,
        payload: dict[str, object],
        *,
        journal_identity=None,
        effect_guard=None,
        on_admitted=None,
    ) -> dict[str, object]:
        try:
            return await super().handle(
                kind,
                payload,
                journal_identity=journal_identity,
                effect_guard=effect_guard,
                on_admitted=on_admitted,
            )
        except asyncio.CancelledError as exc:
            self.transformed.set()
            if self.raise_after_cancel:
                raise RuntimeError("terminal handler transformed cancellation") from exc
            return {"status": "terminal handler swallowed cancellation"}


class _FailingTerminalCancellationHandler(_TerminalHandler):
    async def cancel_execution(self, execution_id: str) -> Execution:
        self.cancel_calls.append(execution_id)
        raise RuntimeError("lost terminal handle")


class _BarrierTerminalStartHandler(_TerminalHandler):
    def __init__(self, execution: Execution, repository: _ExecutionRepository) -> None:
        super().__init__(execution)
        self.repository = repository
        self.start_entered = asyncio.Event()
        self.release_start = asyncio.Event()
        self.spawned = False

    async def handle(
        self,
        kind: RunnerCommandKind,
        payload: dict[str, object],
        *,
        journal_identity=None,
        effect_guard=None,
        on_admitted=None,
    ) -> dict[str, object]:
        del journal_identity
        if effect_guard is not None:
            await effect_guard()
        self.calls.append((kind, payload))
        self.start_entered.set()
        await self.release_start.wait()
        _bind_execution_from_terminal_payload(self.execution, payload)
        self.repository.execution = self.execution
        self.spawned = True
        if effect_guard is not None:
            await effect_guard()
        if on_admitted is not None:
            on_admitted()
        return {"status": "started"}


class _ReportingTerminalStartHandler(_TerminalHandler):
    def __init__(
        self,
        execution: Execution,
        repository: _ExecutionRepository,
        client: _RunnerClient,
    ) -> None:
        super().__init__(execution)
        self.repository = repository
        self.client = client

    async def handle(
        self,
        kind: RunnerCommandKind,
        payload: dict[str, object],
        *,
        journal_identity=None,
        effect_guard=None,
        on_admitted=None,
    ) -> dict[str, object]:
        del journal_identity
        if effect_guard is not None:
            await effect_guard()
        self.calls.append((kind, payload))
        _bind_execution_from_terminal_payload(self.execution, payload)
        self.repository.execution = self.execution
        if effect_guard is not None:
            await effect_guard()
        if on_admitted is not None:
            on_admitted()
        await self.client.report_status(
            str(payload["execution_id"]),
            self.execution.status,
        )
        return {"status": "started"}


class _PersistThenFailTerminalStartHandler(_TerminalHandler):
    def __init__(self, execution: Execution, repository: _ExecutionRepository) -> None:
        super().__init__(execution)
        self.repository = repository

    async def handle(
        self,
        kind: RunnerCommandKind,
        payload: dict[str, object],
        *,
        journal_identity=None,
        effect_guard=None,
        on_admitted=None,
    ) -> dict[str, object]:
        del journal_identity
        if effect_guard is not None:
            await effect_guard()
        self.calls.append((kind, payload))
        _bind_execution_from_terminal_payload(self.execution, payload)
        self.repository.execution = self.execution
        if on_admitted is not None:
            on_admitted()
        raise RuntimeError("terminal backend rejected process creation")


class _BlockingReportTerminalStartHandler(_TerminalHandler):
    def __init__(
        self,
        execution: Execution,
        repository: _ExecutionRepository,
        client: _LeaseFailBlockingStatusRunnerClient,
    ) -> None:
        super().__init__(execution)
        self.repository = repository
        self.client = client

    async def handle(
        self,
        kind: RunnerCommandKind,
        payload: dict[str, object],
        *,
        journal_identity=None,
        effect_guard=None,
        on_admitted=None,
    ) -> dict[str, object]:
        del journal_identity
        if effect_guard is not None:
            await effect_guard()
        _bind_execution_from_terminal_payload(self.execution, payload)
        self.repository.execution = self.execution
        if effect_guard is not None:
            await effect_guard()
        if on_admitted is not None:
            on_admitted()
        await self.client.report_status(self.execution.id, self.execution.status)
        return {"status": self.execution.status.value}


class _BlockingTargetHttpRunner:
    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.active: asyncio.Task[object] | None = None
        self.execute_calls = 0
        self.stop_calls: list[tuple[str, tuple[str, ...]]] = []

    async def execute(
        self,
        launch: TargetHttpRunnerRequest,
        *,
        effect_guard=None,
    ) -> TargetHttpExchange:
        self.execute_calls += 1
        current = asyncio.current_task()
        assert current is not None
        self.active = current
        self.entered.set()
        if effect_guard is not None:
            await effect_guard()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    async def stop_run(
        self,
        run_id: str,
        *,
        node_id: str,
        tool_call_ids: tuple[str, ...],
    ) -> list[TargetHttpRunnerStopOutcome]:
        self.stop_calls.append((run_id, tool_call_ids))
        task = self.active
        if task is None or task.done():
            return [
                TargetHttpRunnerStopOutcome(
                    tool_call_id=tool_call_id,
                    confirmed=False,
                    reason="target_http_local_task_not_registered",
                )
                for tool_call_id in tool_call_ids
            ]
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return [
            TargetHttpRunnerStopOutcome(
                tool_call_id=tool_call_id,
                confirmed=True,
                reason="target_http_local_task_terminated",
            )
            for tool_call_id in tool_call_ids
        ]


class _SentThenBlockingTargetHttpRunner:
    def __init__(self) -> None:
        self.sent = asyncio.Event()
        self.send_count = 0

    async def execute(
        self,
        launch: TargetHttpRunnerRequest,
        *,
        effect_guard=None,
    ) -> TargetHttpExchange:
        if effect_guard is not None:
            await effect_guard()
        self.send_count += 1
        self.sent.set()
        await asyncio.Event().wait()
        raise AssertionError(f"Target HTTP request unexpectedly completed: {launch.tool_call_id}")

    async def stop_run(
        self,
        run_id: str,
        *,
        node_id: str,
        tool_call_ids: tuple[str, ...],
    ) -> list[TargetHttpRunnerStopOutcome]:
        raise AssertionError(f"unexpected stop for {run_id} on {node_id}: {tool_call_ids}")


class _BlockingBrowserRunner:
    def __init__(self, session: BrowserSession) -> None:
        self.session = session.model_copy(deep=True)
        self.observe_entered = asyncio.Event()
        self.observe_calls = 0
        self.close_calls = 0

    async def open(self, command: object) -> BrowserRuntimeExchange:
        raise AssertionError("delayed browser open must be suppressed")

    async def observe(self, command: object) -> BrowserRuntimeExchange:
        self.observe_calls += 1
        self.observe_entered.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    async def act(self, command: object) -> BrowserRuntimeExchange:
        raise AssertionError("not used")

    async def takeover(self, command: object) -> BrowserRuntimeExchange:
        raise AssertionError("not used")

    async def release(self, command: object) -> BrowserRuntimeExchange:
        raise AssertionError("not used")

    async def close(self, command: BrowserSessionCommand) -> BrowserRuntimeExchange:
        self.close_calls += 1
        if self.session.status is not BrowserSessionStatus.CLOSED:
            self.session.transition_to(BrowserSessionStatus.CLOSED)
        return BrowserRuntimeExchange(
            result=BrowserRuntimeResult(session=self.session.model_copy(deep=True))
        )

    async def close_all(self) -> None:
        return None


class _LeaseBlockingBrowserRunner(_BlockingBrowserRunner):
    def __init__(self, session: BrowserSession) -> None:
        super().__init__(session)
        self.close_entered = asyncio.Event()
        self.release_close = asyncio.Event()

    async def close(self, command: BrowserSessionCommand) -> BrowserRuntimeExchange:
        self.close_entered.set()
        await self.release_close.wait()
        return await super().close(command)


class _MissingBrowserRunner:
    def __init__(self) -> None:
        self.open_calls = 0
        self.close_calls = 0

    async def open(self, command: object) -> BrowserRuntimeExchange:
        self.open_calls += 1
        raise AssertionError("tombstoned browser open must be suppressed")

    async def close(self, command: BrowserSessionCommand) -> BrowserRuntimeExchange:
        self.close_calls += 1
        raise KeyError(command.session_id)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("require_containment", "payload_uid", "payload_gid"),
    [(True, None, None), (False, 1001, 1002)],
)
async def test_run_runner_daemon_shares_configured_containment_manager(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    require_containment: bool,
    payload_uid: int | None,
    payload_gid: int | None,
) -> None:
    manager = object()
    captured: dict[str, object] = {}

    class _Executor:
        def __init__(self, **kwargs: object) -> None:
            captured["executor_kwargs"] = kwargs
            self.containment_manager = kwargs["containment_manager"]

    class _ContainmentManager:
        @classmethod
        def autodetect(cls, **kwargs: object) -> object:
            captured["autodetect_kwargs"] = kwargs
            return manager

    class _ProcessRunner:
        def __init__(self, *args: object, **kwargs: object) -> None:
            captured["process_executor"] = kwargs.get("process_executor")

    class _TerminalRunner:
        def __init__(self, **kwargs: object) -> None:
            captured["terminal_kwargs"] = kwargs

    class _Daemon:
        def __init__(self, **kwargs: object) -> None:
            captured["daemon_kwargs"] = kwargs

        async def run_forever(self) -> None:
            captured["ran"] = True

        async def close(self) -> None:
            captured["closed"] = True

    monkeypatch.setattr(daemon_module, "DirectProcessExecutor", _Executor)
    monkeypatch.setattr(daemon_module, "LinuxCgroupV2Manager", _ContainmentManager)
    monkeypatch.setattr(daemon_module, "ProcessSupervisor", _ProcessRunner)
    monkeypatch.setattr(daemon_module, "TerminalSupervisor", _TerminalRunner)
    monkeypatch.setattr(daemon_module, "RunnerDaemon", _Daemon)
    monkeypatch.setattr(daemon_module, "RunnerCredentialStore", lambda *_: object())
    monkeypatch.setattr(daemon_module, "RunnerControlClient", lambda **_: object())
    monkeypatch.setattr(daemon_module, "RemoteTerminalManager", lambda **_: object())
    monkeypatch.setattr(daemon_module, "RunnerBrowserManager", lambda **_: object())
    monkeypatch.setattr(daemon_module, "RunnerTargetHttpClient", lambda **_: object())

    config = RunnerDaemonConfig(
        server_url="http://control.invalid",
        node_id="runner-a",
        name="Runner A",
        state_path=tmp_path / "runner",
        require_containment=require_containment,
        payload_uid=payload_uid,
        payload_gid=payload_gid,
    )
    await daemon_module.run_runner_daemon(config)

    executor = captured["process_executor"]
    terminal_kwargs = captured["terminal_kwargs"]
    assert isinstance(terminal_kwargs, dict)
    assert captured["executor_kwargs"] == {
        "containment_manager": manager,
        "autodetect_containment": False,
        "require_containment": require_containment,
        "defer_activation": True,
    }
    assert captured["autodetect_kwargs"] == {
        "payload_uid": payload_uid,
        "payload_gid": payload_gid,
    }
    assert executor.containment_manager is manager
    assert terminal_kwargs["containment_manager"] is manager
    assert terminal_kwargs["autodetect_containment"] is False
    assert terminal_kwargs["require_containment"] is require_containment
    assert captured["ran"] is True
    assert captured["closed"] is True


@pytest.mark.parametrize(
    ("payload_uid", "payload_gid", "message"),
    [
        (1001, None, "configured together"),
        (None, 1002, "configured together"),
        (0, 1002, "payload_uid must be a positive integer"),
        (1001, -1, "payload_gid must be a positive integer"),
    ],
)
def test_runner_daemon_config_rejects_invalid_payload_identity(
    tmp_path: Path,
    payload_uid: int | None,
    payload_gid: int | None,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        RunnerDaemonConfig(
            server_url="http://control.invalid",
            node_id="runner-a",
            name="Runner A",
            state_path=tmp_path / "runner",
            payload_uid=payload_uid,
            payload_gid=payload_gid,
        )


@pytest.mark.asyncio
async def test_daemon_close_does_not_starve_other_resource_families_after_failure(
    tmp_path: Path,
) -> None:
    execution = _execution(tmp_path)
    release = asyncio.Event()
    process_entered = asyncio.Event()
    terminal_entered = asyncio.Event()
    browser_entered = asyncio.Event()
    client_closed = asyncio.Event()

    class _FailingCloseSupervisor(_Supervisor):
        async def close(self, *, cancel_running: bool = False) -> None:
            assert cancel_running is True
            process_entered.set()
            await release.wait()
            raise RuntimeError("process shutdown failed")

    class _ClosingTerminalHandler:
        async def close(self) -> None:
            terminal_entered.set()
            await release.wait()

    class _ClosingBrowserHandler:
        async def close_all(self) -> None:
            browser_entered.set()
            await release.wait()

    class _ClosingClient(_RunnerClient):
        async def close(self) -> None:
            client_closed.set()

    client = _ClosingClient()
    daemon = _daemon(
        tmp_path,
        client=client,
        supervisor=_FailingCloseSupervisor(execution),
        repository=_ExecutionRepository(execution),
        terminal_handler=_ClosingTerminalHandler(),
        browser_handler=_ClosingBrowserHandler(),
    )

    close_task = asyncio.create_task(daemon.close())
    await asyncio.wait_for(
        asyncio.gather(
            process_entered.wait(),
            terminal_entered.wait(),
            browser_entered.wait(),
        ),
        timeout=1,
    )
    release.set()

    with pytest.raises(RuntimeError, match="process shutdown failed"):
        await asyncio.wait_for(close_task, timeout=1)
    assert client_closed.is_set()


@pytest.mark.asyncio
async def test_cancel_uses_durable_process_row_when_cancellation_journal_write_fails(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    execution = _execution(tmp_path)
    repository = _ExecutionRepository(execution)
    supervisor = _Supervisor(execution)
    client = _RunnerClient()
    daemon = _daemon(
        tmp_path,
        client=client,
        supervisor=supervisor,
        repository=repository,
        journal=_FailingCancellationJournal(),
    )
    command = _command(
        "cancel-1",
        RunnerCommandKind.CANCEL,
        {"execution_id": "server-execution-1", "execution_key": execution.execution_key},
    )

    await daemon.handle_command(command)

    assert supervisor.cancel_calls == [execution.id]
    assert execution.status is ExecutionStatus.CANCELLED
    assert client.statuses == [("server-execution-1", ExecutionStatus.CANCELLED)]
    assert client.status_details[0]["physical_stop_confirmed"] is True
    assert client.finished[0][0:2] == (command.id, True)
    assert client.finished[0][2]["physical_stop_confirmed"] is True
    assert client.finished[0][2]["cancellation_tombstone_persisted"] is False
    assert "tombstone persistence is degraded" in caplog.text


@pytest.mark.asyncio
async def test_cancel_uses_durable_pty_row_when_cancellation_journal_write_fails(
    tmp_path: Path,
) -> None:
    execution = _execution(
        tmp_path,
        executor_type=ExecutorType.PTY,
        execution_key="terminal:journal-write-failure",
    )
    repository = _ExecutionRepository(execution)
    client = _RunnerClient()
    terminal_handler = _TerminalHandler(execution)
    daemon = _daemon(
        tmp_path,
        client=client,
        supervisor=_Supervisor(execution),
        repository=repository,
        journal=_FailingCancellationJournal(),
        terminal_handler=terminal_handler,
    )
    command = _command(
        "cancel-pty-journal-failure",
        RunnerCommandKind.CANCEL,
        {"execution_id": execution.id, "execution_key": execution.execution_key},
    )

    await daemon.handle_command(command)

    assert terminal_handler.cancel_calls == [execution.id]
    assert execution.status is ExecutionStatus.CANCELLED
    assert execution.physical_stop_confirmed_at is not None
    assert client.statuses == [(execution.id, ExecutionStatus.CANCELLED)]
    assert client.finished[0][0:2] == (command.id, True)
    assert client.finished[0][2]["cancellation_tombstone_persisted"] is False


@pytest.mark.parametrize("local_row_exists", [True, False])
@pytest.mark.asyncio
async def test_journal_failure_allows_central_safety_only_with_durable_row_proof(
    tmp_path: Path,
    local_row_exists: bool,
) -> None:
    local_execution = _execution(tmp_path)
    central_repository = FileExecutionRepository(tmp_path / "central-executions.json")
    await central_repository.create_if_absent(local_execution.model_copy(deep=True))

    class _CentralStatusClient(_RunnerClient):
        async def report_status(
            self,
            execution_id: str,
            status: ExecutionStatus,
            **details: object,
        ) -> None:
            await super().report_status(execution_id, status, **details)
            current = await central_repository.get(execution_id)
            assert current is not None
            if current.status is not status:
                current.transition_to(status, exit_code=details.get("exit_code"))  # type: ignore[arg-type]
            if details.get("physical_stop_confirmed") is True:
                current.physical_stop_confirmed_at = datetime.now(UTC)
            await central_repository.save(current)

    class _UnavailableCentralRunner:
        async def cancel(self, execution_id: str) -> Execution:
            raise RuntimeError(f"owning Runner did not ACK {execution_id}")

    client = _CentralStatusClient()
    daemon = _daemon(
        tmp_path,
        client=client,
        supervisor=_Supervisor(local_execution),
        repository=_ExecutionRepository(local_execution if local_row_exists else None),
        journal=_FailingCancellationJournal(),
    )
    command = _command(
        f"cancel-journal-safety-{local_row_exists}",
        RunnerCommandKind.CANCEL,
        {
            "execution_id": local_execution.id,
            "execution_key": local_execution.execution_key,
        },
    )

    await daemon.handle_command(command)
    safety = RunSafetyStopService(
        execution_repository=central_repository,
        execution_runner=_UnavailableCentralRunner(),  # type: ignore[arg-type]
        require_all_resource_stoppers=False,
        execution_cancel_timeout_seconds=0,
        execution_cancel_max_passes=1,
    )
    safety_result = await safety.stop_run(local_execution.run_id, drain=False)
    central = await central_repository.get(local_execution.id)

    assert central is not None
    assert (central.physical_stop_confirmed_at is not None) is local_row_exists
    assert safety_result.resources["executions"].succeeded is local_row_exists
    assert client.finished[0][1] is local_row_exists


@pytest.mark.parametrize("executor_type", [ExecutorType.PROCESS, ExecutorType.PTY])
@pytest.mark.asyncio
async def test_cancel_never_publishes_returned_but_unpersisted_stop_proof(
    tmp_path: Path,
    executor_type: ExecutorType,
) -> None:
    execution = _execution(
        tmp_path,
        executor_type=executor_type,
        execution_key=(
            "unpersisted-process-proof"
            if executor_type is ExecutorType.PROCESS
            else "terminal:unpersisted-pty-proof"
        ),
    )
    repository = _ExecutionRepository(execution)

    def stopped_copy() -> Execution:
        stopped = execution.model_copy(deep=True)
        stopped.transition_to(ExecutionStatus.CANCELLED)
        stopped.physical_stop_confirmed_at = datetime.now(UTC)
        return stopped

    class _UnpersistedProofSupervisor(_Supervisor):
        async def cancel(self, execution_id: str) -> Execution:
            self.cancel_calls.append(execution_id)
            return stopped_copy()

    class _UnpersistedProofTerminal(_TerminalHandler):
        async def cancel_execution(self, execution_id: str) -> Execution:
            self.cancel_calls.append(execution_id)
            return stopped_copy()

    client = _RunnerClient()
    terminal_handler = (
        _UnpersistedProofTerminal(execution) if executor_type is ExecutorType.PTY else None
    )
    daemon = _daemon(
        tmp_path,
        client=client,
        supervisor=_UnpersistedProofSupervisor(execution),
        repository=repository,
        journal=_FailingCancellationJournal(),
        terminal_handler=terminal_handler,
    )
    command = _command(
        f"cancel-unpersisted-{executor_type.value}",
        RunnerCommandKind.CANCEL,
        {"execution_id": execution.id, "execution_key": execution.execution_key},
    )

    await daemon.handle_command(command)

    assert repository.execution is execution
    assert execution.status is ExecutionStatus.RUNNING
    assert execution.physical_stop_confirmed_at is None
    assert client.statuses == []
    assert client.finished[0][0:2] == (command.id, False)
    assert "did not persist" in client.finished[0][3]
    assert "physical_stop_confirmed" not in client.finished[0][2]


@pytest.mark.asyncio
async def test_process_durable_stop_row_blocks_same_key_spawn_after_runner_restart(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "process-row-restart"
    repository_path = state_path / "executions.json"
    repository = FileExecutionRepository(repository_path)
    supervisor = ProcessSupervisor(repository, RunnerPaths(state_path))
    raw_request = ExecutionLaunchRequest(
        execution_id="execution-process-row-restart",
        execution_key="process-row-restart-key",
        run_id="run-1",
        node_id="runner-a",
        runner_principal=_OWNER,
        executor_type=ExecutorType.PROCESS,
        cwd=tmp_path,
        argv=[sys.executable, "-c", "import time; time.sleep(30)"],
    )
    marker = tmp_path / "unsafe-process-restart"
    delayed_execute = _command(
        "delayed-process-row-restart",
        RunnerCommandKind.EXECUTE,
        {
            "execution_id": raw_request.execution_id,
            "request": raw_request.model_copy(
                update={
                    "argv": [
                        sys.executable,
                        "-c",
                        (f"from pathlib import Path; Path({str(marker)!r}).write_text('unsafe')"),
                    ]
                }
            ).model_dump(mode="json"),
        },
    )
    request = raw_request.model_copy(update=_command_callback_binding(delayed_execute))
    execution = await supervisor.start(request)
    first_client = _RunnerClient()
    first_daemon = RunnerDaemon(
        config=RunnerDaemonConfig(
            server_url="http://control.invalid",
            node_id="runner-a",
            name="Runner A",
            state_path=state_path,
        ),
        client=first_client,  # type: ignore[arg-type]
        supervisor=supervisor,
        executions=repository,
        execution_cancellation_journal=_FailingCancellationJournal(),  # type: ignore[arg-type]
    )
    cancel = _command(
        "cancel-process-row-restart",
        RunnerCommandKind.CANCEL,
        {"execution_id": execution.id, "execution_key": execution.execution_key},
    )
    await first_daemon.handle_command(cancel)
    assert first_client.finished[0][1] is True
    await first_daemon.close()

    reopened_repository = FileExecutionRepository(repository_path)
    reopened_supervisor = ProcessSupervisor(
        reopened_repository,
        RunnerPaths(state_path),
    )
    reopened_client = _RunnerClient()
    reopened_daemon = RunnerDaemon(
        config=RunnerDaemonConfig(
            server_url="http://control.invalid",
            node_id="runner-a",
            name="Runner A restarted",
            state_path=state_path,
        ),
        client=reopened_client,  # type: ignore[arg-type]
        supervisor=reopened_supervisor,
        executions=reopened_repository,
    )
    try:
        await reopened_daemon.handle_command(delayed_execute)

        durable = await reopened_repository.get(execution.id)
        assert durable is not None
        assert durable.status is ExecutionStatus.CANCELLED
        assert durable.physical_stop_confirmed_at is not None
        assert durable.launch_fingerprint == request.launch_fingerprint
        assert reopened_client.finished[0][0:2] == (delayed_execute.id, False)
        assert "launch_fingerprint" in reopened_client.finished[0][3]
        assert not marker.exists()
    finally:
        await reopened_daemon.close()


@pytest.mark.asyncio
async def test_execution_cancel_journal_distinguishes_exact_fresh_and_divergent_replay(
    tmp_path: Path,
) -> None:
    execution = _execution(tmp_path).model_copy(
        update={
            "runner_command_id": "launch-command",
            "runner_effect_binding_id": "launch-binding",
            "runner_binding_digest": "1" * 64,
            "runner_envelope_digest": "2" * 64,
        }
    )
    supervisor = _Supervisor(execution)
    client = _RunnerClient()
    daemon = _daemon(
        tmp_path,
        client=client,
        supervisor=supervisor,
        repository=_ExecutionRepository(execution),
    )
    payload = {
        "execution_id": execution.id,
        "execution_key": execution.execution_key,
    }
    first = _command("cancel-journal-replay", RunnerCommandKind.CANCEL, payload)
    await daemon.handle_command(first)
    assert client.finished[-1][0:2] == (first.id, True)
    assert supervisor.cancel_calls == [execution.id]

    exact = _command(first.id, first.kind, first.payload)
    await daemon.handle_command(exact)
    assert client.finished[-1][0:2] == (exact.id, True)
    assert supervisor.cancel_calls == [execution.id]

    fresh = _command("cancel-journal-fresh", first.kind, first.payload)
    await daemon.handle_command(fresh)
    assert client.finished[-1][0:2] == (fresh.id, True)
    assert supervisor.cancel_calls == [execution.id]

    divergent = _command(
        first.id,
        first.kind,
        first.payload,
        binding_id="binding-cancel-journal-divergent",
    )
    await daemon.handle_command(divergent)
    assert client.finished[-1][0:2] == (divergent.id, False)
    assert "conflicts with its durable journal record" in client.finished[-1][3]
    assert supervisor.cancel_calls == [execution.id]
    assert await OperationJournal(
        tmp_path / "runner" / "execution-cancellations.json"
    ).contains(f"execution:{execution.id}")
    await daemon.close()


@pytest.mark.asyncio
async def test_verified_replacement_cancel_acks_legacy_execution_without_launch_binding(
    tmp_path: Path,
) -> None:
    execution = _execution(tmp_path, legacy=True)
    assert all(getattr(execution, field_name) is None for field_name in _EXECUTION_CALLBACK_FIELDS)
    supervisor = _Supervisor(execution)
    client = _RunnerClient()
    daemon = _daemon(
        tmp_path,
        client=client,
        supervisor=supervisor,
        repository=_ExecutionRepository(execution),
    )
    command = _command(
        "verified-legacy-replacement-cancel",
        RunnerCommandKind.CANCEL,
        {
            "execution_id": execution.id,
            "execution_key": execution.execution_key,
        },
    )

    await daemon.handle_command(command)

    assert supervisor.cancel_calls == [execution.id]
    assert execution.status is ExecutionStatus.CANCELLED
    assert execution.physical_stop_confirmed_at is not None
    # A pre-fencing launch cannot authenticate an execution-status callback.
    # The verified replacement command's typed stop ACK is the only projector.
    assert client.statuses == []
    assert client.finished[0][0:2] == (command.id, True)
    assert client.finished[0][2]["physical_stop_confirmed"] is True
    journal = OperationJournal(tmp_path / "runner" / "execution-cancellations.json")
    tombstone = await journal.get_resource(f"execution:{execution.id}")
    assert tombstone is not None
    assert tombstone.outcome.get("state") == "physical_stop_confirmed"
    await daemon.close()


@pytest.mark.parametrize(
    "kind",
    [
        RunnerCommandKind.CANCEL,
        RunnerCommandKind.TERMINAL_CLOSE,
        RunnerCommandKind.TARGET_HTTP_CANCEL,
        RunnerCommandKind.BROWSER_CLOSE,
    ],
)
@pytest.mark.asyncio
async def test_resource_binding_payload_mismatch_fails_before_journal_or_handler_io(
    tmp_path: Path,
    kind: RunnerCommandKind,
) -> None:
    execution = _execution(tmp_path)
    client = _RunnerClient()
    supervisor = _Supervisor(execution)
    execution_journal = _CancellationJournal()
    target_journal = _CancellationJournal()
    browser_journal = _CancellationJournal()
    terminal_handler: _TerminalHandler | None = None
    target_handler: _BlockingTargetHttpRunner | None = None
    browser_handler: _BlockingBrowserRunner | None = None

    if kind is RunnerCommandKind.CANCEL:
        payload: dict[str, object] = {
            "execution_id": execution.id,
            "execution_key": execution.execution_key,
        }
        command = _rebind_command(
            _command("mismatched-execution-binding", kind, payload),
            binding_updates={
                "resource_id": "binding-execution-a",
                "execution_id": "binding-execution-a",
            },
        )
    elif kind is RunnerCommandKind.TERMINAL_CLOSE:
        execution.executor_type = ExecutorType.PTY
        execution.session_id = "payload-terminal-b"
        execution.execution_key = "terminal:payload-terminal-b"
        terminal_handler = _TerminalHandler(execution)
        payload = {
            "session_id": "payload-terminal-b",
            "execution_id": execution.id,
            "execution_key": execution.execution_key,
        }
        command = _rebind_command(
            _command("mismatched-terminal-binding", kind, payload),
            binding_updates={"resource_id": "binding-terminal-a"},
        )
    elif kind is RunnerCommandKind.TARGET_HTTP_CANCEL:
        target_handler = _BlockingTargetHttpRunner()
        payload = {"run_id": "run-1", "tool_call_ids": ["payload-intent-b"]}
        command = _rebind_command(
            _command("mismatched-target-http-binding", kind, payload),
            binding_updates={"resource_id": "binding-intent-a"},
        )
    else:
        session = _browser_session()
        browser_handler = _BlockingBrowserRunner(session)
        payload = {
            "operation": BrowserOperation.CLOSE.value,
            "command": BrowserSessionCommand(
                session_id=session.id,
                session=session,
            ).model_dump(mode="json"),
        }
        command = _rebind_command(
            _command("mismatched-browser-binding", kind, payload),
            binding_updates={"resource_id": "binding-browser-a"},
        )

    daemon = _daemon(
        tmp_path,
        client=client,
        supervisor=supervisor,
        repository=_ExecutionRepository(execution),
        journal=execution_journal,
        terminal_handler=terminal_handler,
        target_http_handler=target_handler,
        target_http_journal=target_journal,
        target_http_confirmation_journal=_CancellationJournal(),
        browser_handler=browser_handler,
        browser_journal=browser_journal,
    )

    await daemon.handle_command(command)

    assert client.finished[-1][0:2] == (command.id, False)
    assert "payload conflicts with its effect binding" in client.finished[-1][3]
    assert supervisor.cancel_calls == []
    assert execution_journal.operations == set()
    assert target_journal.operations == set()
    assert browser_journal.operations == set()
    if terminal_handler is not None:
        assert terminal_handler.cancel_calls == []
    if target_handler is not None:
        assert target_handler.stop_calls == []
    if browser_handler is not None:
        assert browser_handler.close_calls == 0


@pytest.mark.parametrize(
    "mutation",
    ["family", "resource", "code_audit", "origin"],
)
@pytest.mark.asyncio
async def test_self_consistent_disallowed_effect_metadata_fails_before_local_io(
    tmp_path: Path,
    mutation: str,
) -> None:
    execution = _execution(tmp_path)
    payload = {
        "execution_id": execution.id,
        "execution_key": execution.execution_key,
    }
    base = _command(f"disallowed-{mutation}", RunnerCommandKind.CANCEL, payload)
    if mutation == "family":
        command = _rebind_command(
            base,
            binding_updates={"operation_family": RunnerOperationFamily.EXECUTION},
            operation_family=RunnerOperationFamily.EXECUTION,
            output_contract=RunnerOutputContract(
                result_schema="riftx.runner-result/execution-stop/v1"
            ),
        )
    elif mutation == "resource":
        command = _rebind_command(
            base,
            binding_updates={
                "resource_kind": RunnerResourceKind.BROWSER_SESSION,
                "resource_id": "browser-wrong-family",
                "execution_id": None,
            },
            output_contract=RunnerOutputContract(
                result_schema="riftx.runner-result/execution-stop/v1",
                stop_ack_schema=RUNNER_STOP_ACK_BROWSER_SCHEMA,
            ),
        )
    elif mutation == "code_audit":
        command = _rebind_command(
            base,
            binding_updates={
                "run_kind": RunKind.CODE_AUDIT,
                "audit_id": "audit-forged",
                "plan_digest": "c" * 64,
            },
        )
    else:
        command = _rebind_command(
            base,
            binding_updates={"origin": RunnerCommandOrigin.TEMPORAL_WORKER},
        )
    journal = _CancellationJournal()
    supervisor = _Supervisor(execution)
    client = _RunnerClient()
    daemon = _daemon(
        tmp_path,
        client=client,
        supervisor=supervisor,
        repository=_ExecutionRepository(execution),
        journal=journal,
    )

    await daemon.handle_command(command)

    assert client.finished[-1][0:2] == (command.id, False)
    assert supervisor.cancel_calls == []
    assert journal.operations == set()


@pytest.mark.asyncio
async def test_cancel_rejects_non_authoritative_execution_key_before_tombstone(
    tmp_path: Path,
) -> None:
    execution = _execution(tmp_path)
    journal = _CancellationJournal()
    supervisor = _Supervisor(execution)
    client = _RunnerClient()
    daemon = _daemon(
        tmp_path,
        client=client,
        supervisor=supervisor,
        repository=_ExecutionRepository(execution),
        journal=journal,
    )
    command = _command(
        "cancel-wrong-execution-key",
        RunnerCommandKind.CANCEL,
        {
            "execution_id": execution.id,
            "execution_key": "different-resource-key",
        },
    )

    await daemon.handle_command(command)

    assert client.finished[-1][0:2] == (command.id, False)
    assert "execution key" in client.finished[-1][3]
    assert supervisor.cancel_calls == []
    assert journal.operations == set()


@pytest.mark.asyncio
async def test_absent_execution_cancel_uses_typed_tombstone_not_payload_key(
    tmp_path: Path,
) -> None:
    execution = _execution(tmp_path, execution_key="authoritative-delayed-key")
    repository = _ExecutionRepository(None)
    supervisor = _PreSpawnGuardSupervisor(execution, repository)
    journal = _CancellationJournal()
    client = _RunnerClient()
    daemon = _daemon(
        tmp_path,
        client=client,
        supervisor=supervisor,
        repository=repository,
        journal=journal,
    )
    cancel = _command(
        "cancel-absent-with-untrusted-key",
        RunnerCommandKind.CANCEL,
        {
            "execution_id": execution.id,
            "execution_key": "payload-controlled-key",
        },
    )

    await daemon.handle_command(cancel)

    assert client.finished[-1][0:2] == (cancel.id, False)
    assert journal.operations == {f"execution:{execution.id}"}
    assert not await journal.contains("payload-controlled-key")
    assert supervisor.cancel_calls == []

    delayed_execute = _command(
        "execute-after-typed-cancellation",
        RunnerCommandKind.EXECUTE,
        {
            "execution_id": execution.id,
            "request": {
                "execution_key": execution.execution_key,
                "run_id": execution.run_id,
                "node_id": execution.node_id,
                "executor_type": ExecutorType.PROCESS.value,
                "cwd": str(tmp_path),
                "argv": ["echo", "must-not-spawn"],
            },
        },
    )
    await daemon.handle_command(delayed_execute)

    assert supervisor.spawned is False
    assert client.finished[-1][0:2] == (delayed_execute.id, True)
    assert client.finished[-1][2]["suppressed_by_cancellation"] is True


@pytest.mark.asyncio
async def test_legacy_execution_key_matching_typed_id_does_not_suppress_other_execution(
    tmp_path: Path,
) -> None:
    execution = _execution(tmp_path, execution_key="authoritative-execution-key")
    execution.status = ExecutionStatus.STARTING
    repository = _ExecutionRepository(None)
    supervisor = _PreSpawnGuardSupervisor(execution, repository)
    client = _RunnerClient()
    journal_path = tmp_path / "runner" / "execution-cancellations.json"
    journal_path.parent.mkdir(parents=True, exist_ok=True)
    journal_path.write_text(
        json.dumps([f"execution:{execution.id}"]),
        encoding="utf-8",
    )
    daemon = _daemon(
        tmp_path,
        client=client,
        supervisor=supervisor,
        repository=repository,
    )
    daemon._start_monitor = lambda *_: None  # type: ignore[method-assign]
    execute = _command(
        "execute-after-colliding-legacy-key",
        RunnerCommandKind.EXECUTE,
        {
            "execution_id": execution.id,
            "request": {
                "execution_key": execution.execution_key,
                "run_id": execution.run_id,
                "node_id": execution.node_id,
                "executor_type": ExecutorType.PROCESS.value,
                "cwd": str(tmp_path),
                "argv": ["echo", "must-spawn"],
            },
        },
    )

    await daemon.handle_command(execute)

    assert supervisor.spawned is True
    assert client.finished[-1][0:2] == (execute.id, True)
    assert client.finished[-1][2].get("suppressed_by_cancellation") is not True
    journal = OperationJournal(journal_path, legacy_list_resources=True)
    assert await journal.get_resource(f"execution:{execution.id}") is None
    assert await journal.get_legacy_resource(f"execution:{execution.id}") is not None


@pytest.mark.asyncio
async def test_legacy_key_matching_typed_attempt_cannot_block_verified_stop(
    tmp_path: Path,
) -> None:
    execution = _execution(tmp_path)
    repository = _ExecutionRepository(execution)
    supervisor = _Supervisor(execution)
    client = _RunnerClient()
    cancel = _command(
        "cancel-after-colliding-legacy-attempt",
        RunnerCommandKind.CANCEL,
        {
            "execution_id": execution.id,
            "execution_key": execution.execution_key,
        },
    )
    typed_key = f"execution:{execution.id}"
    attempt_key = (
        f"{typed_key}:command:"
        f"{hashlib.sha256(cancel.id.encode('utf-8')).hexdigest()}"
    )
    journal_path = tmp_path / "runner" / "execution-cancellations.json"
    journal_path.parent.mkdir(parents=True, exist_ok=True)
    journal_path.write_text(json.dumps([attempt_key]), encoding="utf-8")
    daemon = _daemon(
        tmp_path,
        client=client,
        supervisor=supervisor,
        repository=repository,
    )

    await daemon.handle_command(cancel)

    assert supervisor.cancel_calls == [execution.id]
    assert execution.status is ExecutionStatus.CANCELLED
    assert execution.physical_stop_confirmed_at is not None
    assert client.finished[-1][0:2] == (cancel.id, True)
    journal = OperationJournal(journal_path, legacy_list_resources=True)
    typed_tombstone = await journal.get_resource(typed_key)
    assert typed_tombstone is not None
    assert typed_tombstone.outcome.get("state") == "physical_stop_confirmed"
    legacy_attempt = await journal.get_legacy_resource(attempt_key)
    assert legacy_attempt is not None
    assert legacy_attempt.outcome == {"state": "legacy_unbound"}


@pytest.mark.asyncio
async def test_cancel_rejects_binding_run_drift_before_tombstone_or_stop(
    tmp_path: Path,
) -> None:
    execution = _execution(tmp_path)
    journal = _CancellationJournal()
    supervisor = _Supervisor(execution)
    client = _RunnerClient()
    daemon = _daemon(
        tmp_path,
        client=client,
        supervisor=supervisor,
        repository=_ExecutionRepository(execution),
        journal=journal,
    )
    command = _command(
        "cancel-wrong-binding-run",
        RunnerCommandKind.CANCEL,
        {
            "execution_id": execution.id,
            "execution_key": execution.execution_key,
        },
        run_id="foreign-run",
    )

    await daemon.handle_command(command)

    assert client.finished[-1][0:2] == (command.id, False)
    assert "run_id" in client.finished[-1][3]
    assert supervisor.cancel_calls == []
    assert journal.operations == set()


@pytest.mark.asyncio
async def test_execution_monitor_truncates_at_immutable_cap_and_reports_terminal_status(
    tmp_path: Path,
) -> None:
    execution = _execution(tmp_path)
    execution.transition_to(ExecutionStatus.EXITED, exit_code=0)
    execution.physical_stop_confirmed_at = datetime.now(UTC)
    supervisor = _OutputSupervisor(
        execution,
        stdout=b"abcdefgh",
        stderr=b"XYZ",
    )
    client = _CappedOutputRunnerClient(max_output_bytes=5)
    daemon = RunnerDaemon(
        config=RunnerDaemonConfig(
            server_url="http://control.invalid",
            node_id="runner-a",
            name="Runner A",
            state_path=tmp_path / "runner",
            output_poll_seconds=0.001,
        ),
        client=client,  # type: ignore[arg-type]
        supervisor=supervisor,  # type: ignore[arg-type]
        executions=_ExecutionRepository(execution),  # type: ignore[arg-type]
    )

    try:
        await asyncio.wait_for(
            daemon._monitor_execution(execution.id, execution.id),
            timeout=1,
        )
    finally:
        await daemon.close()

    assert bytes(client.output["stdout"]) == b"abcde"
    assert bytes(client.output["stderr"]) == b"XYZ"
    assert client.statuses == [(execution.id, ExecutionStatus.EXITED)]
    assert [attempt for attempt in client.output_attempts if attempt[0] == "stdout"] == [
        ("stdout", 0, b"abcdefgh"),
        ("stdout", 0, b"abcde"),
        ("stdout", 2, b"cde"),
    ]


@pytest.mark.asyncio
async def test_zero_output_contract_cannot_admit_target_http_effect(
    tmp_path: Path,
) -> None:
    runner = _BlockingTargetHttpRunner()
    delivery_journal = _CancellationJournal()
    base = _command(
        "target-http-zero-output-contract",
        RunnerCommandKind.TARGET_HTTP,
        _target_http_payload(),
    )
    command = _rebind_command(
        base,
        binding_updates={},
        output_contract=RunnerOutputContract(
            result_schema="riftx.runner-result/target-http/v1"
        ),
    )
    execution = _execution(tmp_path)
    client = _RunnerClient()
    daemon = RunnerDaemon(
        config=RunnerDaemonConfig(
            server_url="http://control.invalid",
            node_id="runner-a",
            name="Runner A",
            state_path=tmp_path / "runner",
        ),
        client=client,  # type: ignore[arg-type]
        supervisor=_Supervisor(execution),  # type: ignore[arg-type]
        executions=_ExecutionRepository(None),  # type: ignore[arg-type]
        target_http_handler=runner,  # type: ignore[arg-type]
        target_http_delivery_journal=delivery_journal,  # type: ignore[arg-type]
    )

    await daemon.handle_command(command)

    assert client.finished[-1][0:2] == (command.id, False)
    assert "invalid output contract" in client.finished[-1][3]
    assert runner.execute_calls == 0
    assert delivery_journal.operations == set()


@pytest.mark.asyncio
async def test_browser_close_cannot_use_normal_browser_operation_kind(
    tmp_path: Path,
) -> None:
    session = _browser_session()
    runner = _BlockingBrowserRunner(session)
    journal = _CancellationJournal()
    client = _RunnerClient()
    execution = _execution(tmp_path)
    daemon = _daemon(
        tmp_path,
        client=client,
        supervisor=_Supervisor(execution),
        repository=_ExecutionRepository(None),
        browser_handler=runner,
        browser_journal=journal,
    )
    command = _command(
        "browser-close-with-normal-kind",
        RunnerCommandKind.BROWSER,
        {
            "operation": BrowserOperation.CLOSE.value,
            "command": BrowserSessionCommand(
                session_id=session.id,
                session=session,
            ).model_dump(mode="json"),
        },
    )

    await daemon.handle_command(command)

    assert client.finished[-1][0:2] == (command.id, False)
    assert "operation" in client.finished[-1][3]
    assert runner.close_calls == 0
    assert journal.operations == set()


@pytest.mark.asyncio
async def test_cancel_lease_loss_cannot_remove_resource_tombstone_or_abort_stop(
    tmp_path: Path,
) -> None:
    execution = _execution(tmp_path).model_copy(
        update={
            "runner_command_id": "launch-command",
            "runner_effect_binding_id": "launch-binding",
            "runner_binding_digest": "3" * 64,
            "runner_envelope_digest": "4" * 64,
        }
    )
    repository = _ExecutionRepository(execution)
    supervisor = _ImmediateStartBlockingCancelSupervisor(execution, repository)
    client = _FailingRenewRunnerClient()
    daemon = _daemon(
        tmp_path,
        client=client,
        supervisor=supervisor,
        repository=repository,
    )
    command = _command(
        "cancel-short-lease-journal",
        RunnerCommandKind.CANCEL,
        {
            "execution_id": execution.id,
            "execution_key": execution.execution_key,
        },
        lease_duration_seconds=0.03,
    )

    lease_task = asyncio.create_task(daemon._run_leased_command(command))
    await asyncio.wait_for(supervisor.cancel_entered.wait(), timeout=1)
    await asyncio.wait_for(lease_task, timeout=1)

    journal = OperationJournal(tmp_path / "runner" / "execution-cancellations.json")
    assert await journal.contains(f"execution:{execution.id}")
    assert execution.status is ExecutionStatus.RUNNING
    assert daemon._execution_stop_tasks

    supervisor.release_cancel.set()
    await asyncio.wait_for(supervisor.cancelled.wait(), timeout=1)
    for _ in range(100):
        if not daemon._execution_stop_tasks:
            break
        await asyncio.sleep(0.001)

    assert execution.status is ExecutionStatus.CANCELLED
    assert command.id not in {item[0] for item in client.finished}
    tombstone = await journal.get_resource(f"execution:{execution.id}")
    assert tombstone is not None
    assert tombstone.outcome.get("state") == "physical_stop_confirmed"
    await daemon.close()


@pytest.mark.parametrize(
    "status",
    [
        ExecutionStatus.COMPLETED,
        ExecutionStatus.EXITED,
        ExecutionStatus.HARD_TIMEOUT,
    ],
)
@pytest.mark.asyncio
async def test_cancel_ack_preserves_confirmed_natural_terminal_outcome(
    tmp_path: Path,
    status: ExecutionStatus,
) -> None:
    execution = _execution(tmp_path)
    execution.transition_to(status)
    execution.physical_stop_confirmed_at = datetime.now(UTC)
    repository = _ExecutionRepository(execution)
    supervisor = _Supervisor(execution)
    client = _RunnerClient()
    daemon = _daemon(
        tmp_path,
        client=client,
        supervisor=supervisor,
        repository=repository,
        journal=_CancellationJournal(),
    )
    command = _command(
        f"cancel-confirmed-{status.value}",
        RunnerCommandKind.CANCEL,
        {"execution_id": execution.id, "execution_key": execution.execution_key},
    )

    await daemon.handle_command(command)

    assert supervisor.cancel_calls == [execution.id]
    assert execution.status is status
    assert client.statuses == [(execution.id, status)]
    assert client.status_details[0]["physical_stop_confirmed"] is True
    assert client.finished[0][0:2] == (command.id, True)
    assert client.finished[0][2]["status"] == ExecutionStatus.CANCELLED.value
    assert client.finished[0][2]["physical_stop_confirmed"] is True


@pytest.mark.asyncio
async def test_cancel_recovers_confirmed_pty_exit_after_status_upload_loss(
    tmp_path: Path,
) -> None:
    execution = _execution(
        tmp_path,
        executor_type=ExecutorType.PTY,
        execution_key="terminal:natural-exit-upload-lost",
    )
    execution.transition_to(ExecutionStatus.EXITED)
    execution.physical_stop_confirmed_at = datetime.now(UTC)
    repository = _ExecutionRepository(execution)
    supervisor = _Supervisor(execution)
    terminal_handler = _TerminalHandler(execution)
    client = _RunnerClient()
    daemon = _daemon(
        tmp_path,
        client=client,
        supervisor=supervisor,
        repository=repository,
        journal=_CancellationJournal(),
        terminal_handler=terminal_handler,
    )
    command = _command(
        "cancel-confirmed-pty-exit",
        RunnerCommandKind.CANCEL,
        {"execution_id": execution.id, "execution_key": execution.execution_key},
    )

    await daemon.handle_command(command)

    assert terminal_handler.cancel_calls == [execution.id]
    assert supervisor.cancel_calls == []
    assert execution.status is ExecutionStatus.EXITED
    assert client.statuses == [(execution.id, ExecutionStatus.EXITED)]
    assert client.status_details[0]["physical_stop_confirmed"] is True
    assert client.finished[0][0:2] == (command.id, True)
    assert client.finished[0][2]["status"] == ExecutionStatus.CANCELLED.value


@pytest.mark.asyncio
async def test_cancel_without_local_execution_fails_when_tombstone_cannot_be_written(
    tmp_path: Path,
) -> None:
    execution = _execution(tmp_path)
    repository = _ExecutionRepository(None)
    supervisor = _Supervisor(execution)
    client = _RunnerClient()
    daemon = _daemon(
        tmp_path,
        client=client,
        supervisor=supervisor,
        repository=repository,
        journal=_FailingCancellationJournal(),
    )
    command = _command(
        "cancel-before-execute",
        RunnerCommandKind.CANCEL,
        {"execution_id": "server-execution-1", "execution_key": execution.execution_key},
    )

    await daemon.handle_command(command)

    assert supervisor.cancel_calls == []
    assert client.statuses == []
    assert client.finished[0][0:2] == (command.id, False)
    assert "cannot be guaranteed" in client.finished[0][3]


@pytest.mark.asyncio
async def test_cancel_rejects_same_key_bound_to_different_local_execution_id(
    tmp_path: Path,
) -> None:
    execution = _execution(tmp_path)
    execution.id = "different-local-execution"
    repository = _ExecutionRepository(execution)
    supervisor = _Supervisor(execution)
    client = _RunnerClient()
    daemon = _daemon(
        tmp_path,
        client=client,
        supervisor=supervisor,
        repository=repository,
    )
    command = _command(
        "cancel-mismatched-local-id",
        RunnerCommandKind.CANCEL,
        {"execution_id": "server-execution-1", "execution_key": execution.execution_key},
    )

    await daemon.handle_command(command)

    assert supervisor.cancel_calls == []
    assert execution.status is ExecutionStatus.RUNNING
    assert client.statuses == []
    assert client.finished[0][0:2] == (command.id, False)
    assert "belongs to local execution" in client.finished[0][3]
    assert "different-local-execution" in client.finished[0][3]


@pytest.mark.asyncio
async def test_cancel_rejects_cloned_same_identity_owned_by_different_runner(
    tmp_path: Path,
) -> None:
    execution = _execution(tmp_path)
    execution.owner = RunnerPrincipal(instance_id="runner-instance-b", epoch=1)
    repository = _ExecutionRepository(execution)
    supervisor = _Supervisor(execution)
    client = _RunnerClient()
    journal = _CancellationJournal()
    daemon = _daemon(
        tmp_path,
        client=client,
        supervisor=supervisor,
        repository=repository,
        journal=journal,
    )
    command = _command(
        "cancel-cloned-owner",
        RunnerCommandKind.CANCEL,
        {"execution_id": execution.id, "execution_key": execution.execution_key},
    )

    await daemon.handle_command(command)

    assert supervisor.cancel_calls == []
    assert execution.status is ExecutionStatus.RUNNING
    assert journal.operations == set()
    assert client.statuses == []
    assert client.finished[0][0:2] == (command.id, False)
    assert "owner mismatch" in client.finished[0][3]


@pytest.mark.asyncio
async def test_cancel_rejects_legacy_ownerless_execution_before_stop(
    tmp_path: Path,
) -> None:
    execution = _execution(tmp_path, legacy=True)
    execution.owner = None
    repository = _ExecutionRepository(execution)
    supervisor = _Supervisor(execution)
    client = _RunnerClient()
    journal = _CancellationJournal()
    daemon = _daemon(
        tmp_path,
        client=client,
        supervisor=supervisor,
        repository=repository,
        journal=journal,
    )
    command = _command(
        "cancel-legacy-ownerless",
        RunnerCommandKind.CANCEL,
        {"execution_id": execution.id, "execution_key": execution.execution_key},
    )

    await daemon.handle_command(command)

    assert supervisor.cancel_calls == []
    assert execution.status is ExecutionStatus.RUNNING
    assert journal.operations == set()
    assert client.statuses == []
    assert client.finished[0][0:2] == (command.id, False)
    assert "owner mismatch" in client.finished[0][3]
    assert "found None" in client.finished[0][3]


@pytest.mark.asyncio
async def test_cancel_rechecks_owner_after_tombstone_before_physical_stop(
    tmp_path: Path,
) -> None:
    owned = _execution(tmp_path)
    cloned = owned.model_copy(deep=True)
    cloned.owner = RunnerPrincipal(instance_id="runner-instance-b", epoch=2)
    repository = _SequencedExecutionRepository(owned, cloned)
    supervisor = _Supervisor(cloned)
    client = _RunnerClient()
    journal = _CancellationJournal()
    daemon = _daemon(
        tmp_path,
        client=client,
        supervisor=supervisor,
        repository=repository,
        journal=journal,
    )
    command = _command(
        "cancel-owner-swapped-before-stop",
        RunnerCommandKind.CANCEL,
        {"execution_id": owned.id, "execution_key": owned.execution_key},
    )

    await daemon.handle_command(command)

    assert repository.get_by_key_calls == 2
    assert supervisor.cancel_calls == []
    assert cloned.status is ExecutionStatus.RUNNING
    assert journal.operations == {f"execution:{owned.id}"}
    assert client.statuses == []
    assert client.finished[0][0:2] == (command.id, False)
    assert "owner mismatch" in client.finished[0][3]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "existing_owner",
    [RunnerPrincipal(instance_id="runner-instance-b", epoch=1), None],
    ids=["different-owner", "legacy-ownerless"],
)
async def test_execute_rejects_existing_identity_with_wrong_owner_before_start(
    tmp_path: Path,
    existing_owner: RunnerPrincipal | None,
) -> None:
    execution = _execution(tmp_path)
    execution.owner = existing_owner
    repository = _ExecutionRepository(execution)
    supervisor = _BarrierStartSupervisor(execution, repository)
    client = _RunnerClient()
    daemon = _daemon(
        tmp_path,
        client=client,
        supervisor=supervisor,
        repository=repository,
    )
    command = _command(
        "execute-existing-wrong-owner",
        RunnerCommandKind.EXECUTE,
        {
            "execution_id": execution.id,
            "request": {
                "execution_key": execution.execution_key,
                "run_id": execution.run_id,
                "node_id": execution.node_id,
                "executor_type": ExecutorType.PROCESS.value,
                "cwd": str(tmp_path),
                "argv": ["echo", "must-not-start"],
            },
        },
    )

    await daemon.handle_command(command)

    assert not supervisor.start_entered.is_set()
    assert supervisor.spawned is False
    assert execution.status is ExecutionStatus.RUNNING
    assert client.statuses == []
    assert client.finished[0][0:2] == (command.id, False)
    assert "owner mismatch" in client.finished[0][3]


@pytest.mark.asyncio
async def test_replacement_runner_cannot_ack_cancel_for_process_owned_by_split_brain_peer(
    tmp_path: Path,
) -> None:
    old_state = tmp_path / "runner-old"
    replacement_state = tmp_path / "runner-replacement"
    old_repository = FileExecutionRepository(old_state / "executions.json")
    replacement_repository = FileExecutionRepository(replacement_state / "executions.json")
    old_supervisor = ProcessSupervisor(
        old_repository,
        RunnerPaths(old_state),
        termination_grace_seconds=0.05,
    )
    replacement_supervisor = ProcessSupervisor(
        replacement_repository,
        RunnerPaths(replacement_state),
        termination_grace_seconds=0.05,
    )
    old_daemon = RunnerDaemon(
        config=RunnerDaemonConfig(
            server_url="http://control.invalid",
            node_id="runner-a",
            name="Runner A old",
            state_path=old_state,
        ),
        client=_RunnerClient(),  # type: ignore[arg-type]
        supervisor=old_supervisor,
        executions=old_repository,
    )
    replacement_client = _RunnerClient()
    replacement_daemon = RunnerDaemon(
        config=RunnerDaemonConfig(
            server_url="http://control.invalid",
            node_id="runner-a",
            name="Runner A replacement",
            state_path=replacement_state,
        ),
        client=replacement_client,  # type: ignore[arg-type]
        supervisor=replacement_supervisor,
        executions=replacement_repository,
    )
    execution = await old_supervisor.start(
        ExecutionLaunchRequest(
            execution_key="split-brain-process",
            run_id="run-1",
            node_id="runner-a",
            executor_type=ExecutorType.PROCESS,
            cwd=tmp_path,
            argv=[sys.executable, "-c", "import time; time.sleep(30)"],
        )
    )
    assert execution.pid is not None

    try:
        command = _command(
            "replacement-cancel-process",
            RunnerCommandKind.CANCEL,
            {
                "execution_id": execution.id,
                "execution_key": execution.execution_key,
            },
        )
        await replacement_daemon.handle_command(command)

        os.kill(execution.pid, 0)
        old_durable = await old_repository.get(execution.id)
        assert old_durable is not None and old_durable.status is ExecutionStatus.RUNNING
        assert await replacement_repository.get_by_key(execution.execution_key) is None
        assert replacement_client.statuses == []
        assert replacement_client.finished[0][0:2] == (command.id, False)
        assert "physical termination could not be confirmed" in replacement_client.finished[0][3]
        assert await OperationJournal(replacement_state / "execution-cancellations.json").contains(
            f"execution:{execution.id}"
        )

        suppressed_marker = tmp_path / "split-brain-replacement-started"
        delayed_execute = _command(
            "replacement-delayed-execute",
            RunnerCommandKind.EXECUTE,
            {
                "execution_id": execution.id,
                "request": ExecutionLaunchRequest(
                    execution_key=execution.execution_key,
                    run_id="run-1",
                    node_id="runner-a",
                    executor_type=ExecutorType.PROCESS,
                    cwd=tmp_path,
                    argv=[
                        sys.executable,
                        "-c",
                        (
                            "from pathlib import Path; "
                            f"Path({str(suppressed_marker)!r}).write_text('unsafe')"
                        ),
                    ],
                ).model_dump(mode="json"),
            },
        )
        await replacement_daemon.handle_command(delayed_execute)

        os.kill(execution.pid, 0)
        assert replacement_client.statuses == []
        assert replacement_client.finished[1][0:2] == (delayed_execute.id, True)
        assert replacement_client.finished[1][2] == {
            "execution_id": execution.id,
            "status": "suppressed",
            "suppressed_by_cancellation": True,
            "physical_stop_confirmed": False,
        }
        assert not suppressed_marker.exists()
    finally:
        await replacement_daemon.close()
        await old_daemon.close()


@pytest.mark.asyncio
async def test_replacement_runner_terminal_close_cannot_ack_pty_owned_by_peer(
    tmp_path: Path,
) -> None:
    old_state = tmp_path / "terminal-runner-old"
    replacement_state = tmp_path / "terminal-runner-replacement"
    execution = _execution(
        old_state,
        executor_type=ExecutorType.PTY,
        execution_key="terminal:split-brain-terminal",
    )
    old_repository = _ExecutionRepository(execution)
    replacement_repository = _ExecutionRepository(None)
    old_supervisor = _Supervisor(execution)
    replacement_supervisor = _Supervisor(execution)
    old_daemon = RunnerDaemon(
        config=RunnerDaemonConfig(
            server_url="http://control.invalid",
            node_id="runner-a",
            name="Runner A old PTY",
            state_path=old_state,
        ),
        client=_RunnerClient(),  # type: ignore[arg-type]
        supervisor=old_supervisor,  # type: ignore[arg-type]
        executions=old_repository,  # type: ignore[arg-type]
        terminal_handler=_TerminalHandler(execution),  # type: ignore[arg-type]
    )
    replacement_client = _RunnerClient()
    replacement_terminal = _TerminalHandler()
    replacement_daemon = RunnerDaemon(
        config=RunnerDaemonConfig(
            server_url="http://control.invalid",
            node_id="runner-a",
            name="Runner A replacement PTY",
            state_path=replacement_state,
        ),
        client=replacement_client,  # type: ignore[arg-type]
        supervisor=replacement_supervisor,  # type: ignore[arg-type]
        executions=replacement_repository,  # type: ignore[arg-type]
        terminal_handler=replacement_terminal,  # type: ignore[arg-type]
    )

    try:
        command = _command(
            "replacement-terminal-close",
            RunnerCommandKind.TERMINAL_CLOSE,
            {
                "session_id": "split-brain-terminal",
                "execution_id": "server-terminal-execution",
                "operation_id": "terminal-close:split-brain-terminal",
            },
        )
        await replacement_daemon.handle_command(command)

        assert execution.status is ExecutionStatus.RUNNING
        assert replacement_terminal.cancel_calls == []
        assert replacement_client.statuses == []
        assert replacement_client.finished[0][0:2] == (command.id, False)
        assert "physical termination could not be confirmed" in replacement_client.finished[0][3]
        assert await OperationJournal(replacement_state / "execution-cancellations.json").contains(
            "execution:server-terminal-execution"
        )

        delayed_start = _command(
            "replacement-delayed-terminal-start",
            RunnerCommandKind.TERMINAL_START,
            {
                "session_id": "split-brain-terminal",
                "execution_id": "server-terminal-execution",
                "request": _terminal_start_request(tmp_path),
            },
        )
        await replacement_daemon.handle_command(delayed_start)

        assert execution.status is ExecutionStatus.RUNNING
        assert replacement_terminal.calls == []
        assert replacement_terminal.cancel_calls == []
        assert replacement_client.statuses == []
        assert replacement_client.finished[1][0:2] == (delayed_start.id, True)
        assert replacement_client.finished[1][2]["result"] == {
            "execution_id": "server-terminal-execution",
            "status": "suppressed",
            "suppressed_by_cancellation": True,
            "physical_stop_confirmed": False,
        }
    finally:
        await replacement_daemon.close()
        await old_daemon.close()


@pytest.mark.asyncio
async def test_cancel_reports_both_failures_when_journal_and_process_stop_fail(
    tmp_path: Path,
) -> None:
    execution = _execution(tmp_path)
    repository = _ExecutionRepository(execution)
    supervisor = _FailingSupervisor(execution)
    client = _RunnerClient()
    daemon = _daemon(
        tmp_path,
        client=client,
        supervisor=supervisor,
        repository=repository,
        journal=_FailingCancellationJournal(),
    )
    command = _command(
        "cancel-1",
        RunnerCommandKind.CANCEL,
        {"execution_id": "server-execution-1", "execution_key": execution.execution_key},
    )

    await daemon.handle_command(command)

    assert supervisor.cancel_calls == [execution.id]
    assert execution.status is ExecutionStatus.RUNNING
    assert client.statuses == []
    assert client.finished[0][0:2] == (command.id, False)
    assert "tombstone could not be persisted" in client.finished[0][3]
    assert "process termination also failed" in client.finished[0][3]


@pytest.mark.asyncio
async def test_execute_pre_spawn_guard_observes_tombstone_after_initial_read(
    tmp_path: Path,
) -> None:
    execution = _execution(tmp_path)
    execution.status = ExecutionStatus.STARTING
    repository = _ExecutionRepository(None)
    supervisor = _PreSpawnGuardSupervisor(execution, repository)
    client = _RunnerClient()
    journal = _PreSpawnGuardJournal()
    daemon = _daemon(
        tmp_path,
        client=client,
        supervisor=supervisor,
        repository=repository,
        journal=journal,
    )
    daemon._start_monitor = lambda *_: None  # type: ignore[method-assign]
    execute = _command(
        "execute-blocked-before-spawn",
        RunnerCommandKind.EXECUTE,
        {
            "execution_id": execution.id,
            "request": {
                "execution_key": execution.execution_key,
                "run_id": execution.run_id,
                "node_id": execution.node_id,
                "executor_type": ExecutorType.PROCESS.value,
                "cwd": str(tmp_path),
                "argv": ["echo", "must-not-spawn"],
            },
        },
    )
    cancel = _command(
        "cancel-before-spawn",
        RunnerCommandKind.CANCEL,
        {
            "execution_id": execution.id,
            "execution_key": execution.execution_key,
        },
    )

    execute_task = asyncio.create_task(daemon.handle_command(execute))
    await journal.guard_entered.wait()
    cancel_task = asyncio.create_task(daemon.handle_command(cancel))
    await journal.added.wait()
    journal.release_guard.set()
    await asyncio.gather(execute_task, cancel_task)

    assert supervisor.spawned is False
    assert supervisor.cancel_calls == [execution.id]
    assert execution.status is ExecutionStatus.CANCELLED
    assert client.statuses == [(execution.id, ExecutionStatus.CANCELLED)]
    assert next(item for item in client.finished if item[0] == execute.id)[1] is False
    assert next(item for item in client.finished if item[0] == cancel.id)[1] is True


@pytest.mark.asyncio
async def test_cancel_waits_for_execute_registration_then_stops_spawned_process(
    tmp_path: Path,
) -> None:
    execution = _execution(tmp_path)
    repository = _ExecutionRepository(None)
    supervisor = _BarrierStartSupervisor(execution, repository)
    client = _RunnerClient()
    journal = _SignallingCancellationJournal()
    daemon = _daemon(
        tmp_path,
        client=client,
        supervisor=supervisor,
        repository=repository,
        journal=journal,
    )
    # Output monitoring is orthogonal to this startup/cancellation barrier.
    daemon._start_monitor = lambda *_: None  # type: ignore[method-assign]
    execute = _command(
        "execute-racing-cancel",
        RunnerCommandKind.EXECUTE,
        {
            "execution_id": "server-execution-1",
            "request": {
                "execution_key": execution.execution_key,
                "run_id": execution.run_id,
                "node_id": execution.node_id,
                "executor_type": ExecutorType.PROCESS.value,
                "cwd": str(tmp_path),
                "argv": ["echo", "started"],
            },
        },
    )
    cancel = _command(
        "cancel-racing-execute",
        RunnerCommandKind.CANCEL,
        {
            "execution_id": "server-execution-1",
            "execution_key": execution.execution_key,
        },
    )

    execute_task = asyncio.create_task(daemon.handle_command(execute))
    await supervisor.start_entered.wait()
    cancel_task = asyncio.create_task(daemon.handle_command(cancel))
    await journal.added.wait()

    assert cancel_task.done() is False
    assert supervisor.cancel_calls == []
    assert client.statuses == []

    supervisor.release_start.set()
    await asyncio.gather(execute_task, cancel_task)

    assert supervisor.spawned is True
    assert supervisor.cancel_calls == [execution.id]
    assert execution.status is ExecutionStatus.CANCELLED
    assert client.statuses == [
        ("server-execution-1", ExecutionStatus.CANCELLED),
    ]
    assert next(item for item in client.finished if item[0] == cancel.id)[1] is True


@pytest.mark.asyncio
async def test_daemon_close_cancels_and_joins_leased_execute_handler(
    tmp_path: Path,
) -> None:
    execution = _execution(tmp_path)
    execution.status = ExecutionStatus.STARTING
    repository = _ExecutionRepository(None)
    supervisor = _BarrierStartSupervisor(execution, repository)
    client = _RenewingRunnerClient()
    daemon = _daemon(
        tmp_path,
        client=client,
        supervisor=supervisor,
        repository=repository,
    )
    daemon._start_monitor = lambda *_: None  # type: ignore[method-assign]
    command = _command(
        "execute-pending-during-daemon-close",
        RunnerCommandKind.EXECUTE,
        {
            "execution_id": execution.id,
            "request": {
                "execution_key": execution.execution_key,
                "run_id": execution.run_id,
                "node_id": execution.node_id,
                "executor_type": ExecutorType.PROCESS.value,
                "cwd": str(tmp_path),
                "argv": ["echo", "must-not-spawn-after-close"],
            },
        },
    )

    daemon._start_command(command)
    await supervisor.start_entered.wait()
    await asyncio.wait_for(daemon.close(), timeout=1)

    assert supervisor.start_cancelled.is_set()
    assert supervisor.spawned is False
    assert daemon._command_tasks == {}
    assert client.finished == []


@pytest.mark.asyncio
async def test_cancel_cleanup_survives_handler_cancellation_after_tombstone(
    tmp_path: Path,
) -> None:
    execution = _execution(tmp_path)
    repository = _ExecutionRepository(execution)
    supervisor = _ImmediateStartBlockingCancelSupervisor(execution, repository)
    client = _RunnerClient()
    journal = _SignallingCancellationJournal()
    daemon = _daemon(
        tmp_path,
        client=client,
        supervisor=supervisor,
        repository=repository,
        journal=journal,
    )
    command = _command(
        "cancel-handler-disconnect",
        RunnerCommandKind.CANCEL,
        {
            "execution_id": execution.id,
            "execution_key": execution.execution_key,
        },
    )

    handler = asyncio.create_task(daemon.handle_command(command))
    await journal.added.wait()
    await supervisor.cancel_entered.wait()
    handler.cancel()
    with pytest.raises(asyncio.CancelledError):
        await handler

    assert execution.status is ExecutionStatus.RUNNING
    assert daemon._execution_stop_tasks
    supervisor.release_cancel.set()
    await asyncio.wait_for(supervisor.cancelled.wait(), timeout=1)
    for _ in range(100):
        if not daemon._execution_stop_tasks:
            break
        await asyncio.sleep(0.001)

    assert execution.status is ExecutionStatus.CANCELLED
    assert client.statuses == [(execution.id, ExecutionStatus.CANCELLED)]
    assert daemon._execution_stop_tasks == {}


@pytest.mark.asyncio
async def test_cancel_short_lease_cannot_abort_cleanup_while_execute_status_upload_blocks(
    tmp_path: Path,
) -> None:
    execution = _execution(tmp_path)
    repository = _ExecutionRepository(None)
    supervisor = _ImmediateStartBlockingCancelSupervisor(execution, repository)
    client = _LeaseFailBlockingStatusRunnerClient()
    journal = _SignallingCancellationJournal()
    daemon = _daemon(
        tmp_path,
        client=client,
        supervisor=supervisor,
        repository=repository,
        journal=journal,
    )
    daemon._start_monitor = lambda *_: None  # type: ignore[method-assign]
    execute = _command(
        "execute-blocked-status-upload",
        RunnerCommandKind.EXECUTE,
        {
            "execution_id": execution.id,
            "request": {
                "execution_key": execution.execution_key,
                "run_id": execution.run_id,
                "node_id": execution.node_id,
                "executor_type": ExecutorType.PROCESS.value,
                "cwd": str(tmp_path),
                "argv": ["echo", "admitted"],
            },
        },
    )
    cancel = _command(
        "cancel-short-lease",
        RunnerCommandKind.CANCEL,
        {
            "execution_id": execution.id,
            "execution_key": execution.execution_key,
        },
        lease_id="lease-cancel-short",
        lease_expires_at=datetime.now(UTC) + timedelta(seconds=0.03),
    )

    execute_task = asyncio.create_task(daemon.handle_command(execute))
    await client.running_report_entered.wait()
    cancel_lease_task = asyncio.create_task(daemon._run_leased_command(cancel))
    await journal.added.wait()
    # The local stop enters immediately even though EXECUTE is still blocked
    # uploading RUNNING, proving admission released the execution lock.
    await asyncio.wait_for(supervisor.cancel_entered.wait(), timeout=1)
    await asyncio.wait_for(cancel_lease_task, timeout=1)

    assert execution.status is ExecutionStatus.RUNNING
    assert daemon._execution_stop_tasks
    supervisor.release_cancel.set()
    await asyncio.wait_for(supervisor.cancelled.wait(), timeout=1)
    await asyncio.wait_for(client.cancelled_reported.wait(), timeout=1)

    assert execution.status is ExecutionStatus.CANCELLED
    assert supervisor.cancel_calls == [execution.id]
    assert all(item[0] != cancel.id for item in client.finished)

    client.release_running_report.set()
    await execute_task


@pytest.mark.asyncio
async def test_terminal_admission_releases_lock_before_blocked_status_upload(
    tmp_path: Path,
) -> None:
    execution = _execution(
        tmp_path,
        executor_type=ExecutorType.PTY,
        execution_key="terminal:terminal-blocked-status",
    )
    execution.id = "terminal-blocked-status-execution"
    repository = _ExecutionRepository(None)
    client = _LeaseFailBlockingStatusRunnerClient()
    handler = _BlockingReportTerminalStartHandler(execution, repository, client)
    daemon = _daemon(
        tmp_path,
        client=client,
        supervisor=_Supervisor(execution),
        repository=repository,
        journal=_SignallingCancellationJournal(),
        terminal_handler=handler,
    )
    start = _command(
        "terminal-start-blocked-status",
        RunnerCommandKind.TERMINAL_START,
        {
            "session_id": "terminal-blocked-status",
            "execution_id": execution.id,
            "request": _terminal_start_request(tmp_path),
        },
    )
    cancel = _command(
        "terminal-cancel-blocked-status",
        RunnerCommandKind.CANCEL,
        {
            "execution_id": execution.id,
            "execution_key": execution.execution_key,
        },
    )

    start_task = asyncio.create_task(daemon.handle_command(start))
    await client.running_report_entered.wait()
    cancel_task = asyncio.create_task(daemon.handle_command(cancel))
    for _ in range(100):
        if handler.cancel_calls:
            break
        await asyncio.sleep(0.001)

    assert handler.cancel_calls == [execution.id]
    assert execution.status is ExecutionStatus.CANCELLED
    await cancel_task
    client.release_running_report.set()
    await start_task


@pytest.mark.asyncio
async def test_resume_active_stops_tombstoned_execution_before_starting_monitor(
    tmp_path: Path,
) -> None:
    execution = _execution(tmp_path)
    repository = _ActiveExecutionRepository(execution)
    supervisor = _Supervisor(execution)
    client = _RunnerClient()
    daemon = _daemon(
        tmp_path,
        client=client,
        supervisor=supervisor,
        repository=repository,
        journal=_CancellationJournal({execution.execution_key}),
    )
    monitored: list[tuple[str, str]] = []
    daemon._start_monitor = lambda *ids: monitored.append(ids)  # type: ignore[method-assign]

    await daemon.resume_active()

    assert execution.status is ExecutionStatus.CANCELLED
    assert supervisor.cancel_calls == [execution.id]
    assert monitored == []
    assert client.statuses == [(execution.id, ExecutionStatus.CANCELLED)]


@pytest.mark.asyncio
async def test_resume_active_failure_does_not_starve_later_tombstoned_stop(
    tmp_path: Path,
) -> None:
    lost_terminal = _execution(
        tmp_path,
        executor_type=ExecutorType.PTY,
        execution_key="terminal:lost-handle",
    )
    lost_terminal.id = "lost-terminal-execution"
    live_process = _execution(
        tmp_path,
        execution_key="live-process-after-lost-terminal",
    )
    live_process.id = "live-process-execution"
    repository = _MultipleActiveExecutionRepository([lost_terminal, live_process])
    supervisor = _Supervisor(live_process)
    terminal_handler = _FailingTerminalCancellationHandler(lost_terminal)
    client = _RunnerClient()
    daemon = _daemon(
        tmp_path,
        client=client,
        supervisor=supervisor,
        repository=repository,  # type: ignore[arg-type]
        journal=_CancellationJournal({lost_terminal.execution_key, live_process.execution_key}),
        terminal_handler=terminal_handler,
    )
    monitored: list[tuple[str, str]] = []
    daemon._start_monitor = lambda *ids: monitored.append(ids)  # type: ignore[method-assign]

    await daemon.resume_active()

    assert terminal_handler.cancel_calls == [lost_terminal.id]
    assert lost_terminal.status is ExecutionStatus.RUNNING
    assert supervisor.cancel_calls == [live_process.id]
    assert live_process.status is ExecutionStatus.CANCELLED
    assert client.statuses == [(live_process.id, ExecutionStatus.CANCELLED)]
    assert monitored == []


@pytest.mark.asyncio
async def test_cancel_waits_for_terminal_registration_then_stops_spawned_pty(
    tmp_path: Path,
) -> None:
    execution = _execution(
        tmp_path,
        executor_type=ExecutorType.PTY,
        execution_key="terminal:terminal-racing-cancel",
    )
    execution.id = "server-terminal-execution-1"
    repository = _ExecutionRepository(None)
    supervisor = _Supervisor(execution)
    client = _RunnerClient()
    journal = _SignallingCancellationJournal()
    terminal_handler = _BarrierTerminalStartHandler(execution, repository)
    daemon = _daemon(
        tmp_path,
        client=client,
        supervisor=supervisor,
        repository=repository,
        journal=journal,
        terminal_handler=terminal_handler,
    )
    start = _command(
        "terminal-start-racing-cancel",
        RunnerCommandKind.TERMINAL_START,
        {
            "session_id": "terminal-racing-cancel",
            "execution_id": "server-terminal-execution-1",
            "request": _terminal_start_request(tmp_path),
        },
    )
    cancel = _command(
        "cancel-racing-terminal-start",
        RunnerCommandKind.CANCEL,
        {
            "execution_id": "server-terminal-execution-1",
            "execution_key": execution.execution_key,
        },
    )

    start_task = asyncio.create_task(daemon.handle_command(start))
    await terminal_handler.start_entered.wait()
    cancel_task = asyncio.create_task(daemon.handle_command(cancel))
    await journal.added.wait()

    assert cancel_task.done() is False
    assert terminal_handler.cancel_calls == []
    assert client.statuses == []

    terminal_handler.release_start.set()
    await asyncio.gather(start_task, cancel_task)

    assert terminal_handler.spawned is True
    assert terminal_handler.cancel_calls == [execution.id]
    assert execution.status is ExecutionStatus.CANCELLED
    assert client.statuses == [
        ("server-terminal-execution-1", ExecutionStatus.CANCELLED),
    ]
    assert next(item for item in client.finished if item[0] == cancel.id)[1] is True


@pytest.mark.asyncio
async def test_delayed_terminal_start_is_suppressed_by_cancellation_tombstone(
    tmp_path: Path,
) -> None:
    execution = _execution(tmp_path)
    repository = _ExecutionRepository(None)
    supervisor = _Supervisor(execution)
    client = _RunnerClient()
    terminal_handler = _TerminalHandler()
    journal = _CancellationJournal({"terminal:terminal-1"})
    daemon = _daemon(
        tmp_path,
        client=client,
        supervisor=supervisor,
        repository=repository,
        journal=journal,
        terminal_handler=terminal_handler,
    )
    command = _command(
        "terminal-start-after-cancel",
        RunnerCommandKind.TERMINAL_START,
        {
            "session_id": "terminal-1",
            "execution_id": "server-terminal-execution-1",
            "request": _terminal_start_request(tmp_path),
        },
    )

    await daemon.handle_command(command)

    assert terminal_handler.calls == []
    assert client.statuses == []
    assert client.finished[0][0:2] == (command.id, True)
    assert client.finished[0][2]["result"] == {
        "execution_id": "server-terminal-execution-1",
        "status": "suppressed",
        "suppressed_by_cancellation": True,
        "physical_stop_confirmed": False,
    }


@pytest.mark.asyncio
async def test_cancel_routes_pty_through_terminal_after_persisting_tombstone(
    tmp_path: Path,
) -> None:
    execution = _execution(
        tmp_path,
        executor_type=ExecutorType.PTY,
        execution_key="terminal:terminal-1",
    )
    execution.id = "server-terminal-execution-1"
    repository = _ExecutionRepository(execution)
    supervisor = _Supervisor(execution)
    client = _RunnerClient()
    journal = _CancellationJournal()
    terminal_handler = _TerminalHandler(execution, journal)
    daemon = _daemon(
        tmp_path,
        client=client,
        supervisor=supervisor,
        repository=repository,
        journal=journal,
        terminal_handler=terminal_handler,
    )
    command = _command(
        "cancel-terminal-1",
        RunnerCommandKind.CANCEL,
        {
            "execution_id": "server-terminal-execution-1",
            "execution_key": execution.execution_key,
        },
    )

    await daemon.handle_command(command)

    assert journal.operations == {f"execution:{execution.id}"}
    assert terminal_handler.cancel_calls == [execution.id]
    assert supervisor.cancel_calls == []
    assert execution.status is ExecutionStatus.CANCELLED
    assert client.statuses == [("server-terminal-execution-1", ExecutionStatus.CANCELLED)]
    assert client.status_details[0]["physical_stop_confirmed"] is True
    assert client.finished[0][0:2] == (command.id, True)


@pytest.mark.asyncio
async def test_cancel_does_not_ack_terminal_without_cancelled_stop_evidence(
    tmp_path: Path,
) -> None:
    execution = _execution(
        tmp_path,
        executor_type=ExecutorType.PTY,
        execution_key="terminal:unattached-exited-conpty",
    )
    execution.id = "unattached-exited-conpty-execution"
    execution.status = ExecutionStatus.EXITED
    repository = _ExecutionRepository(execution)
    client = _RunnerClient()
    terminal_handler = _TerminalHandler(execution)
    daemon = _daemon(
        tmp_path,
        client=client,
        supervisor=_Supervisor(execution),
        repository=repository,
        journal=_CancellationJournal(),
        terminal_handler=terminal_handler,
    )
    command = _command(
        "cancel-unattached-exited-conpty",
        RunnerCommandKind.CANCEL,
        {
            "execution_id": execution.id,
            "execution_key": execution.execution_key,
        },
    )

    await daemon.handle_command(command)

    assert terminal_handler.cancel_calls == [execution.id]
    assert execution.status is ExecutionStatus.EXITED
    assert client.statuses == []
    assert client.finished[0][0:2] == (command.id, False)
    assert "durable physical-stop proof" in client.finished[0][3]
    assert "physical_stop_confirmed" not in client.finished[0][2]


@pytest.mark.asyncio
async def test_legacy_terminal_close_also_uses_durable_pty_cancellation(
    tmp_path: Path,
) -> None:
    execution = _execution(
        tmp_path,
        executor_type=ExecutorType.PTY,
        execution_key="terminal:terminal-legacy",
        legacy=True,
    )
    execution.id = "server-terminal-execution-legacy"
    repository = _ExecutionRepository(execution)
    supervisor = _Supervisor(execution)
    client = _RunnerClient()
    journal = _CancellationJournal()
    terminal_handler = _TerminalHandler(execution, journal)
    daemon = _daemon(
        tmp_path,
        client=client,
        supervisor=supervisor,
        repository=repository,
        journal=journal,
        terminal_handler=terminal_handler,
    )
    command = _command(
        "legacy-terminal-close",
        RunnerCommandKind.TERMINAL_CLOSE,
        {
            "session_id": "terminal-legacy",
            "execution_id": "server-terminal-execution-legacy",
            "operation_id": "terminal-close:terminal-legacy",
        },
    )

    await daemon.handle_command(command)

    assert journal.operations == {f"execution:{execution.id}"}
    assert terminal_handler.cancel_calls == [execution.id]
    assert supervisor.cancel_calls == []
    assert client.statuses == []
    assert client.finished[0][0:2] == (command.id, True)
    assert client.finished[0][2]["session_id"] == "terminal-legacy"


@pytest.mark.asyncio
async def test_persisted_terminal_cancellation_suppresses_out_of_order_start_after_reconnect(
    tmp_path: Path,
) -> None:
    execution = _execution(tmp_path)
    repository = _ExecutionRepository(None)
    supervisor = _Supervisor(execution)
    journal_path = tmp_path / "runner" / "execution-cancellations.json"
    first_client = _RunnerClient()
    first_daemon = _daemon(
        tmp_path,
        client=first_client,
        supervisor=supervisor,
        repository=repository,
        journal=OperationJournal(journal_path),
    )
    cancel = _command(
        "cancel-before-terminal-start",
        RunnerCommandKind.CANCEL,
        {
            "execution_id": "server-terminal-execution-1",
            "execution_key": "terminal:terminal-reconnect",
        },
    )

    await first_daemon.handle_command(cancel)

    reloaded_journal = OperationJournal(journal_path)
    assert await reloaded_journal.contains("execution:server-terminal-execution-1")
    second_client = _RunnerClient()
    terminal_handler = _TerminalHandler()
    reconnected_daemon = _daemon(
        tmp_path,
        client=second_client,
        supervisor=supervisor,
        repository=repository,
        journal=reloaded_journal,
        terminal_handler=terminal_handler,
    )
    delayed_start = _command(
        "terminal-start-after-reconnect",
        RunnerCommandKind.TERMINAL_START,
        {
            "session_id": "terminal-reconnect",
            "execution_id": "server-terminal-execution-1",
            "request": _terminal_start_request(tmp_path),
        },
    )

    await reconnected_daemon.handle_command(delayed_start)

    assert terminal_handler.calls == []
    assert second_client.statuses == []
    assert second_client.finished[0][0:2] == (delayed_start.id, True)
    assert second_client.finished[0][2]["result"] == {
        "execution_id": "server-terminal-execution-1",
        "status": "suppressed",
        "suppressed_by_cancellation": True,
        "physical_stop_confirmed": False,
    }


@pytest.mark.asyncio
async def test_target_http_short_lease_replay_never_sends_non_idempotent_request_twice(
    tmp_path: Path,
) -> None:
    execution = _execution(tmp_path)
    client = _FailingRenewRunnerClient()
    runner = _SentThenBlockingTargetHttpRunner()
    daemon = _daemon(
        tmp_path,
        client=client,
        supervisor=_Supervisor(execution),
        repository=_ExecutionRepository(None),
        target_http_handler=runner,
    )
    payload = _target_http_payload()
    first_lease = _command(
        "target-http-short-lease",
        RunnerCommandKind.TARGET_HTTP,
        payload,
        lease_id="lease-target-http-short-lease-1",
        lease_duration_seconds=0.05,
    )

    first_delivery = asyncio.create_task(daemon._run_leased_command(first_lease))
    await asyncio.wait_for(runner.sent.wait(), timeout=1)
    await asyncio.wait_for(first_delivery, timeout=1)
    assert client.finished == []

    replayed_lease = _command(
        first_lease.id,
        first_lease.kind,
        first_lease.payload,
        lease_id="lease-target-http-short-lease-2",
        attempts=2,
        lease_duration_seconds=0.05,
    )
    await asyncio.wait_for(daemon._run_leased_command(replayed_lease), timeout=1)

    assert client.renew_calls >= 1
    assert runner.send_count == 1
    assert len(client.finished) == 1
    assert client.finished[0][0:2] == (replayed_lease.id, False)
    assert "delivery was already claimed" in client.finished[0][3]
    assert "physical outcome is unconfirmed" in client.finished[0][3]
    assert await OperationJournal(tmp_path / "runner" / "target-http-deliveries.json").contains(
        "target-http:run-1:tool-call-1"
    )

    divergent_replay = _command(
        first_lease.id,
        first_lease.kind,
        first_lease.payload,
        binding_id="binding-target-http-divergent",
    )
    await daemon.handle_command(divergent_replay)
    assert runner.send_count == 1
    assert client.finished[-1][0:2] == (divergent_replay.id, False)
    assert "conflicts with its durable journal record" in client.finished[-1][3]
    await daemon.close()


@pytest.mark.asyncio
async def test_target_http_cancel_preempts_inflight_request_and_suppresses_replay(
    tmp_path: Path,
) -> None:
    execution = _execution(tmp_path)
    repository = _ExecutionRepository(None)
    supervisor = _Supervisor(execution)
    client = _RunnerClient()
    runner = _BlockingTargetHttpRunner()
    daemon = _daemon(
        tmp_path,
        client=client,
        supervisor=supervisor,
        repository=repository,
        target_http_handler=runner,
    )
    request = TargetHttpRequest(
        execution_key="target-http-key",
        method="POST",
        url="https://target.internal/probe",
        timeout_seconds=30,
    )
    launch = TargetHttpRunnerRequest(
        run_id="run-1",
        session_id="session-1",
        tool_call_id="tool-call-1",
        node_id="runner-a",
        scope=Scope(domains=["target.internal"]),
        request=request,
    )
    target_payload = {
        "launch": {
            **launch.model_dump(mode="json", exclude={"request"}),
            "request": request.runner_payload(),
        },
        "max_response_bytes": request.max_response_bytes,
    }
    running = asyncio.create_task(
        daemon.handle_command(
            _command("target-http-running", RunnerCommandKind.TARGET_HTTP, target_payload)
        )
    )
    await runner.entered.wait()

    cancel = _command(
        "target-http-cancel",
        RunnerCommandKind.TARGET_HTTP_CANCEL,
        {"run_id": "run-1", "tool_call_ids": ["tool-call-1"]},
    )
    await daemon.handle_command(cancel)
    with pytest.raises(asyncio.CancelledError):
        await running

    cancel_finish = next(item for item in client.finished if item[0] == cancel.id)
    assert cancel_finish[1] is True
    assert cancel_finish[2]["outcomes"][0]["confirmed"] is True  # type: ignore[index]
    original_finish = next(item for item in client.finished if item[0] == "target-http-running")
    assert original_finish[1] is False
    assert "preempted by a safety stop" in original_finish[3]

    await daemon.handle_command(
        _command("target-http-replayed", RunnerCommandKind.TARGET_HTTP, target_payload)
    )
    replay_finish = next(item for item in client.finished if item[0] == "target-http-replayed")
    assert replay_finish[1] is False
    assert "cancelled on this Runner" in replay_finish[3]
    assert runner.execute_calls == 1


@pytest.mark.asyncio
async def test_target_http_late_cancel_ack_retry_reuses_durable_physical_confirmation(
    tmp_path: Path,
) -> None:
    execution = _execution(tmp_path)
    runner = _BlockingTargetHttpRunner()
    delayed_client = _DelayedFinishRunnerClient("target-http-cancel-late-ack")
    daemon = _daemon(
        tmp_path,
        client=delayed_client,
        supervisor=_Supervisor(execution),
        repository=_ExecutionRepository(None),
        target_http_handler=runner,
    )
    running = asyncio.create_task(
        daemon.handle_command(
            _command(
                "target-http-running-before-late-ack",
                RunnerCommandKind.TARGET_HTTP,
                _target_http_payload(),
            )
        )
    )
    await runner.entered.wait()
    first_cancel = _command(
        "target-http-cancel-late-ack",
        RunnerCommandKind.TARGET_HTTP_CANCEL,
        {"run_id": "run-1", "tool_call_ids": ["tool-call-1"]},
    )
    first_cancel_task = asyncio.create_task(daemon.handle_command(first_cancel))
    await asyncio.wait_for(delayed_client.finish_entered.wait(), timeout=1)
    with pytest.raises(asyncio.CancelledError):
        await running

    confirmation_path = tmp_path / "runner" / "target-http-stop-confirmations.json"
    assert await OperationJournal(confirmation_path).contains("target-http:run-1:tool-call-1")
    assert first_cancel_task.done() is False

    retry_runner = _BlockingTargetHttpRunner()
    retry_client = _RunnerClient()
    retry_daemon = _daemon(
        tmp_path,
        client=retry_client,
        supervisor=_Supervisor(execution),
        repository=_ExecutionRepository(None),
        target_http_handler=retry_runner,
    )
    exact_replay = _command(
        first_cancel.id,
        first_cancel.kind,
        first_cancel.payload,
    )
    await retry_daemon.handle_command(exact_replay)
    assert retry_client.finished[-1][0:2] == (exact_replay.id, True)
    assert retry_runner.stop_calls == []

    divergent_replay = _command(
        first_cancel.id,
        first_cancel.kind,
        first_cancel.payload,
        binding_id="binding-target-http-cancel-divergent",
    )
    await retry_daemon.handle_command(divergent_replay)
    assert retry_client.finished[-1][0:2] == (divergent_replay.id, False)
    assert "conflicts with its durable journal record" in retry_client.finished[-1][3]
    assert retry_runner.stop_calls == []

    retry = _command(
        "target-http-cancel-late-ack-retry",
        RunnerCommandKind.TARGET_HTTP_CANCEL,
        {"run_id": "run-1", "tool_call_ids": ["tool-call-1"]},
    )
    await retry_daemon.handle_command(retry)

    retry_finish = next(item for item in retry_client.finished if item[0] == retry.id)
    retry_outcome = retry_finish[2]["outcomes"][0]  # type: ignore[index]
    assert retry_outcome == {
        "tool_call_id": "tool-call-1",
        "confirmed": True,
        "reason": "target_http_local_task_termination_previously_confirmed",
    }
    assert retry_runner.stop_calls == []

    delayed_client.release_finish.set()
    await first_cancel_task
    original_outcome = next(item for item in delayed_client.finished if item[0] == first_cancel.id)[
        2
    ]["outcomes"][0]  # type: ignore[index]
    assert original_outcome["confirmed"] is True  # type: ignore[index]
    await retry_daemon.close()
    await daemon.close()


@pytest.mark.asyncio
async def test_target_http_stop_confirmation_is_not_shared_across_split_brain_state_paths(
    tmp_path: Path,
) -> None:
    execution = _execution(tmp_path)
    owner_root = tmp_path / "owner"
    replacement_root = tmp_path / "replacement"
    owner_runner = _BlockingTargetHttpRunner()
    owner_client = _RunnerClient()
    owner = _daemon(
        owner_root,
        client=owner_client,
        supervisor=_Supervisor(execution),
        repository=_ExecutionRepository(None),
        target_http_handler=owner_runner,
    )
    running = asyncio.create_task(
        owner.handle_command(
            _command(
                "target-http-owned-by-peer",
                RunnerCommandKind.TARGET_HTTP,
                _target_http_payload(),
            )
        )
    )
    await owner_runner.entered.wait()
    owner_cancel = _command(
        "target-http-owner-cancel",
        RunnerCommandKind.TARGET_HTTP_CANCEL,
        {"run_id": "run-1", "tool_call_ids": ["tool-call-1"]},
    )
    await owner.handle_command(owner_cancel)
    with pytest.raises(asyncio.CancelledError):
        await running
    assert owner_client.finished[-1][2]["outcomes"][0]["confirmed"] is True  # type: ignore[index]

    replacement_runner = _BlockingTargetHttpRunner()
    replacement_client = _RunnerClient()
    replacement = _daemon(
        replacement_root,
        client=replacement_client,
        supervisor=_Supervisor(execution),
        repository=_ExecutionRepository(None),
        target_http_handler=replacement_runner,
    )
    replacement_cancel = _command(
        "target-http-replacement-cancel",
        RunnerCommandKind.TARGET_HTTP_CANCEL,
        {"run_id": "run-1", "tool_call_ids": ["tool-call-1"]},
    )
    await replacement.handle_command(replacement_cancel)

    replacement_outcome = replacement_client.finished[0][2]["outcomes"][0]  # type: ignore[index]
    assert replacement_outcome == {
        "tool_call_id": "tool-call-1",
        "confirmed": False,
        "reason": "target_http_local_task_not_registered",
    }
    assert replacement_runner.stop_calls == [("run-1", ("tool-call-1",))]
    assert not await OperationJournal(
        replacement_root / "runner" / "target-http-stop-confirmations.json"
    ).contains("target-http:run-1:tool-call-1")
    await replacement.close()
    await owner.close()


@pytest.mark.asyncio
async def test_target_http_cancel_without_local_task_stays_unconfirmed(
    tmp_path: Path,
) -> None:
    execution = _execution(tmp_path)
    client = _RunnerClient()
    runner = _BlockingTargetHttpRunner()
    daemon = _daemon(
        tmp_path,
        client=client,
        supervisor=_Supervisor(execution),
        repository=_ExecutionRepository(None),
        target_http_handler=runner,
    )
    cancel = _command(
        "target-http-cancel-no-local-task",
        RunnerCommandKind.TARGET_HTTP_CANCEL,
        {"run_id": "run-1", "tool_call_ids": ["tool-call-1"]},
    )

    await daemon.handle_command(cancel)

    finish = client.finished[0]
    assert finish[0:2] == (cancel.id, True)
    assert finish[2]["outcomes"] == [
        {
            "tool_call_id": "tool-call-1",
            "confirmed": False,
            "reason": "target_http_local_task_not_registered",
        }
    ]


@pytest.mark.asyncio
async def test_target_http_cancel_journal_failure_never_returns_confirmed_ack(
    tmp_path: Path,
) -> None:
    execution = _execution(tmp_path)
    client = _RunnerClient()
    daemon = _daemon(
        tmp_path,
        client=client,
        supervisor=_Supervisor(execution),
        repository=_ExecutionRepository(None),
        target_http_handler=_BlockingTargetHttpRunner(),
        target_http_journal=_FailingCancellationJournal(),
    )
    cancel = _command(
        "target-http-cancel-journal-failure",
        RunnerCommandKind.TARGET_HTTP_CANCEL,
        {"run_id": "run-1", "tool_call_ids": ["tool-call-1"]},
    )

    await daemon.handle_command(cancel)

    outcome = client.finished[0][2]["outcomes"][0]  # type: ignore[index]
    assert outcome["confirmed"] is False  # type: ignore[index]
    assert "tombstone could not be persisted" in outcome["reason"]  # type: ignore[index]


@pytest.mark.asyncio
async def test_target_http_confirmation_journal_failure_does_not_skip_physical_stop(
    tmp_path: Path,
) -> None:
    execution = _execution(tmp_path)
    client = _RunnerClient()
    runner = _BlockingTargetHttpRunner()
    daemon = _daemon(
        tmp_path,
        client=client,
        supervisor=_Supervisor(execution),
        repository=_ExecutionRepository(None),
        target_http_handler=runner,
        target_http_confirmation_journal=_FailingConfirmationJournal(),
    )
    cancel = _command(
        "target-http-cancel-confirmation-journal-failure",
        RunnerCommandKind.TARGET_HTTP_CANCEL,
        {"run_id": "run-1", "tool_call_ids": ["tool-call-1"]},
    )

    await daemon.handle_command(cancel)

    assert runner.stop_calls == [("run-1", ("tool-call-1",))]
    outcome = client.finished[0][2]["outcomes"][0]  # type: ignore[index]
    assert outcome == {
        "tool_call_id": "tool-call-1",
        "confirmed": False,
        "reason": "target_http_local_task_not_registered",
    }


@pytest.mark.asyncio
async def test_browser_close_preempts_inflight_operation_and_persists_tombstone(
    tmp_path: Path,
) -> None:
    execution = _execution(tmp_path)
    repository = _ExecutionRepository(None)
    supervisor = _Supervisor(execution)
    client = _SlowPreemptionRunnerClient("browser-observe-running")
    session = _browser_session()
    runner = _BlockingBrowserRunner(session)
    daemon = _daemon(
        tmp_path,
        client=client,
        supervisor=supervisor,
        repository=repository,
        browser_handler=runner,
    )
    observe = _command(
        "browser-observe-running",
        RunnerCommandKind.BROWSER,
        {
            "operation": "observe",
            "command": {
                "session_id": session.id,
                "run_id": session.run_id,
                "node_id": session.node_id,
            },
        },
    )
    running = asyncio.create_task(daemon.handle_command(observe))
    await runner.observe_entered.wait()

    close = _command(
        "browser-close",
        RunnerCommandKind.BROWSER_CLOSE,
        {
            "operation": "close",
            "command": BrowserSessionCommand(
                session_id=session.id,
                session=session,
            ).model_dump(mode="json"),
        },
    )
    await daemon.handle_command(close)
    await client.preemption_finish_entered.wait()
    assert running.done() is False
    client.release_preemption_finish.set()
    with pytest.raises(asyncio.CancelledError):
        await running

    close_finish = next(item for item in client.finished if item[0] == close.id)
    assert close_finish[1] is True
    assert close_finish[2]["result"]["session"]["status"] == "closed"  # type: ignore[index]
    observe_finish = next(item for item in client.finished if item[0] == observe.id)
    assert observe_finish[1] is False
    assert runner.close_calls == 1

    exact_replay = _command(
        close.id,
        close.kind,
        close.payload,
    )
    await daemon.handle_command(exact_replay)
    assert client.finished[-1][0:2] == (exact_replay.id, True)
    assert runner.close_calls == 1

    fresh_retry = _command(
        "browser-close-fresh-retry",
        close.kind,
        close.payload,
    )
    await daemon.handle_command(fresh_retry)
    assert client.finished[-1][0:2] == (fresh_retry.id, True)
    assert runner.close_calls == 1

    divergent_replay = _command(
        close.id,
        close.kind,
        close.payload,
        binding_id="binding-browser-close-divergent",
    )
    await daemon.handle_command(divergent_replay)
    assert client.finished[-1][0:2] == (divergent_replay.id, False)
    assert "conflicts with its durable journal record" in client.finished[-1][3]
    assert runner.close_calls == 1

    reconnected_client = _RunnerClient()
    reconnected = _daemon(
        tmp_path,
        client=reconnected_client,
        supervisor=supervisor,
        repository=repository,
        browser_handler=runner,
    )
    delayed = _command(
        "browser-delayed-open",
        RunnerCommandKind.BROWSER,
        {
            "operation": "open",
            "command": {
                "session_id": session.id,
                "run_id": session.run_id,
                "node_id": session.node_id,
            },
        },
    )
    await reconnected.handle_command(delayed)

    assert reconnected_client.finished[0][1] is False
    assert "cancelled on this Runner" in reconnected_client.finished[0][3]


@pytest.mark.asyncio
async def test_browser_close_survives_command_lease_loss_after_tombstone(
    tmp_path: Path,
) -> None:
    execution = _execution(tmp_path)
    session = _browser_session()
    runner = _LeaseBlockingBrowserRunner(session)
    client = _FailingRenewRunnerClient()
    daemon = _daemon(
        tmp_path,
        client=client,
        supervisor=_Supervisor(execution),
        repository=_ExecutionRepository(None),
        browser_handler=runner,
    )
    command = _command(
        "browser-close-short-lease",
        RunnerCommandKind.BROWSER_CLOSE,
        {
            "operation": "close",
            "command": BrowserSessionCommand(
                session_id=session.id,
                session=session,
            ).model_dump(mode="json"),
        },
        lease_duration_seconds=0.03,
    )

    lease_task = asyncio.create_task(daemon._run_leased_command(command))
    await asyncio.wait_for(runner.close_entered.wait(), timeout=1)
    await asyncio.wait_for(lease_task, timeout=1)

    journal = OperationJournal(tmp_path / "runner" / "browser-cancellations.json")
    assert await journal.contains(f"browser:{session.id}")
    assert daemon._resource_stop_tasks
    assert runner.session.status is BrowserSessionStatus.ACTIVE

    runner.release_close.set()
    for _ in range(100):
        if not daemon._resource_stop_tasks:
            break
        await asyncio.sleep(0.001)

    assert runner.session.status is BrowserSessionStatus.CLOSED
    assert command.id not in {item[0] for item in client.finished}
    tombstone = await journal.get_resource(f"browser:{session.id}")
    assert tombstone is not None
    assert tombstone.outcome.get("state") == "physical_stop_confirmed"
    await daemon.close()


@pytest.mark.asyncio
async def test_browser_close_without_local_session_remains_unconfirmed(
    tmp_path: Path,
) -> None:
    execution = _execution(tmp_path)
    repository = _ExecutionRepository(None)
    supervisor = _Supervisor(execution)
    client = _RunnerClient()
    runner = _MissingBrowserRunner()
    journal = _CancellationJournal()
    session = _browser_session()
    daemon = _daemon(
        tmp_path,
        client=client,
        supervisor=supervisor,
        repository=repository,
        browser_handler=runner,
        browser_journal=journal,
    )
    close = _command(
        "browser-close-without-local-session",
        RunnerCommandKind.BROWSER_CLOSE,
        {
            "operation": "close",
            "command": BrowserSessionCommand(
                session_id=session.id,
                session=session,
            ).model_dump(mode="json"),
        },
    )

    await daemon.handle_command(close)

    assert journal.operations == {f"browser:{session.id}"}
    assert runner.close_calls == 1
    close_finish = next(item for item in client.finished if item[0] == close.id)
    assert close_finish[1] is False
    assert "physical close could not be confirmed" in close_finish[3]

    await daemon.handle_command(
        _command(
            "browser-open-after-unconfirmed-close",
            RunnerCommandKind.BROWSER,
            {
                "operation": "open",
                "command": {
                    "session_id": session.id,
                    "run_id": session.run_id,
                    "node_id": session.node_id,
                },
            },
        )
    )
    assert runner.open_calls == 0


@pytest.mark.asyncio
async def test_run_loop_keeps_safety_poll_channel_open_at_regular_capacity(
    tmp_path: Path,
) -> None:
    execution = _execution(tmp_path)
    repository = _ExecutionRepository(None)
    supervisor = _Supervisor(execution)
    session = _browser_session()
    runner = _BlockingBrowserRunner(session)
    observe = _command(
        "browser-observe-running",
        RunnerCommandKind.BROWSER,
        {
            "operation": "observe",
            "command": {
                "session_id": session.id,
                "run_id": session.run_id,
                "node_id": session.node_id,
            },
        },
    )
    close = _command(
        "browser-close",
        RunnerCommandKind.BROWSER_CLOSE,
        {
            "operation": "close",
            "command": BrowserSessionCommand(
                session_id=session.id,
                session=session,
            ).model_dump(mode="json"),
        },
    )
    client = _PollingRunnerClient([observe, close])
    daemon = RunnerDaemon(
        config=RunnerDaemonConfig(
            server_url="http://control.invalid",
            node_id="runner-a",
            name="Runner A",
            state_path=tmp_path / "runner",
            poll_wait_seconds=0.01,
            max_concurrent_commands=1,
        ),
        client=client,  # type: ignore[arg-type]
        supervisor=supervisor,  # type: ignore[arg-type]
        executions=repository,  # type: ignore[arg-type]
        browser_handler=runner,  # type: ignore[arg-type]
    )

    run_task = asyncio.create_task(daemon.run_forever())
    await asyncio.wait_for(client.close_acknowledged.wait(), timeout=1)

    assert client.poll_modes[:2] == [False, True]
    assert runner.observe_calls == 1
    assert runner.close_calls == 1
    await daemon.close()
    await asyncio.wait_for(run_task, timeout=1)


@pytest.mark.asyncio
async def test_long_runner_command_renews_lease_until_handler_finishes(
    tmp_path: Path,
) -> None:
    execution = _terminal_execution(tmp_path, "terminal-lease-long")
    repository = _ExecutionRepository(execution)
    supervisor = _Supervisor(execution)
    client = _RenewingRunnerClient()
    terminal = _BlockingTerminalHandler()
    daemon = RunnerDaemon(
        config=RunnerDaemonConfig(
            server_url="http://control.invalid",
            node_id="runner-a",
            name="Runner A",
            state_path=tmp_path / "runner",
        ),
        client=client,  # type: ignore[arg-type]
        supervisor=supervisor,  # type: ignore[arg-type]
        executions=repository,  # type: ignore[arg-type]
        terminal_handler=terminal,  # type: ignore[arg-type]
    )
    command = _command(
        "terminal-write-long",
        RunnerCommandKind.TERMINAL_WRITE,
        {
            "session_id": execution.session_id,
            "execution_id": execution.id,
            "operation_id": "write-1",
            "data": "eA==",
        },
        lease_id="lease-terminal-write-long",
        # The Control Plane may use a shorter lease than the Runner's local
        # default. Renewal must honor the leased command's actual expiry.
        lease_expires_at=datetime.now(UTC) + timedelta(seconds=0.06),
    )

    task = asyncio.create_task(daemon._run_leased_command(command))
    await terminal.entered.wait()
    await asyncio.wait_for(client.renewed.wait(), timeout=1)
    terminal.release.set()
    await task

    assert client.renew_calls >= 1
    assert client.finished[-1][0:2] == (command.id, True)


@pytest.mark.asyncio
async def test_blocked_renewal_cancels_handler_at_current_lease_deadline(
    tmp_path: Path,
) -> None:
    execution = _terminal_execution(tmp_path, "terminal-lease-blocked")
    repository = _ExecutionRepository(execution)
    client = _BlockingRenewRunnerClient()
    terminal = _CancellationAwareBlockingTerminalHandler()
    daemon = RunnerDaemon(
        config=RunnerDaemonConfig(
            server_url="http://control.invalid",
            node_id="runner-a",
            name="Runner A",
            state_path=tmp_path / "runner",
        ),
        client=client,  # type: ignore[arg-type]
        supervisor=_Supervisor(execution),  # type: ignore[arg-type]
        executions=repository,  # type: ignore[arg-type]
        terminal_handler=terminal,  # type: ignore[arg-type]
    )
    command = _command(
        "terminal-write-blocked-renewal",
        RunnerCommandKind.TERMINAL_WRITE,
        {
            "session_id": execution.session_id,
            "execution_id": execution.id,
            "operation_id": "write-blocked-renewal",
            "data": "eA==",
        },
        lease_id="lease-terminal-write-blocked-renewal",
        lease_duration_seconds=0.03,
    )

    task = asyncio.create_task(daemon._run_leased_command(command))
    await asyncio.wait_for(terminal.entered.wait(), timeout=1)
    await asyncio.wait_for(client.renew_entered.wait(), timeout=1)
    await asyncio.wait_for(task, timeout=1)

    assert client.renew_cancelled.is_set()
    assert terminal.cancelled.is_set()
    assert not terminal.completed.is_set()
    assert client.finished == []

    terminal.release.set()
    client.release_renew.set()
    await asyncio.sleep(0)
    assert not terminal.completed.is_set()


@pytest.mark.asyncio
async def test_natural_lease_expiry_cancels_handler_without_finish(tmp_path: Path) -> None:
    execution = _terminal_execution(tmp_path, "terminal-lease-natural-expiry")
    repository = _ExecutionRepository(execution)
    client = _RenewingRunnerClient()
    terminal = _CancellationAwareBlockingTerminalHandler()
    daemon = RunnerDaemon(
        config=RunnerDaemonConfig(
            server_url="http://control.invalid",
            node_id="runner-a",
            name="Runner A",
            state_path=tmp_path / "runner",
        ),
        client=client,  # type: ignore[arg-type]
        supervisor=_Supervisor(execution),  # type: ignore[arg-type]
        executions=repository,  # type: ignore[arg-type]
        terminal_handler=terminal,  # type: ignore[arg-type]
    )
    command = _command(
        "terminal-write-natural-lease-expiry",
        RunnerCommandKind.TERMINAL_WRITE,
        {
            "session_id": execution.session_id,
            "execution_id": execution.id,
            "operation_id": "write-natural-expiry",
            "data": "eA==",
        },
        lease_id="lease-terminal-write-natural-expiry",
        # Shorter than the minimum renewal interval, forcing the local
        # deadline to expire before the first renewal attempt.
        lease_duration_seconds=0.005,
    )

    task = asyncio.create_task(daemon._run_leased_command(command))
    await asyncio.wait_for(terminal.entered.wait(), timeout=1)
    await asyncio.wait_for(task, timeout=1)

    assert client.renew_calls == 0
    assert terminal.cancelled.is_set()
    assert not terminal.completed.is_set()
    assert client.finished == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status_code",
    [None, 503],
    ids=["network-error", "server-error"],
)
async def test_retryable_renewal_failures_reaching_deadline_cancel_without_finish(
    tmp_path: Path,
    status_code: int | None,
) -> None:
    execution = _terminal_execution(tmp_path, "terminal-lease-renew-failure")
    repository = _ExecutionRepository(execution)
    client = (
        _FailingRenewRunnerClient()
        if status_code is None
        else _RejectedRenewRunnerClient(status_code)
    )
    terminal = _CancellationAwareBlockingTerminalHandler()
    daemon = RunnerDaemon(
        config=RunnerDaemonConfig(
            server_url="http://control.invalid",
            node_id="runner-a",
            name="Runner A",
            state_path=tmp_path / "runner",
        ),
        client=client,  # type: ignore[arg-type]
        supervisor=_Supervisor(execution),  # type: ignore[arg-type]
        executions=repository,  # type: ignore[arg-type]
        terminal_handler=terminal,  # type: ignore[arg-type]
    )
    command = _command(
        "terminal-write-failed-renewals",
        RunnerCommandKind.TERMINAL_WRITE,
        {
            "session_id": execution.session_id,
            "execution_id": execution.id,
            "operation_id": "write-failed-renewals",
            "data": "eA==",
        },
        lease_id="lease-terminal-write-failed-renewals",
        lease_duration_seconds=0.03,
    )

    task = asyncio.create_task(daemon._run_leased_command(command))
    await asyncio.wait_for(terminal.entered.wait(), timeout=1)
    await asyncio.wait_for(task, timeout=1)

    assert client.renew_calls >= 1
    assert terminal.cancelled.is_set()
    assert not terminal.completed.is_set()
    assert client.finished == []


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [401, 404, 409])
async def test_rejected_renewal_immediately_cancels_handler_without_finish(
    tmp_path: Path,
    status_code: int,
) -> None:
    execution = _terminal_execution(tmp_path, "terminal-lease-rejected")
    repository = _ExecutionRepository(execution)
    client = _RejectedRenewRunnerClient(status_code)
    terminal = _CancellationAwareBlockingTerminalHandler()
    daemon = RunnerDaemon(
        config=RunnerDaemonConfig(
            server_url="http://control.invalid",
            node_id="runner-a",
            name="Runner A",
            state_path=tmp_path / "runner",
        ),
        client=client,  # type: ignore[arg-type]
        supervisor=_Supervisor(execution),  # type: ignore[arg-type]
        executions=repository,  # type: ignore[arg-type]
        terminal_handler=terminal,  # type: ignore[arg-type]
    )
    command = _command(
        f"terminal-write-rejected-lease-{status_code}",
        RunnerCommandKind.TERMINAL_WRITE,
        {
            "session_id": execution.session_id,
            "execution_id": execution.id,
            "operation_id": "write-reclaimed-lease",
            "data": "eA==",
        },
        lease_id=f"lease-terminal-write-rejected-{status_code}",
        lease_duration_seconds=0.3,
    )

    task = asyncio.create_task(daemon._run_leased_command(command))
    await asyncio.wait_for(terminal.entered.wait(), timeout=1)
    await asyncio.wait_for(client.renew_entered.wait(), timeout=1)
    await asyncio.wait_for(task, timeout=0.15)

    assert client.rejected_at is not None
    assert client.renew_calls == 1
    assert asyncio.get_running_loop().time() - client.rejected_at < 0.15
    assert terminal.cancelled.is_set()
    assert not terminal.completed.is_set()
    assert client.finished == []


@pytest.mark.asyncio
@pytest.mark.parametrize("raise_after_cancel", [False, True], ids=["success", "error"])
async def test_lease_loss_suppresses_all_finish_paths_when_handler_transforms_cancel(
    tmp_path: Path,
    raise_after_cancel: bool,
) -> None:
    execution = _terminal_execution(tmp_path, "terminal-lease-transformed")
    repository = _ExecutionRepository(execution)
    client = _BlockingRenewRunnerClient()
    terminal = _CancellationTransformingTerminalHandler(
        raise_after_cancel=raise_after_cancel,
    )
    daemon = RunnerDaemon(
        config=RunnerDaemonConfig(
            server_url="http://control.invalid",
            node_id="runner-a",
            name="Runner A",
            state_path=tmp_path / "runner",
        ),
        client=client,  # type: ignore[arg-type]
        supervisor=_Supervisor(execution),  # type: ignore[arg-type]
        executions=repository,  # type: ignore[arg-type]
        terminal_handler=terminal,  # type: ignore[arg-type]
    )
    command = _command(
        f"terminal-write-transformed-cancel-{raise_after_cancel}",
        RunnerCommandKind.TERMINAL_WRITE,
        {
            "session_id": execution.session_id,
            "execution_id": execution.id,
            "operation_id": "write-transform-cancel",
            "data": "eA==",
        },
        lease_id=f"lease-terminal-write-transformed-cancel-{raise_after_cancel}",
        lease_duration_seconds=0.03,
    )

    task = asyncio.create_task(daemon._run_leased_command(command))
    await asyncio.wait_for(terminal.entered.wait(), timeout=1)
    await asyncio.wait_for(client.renew_entered.wait(), timeout=1)
    await asyncio.wait_for(task, timeout=1)

    assert client.renew_cancelled.is_set()
    assert terminal.transformed.is_set()
    assert client.finished == []


@pytest.mark.asyncio
async def test_terminal_start_status_upload_failure_retries_durable_running_status(
    tmp_path: Path,
) -> None:
    execution = _execution(
        tmp_path,
        executor_type=ExecutorType.PTY,
        execution_key="terminal:terminal-status-retry",
    )
    execution.id = "server-terminal-status-retry"
    repository = _ExecutionRepository(None)
    client = _FailFirstStatusRunnerClient()
    terminal_handler = _ReportingTerminalStartHandler(execution, repository, client)
    daemon = _daemon(
        tmp_path,
        client=client,
        supervisor=_Supervisor(execution),
        repository=repository,
        terminal_handler=terminal_handler,
    )
    command = _command(
        "terminal-start-status-retry",
        RunnerCommandKind.TERMINAL_START,
        {
            "session_id": "terminal-status-retry",
            "execution_id": execution.id,
            "request": _terminal_start_request(tmp_path),
        },
    )

    await daemon.handle_command(command)

    assert client.status_attempts == [
        (execution.id, ExecutionStatus.RUNNING),
        (execution.id, ExecutionStatus.RUNNING),
    ]
    assert client.statuses == [(execution.id, ExecutionStatus.RUNNING)]
    assert all(status is not ExecutionStatus.FAILED for _, status in client.status_attempts)
    assert client.finished[-1][0:2] == (command.id, False)


@pytest.mark.asyncio
async def test_terminal_start_failure_reports_only_durable_pre_spawn_failed_status(
    tmp_path: Path,
) -> None:
    execution = _execution(
        tmp_path,
        executor_type=ExecutorType.PTY,
        execution_key="terminal:terminal-pre-spawn-failure",
    )
    execution.id = "server-terminal-pre-spawn-failure"
    execution.status = ExecutionStatus.FAILED
    execution.started_at = None
    repository = _ExecutionRepository(None)
    client = _RunnerClient()
    terminal_handler = _PersistThenFailTerminalStartHandler(execution, repository)
    daemon = _daemon(
        tmp_path,
        client=client,
        supervisor=_Supervisor(execution),
        repository=repository,
        terminal_handler=terminal_handler,
    )
    command = _command(
        "terminal-start-pre-spawn-failure",
        RunnerCommandKind.TERMINAL_START,
        {
            "session_id": "terminal-pre-spawn-failure",
            "execution_id": execution.id,
            "request": _terminal_start_request(tmp_path),
        },
    )

    await daemon.handle_command(command)

    assert client.statuses == [(execution.id, ExecutionStatus.FAILED)]
    assert client.finished[-1][0:2] == (command.id, False)


def _daemon(
    tmp_path: Path,
    *,
    client: object,
    supervisor: _Supervisor,
    repository: _ExecutionRepository,
    journal: object | None = None,
    terminal_handler: object | None = None,
    target_http_handler: object | None = None,
    target_http_journal: object | None = None,
    target_http_confirmation_journal: object | None = None,
    browser_handler: object | None = None,
    browser_journal: object | None = None,
) -> RunnerDaemon:
    return RunnerDaemon(
        config=RunnerDaemonConfig(
            server_url="http://control.invalid",
            node_id="runner-a",
            name="Runner A",
            state_path=tmp_path / "runner",
            poll_wait_seconds=0.01,
        ),
        client=client,  # type: ignore[arg-type]
        supervisor=supervisor,  # type: ignore[arg-type]
        executions=repository,  # type: ignore[arg-type]
        terminal_handler=terminal_handler,  # type: ignore[arg-type]
        target_http_handler=target_http_handler,  # type: ignore[arg-type]
        browser_handler=browser_handler,  # type: ignore[arg-type]
        execution_cancellation_journal=journal,  # type: ignore[arg-type]
        target_http_cancellation_journal=target_http_journal,  # type: ignore[arg-type]
        target_http_stop_confirmation_journal=target_http_confirmation_journal,  # type: ignore[arg-type]
        browser_cancellation_journal=browser_journal,  # type: ignore[arg-type]
    )


def test_runner_daemon_accepts_distinct_pentest_effect_binding(tmp_path: Path) -> None:
    execution = _execution(tmp_path)
    daemon = _daemon(
        tmp_path,
        client=_RunnerClient(),
        supervisor=_Supervisor(execution),
        repository=_ExecutionRepository(execution),
    )
    command = _command(
        "pentest-effect-binding",
        RunnerCommandKind.CANCEL,
        {
            "execution_id": execution.id,
            "execution_key": execution.execution_key,
        },
        run_id="pentest-1",
        run_kind=RunKind.PENTEST,
    )

    binding = daemon._require_command_effect_binding(command)

    assert binding.run_kind is RunKind.PENTEST
    assert binding.audit_id is None
    assert binding.plan_digest is None


def _terminal_start_request(tmp_path: Path) -> dict[str, object]:
    return {
        "run_id": "run-1",
        "node_id": "runner-a",
        "cwd": str(tmp_path),
        "argv": ["test-shell"],
    }


def _terminal_execution(tmp_path: Path, session_id: str) -> Execution:
    execution = _execution(
        tmp_path,
        executor_type=ExecutorType.PTY,
        execution_key=f"terminal:{session_id}",
    )
    execution.session_id = session_id
    return execution


def _execution(
    tmp_path: Path,
    *,
    executor_type: ExecutorType = ExecutorType.PROCESS,
    execution_key: str = "execution-key-1",
    legacy: bool = False,
) -> Execution:
    callback_binding: dict[str, str] = {}
    if not legacy:
        callback_binding = {
            "runner_command_id": "verified-launch-command",
            "runner_effect_binding_id": "verified-launch-binding",
            "runner_binding_digest": "a" * 64,
            "runner_envelope_digest": "b" * 64,
        }
    return Execution(
        id="server-execution-1",
        execution_key=execution_key,
        run_id="run-1",
        node_id="runner-a",
        owner=_OWNER,
        executor_type=executor_type,
        cwd=str(tmp_path),
        stdout_path=str(tmp_path / "stdout"),
        stderr_path=str(tmp_path / "stderr"),
        status=ExecutionStatus.RUNNING,
        **callback_binding,
    )


def _command_callback_binding(command: LeasedRunnerCommand) -> dict[str, str]:
    ownership = command.ownership
    assert ownership is not None
    return {
        "runner_command_id": command.id,
        "runner_effect_binding_id": ownership.effect_binding.id,
        "runner_binding_digest": ownership.effect_binding.binding_digest,
        "runner_envelope_digest": ownership.envelope_digest,
    }


def _rebind_command(
    command: LeasedRunnerCommand,
    *,
    binding_updates: dict[str, object],
    operation_family: RunnerOperationFamily | None = None,
    output_contract: RunnerOutputContract | None = None,
) -> LeasedRunnerCommand:
    ownership = command.ownership
    assert ownership is not None
    raw_binding = ownership.effect_binding.model_dump(mode="python")
    raw_binding.update(binding_updates)
    raw_binding["binding_digest"] = ""
    binding = RunnerEffectBinding.model_validate(raw_binding)
    family = operation_family or binding.operation_family
    contract = output_contract or ownership.output_contract
    rebound_ownership = RunnerCommandOwnership(
        command_id=command.id,
        effect_binding=binding,
        operation=command.kind,
        operation_family=family,
        payload_digest=runner_payload_digest(command.payload),
        output_contract=contract,
    )
    return LeasedRunnerCommand(
        id=command.id,
        kind=command.kind,
        payload=command.payload,
        lease_id=command.lease_id,
        attempts=command.attempts,
        target=binding.target,
        ownership=rebound_ownership,
        effect_binding_id=binding.id,
        binding_digest=binding.binding_digest,
        envelope_digest=rebound_ownership.envelope_digest,
        state_version=command.state_version,
        operation_family=family,
        output_contract=contract,
        lease_expires_at=command.lease_expires_at,
        lease_duration_seconds=command.lease_duration_seconds,
    )


def _command(
    command_id: str,
    kind: RunnerCommandKind,
    payload: dict[str, object],
    *,
    lease_id: str | None = None,
    attempts: int = 1,
    lease_expires_at: datetime | None = None,
    lease_duration_seconds: float | None = None,
    run_id: str = "run-1",
    run_kind: RunKind = RunKind.GENERAL,
    binding_id: str | None = None,
) -> LeasedRunnerCommand:
    if kind in {RunnerCommandKind.EXECUTE, RunnerCommandKind.TERMINAL_START}:
        raw_request = payload.get("request")
        if isinstance(raw_request, dict):
            payload = {
                **payload,
                "request": {
                    **raw_request,
                    "runner_principal": _OWNER.model_dump(mode="json"),
                },
            }
    execution_id = payload.get("execution_id")
    typed_execution_id = execution_id if isinstance(execution_id, str) else None
    if kind is RunnerCommandKind.EXECUTE:
        operation_family = RunnerOperationFamily.EXECUTION
        resource_kind = RunnerResourceKind.EXECUTION
        resource_id = str(execution_id)
        output_contract = RunnerOutputContract(
            max_output_bytes=100_000_000,
            allowed_streams=("stderr", "stdout"),
            result_schema="riftx.runner-result/execution-start/v1",
        )
    elif kind is RunnerCommandKind.TERMINAL_START:
        operation_family = RunnerOperationFamily.TERMINAL
        resource_kind = RunnerResourceKind.TERMINAL_SESSION
        resource_id = str(payload.get("session_id"))
        output_contract = RunnerOutputContract(
            max_output_bytes=100_000_000,
            allowed_streams=("stderr", "stdout"),
            result_schema="riftx.runner-result/terminal-start/v1",
        )
    elif kind in {
        RunnerCommandKind.CANCEL,
        RunnerCommandKind.TERMINAL_CLOSE,
        RunnerCommandKind.TARGET_HTTP_CANCEL,
        RunnerCommandKind.BROWSER_CLOSE,
    }:
        operation_family = RunnerOperationFamily.SAFETY_STOP
        if kind is RunnerCommandKind.TARGET_HTTP_CANCEL:
            resource_kind = RunnerResourceKind.TARGET_HTTP_INTENT
            raw_ids = payload.get("tool_call_ids")
            resource_id = str(raw_ids[0] if isinstance(raw_ids, list) and raw_ids else "missing")
        elif kind is RunnerCommandKind.BROWSER_CLOSE:
            resource_kind = RunnerResourceKind.BROWSER_SESSION
            raw_command = payload.get("command")
            resource_id = str(
                raw_command.get("session_id") if isinstance(raw_command, dict) else "missing"
            )
        elif kind is RunnerCommandKind.TERMINAL_CLOSE:
            resource_kind = RunnerResourceKind.TERMINAL_SESSION
            resource_id = str(payload.get("session_id"))
        else:
            resource_kind = RunnerResourceKind.EXECUTION
            resource_id = str(execution_id)
        stop_ack_schemas = {
            RunnerResourceKind.EXECUTION: RUNNER_STOP_ACK_EXECUTION_SCHEMA,
            RunnerResourceKind.TERMINAL_SESSION: RUNNER_STOP_ACK_TERMINAL_SCHEMA,
            RunnerResourceKind.TARGET_HTTP_INTENT: RUNNER_STOP_ACK_TARGET_HTTP_SCHEMA,
            RunnerResourceKind.BROWSER_SESSION: RUNNER_STOP_ACK_BROWSER_SCHEMA,
        }
        stop_result_schemas = {
            RunnerCommandKind.CANCEL: "riftx.runner-result/execution-stop/v1",
            RunnerCommandKind.TERMINAL_CLOSE: "riftx.runner-result/terminal-stop/v1",
            RunnerCommandKind.TARGET_HTTP_CANCEL: (
                "riftx.runner-result/target-http-stop/v1"
            ),
            RunnerCommandKind.BROWSER_CLOSE: "riftx.runner-result/browser-stop/v1",
        }
        output_contract = RunnerOutputContract(
            result_schema=stop_result_schemas[kind],
            stop_ack_schema=stop_ack_schemas[resource_kind],
        )
    elif kind is RunnerCommandKind.TARGET_HTTP:
        operation_family = RunnerOperationFamily.TARGET_HTTP
        resource_kind = RunnerResourceKind.TARGET_HTTP_INTENT
        raw_launch = payload.get("launch")
        resource_id = str(
            raw_launch.get("tool_call_id") if isinstance(raw_launch, dict) else "missing"
        )
        output_contract = RunnerOutputContract(
            max_output_bytes=100_000_000,
            allowed_streams=("command",),
            result_schema="riftx.runner-result/target-http/v1",
        )
    elif kind in {
        RunnerCommandKind.BROWSER,
    }:
        operation_family = RunnerOperationFamily.BROWSER
        resource_kind = RunnerResourceKind.BROWSER_SESSION
        raw_command = payload.get("command")
        resource_id = str(
            raw_command.get("session_id") if isinstance(raw_command, dict) else "missing"
        )
        output_contract = RunnerOutputContract(
            max_output_bytes=100_000_000,
            allowed_streams=("command",),
            result_schema="riftx.runner-result/browser/v1",
        )
    else:
        operation_family = RunnerOperationFamily.TERMINAL
        resource_kind = RunnerResourceKind.TERMINAL_SESSION
        resource_id = str(payload.get("session_id") or execution_id or "missing")
        output_contract = RunnerOutputContract(
            result_schema="riftx.runner-result/terminal-operation/v1",
        )
    binding = RunnerEffectBinding(
        id=binding_id or f"binding-{command_id}",
        run_id=run_id,
        run_kind=run_kind,
        node_id="runner-a",
        target=_OWNER,
        origin=RunnerCommandOrigin.APPLICATION_SERVICE,
        operation_family=operation_family,
        execution_id=typed_execution_id,
        resource_kind=resource_kind,
        resource_id=resource_id,
    )
    ownership = RunnerCommandOwnership(
        command_id=command_id,
        effect_binding=binding,
        operation=kind,
        operation_family=operation_family,
        payload_digest=runner_payload_digest(payload),
        output_contract=output_contract,
    )
    return LeasedRunnerCommand(
        id=command_id,
        kind=kind,
        payload=payload,
        lease_id=lease_id or f"lease-{command_id}",
        attempts=attempts,
        target=_OWNER,
        ownership=ownership,
        effect_binding_id=binding.id,
        binding_digest=binding.binding_digest,
        envelope_digest=ownership.envelope_digest,
        operation_family=operation_family,
        output_contract=output_contract,
        lease_expires_at=lease_expires_at,
        lease_duration_seconds=lease_duration_seconds,
    )


def _target_http_payload() -> dict[str, object]:
    request = TargetHttpRequest(
        execution_key="target-http-key",
        method="POST",
        url="https://target.internal/probe",
        timeout_seconds=30,
    )
    launch = TargetHttpRunnerRequest(
        run_id="run-1",
        session_id="session-1",
        tool_call_id="tool-call-1",
        node_id="runner-a",
        scope=Scope(domains=["target.internal"]),
        request=request,
    )
    return {
        "launch": {
            **launch.model_dump(mode="json", exclude={"request"}),
            "request": request.runner_payload(),
        },
        "max_response_bytes": request.max_response_bytes,
    }


def _browser_session() -> BrowserSession:
    session = BrowserSession(
        id="browser-1",
        run_id="run-1",
        agent_session_id="agent-session-1",
        node_id="runner-a",
        mode=BrowserMode.MANAGED_EPHEMERAL,
    )
    session.transition_to(BrowserSessionStatus.STARTING)
    session.transition_to(BrowserSessionStatus.ACTIVE)
    return session
