from __future__ import annotations

import asyncio
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import riftx.runner.daemon as daemon_module
from riftx.application.services.run_safety import RunSafetyStopService
from riftx.browser import BrowserRuntimeExchange, BrowserRuntimeResult, BrowserSessionCommand
from riftx.domain import (
    BrowserMode,
    BrowserSession,
    BrowserSessionStatus,
    Execution,
    ExecutionStatus,
    ExecutorType,
    RunnerCommandKind,
    RunnerPrincipal,
    Scope,
)
from riftx.runner.control_client import LeasedRunnerCommand, RunnerControlClientError
from riftx.runner.daemon import RunnerDaemon, RunnerDaemonConfig
from riftx.runner.models import ExecutionLaunchRequest
from riftx.runner.paths import RunnerPaths
from riftx.runner.state import FileExecutionRepository
from riftx.runner.supervisor import ProcessSupervisor
from riftx.runner.terminal_manager import OperationJournal
from riftx.target_http.models import (
    TargetHttpExchange,
    TargetHttpRequest,
    TargetHttpRunnerRequest,
    TargetHttpRunnerStopOutcome,
)

_OWNER = RunnerPrincipal(instance_id="runner-instance-a", epoch=1)


class _FailingCancellationJournal:
    async def add(self, operation_id: str) -> None:
        raise OSError(f"cannot persist {operation_id}")

    async def contains(self, operation_id: str) -> bool:
        return False


class _FailingConfirmationJournal:
    async def add(self, operation_id: str) -> None:
        raise OSError(f"cannot persist confirmation {operation_id}")

    async def contains(self, operation_id: str) -> bool:
        raise OSError(f"cannot read confirmation {operation_id}")


class _CancellationJournal:
    def __init__(self, operations: set[str] | None = None) -> None:
        self.operations = operations or set()

    async def add(self, operation_id: str) -> None:
        self.operations.add(operation_id)

    async def contains(self, operation_id: str) -> bool:
        return operation_id in self.operations


class _SignallingCancellationJournal(_CancellationJournal):
    def __init__(self) -> None:
        super().__init__()
        self.added = asyncio.Event()

    async def add(self, operation_id: str) -> None:
        await super().add(operation_id)
        self.added.set()


class _PreSpawnGuardJournal(_SignallingCancellationJournal):
    """Pause the supervisor's guard after the daemon's initial tombstone read."""

    def __init__(self) -> None:
        super().__init__()
        self.contains_calls = 0
        self.guard_entered = asyncio.Event()
        self.release_guard = asyncio.Event()

    async def contains(self, operation_id: str) -> bool:
        self.contains_calls += 1
        if self.contains_calls == 2:
            self.guard_entered.set()
            await self.release_guard.wait()
        return await super().contains(operation_id)


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
        effect_guard=None,
        on_admitted=None,
    ) -> dict[str, object]:
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
            assert await self.journal.contains(self.execution.execution_key)
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
        effect_guard=None,
        on_admitted=None,
    ) -> dict[str, object]:
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
        effect_guard=None,
        on_admitted=None,
    ) -> dict[str, object]:
        try:
            result = await super().handle(
                kind,
                payload,
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
        effect_guard=None,
        on_admitted=None,
    ) -> dict[str, object]:
        try:
            return await super().handle(
                kind,
                payload,
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
        effect_guard=None,
        on_admitted=None,
    ) -> dict[str, object]:
        if effect_guard is not None:
            await effect_guard()
        self.calls.append((kind, payload))
        self.start_entered.set()
        await self.release_start.wait()
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
        effect_guard=None,
        on_admitted=None,
    ) -> dict[str, object]:
        if effect_guard is not None:
            await effect_guard()
        self.calls.append((kind, payload))
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
        effect_guard=None,
        on_admitted=None,
    ) -> dict[str, object]:
        if effect_guard is not None:
            await effect_guard()
        self.calls.append((kind, payload))
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
        effect_guard=None,
        on_admitted=None,
    ) -> dict[str, object]:
        if effect_guard is not None:
            await effect_guard()
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
        _UnpersistedProofTerminal(execution)
        if executor_type is ExecutorType.PTY
        else None
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
    request = ExecutionLaunchRequest(
        execution_id="execution-process-row-restart",
        execution_key="process-row-restart-key",
        run_id="run-1",
        node_id="runner-a",
        runner_principal=_OWNER,
        executor_type=ExecutorType.PROCESS,
        cwd=tmp_path,
        argv=[sys.executable, "-c", "import time; time.sleep(30)"],
    )
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

    marker = tmp_path / "unsafe-process-restart"
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
    delayed_execute = _command(
        "delayed-process-row-restart",
        RunnerCommandKind.EXECUTE,
        {
            "execution_id": execution.id,
            "request": request.model_copy(
                update={
                    "argv": [
                        sys.executable,
                        "-c",
                        (
                            "from pathlib import Path; "
                            f"Path({str(marker)!r}).write_text('unsafe')"
                        ),
                    ]
                }
            ).model_dump(mode="json"),
        },
    )
    try:
        await reopened_daemon.handle_command(delayed_execute)

        durable = await reopened_repository.get(execution.id)
        assert durable is not None
        assert durable.status is ExecutionStatus.CANCELLED
        assert durable.physical_stop_confirmed_at is not None
        assert reopened_client.finished[0][1] is True
        assert not marker.exists()
    finally:
        await reopened_daemon.close()


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
    execution = _execution(tmp_path)
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
    assert journal.operations == {owned.execution_key}
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
            execution.execution_key
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
            execution.execution_key
        )

        delayed_start = _command(
            "replacement-delayed-terminal-start",
            RunnerCommandKind.TERMINAL_START,
            {
                "session_id": "split-brain-terminal",
                "execution_id": "server-terminal-execution",
                "request": {},
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
    cancel = LeasedRunnerCommand(
        id="cancel-short-lease",
        kind=RunnerCommandKind.CANCEL,
        payload={
            "execution_id": execution.id,
            "execution_key": execution.execution_key,
        },
        lease_id="lease-cancel-short",
        attempts=1,
        lease_expires_at=datetime.now(UTC) + timedelta(seconds=0.03),
        target=_OWNER,
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
            "request": {},
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
            "request": {},
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
            "request": {},
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

    assert journal.operations == {execution.execution_key}
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

    assert journal.operations == {"terminal:terminal-legacy"}
    assert terminal_handler.cancel_calls == [execution.id]
    assert supervisor.cancel_calls == []
    assert client.statuses == [("server-terminal-execution-legacy", ExecutionStatus.CANCELLED)]
    assert client.finished[0][0:2] == (command.id, True)


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
    assert await reloaded_journal.contains("terminal:terminal-reconnect")
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
            "request": {},
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
    first_lease = LeasedRunnerCommand(
        id="target-http-short-lease",
        kind=RunnerCommandKind.TARGET_HTTP,
        payload=payload,
        lease_id="lease-target-http-short-lease-1",
        attempts=1,
        lease_duration_seconds=0.05,
        target=_OWNER,
    )

    first_delivery = asyncio.create_task(daemon._run_leased_command(first_lease))
    await asyncio.wait_for(runner.sent.wait(), timeout=1)
    await asyncio.wait_for(first_delivery, timeout=1)
    assert client.finished == []

    replayed_lease = LeasedRunnerCommand(
        id=first_lease.id,
        kind=first_lease.kind,
        payload=first_lease.payload,
        lease_id="lease-target-http-short-lease-2",
        attempts=2,
        lease_duration_seconds=0.05,
        target=_OWNER,
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
    retry = _command(
        "target-http-cancel-late-ack-retry",
        RunnerCommandKind.TARGET_HTTP_CANCEL,
        {"run_id": "run-1", "tool_call_ids": ["tool-call-1"]},
    )
    await retry_daemon.handle_command(retry)

    retry_outcome = retry_client.finished[0][2]["outcomes"][0]  # type: ignore[index]
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
            "command": {"session_id": session.id},
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
        {"operation": "open", "command": {"session_id": session.id}},
    )
    await reconnected.handle_command(delayed)

    assert reconnected_client.finished[0][1] is False
    assert "cancelled on this Runner" in reconnected_client.finished[0][3]


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
            {"operation": "open", "command": {"session_id": session.id}},
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
        {"operation": "observe", "command": {"session_id": session.id}},
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
    execution = _execution(tmp_path)
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
    command = LeasedRunnerCommand(
        id="terminal-write-long",
        kind=RunnerCommandKind.TERMINAL_WRITE,
        payload={"execution_id": execution.id, "operation_id": "write-1"},
        lease_id="lease-terminal-write-long",
        attempts=1,
        # The Control Plane may use a shorter lease than the Runner's local
        # default. Renewal must honor the leased command's actual expiry.
        lease_expires_at=datetime.now(UTC) + timedelta(seconds=0.06),
        target=_OWNER,
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
    execution = _execution(tmp_path)
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
    command = LeasedRunnerCommand(
        id="terminal-write-blocked-renewal",
        kind=RunnerCommandKind.TERMINAL_WRITE,
        payload={"execution_id": execution.id, "operation_id": "write-blocked-renewal"},
        lease_id="lease-terminal-write-blocked-renewal",
        attempts=1,
        lease_duration_seconds=0.03,
        target=_OWNER,
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
    execution = _execution(tmp_path)
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
    command = LeasedRunnerCommand(
        id="terminal-write-natural-lease-expiry",
        kind=RunnerCommandKind.TERMINAL_WRITE,
        payload={"execution_id": execution.id, "operation_id": "write-natural-expiry"},
        lease_id="lease-terminal-write-natural-expiry",
        attempts=1,
        # Shorter than the minimum renewal interval, forcing the local
        # deadline to expire before the first renewal attempt.
        lease_duration_seconds=0.005,
        target=_OWNER,
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
    execution = _execution(tmp_path)
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
    command = LeasedRunnerCommand(
        id="terminal-write-failed-renewals",
        kind=RunnerCommandKind.TERMINAL_WRITE,
        payload={"execution_id": execution.id, "operation_id": "write-failed-renewals"},
        lease_id="lease-terminal-write-failed-renewals",
        attempts=1,
        lease_duration_seconds=0.03,
        target=_OWNER,
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
    execution = _execution(tmp_path)
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
    command = LeasedRunnerCommand(
        id=f"terminal-write-rejected-lease-{status_code}",
        kind=RunnerCommandKind.TERMINAL_WRITE,
        payload={"execution_id": execution.id, "operation_id": "write-reclaimed-lease"},
        lease_id=f"lease-terminal-write-rejected-{status_code}",
        attempts=1,
        lease_duration_seconds=0.3,
        target=_OWNER,
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
    execution = _execution(tmp_path)
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
    command = LeasedRunnerCommand(
        id=f"terminal-write-transformed-cancel-{raise_after_cancel}",
        kind=RunnerCommandKind.TERMINAL_WRITE,
        payload={"execution_id": execution.id, "operation_id": "write-transform-cancel"},
        lease_id=f"lease-terminal-write-transformed-cancel-{raise_after_cancel}",
        attempts=1,
        lease_duration_seconds=0.03,
        target=_OWNER,
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
            "request": {},
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
            "request": {},
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


def _execution(
    tmp_path: Path,
    *,
    executor_type: ExecutorType = ExecutorType.PROCESS,
    execution_key: str = "execution-key-1",
) -> Execution:
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
    )


def _command(
    command_id: str,
    kind: RunnerCommandKind,
    payload: dict[str, object],
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
    return LeasedRunnerCommand(
        id=command_id,
        kind=kind,
        payload=payload,
        lease_id=f"lease-{command_id}",
        attempts=1,
        target=_OWNER,
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
