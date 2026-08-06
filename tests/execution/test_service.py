from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import get_type_hints

import pytest

from riftx.application.errors import (
    ApplicationConflictError,
    EntityNotFoundError,
    PentestBudgetExceededError,
    ServiceUnavailableError,
)
from riftx.application.ports import (
    ExecutionAdmissionIdentity,
    ToolCallIntentExecutionClaim,
    ToolCallIntentRepository,
)
from riftx.application.services.runs import RunApplicationService
from riftx.domain import (
    Engagement,
    EntryPoint,
    EntryPointKind,
    Execution,
    ExecutionStatus,
    ExecutorType,
    Objective,
    PentestAdmission,
    PentestBudget,
    Run,
    RunKind,
    RunStatus,
    Scope,
)
from riftx.execution import (
    DeferredExecutionDispatcher,
    ExecutionService,
    SubmitExecutionRequest,
    build_execution_key,
)
from riftx.persistence import (
    Database,
    SQLAlchemyAgentCycleRepository,
    SQLAlchemyAgentSessionRepository,
    SQLAlchemyAgentStepRepository,
    SQLAlchemyEngagementRepository,
    SQLAlchemyExecutionRepository,
    SQLAlchemyRunEventRepository,
    SQLAlchemyRunRepository,
    SQLAlchemyToolCallIntentRepository,
)
from riftx.runner import (
    ExecutionLaunchRequest,
    ExecutionOutput,
    OutputSlice,
    ProcessSupervisor,
    RunnerPaths,
    TerminalLaunchRequest,
)
from riftx.runtime.types import (
    AgentCycle,
    AgentSession,
    AgentStep,
    AgentStepType,
    ToolCallIntent,
    ToolCallStatus,
)


class RecordingRunner:
    def __init__(self, repository: SQLAlchemyExecutionRepository) -> None:
        self.repository = repository
        self.launches = 0
        self.waits = 0
        self.cancellations = 0
        self.output_reads = 0

    async def start(self, request: ExecutionLaunchRequest, *, effect_guard=None) -> Execution:
        if effect_guard is not None:
            await effect_guard()
        execution = Execution(
            execution_key=request.execution_key,
            run_id=request.run_id,
            session_id=request.session_id,
            tool_call_id=request.tool_call_id,
            attempt_group=request.attempt_group,
            node_id=request.node_id,
            executor_type=request.executor_type,
            argv=request.argv,
            command_text=request.command_text,
            tool_id=request.tool_id,
            tool_version=request.tool_version,
            cwd=str(request.cwd),
            env_diff=request.env,
            status=ExecutionStatus.QUEUED,
            stdout_path=str(request.cwd / "stdout.log"),
            stderr_path=str(request.cwd / "stderr.log"),
        )
        execution, created = await self.repository.create_if_absent(execution)
        if not created:
            return execution
        self.launches += 1
        execution.transition_to(ExecutionStatus.STARTING)
        execution.transition_to(ExecutionStatus.RUNNING)
        return await self.repository.save(execution)

    async def get(self, execution_id: str) -> Execution:
        execution = await self.repository.get(execution_id)
        assert execution is not None
        return execution

    async def wait(self, execution_id: str) -> Execution:
        self.waits += 1
        execution = await self.get(execution_id)
        if execution.status is ExecutionStatus.RUNNING:
            execution.transition_to(ExecutionStatus.COMPLETED, exit_code=0)
            await self.repository.save(execution)
        return execution

    async def cancel(self, execution_id: str) -> Execution:
        self.cancellations += 1
        execution = await self.get(execution_id)
        if execution.status is ExecutionStatus.RUNNING:
            execution.transition_to(ExecutionStatus.CANCELLED)
            await self.repository.save(execution)
        return execution

    async def read_output(
        self,
        execution_id: str,
        *,
        stdout_cursor: int = 0,
        stderr_cursor: int = 0,
        max_bytes: int = 64 * 1024,
    ) -> ExecutionOutput:
        self.output_reads += 1
        return ExecutionOutput(
            stdout=OutputSlice(data=b"", cursor=stdout_cursor, next_cursor=stdout_cursor, eof=True),
            stderr=OutputSlice(data=b"", cursor=stderr_cursor, next_cursor=stderr_cursor, eof=True),
        )


class DelayedRegistrationRunner:
    """Hold the original pre-registration race window open on demand."""

    def __init__(self, delegate: RecordingRunner) -> None:
        self.delegate = delegate
        self.entered = asyncio.Event()
        self.second_entered = asyncio.Event()
        self.release = asyncio.Event()
        self.entries = 0

    def __getattr__(self, name: str):
        return getattr(self.delegate, name)

    async def start(self, request: ExecutionLaunchRequest, *, effect_guard=None) -> Execution:
        self.entries += 1
        self.entered.set()
        if self.entries >= 2:
            self.second_entered.set()
        await self.release.wait()
        return await self.delegate.start(request, effect_guard=effect_guard)


class BlockingClaimToolCalls:
    def __init__(self, delegate: SQLAlchemyToolCallIntentRepository) -> None:
        self.delegate = delegate
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    def __getattr__(self, name: str):
        return getattr(self.delegate, name)

    async def claim_execution(
        self,
        intent_id: str,
        *,
        execution_key: str,
        attempt_group: str,
    ) -> ToolCallIntentExecutionClaim:
        self.entered.set()
        await self.release.wait()
        return await self.delegate.claim_execution(
            intent_id,
            execution_key=execution_key,
            attempt_group=attempt_group,
        )


class FailingStartRunner:
    def __init__(
        self,
        tool_calls: SQLAlchemyToolCallIntentRepository | None = None,
    ) -> None:
        self.tool_calls = tool_calls
        self.starts = 0

    async def start(self, request: ExecutionLaunchRequest, *, effect_guard=None) -> Execution:
        self.starts += 1
        if effect_guard is not None:
            await effect_guard()
        if self.tool_calls is not None:
            terminal, changed = await self.tool_calls.compare_and_set_status(
                request.tool_call_id or "",
                expected={ToolCallStatus.EXECUTING},
                target=ToolCallStatus.CANCELLED,
            )
            assert changed is True and terminal.status is ToolCallStatus.CANCELLED
        raise RuntimeError("runner start failed")


class SnapshotBoundaryRunner:
    """Exercise caller and runner mutation around one durable admission."""

    def __init__(
        self,
        executions: SQLAlchemyExecutionRepository,
        *,
        fail_after_admission: bool = False,
    ) -> None:
        self.executions = executions
        self.fail_after_admission = fail_after_admission
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.received: ExecutionLaunchRequest | None = None
        self.snapshot: ExecutionLaunchRequest | None = None

    async def start(self, request: ExecutionLaunchRequest, *, effect_guard=None) -> Execution:
        self.received = request
        self.entered.set()
        await self.release.wait()

        snapshot = request.model_copy(deep=True)
        self.snapshot = snapshot
        request.argv.append("runner-mutated")
        request.env["RUNNER_MUTATED"] = "1"
        request.cwd = request.cwd.parent / "runner-mutated-cwd"
        if effect_guard is not None:
            await effect_guard()

        execution = Execution(
            execution_key=snapshot.execution_key,
            launch_fingerprint=snapshot.launch_fingerprint,
            run_id=snapshot.run_id,
            session_id=snapshot.session_id,
            tool_call_id=snapshot.tool_call_id,
            attempt_group=snapshot.attempt_group,
            node_id=snapshot.node_id,
            executor_type=snapshot.executor_type,
            argv=list(snapshot.argv),
            command_text=snapshot.command_text,
            tool_id=snapshot.tool_id,
            tool_version=snapshot.tool_version,
            cwd=str(snapshot.cwd),
            env_diff=dict(snapshot.env),
            status=ExecutionStatus.STARTING,
            stdout_path=str(snapshot.cwd / "snapshot-stdout.log"),
            stderr_path=str(snapshot.cwd / "snapshot-stderr.log"),
        )
        execution, created = await self.executions.create_if_absent(execution)
        assert created is True
        if self.fail_after_admission:
            raise RuntimeError("runner failed after snapshot admission")
        execution.transition_to(ExecutionStatus.RUNNING)
        return await self.executions.save(execution)


class ForeignSameKeyFailingRunner:
    """Register a foreign same-key row, then preserve the original start error."""

    def __init__(self, executions: SQLAlchemyExecutionRepository) -> None:
        self.executions = executions
        self.starts = 0

    async def start(self, request: ExecutionLaunchRequest, *, effect_guard=None) -> Execution:
        self.starts += 1
        if effect_guard is not None:
            await effect_guard()
        foreign = Execution(
            id="foreign-same-key-execution",
            execution_key=request.execution_key,
            launch_fingerprint=request.launch_fingerprint,
            run_id=request.run_id,
            session_id=None,
            tool_call_id=None,
            attempt_group=request.attempt_group,
            node_id=request.node_id,
            executor_type=request.executor_type,
            argv=request.argv,
            command_text=request.command_text,
            tool_id=request.tool_id,
            tool_version=request.tool_version,
            cwd=str(request.cwd),
            env_diff=request.env,
            status=ExecutionStatus.STARTING,
            stdout_path=str(request.cwd / "foreign-stdout.log"),
            stderr_path=str(request.cwd / "foreign-stderr.log"),
        )
        assert (await self.executions.create_if_absent(foreign))[1] is True
        raise RuntimeError("runner failed after foreign admission")


class AdmitOnFirstAdmissionRead:
    """Expose an exact row only after settlement's first admission read."""

    def __init__(
        self,
        delegate: SQLAlchemyExecutionRepository,
        execution: Execution,
    ) -> None:
        self.delegate = delegate
        self.execution = execution
        self.reads = 0

    def __getattr__(self, name: str):
        return getattr(self.delegate, name)

    async def find_admission(
        self,
        identity: ExecutionAdmissionIdentity,
    ) -> Execution | None:
        self.reads += 1
        if self.reads == 1:
            assert identity.matches(self.execution)
            assert (await self.delegate.create_if_absent(self.execution))[1] is True
            return None
        return await self.delegate.find_admission(identity)


class RunControlWorkflow:
    async def pause(self, run_id: str) -> None:
        return None

    async def cancel(self, run_id: str) -> None:
        return None


class BlockSecondRunRead:
    """Block the supervisor's post-registration effect guard."""

    def __init__(self, delegate: SQLAlchemyRunRepository) -> None:
        self._delegate = delegate
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self._reads = 0

    def __getattr__(self, name: str):
        return getattr(self._delegate, name)

    async def get(self, run_id: str) -> Run | None:
        self._reads += 1
        if self._reads == 2:
            self.entered.set()
            await self.release.wait()
        return await self._delegate.get(run_id)


class RecordingEvents:
    def __init__(self) -> None:
        self.rows: list[tuple[str, str, dict[str, object]]] = []
        self.event_ids: list[str | None] = []

    async def append(
        self,
        run_id: str,
        event_type: str,
        payload: dict[str, object],
        *,
        event_id: str | None = None,
    ) -> None:
        self.rows.append((run_id, event_type, payload))
        self.event_ids.append(event_id)


async def build_service(
    tmp_path: Path,
    *,
    run_kind: RunKind = RunKind.GENERAL,
    pentest_budget: PentestBudget | None = None,
) -> tuple[Database, ExecutionService, RecordingRunner, dict[str, object]]:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'execution.db'}")
    await database.create_schema()
    await SQLAlchemyEngagementRepository(database.session_factory).create(
        Engagement(
            id="engagement-1",
            name="Authorized",
            authorization_reference=(
                "ticket://execution-test" if run_kind is RunKind.PENTEST else None
            ),
        )
    )
    runs = SQLAlchemyRunRepository(database.session_factory)
    is_pentest = run_kind is RunKind.PENTEST
    await runs.create(
        Run(
            kind=run_kind,
            id="run-1",
            engagement_id="engagement-1",
            node_id="node-1",
            objective=Objective(description="Run a durable tool"),
            entry_points=(
                [EntryPoint(kind=EntryPointKind.IP, value="127.0.0.1")]
                if is_pentest
                else []
            ),
            scope=Scope(ips=["127.0.0.1"]) if is_pentest else Scope(),
            pentest_admission=(
                PentestAdmission(
                    budget=pentest_budget
                    or PentestBudget(
                        max_duration_seconds=600,
                        max_model_calls=10,
                        max_tokens=10_000,
                        max_tool_calls=10,
                        max_target_interactions=10,
                        max_concurrent_target_interactions=1,
                    )
                )
                if is_pentest
                else None
            ),
            status=RunStatus.RUNNING if is_pentest else RunStatus.CREATED,
            workspace_path=str(tmp_path),
        )
    )
    sessions = SQLAlchemyAgentSessionRepository(database.session_factory)
    await sessions.create(AgentSession(id="session-1", run_id="run-1", model_profile="fake-model"))
    cycles = SQLAlchemyAgentCycleRepository(database.session_factory)
    await cycles.create(
        AgentCycle(id="cycle-1", run_id="run-1", session_id="session-1", sequence=1)
    )
    steps = SQLAlchemyAgentStepRepository(database.session_factory)
    await steps.create(
        AgentStep(
            id="step-1",
            cycle_id="cycle-1",
            sequence=1,
            step_type=AgentStepType.TOOL_PROPOSAL,
        )
    )
    tool_calls = SQLAlchemyToolCallIntentRepository(database.session_factory)
    await tool_calls.create(
        ToolCallIntent(
            id="tool-call-1",
            run_id="run-1",
            session_id="session-1",
            cycle_id="cycle-1",
            step_id="step-1",
            tool_id="shell",
            status=ToolCallStatus.READY,
        )
    )
    executions = SQLAlchemyExecutionRepository(database.session_factory)
    runner = RecordingRunner(executions)
    service = ExecutionService(
        execution_repository=executions,
        session_repository=sessions,
        tool_call_repository=tool_calls,
        runner=runner,
        run_repository=runs,
    )
    return (
        database,
        service,
        runner,
        {
            "executions": executions,
            "runs": runs,
            "sessions": sessions,
            "tool_calls": tool_calls,
        },
    )


async def test_pentest_tool_budget_stops_before_runner_start(tmp_path: Path) -> None:
    database, service, runner, repos = await build_service(
        tmp_path,
        run_kind=RunKind.PENTEST,
        pentest_budget=PentestBudget(
            max_duration_seconds=600,
            max_model_calls=10,
            max_tokens=10_000,
            max_tool_calls=1,
            max_target_interactions=10,
            max_concurrent_target_interactions=1,
        ),
    )
    sessions = repos["sessions"]
    tool_calls = repos["tool_calls"]
    assert isinstance(sessions, SQLAlchemyAgentSessionRepository)
    assert isinstance(tool_calls, SQLAlchemyToolCallIntentRepository)
    session = await sessions.get("session-1")
    assert session is not None
    session.tool_call_count = 1
    await sessions.save(session)

    with pytest.raises(PentestBudgetExceededError) as exhausted:
        await service.submit(request(tmp_path))

    assert exhausted.value.budget_name == "max_tool_calls"
    assert runner.launches == 0
    intent = await tool_calls.get("tool-call-1")
    assert intent is not None and intent.status is ToolCallStatus.READY
    await database.dispose()


def request(tmp_path: Path, *, attempt_group: str = "initial") -> SubmitExecutionRequest:
    return SubmitExecutionRequest(
        run_id="run-1",
        session_id="session-1",
        tool_call_id="tool-call-1",
        attempt_group=attempt_group,
        node_id="node-1",
        executor_type=ExecutorType.PROCESS,
        cwd=tmp_path,
        argv=[sys.executable, "-c", "print('ok')"],
        tool_id="shell",
    )


def snapshot_request(
    tmp_path: Path,
) -> tuple[SubmitExecutionRequest, Path, Path, Path]:
    initial_cwd = tmp_path / "initial-cwd"
    drifted_cwd = tmp_path / "drifted-cwd"
    initial_cwd.mkdir()
    drifted_cwd.mkdir()
    cwd_link = tmp_path / "cwd-link"
    cwd_link.symlink_to(initial_cwd, target_is_directory=True)
    submit_request = request(tmp_path).model_copy(
        update={
            "cwd": cwd_link,
            "env": {"SNAPSHOT": "frozen"},
        }
    )
    return submit_request, initial_cwd, drifted_cwd, cwd_link


def drift_submit_request(
    submit_request: SubmitExecutionRequest,
    *,
    cwd_link: Path,
    drifted_cwd: Path,
) -> None:
    submit_request.run_id = "drifted-run"
    submit_request.session_id = "drifted-session"
    submit_request.tool_call_id = "drifted-tool-call"
    submit_request.attempt_group = "drifted-attempt"
    submit_request.node_id = "drifted-node"
    submit_request.argv.append("original-request-mutated")
    submit_request.env["ORIGINAL_MUTATED"] = "1"
    cwd_link.unlink()
    cwd_link.symlink_to(drifted_cwd, target_is_directory=True)


def terminal_launch_request(
    tmp_path: Path,
    *,
    session_id: str,
    execution_id: str,
    execution_key: str,
) -> TerminalLaunchRequest:
    return TerminalLaunchRequest(
        session_id=session_id,
        execution_id=execution_id,
        execution_key=execution_key,
        agent_session_id="session-1",
        tool_call_id="tool-call-1",
        attempt_group="initial",
        run_id="run-1",
        node_id="node-1",
        cwd=tmp_path,
        argv=["terminal-shell"],
        tool_id="shell",
    )


async def set_intent_status(
    repository: SQLAlchemyToolCallIntentRepository,
    status: ToolCallStatus,
) -> ToolCallIntent:
    intent, changed = await repository.compare_and_set_status(
        "tool-call-1",
        expected=set(ToolCallStatus),
        target=status,
    )
    assert changed is True
    return intent


def execution_with_status(tmp_path: Path, status: ExecutionStatus) -> Execution:
    return Execution(
        id=f"execution-{status.value}",
        execution_key=f"key-{status.value}",
        run_id="run-1",
        session_id="session-1",
        tool_call_id="tool-call-1",
        attempt_group="initial",
        node_id="node-1",
        executor_type=ExecutorType.PROCESS,
        cwd=str(tmp_path),
        status=status,
        stdout_path=str(tmp_path / "stdout.log"),
        stderr_path=str(tmp_path / "stderr.log"),
    )


async def claim_intent_for_execution(
    repository: SQLAlchemyToolCallIntentRepository,
    execution: Execution,
    *,
    status: ToolCallStatus,
) -> ToolCallIntent:
    claim = await repository.claim_execution(
        execution.tool_call_id or "",
        execution_key=execution.execution_key,
        attempt_group=execution.attempt_group or "initial",
    )
    assert claim.acquired is True
    intent, changed = await repository.compare_and_set_status(
        claim.intent.id,
        expected=set(ToolCallStatus),
        target=status,
    )
    assert changed is True
    return intent


def test_execution_components_depend_on_tool_call_intent_port() -> None:
    assert (
        get_type_hints(ExecutionService.__init__)["tool_call_repository"]
        is ToolCallIntentRepository
    )
    assert (
        get_type_hints(DeferredExecutionDispatcher.__init__)["tool_call_repository"]
        is ToolCallIntentRepository
    )


async def test_code_audit_submit_is_denied_before_claim_runner_and_event(
    tmp_path: Path,
) -> None:
    database, _, runner, repos = await build_service(tmp_path, run_kind=RunKind.CODE_AUDIT)
    executions = repos["executions"]
    runs = repos["runs"]
    sessions = repos["sessions"]
    tool_calls = repos["tool_calls"]
    assert isinstance(executions, SQLAlchemyExecutionRepository)
    assert isinstance(runs, SQLAlchemyRunRepository)
    assert isinstance(sessions, SQLAlchemyAgentSessionRepository)
    assert isinstance(tool_calls, SQLAlchemyToolCallIntentRepository)
    events = RecordingEvents()
    service = ExecutionService(
        execution_repository=executions,
        session_repository=sessions,
        tool_call_repository=tool_calls,
        runner=runner,
        event_repository=events,  # type: ignore[arg-type]
        run_repository=runs,
    )
    submission = request(tmp_path)

    with pytest.raises(ApplicationConflictError) as captured:
        await service.submit(submission)

    assert captured.value.code == "run_kind_operation_unsupported"
    intent = await tool_calls.get("tool-call-1")
    assert intent is not None and intent.status is ToolCallStatus.READY
    assert not await tool_calls.execution_claim_is_current(
        intent.id,
        execution_key=submission.execution_key,
        attempt_group=submission.attempt_group,
    )
    assert await executions.get_by_key(submission.execution_key) is None
    assert runner.launches == 0
    assert events.rows == []
    await database.dispose()


async def test_code_audit_submit_preserves_cross_owner_session_error_precedence(
    tmp_path: Path,
) -> None:
    database, service, runner, repos = await build_service(
        tmp_path,
        run_kind=RunKind.CODE_AUDIT,
    )
    runs = repos["runs"]
    sessions = repos["sessions"]
    assert isinstance(runs, SQLAlchemyRunRepository)
    assert isinstance(sessions, SQLAlchemyAgentSessionRepository)
    await runs.create(
        Run(
            kind=RunKind.GENERAL,
            id="run-foreign",
            engagement_id="engagement-1",
            node_id="node-1",
            objective=Objective(description="Foreign owner"),
            workspace_path=str(tmp_path),
        )
    )
    await sessions.create(
        AgentSession(
            id="session-foreign",
            run_id="run-foreign",
            model_profile="fake-model",
        )
    )

    with pytest.raises(EntityNotFoundError) as captured:
        await service.submit(request(tmp_path).model_copy(update={"session_id": "session-foreign"}))

    assert captured.value.entity == "AgentSession"
    assert runner.launches == 0
    await database.dispose()


async def test_code_audit_execution_mutations_are_denied_before_runner_and_events(
    tmp_path: Path,
) -> None:
    database, _, runner, repos = await build_service(tmp_path, run_kind=RunKind.CODE_AUDIT)
    executions = repos["executions"]
    runs = repos["runs"]
    sessions = repos["sessions"]
    tool_calls = repos["tool_calls"]
    assert isinstance(executions, SQLAlchemyExecutionRepository)
    assert isinstance(runs, SQLAlchemyRunRepository)
    assert isinstance(sessions, SQLAlchemyAgentSessionRepository)
    assert isinstance(tool_calls, SQLAlchemyToolCallIntentRepository)
    execution = execution_with_status(tmp_path, ExecutionStatus.RUNNING)
    assert (await executions.create_if_absent(execution))[1] is True
    intent = await tool_calls.get("tool-call-1")
    assert intent is not None
    events = RecordingEvents()
    service = ExecutionService(
        execution_repository=executions,
        session_repository=sessions,
        tool_call_repository=tool_calls,
        runner=runner,
        event_repository=events,  # type: ignore[arg-type]
        run_repository=runs,
    )

    for operation in (
        service.sync_intent_execution(intent, execution),
        service.wait(execution.id, timeout_seconds=0.01),
        service.cancel(execution.id),
    ):
        with pytest.raises(ApplicationConflictError) as captured:
            await operation
        assert captured.value.code == "run_kind_operation_unsupported"

    durable_intent = await tool_calls.get(intent.id)
    durable_execution = await executions.get(execution.id)
    assert durable_intent is not None and durable_intent.status is ToolCallStatus.READY
    assert durable_execution is not None and durable_execution.status is ExecutionStatus.RUNNING
    assert runner.waits == 0
    assert runner.cancellations == 0
    assert runner.output_reads == 0
    assert events.rows == []
    await database.dispose()


async def test_submit_uses_one_frozen_launch_snapshot_across_runner_await(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, _, _, repos = await build_service(tmp_path)
    executions = repos["executions"]
    sessions = repos["sessions"]
    tool_calls = repos["tool_calls"]
    runs = repos["runs"]
    assert isinstance(executions, SQLAlchemyExecutionRepository)
    assert isinstance(sessions, SQLAlchemyAgentSessionRepository)
    assert isinstance(tool_calls, SQLAlchemyToolCallIntentRepository)
    assert isinstance(runs, SQLAlchemyRunRepository)
    runner = SnapshotBoundaryRunner(executions)
    events = RecordingEvents()
    service = ExecutionService(
        execution_repository=executions,
        session_repository=sessions,
        tool_call_repository=tool_calls,
        runner=runner,  # type: ignore[arg-type]
        event_repository=events,  # type: ignore[arg-type]
        run_repository=runs,
    )
    submit_request, initial_cwd, drifted_cwd, cwd_link = snapshot_request(tmp_path)
    expected_key = build_execution_key(
        run_id="run-1",
        session_id="session-1",
        tool_call_id="tool-call-1",
        attempt_group="initial",
    )
    expected_argv = list(submit_request.argv)
    materialized: list[ExecutionLaunchRequest] = []
    original_materialize = SubmitExecutionRequest.to_launch_request

    def tracked_materialize(current: SubmitExecutionRequest) -> ExecutionLaunchRequest:
        launch = original_materialize(current)
        materialized.append(launch)
        return launch

    monkeypatch.setattr(SubmitExecutionRequest, "to_launch_request", tracked_materialize)

    submitted = asyncio.create_task(service.submit(submit_request))
    await runner.entered.wait()
    drift_submit_request(
        submit_request,
        cwd_link=cwd_link,
        drifted_cwd=drifted_cwd,
    )
    runner.release.set()
    execution = await submitted

    assert len(materialized) == 1
    assert runner.received is not None and runner.snapshot is not None
    assert runner.received is not materialized[0]
    assert runner.received.argv is not materialized[0].argv
    assert runner.received.env is not materialized[0].env
    assert runner.snapshot.cwd == initial_cwd.resolve()
    assert runner.received.argv[-1] == "runner-mutated"
    assert runner.received.env["RUNNER_MUTATED"] == "1"
    assert execution.execution_key == expected_key
    assert execution.run_id == "run-1"
    assert execution.session_id == "session-1"
    assert execution.tool_call_id == "tool-call-1"
    assert execution.attempt_group == "initial"
    assert execution.node_id == "node-1"
    assert execution.cwd == str(initial_cwd.resolve())
    assert execution.argv == expected_argv
    assert execution.env_diff == {"SNAPSHOT": "frozen"}
    assert events.rows == [
        (
            "run-1",
            "execution.submitted",
            {
                "execution_id": execution.id,
                "session_id": "session-1",
                "tool_call_id": "tool-call-1",
                "attempt_group": "initial",
                "execution_key": expected_key,
            },
        )
    ]
    assert await tool_calls.execution_claim_is_current(
        "tool-call-1",
        execution_key=expected_key,
        attempt_group="initial",
    )
    await database.dispose()


async def test_failed_submit_settles_the_same_frozen_admission_after_request_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, _, _, repos = await build_service(tmp_path)
    executions = repos["executions"]
    sessions = repos["sessions"]
    tool_calls = repos["tool_calls"]
    runs = repos["runs"]
    assert isinstance(executions, SQLAlchemyExecutionRepository)
    assert isinstance(sessions, SQLAlchemyAgentSessionRepository)
    assert isinstance(tool_calls, SQLAlchemyToolCallIntentRepository)
    assert isinstance(runs, SQLAlchemyRunRepository)
    runner = SnapshotBoundaryRunner(executions, fail_after_admission=True)
    service = ExecutionService(
        execution_repository=executions,
        session_repository=sessions,
        tool_call_repository=tool_calls,
        runner=runner,  # type: ignore[arg-type]
        run_repository=runs,
    )
    submit_request, initial_cwd, drifted_cwd, cwd_link = snapshot_request(tmp_path)
    expected_key = build_execution_key(
        run_id="run-1",
        session_id="session-1",
        tool_call_id="tool-call-1",
        attempt_group="initial",
    )
    materializations = 0
    original_materialize = SubmitExecutionRequest.to_launch_request

    def tracked_materialize(current: SubmitExecutionRequest) -> ExecutionLaunchRequest:
        nonlocal materializations
        materializations += 1
        return original_materialize(current)

    monkeypatch.setattr(SubmitExecutionRequest, "to_launch_request", tracked_materialize)

    submitted = asyncio.create_task(service.submit(submit_request))
    await runner.entered.wait()
    drift_submit_request(
        submit_request,
        cwd_link=cwd_link,
        drifted_cwd=drifted_cwd,
    )
    runner.release.set()
    with pytest.raises(RuntimeError, match="runner failed after snapshot admission"):
        await submitted

    assert materializations == 1
    admitted = await executions.get_by_key(expected_key)
    assert admitted is not None
    assert admitted.run_id == "run-1"
    assert admitted.session_id == "session-1"
    assert admitted.tool_call_id == "tool-call-1"
    assert admitted.attempt_group == "initial"
    assert admitted.cwd == str(initial_cwd.resolve())
    assert admitted.env_diff == {"SNAPSHOT": "frozen"}
    intent = await tool_calls.get("tool-call-1")
    assert intent is not None and intent.status is ToolCallStatus.EXECUTING
    assert await tool_calls.execution_claim_is_current(
        intent.id,
        execution_key=expected_key,
        attempt_group="initial",
    )
    assert not await tool_calls.execution_claim_is_current(
        intent.id,
        execution_key=submit_request.execution_key,
        attempt_group=submit_request.attempt_group,
    )
    await database.dispose()


async def test_cancel_winning_before_claim_prevents_runner_and_submitted_event(
    tmp_path: Path,
) -> None:
    database, _, runner, repos = await build_service(tmp_path)
    executions = repos["executions"]
    sessions = repos["sessions"]
    tool_calls = repos["tool_calls"]
    runs = repos["runs"]
    assert isinstance(executions, SQLAlchemyExecutionRepository)
    assert isinstance(sessions, SQLAlchemyAgentSessionRepository)
    assert isinstance(tool_calls, SQLAlchemyToolCallIntentRepository)
    assert isinstance(runs, SQLAlchemyRunRepository)
    blocking_claims = BlockingClaimToolCalls(tool_calls)
    events = RecordingEvents()
    service = ExecutionService(
        execution_repository=executions,
        session_repository=sessions,
        tool_call_repository=blocking_claims,  # type: ignore[arg-type]
        runner=runner,
        event_repository=events,  # type: ignore[arg-type]
        run_repository=runs,
    )

    submit = asyncio.create_task(service.submit(request(tmp_path)))
    await blocking_claims.entered.wait()
    cancelled, changed = await tool_calls.compare_and_set_status(
        "tool-call-1",
        expected={ToolCallStatus.READY},
        target=ToolCallStatus.CANCELLED,
    )
    assert changed is True and cancelled.status is ToolCallStatus.CANCELLED
    blocking_claims.release.set()

    with pytest.raises(ApplicationConflictError) as captured:
        await submit
    assert captured.value.code == "tool_call_not_ready"
    assert runner.launches == 0
    assert events.rows == []
    assert await executions.get_by_key(request(tmp_path).execution_key) is None
    await database.dispose()


async def test_cancel_after_claim_fails_effect_guard_before_physical_launch(
    tmp_path: Path,
) -> None:
    database, _, runner, repos = await build_service(tmp_path)
    executions = repos["executions"]
    sessions = repos["sessions"]
    tool_calls = repos["tool_calls"]
    runs = repos["runs"]
    assert isinstance(executions, SQLAlchemyExecutionRepository)
    assert isinstance(sessions, SQLAlchemyAgentSessionRepository)
    assert isinstance(tool_calls, SQLAlchemyToolCallIntentRepository)
    assert isinstance(runs, SQLAlchemyRunRepository)
    delayed = DelayedRegistrationRunner(runner)
    events = RecordingEvents()
    service = ExecutionService(
        execution_repository=executions,
        session_repository=sessions,
        tool_call_repository=tool_calls,
        runner=delayed,  # type: ignore[arg-type]
        event_repository=events,  # type: ignore[arg-type]
        run_repository=runs,
    )

    submit = asyncio.create_task(service.submit(request(tmp_path)))
    await delayed.entered.wait()
    cancelled, changed = await tool_calls.compare_and_set_status(
        "tool-call-1",
        expected={ToolCallStatus.EXECUTING},
        target=ToolCallStatus.CANCELLED,
    )
    assert changed is True and cancelled.status is ToolCallStatus.CANCELLED
    delayed.release.set()

    with pytest.raises(ApplicationConflictError) as captured:
        await submit
    assert captured.value.code == "tool_call_execution_claim_lost"
    assert runner.launches == 0
    assert events.rows == []
    await database.dispose()


async def test_terminal_without_execution_rejects_initial_but_allows_retry(
    tmp_path: Path,
) -> None:
    database, service, runner, repos = await build_service(tmp_path)
    await set_intent_status(repos["tool_calls"], ToolCallStatus.FAILED)

    with pytest.raises(ApplicationConflictError) as captured:
        await service.submit(request(tmp_path))
    assert captured.value.code == "tool_call_not_ready"
    assert runner.launches == 0

    retry = await service.submit(request(tmp_path, attempt_group="retry-1"))

    assert retry.attempt_group == "retry-1"
    assert retry.status is ExecutionStatus.RUNNING
    assert runner.launches == 1
    await database.dispose()


async def test_two_distinct_terminal_retries_have_one_claim_winner(tmp_path: Path) -> None:
    database, service, runner, repos = await build_service(tmp_path)
    await set_intent_status(repos["tool_calls"], ToolCallStatus.FAILED)

    results = await asyncio.gather(
        service.submit(request(tmp_path, attempt_group="retry-1")),
        service.submit(request(tmp_path, attempt_group="retry-2")),
        return_exceptions=True,
    )

    executions = [item for item in results if isinstance(item, Execution)]
    conflicts = [item for item in results if isinstance(item, ApplicationConflictError)]
    assert len(executions) == 1
    assert len(conflicts) == 1
    assert conflicts[0].code == "tool_call_not_ready"
    assert runner.launches == 1
    assert executions[0].attempt_group in {"retry-1", "retry-2"}
    await database.dispose()


async def test_runner_failure_rolls_back_exact_initial_claim_for_replay(tmp_path: Path) -> None:
    database, _, runner, repos = await build_service(tmp_path)
    executions = repos["executions"]
    sessions = repos["sessions"]
    tool_calls = repos["tool_calls"]
    runs = repos["runs"]
    assert isinstance(executions, SQLAlchemyExecutionRepository)
    assert isinstance(sessions, SQLAlchemyAgentSessionRepository)
    assert isinstance(tool_calls, SQLAlchemyToolCallIntentRepository)
    assert isinstance(runs, SQLAlchemyRunRepository)
    failing = FailingStartRunner()
    service = ExecutionService(
        execution_repository=executions,
        session_repository=sessions,
        tool_call_repository=tool_calls,
        runner=failing,  # type: ignore[arg-type]
        run_repository=runs,
    )

    with pytest.raises(RuntimeError, match="runner start failed"):
        await service.submit(request(tmp_path))

    restored = await tool_calls.get("tool-call-1")
    assert restored is not None and restored.status is ToolCallStatus.READY
    assert not await tool_calls.execution_claim_is_current(
        restored.id,
        execution_key=request(tmp_path).execution_key,
        attempt_group="initial",
    )
    replay_service = ExecutionService(
        execution_repository=executions,
        session_repository=sessions,
        tool_call_repository=tool_calls,
        runner=runner,
        run_repository=runs,
    )
    replay = await replay_service.submit(request(tmp_path))
    assert replay.status is ExecutionStatus.RUNNING
    assert runner.launches == 1
    await database.dispose()


async def test_runner_failure_ignores_foreign_same_key_admission_and_rolls_back(
    tmp_path: Path,
) -> None:
    database, _, _, repos = await build_service(tmp_path)
    executions = repos["executions"]
    sessions = repos["sessions"]
    tool_calls = repos["tool_calls"]
    runs = repos["runs"]
    assert isinstance(executions, SQLAlchemyExecutionRepository)
    assert isinstance(sessions, SQLAlchemyAgentSessionRepository)
    assert isinstance(tool_calls, SQLAlchemyToolCallIntentRepository)
    assert isinstance(runs, SQLAlchemyRunRepository)
    submit_request = request(tmp_path)
    service = ExecutionService(
        execution_repository=executions,
        session_repository=sessions,
        tool_call_repository=tool_calls,
        runner=ForeignSameKeyFailingRunner(executions),  # type: ignore[arg-type]
        run_repository=runs,
    )

    with pytest.raises(RuntimeError, match="runner failed after foreign admission"):
        await service.submit(submit_request)

    foreign = await executions.get_by_key(submit_request.execution_key)
    assert foreign is not None
    assert foreign.session_id is None and foreign.tool_call_id is None
    restored = await tool_calls.get(submit_request.tool_call_id)
    assert restored is not None and restored.status is ToolCallStatus.READY
    assert not await tool_calls.execution_claim_is_current(
        restored.id,
        execution_key=submit_request.execution_key,
        attempt_group=submit_request.attempt_group,
    )
    await database.dispose()


async def test_failed_submit_second_read_syncs_concurrent_exact_admission(
    tmp_path: Path,
) -> None:
    database, _, _, repos = await build_service(tmp_path)
    executions = repos["executions"]
    sessions = repos["sessions"]
    tool_calls = repos["tool_calls"]
    runs = repos["runs"]
    assert isinstance(executions, SQLAlchemyExecutionRepository)
    assert isinstance(sessions, SQLAlchemyAgentSessionRepository)
    assert isinstance(tool_calls, SQLAlchemyToolCallIntentRepository)
    assert isinstance(runs, SQLAlchemyRunRepository)
    submit_request = request(tmp_path)
    launch = submit_request.to_launch_request()
    concurrent = Execution(
        id="concurrent-exact-admission",
        execution_key=launch.execution_key,
        launch_fingerprint=launch.launch_fingerprint,
        run_id=launch.run_id,
        session_id=launch.session_id,
        tool_call_id=launch.tool_call_id,
        attempt_group=launch.attempt_group,
        node_id=launch.node_id,
        executor_type=launch.executor_type,
        argv=launch.argv,
        command_text=launch.command_text,
        tool_id=launch.tool_id,
        tool_version=launch.tool_version,
        cwd=str(launch.cwd),
        env_diff=launch.env,
        status=ExecutionStatus.STARTING,
        stdout_path=str(tmp_path / "concurrent-stdout.log"),
        stderr_path=str(tmp_path / "concurrent-stderr.log"),
    )
    racing_executions = AdmitOnFirstAdmissionRead(executions, concurrent)
    service = ExecutionService(
        execution_repository=racing_executions,  # type: ignore[arg-type]
        session_repository=sessions,
        tool_call_repository=tool_calls,
        runner=FailingStartRunner(),  # type: ignore[arg-type]
        run_repository=runs,
    )

    with pytest.raises(RuntimeError, match="runner start failed"):
        await service.submit(submit_request)

    assert racing_executions.reads == 2
    durable = await tool_calls.get(submit_request.tool_call_id)
    assert durable is not None and durable.status is ToolCallStatus.EXECUTING
    assert await tool_calls.execution_claim_is_current(
        durable.id,
        execution_key=submit_request.execution_key,
        attempt_group=submit_request.attempt_group,
    )
    admitted = await executions.get(concurrent.id)
    assert admitted is not None and admitted.launch_fingerprint == launch.launch_fingerprint
    await database.dispose()


async def test_runner_failure_rollback_never_overwrites_concurrent_terminal(
    tmp_path: Path,
) -> None:
    database, _, _, repos = await build_service(tmp_path)
    executions = repos["executions"]
    sessions = repos["sessions"]
    tool_calls = repos["tool_calls"]
    runs = repos["runs"]
    assert isinstance(executions, SQLAlchemyExecutionRepository)
    assert isinstance(sessions, SQLAlchemyAgentSessionRepository)
    assert isinstance(tool_calls, SQLAlchemyToolCallIntentRepository)
    assert isinstance(runs, SQLAlchemyRunRepository)
    service = ExecutionService(
        execution_repository=executions,
        session_repository=sessions,
        tool_call_repository=tool_calls,
        runner=FailingStartRunner(tool_calls),  # type: ignore[arg-type]
        run_repository=runs,
    )

    with pytest.raises(RuntimeError, match="runner start failed"):
        await service.submit(request(tmp_path))

    durable = await tool_calls.get("tool-call-1")
    assert durable is not None and durable.status is ToolCallStatus.CANCELLED
    await database.dispose()


async def test_failed_terminal_retry_claim_can_be_replayed_after_start_failure(
    tmp_path: Path,
) -> None:
    database, _, runner, repos = await build_service(tmp_path)
    executions = repos["executions"]
    sessions = repos["sessions"]
    tool_calls = repos["tool_calls"]
    runs = repos["runs"]
    assert isinstance(executions, SQLAlchemyExecutionRepository)
    assert isinstance(sessions, SQLAlchemyAgentSessionRepository)
    assert isinstance(tool_calls, SQLAlchemyToolCallIntentRepository)
    assert isinstance(runs, SQLAlchemyRunRepository)
    await set_intent_status(tool_calls, ToolCallStatus.FAILED)
    retry = request(tmp_path, attempt_group="retry-1")
    failing_service = ExecutionService(
        execution_repository=executions,
        session_repository=sessions,
        tool_call_repository=tool_calls,
        runner=FailingStartRunner(),  # type: ignore[arg-type]
        run_repository=runs,
    )

    with pytest.raises(RuntimeError, match="runner start failed"):
        await failing_service.submit(retry)
    restored = await tool_calls.get("tool-call-1")
    assert restored is not None and restored.status is ToolCallStatus.FAILED

    replay_service = ExecutionService(
        execution_repository=executions,
        session_repository=sessions,
        tool_call_repository=tool_calls,
        runner=runner,
        run_repository=runs,
    )
    replay = await replay_service.submit(retry)
    assert replay.status is ExecutionStatus.RUNNING
    assert runner.launches == 1
    await database.dispose()


@pytest.mark.parametrize(
    ("initial", "expected", "error_code"),
    [
        (ToolCallStatus.PROPOSED, ToolCallStatus.PROPOSED, "tool_call_not_waiting_approval"),
        (ToolCallStatus.WAITING_APPROVAL, ToolCallStatus.READY, None),
        (ToolCallStatus.READY, ToolCallStatus.READY, None),
        (ToolCallStatus.EXECUTING, ToolCallStatus.EXECUTING, None),
        (ToolCallStatus.COMPLETED, ToolCallStatus.COMPLETED, None),
        (ToolCallStatus.FAILED, ToolCallStatus.FAILED, None),
        (ToolCallStatus.CANCELLED, ToolCallStatus.CANCELLED, None),
        (ToolCallStatus.REJECTED, ToolCallStatus.REJECTED, "tool_call_rejected"),
    ],
)
async def test_approve_intent_status_matrix_uses_authoritative_cas_result(
    tmp_path: Path,
    initial: ToolCallStatus,
    expected: ToolCallStatus,
    error_code: str | None,
) -> None:
    database, service, _, repos = await build_service(tmp_path)
    tool_calls = repos["tool_calls"]
    assert isinstance(tool_calls, SQLAlchemyToolCallIntentRepository)
    await set_intent_status(tool_calls, initial)
    dispatcher = DeferredExecutionDispatcher(
        tool_call_repository=tool_calls,
        execution_service=service,
    )

    if error_code is None:
        resolved = await dispatcher.approve_intent("tool-call-1")
        assert resolved.status is expected
    else:
        with pytest.raises(ApplicationConflictError) as captured:
            await dispatcher.approve_intent("tool-call-1")
        assert captured.value.code == error_code

    durable = await tool_calls.get("tool-call-1")
    assert durable is not None and durable.status is expected
    await database.dispose()


@pytest.mark.parametrize("initial", list(ToolCallStatus))
async def test_reject_intent_status_matrix_is_linearizable(
    tmp_path: Path,
    initial: ToolCallStatus,
) -> None:
    database, service, _, repos = await build_service(tmp_path)
    tool_calls = repos["tool_calls"]
    assert isinstance(tool_calls, SQLAlchemyToolCallIntentRepository)
    await set_intent_status(tool_calls, initial)
    dispatcher = DeferredExecutionDispatcher(
        tool_call_repository=tool_calls,
        execution_service=service,
    )

    if initial in {ToolCallStatus.WAITING_APPROVAL, ToolCallStatus.REJECTED}:
        resolved = await dispatcher.reject_intent("tool-call-1")
        assert resolved.status is ToolCallStatus.REJECTED
        expected = ToolCallStatus.REJECTED
    else:
        with pytest.raises(ApplicationConflictError) as captured:
            await dispatcher.reject_intent("tool-call-1")
        assert captured.value.code == "tool_call_not_waiting_approval"
        expected = initial

    durable = await tool_calls.get("tool-call-1")
    assert durable is not None and durable.status is expected
    await database.dispose()


@pytest.mark.parametrize("initial", list(ToolCallStatus))
async def test_mark_intent_executing_status_matrix_is_linearizable(
    tmp_path: Path,
    initial: ToolCallStatus,
) -> None:
    database, service, _, repos = await build_service(tmp_path)
    tool_calls = repos["tool_calls"]
    assert isinstance(tool_calls, SQLAlchemyToolCallIntentRepository)
    supplied = await set_intent_status(tool_calls, initial)
    dispatcher = DeferredExecutionDispatcher(
        tool_call_repository=tool_calls,
        execution_service=service,
    )

    if initial in {ToolCallStatus.READY, ToolCallStatus.EXECUTING}:
        resolved = await dispatcher.mark_intent_executing(supplied)
        assert resolved.status is ToolCallStatus.EXECUTING
        expected = ToolCallStatus.EXECUTING
    else:
        with pytest.raises(ApplicationConflictError) as captured:
            await dispatcher.mark_intent_executing(supplied)
        assert captured.value.code == "tool_call_not_ready"
        expected = initial

    durable = await tool_calls.get("tool-call-1")
    assert durable is not None and durable.status is expected
    await database.dispose()


async def test_concurrent_approve_and_reject_have_one_authoritative_winner(
    tmp_path: Path,
) -> None:
    database, service, _, repos = await build_service(tmp_path)
    tool_calls = repos["tool_calls"]
    assert isinstance(tool_calls, SQLAlchemyToolCallIntentRepository)
    await set_intent_status(tool_calls, ToolCallStatus.WAITING_APPROVAL)
    dispatcher = DeferredExecutionDispatcher(
        tool_call_repository=tool_calls,
        execution_service=service,
    )

    results = await asyncio.gather(
        dispatcher.approve_intent("tool-call-1"),
        dispatcher.reject_intent("tool-call-1"),
        return_exceptions=True,
    )

    successes = [item for item in results if isinstance(item, ToolCallIntent)]
    conflicts = [item for item in results if isinstance(item, ApplicationConflictError)]
    assert len(successes) == 1
    assert len(conflicts) == 1
    durable = await tool_calls.get("tool-call-1")
    assert durable is not None and durable.status is successes[0].status
    if durable.status is ToolCallStatus.READY:
        assert conflicts[0].code == "tool_call_not_waiting_approval"
    else:
        assert durable.status is ToolCallStatus.REJECTED
        assert conflicts[0].code == "tool_call_rejected"
    await database.dispose()


@pytest.mark.parametrize(
    "terminal",
    [ToolCallStatus.CANCELLED, ToolCallStatus.COMPLETED],
)
async def test_stale_ready_snapshot_never_revives_authoritative_terminal_intent(
    tmp_path: Path,
    terminal: ToolCallStatus,
) -> None:
    database, service, _, repos = await build_service(tmp_path)
    tool_calls = repos["tool_calls"]
    assert isinstance(tool_calls, SQLAlchemyToolCallIntentRepository)
    stale_ready = await tool_calls.get("tool-call-1")
    assert stale_ready is not None and stale_ready.status is ToolCallStatus.READY
    authoritative, changed = await tool_calls.compare_and_set_status(
        stale_ready.id,
        expected={ToolCallStatus.READY},
        target=terminal,
    )
    assert changed is True and authoritative.status is terminal
    dispatcher = DeferredExecutionDispatcher(
        tool_call_repository=tool_calls,
        execution_service=service,
    )

    with pytest.raises(ApplicationConflictError) as captured:
        await dispatcher.mark_intent_executing(stale_ready)

    assert captured.value.code == "tool_call_not_ready"
    assert terminal.value in str(captured.value)
    durable = await tool_calls.get(stale_ready.id)
    assert durable is not None and durable.status is terminal
    await database.dispose()


@pytest.mark.parametrize(
    ("initial", "execution_status", "expected"),
    [
        (ToolCallStatus.READY, ExecutionStatus.RUNNING, ToolCallStatus.EXECUTING),
        (ToolCallStatus.EXECUTING, ExecutionStatus.RUNNING, ToolCallStatus.EXECUTING),
        (ToolCallStatus.READY, ExecutionStatus.COMPLETED, ToolCallStatus.COMPLETED),
        (ToolCallStatus.EXECUTING, ExecutionStatus.EXITED, ToolCallStatus.COMPLETED),
        (ToolCallStatus.COMPLETED, ExecutionStatus.COMPLETED, ToolCallStatus.COMPLETED),
        (ToolCallStatus.READY, ExecutionStatus.FAILED, ToolCallStatus.FAILED),
        (ToolCallStatus.EXECUTING, ExecutionStatus.HARD_TIMEOUT, ToolCallStatus.FAILED),
        (ToolCallStatus.FAILED, ExecutionStatus.FAILED, ToolCallStatus.FAILED),
        (ToolCallStatus.READY, ExecutionStatus.CANCELLED, ToolCallStatus.CANCELLED),
        (ToolCallStatus.EXECUTING, ExecutionStatus.CANCELLED, ToolCallStatus.CANCELLED),
        (ToolCallStatus.FAILED, ExecutionStatus.CANCELLED, ToolCallStatus.CANCELLED),
        (ToolCallStatus.CANCELLED, ExecutionStatus.CANCELLED, ToolCallStatus.CANCELLED),
        (ToolCallStatus.COMPLETED, ExecutionStatus.FAILED, ToolCallStatus.COMPLETED),
        (ToolCallStatus.FAILED, ExecutionStatus.COMPLETED, ToolCallStatus.FAILED),
        (ToolCallStatus.CANCELLED, ExecutionStatus.COMPLETED, ToolCallStatus.CANCELLED),
        (ToolCallStatus.COMPLETED, ExecutionStatus.CANCELLED, ToolCallStatus.COMPLETED),
    ],
)
async def test_execution_sync_cas_matrix_preserves_terminal_winner(
    tmp_path: Path,
    initial: ToolCallStatus,
    execution_status: ExecutionStatus,
    expected: ToolCallStatus,
) -> None:
    database, service, _, repos = await build_service(tmp_path)
    tool_calls = repos["tool_calls"]
    assert isinstance(tool_calls, SQLAlchemyToolCallIntentRepository)
    execution = execution_with_status(tmp_path, execution_status)
    intent = await claim_intent_for_execution(
        tool_calls,
        execution,
        status=initial,
    )

    await service._sync_intent(intent, execution)

    durable = await tool_calls.get(intent.id)
    assert durable is not None and durable.status is expected
    await database.dispose()


async def test_unique_legacy_execution_adopts_claim_before_projection(tmp_path: Path) -> None:
    database, service, _, repos = await build_service(tmp_path)
    executions = repos["executions"]
    tool_calls = repos["tool_calls"]
    assert isinstance(executions, SQLAlchemyExecutionRepository)
    assert isinstance(tool_calls, SQLAlchemyToolCallIntentRepository)
    execution = execution_with_status(tmp_path, ExecutionStatus.COMPLETED)
    _, created = await executions.create_if_absent(execution)
    assert created is True
    intent = await tool_calls.get("tool-call-1")
    assert intent is not None

    resolved = await service.sync_intent_execution(intent, execution)

    assert resolved.status is ToolCallStatus.COMPLETED
    assert (
        await tool_calls.execution_claim_is_current(
            intent.id,
            execution_key=execution.execution_key,
            attempt_group="initial",
        )
        is False
    )
    replayed, projected = await tool_calls.project_execution_status(
        intent.id,
        execution_key=execution.execution_key,
        attempt_group="initial",
        expected={ToolCallStatus.READY},
        target=ToolCallStatus.COMPLETED,
    )
    assert projected is True and replayed.status is ToolCallStatus.COMPLETED
    await database.dispose()


async def test_unique_legacy_pty_backfills_initial_attempt_and_adopts(
    tmp_path: Path,
) -> None:
    database, service, _, repos = await build_service(tmp_path)
    executions = repos["executions"]
    tool_calls = repos["tool_calls"]
    assert isinstance(executions, SQLAlchemyExecutionRepository)
    assert isinstance(tool_calls, SQLAlchemyToolCallIntentRepository)
    execution = execution_with_status(tmp_path, ExecutionStatus.COMPLETED).model_copy(
        update={
            "id": "legacy-pty-execution",
            "execution_key": "terminal:legacy-session",
            "attempt_group": None,
            "executor_type": ExecutorType.PTY,
        }
    )
    _, created = await executions.create_if_absent(execution)
    assert created is True
    intent = await tool_calls.get("tool-call-1")
    assert intent is not None

    resolved = await service.sync_intent_execution(intent, execution)

    assert resolved.status is ToolCallStatus.COMPLETED
    adopted = await executions.get(execution.id)
    assert adopted is not None and adopted.attempt_group == "initial"
    await database.dispose()


async def test_legacy_adoption_rejects_ambiguous_execution_rows(tmp_path: Path) -> None:
    database, service, _, repos = await build_service(tmp_path)
    executions = repos["executions"]
    tool_calls = repos["tool_calls"]
    assert isinstance(executions, SQLAlchemyExecutionRepository)
    assert isinstance(tool_calls, SQLAlchemyToolCallIntentRepository)
    execution = execution_with_status(tmp_path, ExecutionStatus.RUNNING)
    other = execution.model_copy(
        update={
            "id": "execution-other",
            "execution_key": "key-other",
            "attempt_group": "retry-1",
        }
    )
    assert (await executions.create_if_absent(execution))[1] is True
    assert (await executions.create_if_absent(other))[1] is True
    intent = await tool_calls.get("tool-call-1")
    assert intent is not None

    resolved = await service.sync_intent_execution(intent, execution)

    assert resolved.status is ToolCallStatus.READY
    assert not await tool_calls.execution_claim_is_current(
        intent.id,
        execution_key=execution.execution_key,
        attempt_group="initial",
    )
    await database.dispose()


async def test_old_attempt_callbacks_cannot_project_over_current_retry(
    tmp_path: Path,
) -> None:
    database, service, _, repos = await build_service(tmp_path)
    executions = repos["executions"]
    tool_calls = repos["tool_calls"]
    assert isinstance(executions, SQLAlchemyExecutionRepository)
    assert isinstance(tool_calls, SQLAlchemyToolCallIntentRepository)
    initial = await service.submit(request(tmp_path))
    failed = initial.model_copy(deep=True)
    failed.transition_to(ExecutionStatus.FAILED)
    await executions.save(failed)
    intent = await tool_calls.get("tool-call-1")
    assert intent is not None
    await service.sync_intent_execution(intent, failed)
    retry_request = request(tmp_path, attempt_group="retry-1")
    retry = await service.submit(retry_request)

    for stale_status in (
        ExecutionStatus.COMPLETED,
        ExecutionStatus.CANCELLED,
        ExecutionStatus.LOST,
    ):
        await service.sync_intent_execution(
            intent,
            failed.model_copy(update={"status": stale_status}),
        )
        durable = await tool_calls.get(intent.id)
        assert durable is not None and durable.status is ToolCallStatus.EXECUTING
        assert await tool_calls.execution_claim_is_current(
            intent.id,
            execution_key=retry.execution_key,
            attempt_group="retry-1",
        )

    with pytest.raises(ApplicationConflictError) as captured:
        await service.submit(request(tmp_path, attempt_group="retry-2"))
    assert captured.value.code == "tool_call_not_ready"
    await database.dispose()


@pytest.mark.parametrize(
    "terminal",
    [ToolCallStatus.CANCELLED, ToolCallStatus.COMPLETED],
)
async def test_execution_sync_returns_cas_loser_authority_without_reviving_terminal(
    tmp_path: Path,
    terminal: ToolCallStatus,
) -> None:
    database, service, _, repos = await build_service(tmp_path)
    tool_calls = repos["tool_calls"]
    assert isinstance(tool_calls, SQLAlchemyToolCallIntentRepository)
    stale_ready = await tool_calls.get("tool-call-1")
    assert stale_ready is not None and stale_ready.status is ToolCallStatus.READY
    authoritative, changed = await tool_calls.compare_and_set_status(
        stale_ready.id,
        expected={ToolCallStatus.READY},
        target=terminal,
    )
    assert changed is True and authoritative.status is terminal

    resolved = await service._sync_intent(
        stale_ready,
        execution_with_status(tmp_path, ExecutionStatus.RUNNING),
    )

    assert resolved.status is terminal
    durable = await tool_calls.get(stale_ready.id)
    assert durable is not None and durable.status is terminal
    await database.dispose()


async def test_stale_metadata_save_cannot_overwrite_terminal_status(tmp_path: Path) -> None:
    database, _, _, repos = await build_service(tmp_path)
    tool_calls = repos["tool_calls"]
    assert isinstance(tool_calls, SQLAlchemyToolCallIntentRepository)
    stale_ready = await tool_calls.get("tool-call-1")
    assert stale_ready is not None and stale_ready.status is ToolCallStatus.READY
    cancelled, changed = await tool_calls.compare_and_set_status(
        stale_ready.id,
        expected={ToolCallStatus.READY},
        target=ToolCallStatus.CANCELLED,
    )
    assert changed is True and cancelled.status is ToolCallStatus.CANCELLED
    stale_ready.reason = "safe metadata update"

    authoritative = await tool_calls.save(stale_ready)

    assert authoritative.status is ToolCallStatus.CANCELLED
    assert authoritative.reason == "safe metadata update"
    durable = await tool_calls.get(stale_ready.id)
    assert durable is not None and durable.status is ToolCallStatus.CANCELLED
    await database.dispose()


async def test_claim_rollback_preserves_a_concurrent_terminal_winner(
    tmp_path: Path,
) -> None:
    database, _, _, repos = await build_service(tmp_path)
    tool_calls = repos["tool_calls"]
    assert isinstance(tool_calls, SQLAlchemyToolCallIntentRepository)
    observed_failed = await set_intent_status(tool_calls, ToolCallStatus.FAILED)
    retry = request(tmp_path, attempt_group="retry-1")
    claim = await tool_calls.claim_execution(
        observed_failed.id,
        execution_key=retry.execution_key,
        attempt_group=retry.attempt_group,
    )
    assert claim.acquired is True and claim.newly_acquired is True
    authoritative, changed = await tool_calls.compare_and_set_status(
        observed_failed.id,
        expected={ToolCallStatus.EXECUTING},
        target=ToolCallStatus.CANCELLED,
    )
    assert changed is True and authoritative.status is ToolCallStatus.CANCELLED

    resolved, rolled_back = await tool_calls.rollback_execution_claim(
        claim,
        admission=ExecutionAdmissionIdentity(
            execution_id=retry.to_launch_request().execution_id,
            execution_key=retry.execution_key,
            run_id=retry.run_id,
            session_id=retry.session_id,
            tool_call_id=retry.tool_call_id,
            attempt_group=retry.attempt_group,
            executor_type=retry.executor_type,
            node_id=retry.node_id,
            argv=tuple(retry.argv),
            command_text=retry.command_text,
            tool_id=retry.tool_id,
            tool_version=retry.tool_version,
            cwd=str(retry.cwd),
            env=dict(retry.env),
            launch_fingerprint=retry.to_launch_request().launch_fingerprint,
        ),
    )

    assert rolled_back is False
    assert resolved.status is ToolCallStatus.CANCELLED
    durable = await tool_calls.get(observed_failed.id)
    assert durable is not None and durable.status is ToolCallStatus.CANCELLED
    await database.dispose()


async def test_failed_pty_start_with_admission_row_never_rolls_claim_back(
    tmp_path: Path,
) -> None:
    database, service, _, repos = await build_service(tmp_path)
    executions = repos["executions"]
    tool_calls = repos["tool_calls"]
    assert isinstance(executions, SQLAlchemyExecutionRepository)
    assert isinstance(tool_calls, SQLAlchemyToolCallIntentRepository)
    dispatcher = DeferredExecutionDispatcher(
        tool_call_repository=tool_calls,
        execution_service=service,
    )
    intent = await tool_calls.get("tool-call-1")
    assert intent is not None
    launch = terminal_launch_request(
        tmp_path,
        session_id="terminal:admitted",
        execution_id="terminal-exec:admitted",
        execution_key="terminal:terminal:admitted",
    )
    execution = execution_with_status(tmp_path, ExecutionStatus.STARTING).model_copy(
        update={
            "id": launch.execution_id,
            "execution_key": launch.execution_key,
            "launch_fingerprint": launch.launch_fingerprint,
            "run_id": launch.run_id,
            "session_id": launch.agent_session_id,
            "tool_call_id": launch.tool_call_id,
            "attempt_group": launch.attempt_group,
            "node_id": launch.node_id,
            "executor_type": ExecutorType.PTY,
            "argv": launch.argv,
            "tool_id": launch.tool_id,
            "tool_version": launch.tool_version,
            "cwd": str(launch.cwd),
            "env_diff": launch.env,
        }
    )
    claim = await dispatcher.claim_intent_execution(
        intent,
        execution_key=execution.execution_key,
        attempt_group="initial",
    )
    assert (await executions.create_if_absent(execution))[1] is True

    settled = await dispatcher.settle_failed_intent_execution_start(
        claim,
        launch_request=launch,
    )

    assert settled.status is ToolCallStatus.EXECUTING
    assert await tool_calls.execution_claim_is_current(
        intent.id,
        execution_key=execution.execution_key,
        attempt_group="initial",
    )
    await database.dispose()


async def test_failed_pty_start_rolls_back_when_deterministic_id_is_foreign(
    tmp_path: Path,
) -> None:
    database, service, _, repos = await build_service(tmp_path)
    executions = repos["executions"]
    tool_calls = repos["tool_calls"]
    assert isinstance(executions, SQLAlchemyExecutionRepository)
    assert isinstance(tool_calls, SQLAlchemyToolCallIntentRepository)
    dispatcher = DeferredExecutionDispatcher(
        tool_call_repository=tool_calls,
        execution_service=service,
    )
    intent = await tool_calls.get("tool-call-1")
    assert intent is not None
    execution_id = "terminal-exec:deterministic-collision"
    intended_key = "terminal:expected-session"
    launch = terminal_launch_request(
        tmp_path,
        session_id="expected-session",
        execution_id=execution_id,
        execution_key=intended_key,
    )
    foreign = execution_with_status(tmp_path, ExecutionStatus.STARTING).model_copy(
        update={
            "id": execution_id,
            "execution_key": "terminal:foreign-session",
            "launch_fingerprint": launch.launch_fingerprint,
            "run_id": launch.run_id,
            "session_id": launch.agent_session_id,
            "tool_call_id": launch.tool_call_id,
            "attempt_group": launch.attempt_group,
            "node_id": launch.node_id,
            "executor_type": ExecutorType.PTY,
            "argv": launch.argv,
            "tool_id": launch.tool_id,
            "tool_version": launch.tool_version,
            "cwd": str(launch.cwd),
            "env_diff": launch.env,
        }
    )
    assert (await executions.create_if_absent(foreign))[1] is True
    claim = await dispatcher.claim_intent_execution(
        intent,
        execution_key=intended_key,
        attempt_group="initial",
    )

    settled = await dispatcher.settle_failed_intent_execution_start(
        claim,
        launch_request=launch,
    )

    assert settled.status is ToolCallStatus.READY
    assert not await tool_calls.execution_claim_is_current(
        intent.id,
        execution_key=intended_key,
        attempt_group="initial",
    )
    durable_foreign = await executions.get(execution_id)
    assert durable_foreign is not None
    assert durable_foreign.execution_key == "terminal:foreign-session"
    await database.dispose()


@pytest.mark.parametrize("foreign_field", ["launch_fingerprint", "node_id"])
async def test_failed_pty_settlement_rejects_foreign_stable_launch_identity(
    tmp_path: Path,
    foreign_field: str,
) -> None:
    database, service, _, repos = await build_service(tmp_path)
    executions = repos["executions"]
    tool_calls = repos["tool_calls"]
    assert isinstance(executions, SQLAlchemyExecutionRepository)
    assert isinstance(tool_calls, SQLAlchemyToolCallIntentRepository)
    dispatcher = DeferredExecutionDispatcher(
        tool_call_repository=tool_calls,
        execution_service=service,
    )
    intent = await tool_calls.get("tool-call-1")
    assert intent is not None
    launch = terminal_launch_request(
        tmp_path,
        session_id="stable-identity-session",
        execution_id="stable-identity-execution",
        execution_key="terminal:stable-identity-session",
    )
    foreign = Execution(
        id=str(launch.execution_id),
        execution_key=str(launch.execution_key),
        launch_fingerprint=launch.launch_fingerprint,
        run_id=launch.run_id,
        session_id=launch.agent_session_id,
        tool_call_id=launch.tool_call_id,
        attempt_group=launch.attempt_group,
        node_id=launch.node_id,
        executor_type=ExecutorType.PTY,
        argv=launch.argv,
        tool_id=launch.tool_id,
        tool_version=launch.tool_version,
        cwd=str(launch.cwd),
        env_diff=launch.env,
        status=ExecutionStatus.STARTING,
        stdout_path=str(tmp_path / "foreign-stable.log"),
        stderr_path=str(tmp_path / "foreign-stable.log"),
    )
    if foreign_field == "launch_fingerprint":
        foreign.launch_fingerprint = "launch:v1:foreign"
    else:
        # A copied valid fingerprint must not override inconsistent durable fields.
        foreign.node_id = "foreign-node"
    assert (await executions.create_if_absent(foreign))[1] is True
    claim = await dispatcher.claim_intent_execution(
        intent,
        execution_key=str(launch.execution_key),
        attempt_group="initial",
    )

    settled = await dispatcher.settle_failed_intent_execution_start(
        claim,
        launch_request=launch,
    )

    assert settled.status is ToolCallStatus.READY
    assert not await tool_calls.execution_claim_is_current(
        intent.id,
        execution_key=str(launch.execution_key),
        attempt_group="initial",
    )
    await database.dispose()


async def test_submit_requires_persisted_tool_call_before_runner_launch(tmp_path: Path) -> None:
    database, service, runner, _ = await build_service(tmp_path)
    missing = request(tmp_path).model_copy(update={"tool_call_id": "missing"})

    with pytest.raises(EntityNotFoundError, match="ToolCallIntent"):
        await service.submit(missing)

    assert runner.launches == 0
    await database.dispose()


@pytest.mark.parametrize("fence_status", [RunStatus.PAUSING, RunStatus.COMPLETING])
async def test_submit_is_blocked_after_run_enters_safety_fence(
    tmp_path: Path,
    fence_status: RunStatus,
) -> None:
    database, service, runner, repos = await build_service(tmp_path)
    runs = repos["runs"]
    assert isinstance(runs, SQLAlchemyRunRepository)
    await runs.update_status("run-1", RunStatus.PREPARING)
    await runs.update_status("run-1", RunStatus.RUNNING)
    await runs.update_status("run-1", fence_status)

    with pytest.raises(ApplicationConflictError) as captured:
        await service.submit(request(tmp_path))

    assert captured.value.code == "run_execution_blocked"
    assert runner.launches == 0
    await database.dispose()


@pytest.mark.parametrize(
    ("operation", "expected_status"),
    [
        ("pause", RunStatus.PAUSED),
        ("cancel", RunStatus.CANCELLED),
    ],
)
async def test_run_stop_wins_pre_registration_race_and_delayed_execution_never_launches(
    tmp_path: Path,
    operation: str,
    expected_status: RunStatus,
) -> None:
    database, _, _, repos = await build_service(tmp_path)
    runs = repos["runs"]
    sessions = repos["sessions"]
    tool_calls = repos["tool_calls"]
    executions = repos["executions"]
    assert isinstance(runs, SQLAlchemyRunRepository)
    assert isinstance(sessions, SQLAlchemyAgentSessionRepository)
    assert isinstance(tool_calls, SQLAlchemyToolCallIntentRepository)
    assert isinstance(executions, SQLAlchemyExecutionRepository)
    await runs.update_status("run-1", RunStatus.PREPARING)
    await runs.update_status("run-1", RunStatus.RUNNING)

    delegate = RecordingRunner(executions)
    delayed = DelayedRegistrationRunner(delegate)
    service = ExecutionService(
        execution_repository=executions,
        session_repository=sessions,
        tool_call_repository=tool_calls,
        runner=delayed,  # type: ignore[arg-type]
        run_repository=runs,
    )
    events = SQLAlchemyRunEventRepository(database.session_factory)
    run_control = RunApplicationService(
        engagement_repository=object(),  # type: ignore[arg-type]
        run_repository=runs,
        event_repository=events,
        workflow_client=RunControlWorkflow(),  # type: ignore[arg-type]
        execution_repository=executions,
        execution_runner=delayed,  # type: ignore[arg-type]
        workspace_root=tmp_path,
        execution_cancel_timeout_seconds=0.01,
        execution_cancel_poll_seconds=0.001,
    )

    submit_task = asyncio.create_task(service.submit(request(tmp_path)))
    await delayed.entered.wait()
    stopped = await getattr(run_control, operation)("run-1")
    assert stopped.status is expected_status

    delayed.release.set()
    with pytest.raises(ApplicationConflictError) as captured:
        await submit_task

    assert captured.value.code == "run_execution_blocked"
    assert delegate.launches == 0
    assert list(await executions.list("run-1")) == []
    await database.dispose()


async def test_registered_starting_execution_keeps_pause_unconfirmed_until_guard_aborts(
    tmp_path: Path,
) -> None:
    database, _, _, repos = await build_service(tmp_path)
    runs = repos["runs"]
    sessions = repos["sessions"]
    tool_calls = repos["tool_calls"]
    executions = repos["executions"]
    assert isinstance(runs, SQLAlchemyRunRepository)
    assert isinstance(sessions, SQLAlchemyAgentSessionRepository)
    assert isinstance(tool_calls, SQLAlchemyToolCallIntentRepository)
    assert isinstance(executions, SQLAlchemyExecutionRepository)
    await runs.update_status("run-1", RunStatus.PREPARING)
    await runs.update_status("run-1", RunStatus.RUNNING)

    guarded_runs = BlockSecondRunRead(runs)
    supervisor = ProcessSupervisor(executions, RunnerPaths(tmp_path / "admission-state"))
    service = ExecutionService(
        execution_repository=executions,
        session_repository=sessions,
        tool_call_repository=tool_calls,
        runner=supervisor,
        run_repository=guarded_runs,  # type: ignore[arg-type]
    )
    run_control = RunApplicationService(
        engagement_repository=object(),  # type: ignore[arg-type]
        run_repository=runs,
        event_repository=SQLAlchemyRunEventRepository(database.session_factory),
        workflow_client=RunControlWorkflow(),  # type: ignore[arg-type]
        execution_repository=executions,
        execution_runner=supervisor,
        workspace_root=tmp_path,
        execution_cancel_timeout_seconds=0.01,
        execution_cancel_poll_seconds=0.001,
        execution_cancel_max_passes=1,
    )

    submit_task = asyncio.create_task(service.submit(request(tmp_path)))
    await guarded_runs.entered.wait()
    registered = list(await executions.list("run-1"))
    assert len(registered) == 1
    assert registered[0].status is ExecutionStatus.STARTING
    assert registered[0].pid is None

    with pytest.raises(ServiceUnavailableError) as captured:
        await run_control.pause("run-1")

    assert captured.value.code == "execution_cancel_failed"
    fenced = await runs.get("run-1")
    assert fenced is not None and fenced.status is RunStatus.PAUSING
    assert (await executions.get(registered[0].id)).status is ExecutionStatus.STARTING  # type: ignore[union-attr]

    guarded_runs.release.set()
    with pytest.raises(ApplicationConflictError) as blocked:
        await submit_task
    assert blocked.value.code == "run_execution_blocked"
    aborted = await executions.get(registered[0].id)
    assert aborted is not None and aborted.status is ExecutionStatus.CANCELLED

    paused = await run_control.pause("run-1")
    assert paused.status is RunStatus.PAUSED
    await supervisor.close()
    await database.dispose()


async def test_same_key_and_running_resubmission_launch_only_once(tmp_path: Path) -> None:
    database, service, runner, _ = await build_service(tmp_path)
    first = await service.submit(request(tmp_path))
    second = await service.submit(request(tmp_path))

    assert first.id == second.id
    assert second.status is ExecutionStatus.RUNNING
    assert runner.launches == 1
    assert first.execution_key == build_execution_key(
        run_id="run-1",
        session_id="session-1",
        tool_call_id="tool-call-1",
        attempt_group="initial",
    )
    await database.dispose()


async def test_same_key_replay_never_reopens_terminal_intent(tmp_path: Path) -> None:
    database, service, runner, repos = await build_service(tmp_path)
    first = await service.submit(request(tmp_path))
    tool_calls = repos["tool_calls"]
    assert isinstance(tool_calls, SQLAlchemyToolCallIntentRepository)
    intent = await tool_calls.get("tool-call-1")
    assert intent is not None
    terminal, changed = await tool_calls.compare_and_set_status(
        intent.id,
        expected={ToolCallStatus.EXECUTING},
        target=ToolCallStatus.COMPLETED,
    )
    assert changed is True and terminal.status is ToolCallStatus.COMPLETED

    replay = await service.submit(request(tmp_path))

    assert replay.id == first.id
    assert replay.status is ExecutionStatus.RUNNING
    assert runner.launches == 1
    durable = await tool_calls.get(intent.id)
    assert durable is not None and durable.status is ToolCallStatus.COMPLETED
    await database.dispose()


async def test_same_key_replay_does_not_duplicate_submission_event(tmp_path: Path) -> None:
    database, _, runner, repos = await build_service(tmp_path)
    executions = repos["executions"]
    sessions = repos["sessions"]
    tool_calls = repos["tool_calls"]
    runs = repos["runs"]
    assert isinstance(executions, SQLAlchemyExecutionRepository)
    assert isinstance(sessions, SQLAlchemyAgentSessionRepository)
    assert isinstance(tool_calls, SQLAlchemyToolCallIntentRepository)
    assert isinstance(runs, SQLAlchemyRunRepository)
    events = RecordingEvents()
    service = ExecutionService(
        execution_repository=executions,
        session_repository=sessions,
        tool_call_repository=tool_calls,
        runner=runner,
        event_repository=events,  # type: ignore[arg-type]
        run_repository=runs,
    )

    await service.submit(request(tmp_path))
    await service.submit(request(tmp_path))

    assert [event_type for _, event_type, _ in events.rows] == ["execution.submitted"]
    assert len(events.event_ids) == 1
    assert events.event_ids[0] is not None
    await database.dispose()


async def test_concurrent_same_claim_submissions_append_one_deterministic_event(
    tmp_path: Path,
) -> None:
    database, _, runner, repos = await build_service(tmp_path)
    executions = repos["executions"]
    sessions = repos["sessions"]
    tool_calls = repos["tool_calls"]
    runs = repos["runs"]
    assert isinstance(executions, SQLAlchemyExecutionRepository)
    assert isinstance(sessions, SQLAlchemyAgentSessionRepository)
    assert isinstance(tool_calls, SQLAlchemyToolCallIntentRepository)
    assert isinstance(runs, SQLAlchemyRunRepository)
    delayed = DelayedRegistrationRunner(runner)
    events = SQLAlchemyRunEventRepository(database.session_factory)
    service = ExecutionService(
        execution_repository=executions,
        session_repository=sessions,
        tool_call_repository=tool_calls,
        runner=delayed,  # type: ignore[arg-type]
        event_repository=events,
        run_repository=runs,
    )

    first = asyncio.create_task(service.submit(request(tmp_path)))
    second = asyncio.create_task(service.submit(request(tmp_path)))
    await delayed.second_entered.wait()
    delayed.release.set()
    first_result, second_result = await asyncio.gather(first, second)

    assert first_result.id == second_result.id
    assert runner.launches == 1
    submitted = [
        event
        for event in await events.list_after("run-1")
        if event.event_type == "execution.submitted"
    ]
    assert len(submitted) == 1
    await database.dispose()


@pytest.mark.parametrize(
    ("operation", "terminal"),
    [
        ("wait", ToolCallStatus.CANCELLED),
        ("cancel", ToolCallStatus.COMPLETED),
    ],
)
async def test_wait_and_cancel_never_reopen_a_terminal_intent(
    tmp_path: Path,
    operation: str,
    terminal: ToolCallStatus,
) -> None:
    database, service, _, repos = await build_service(tmp_path)
    execution = await service.submit(request(tmp_path))
    tool_calls = repos["tool_calls"]
    assert isinstance(tool_calls, SQLAlchemyToolCallIntentRepository)
    intent = await tool_calls.get("tool-call-1")
    assert intent is not None
    authoritative, changed = await tool_calls.compare_and_set_status(
        intent.id,
        expected={ToolCallStatus.EXECUTING},
        target=terminal,
    )
    assert changed is True and authoritative.status is terminal

    if operation == "wait":
        await service.wait(execution.id)
    else:
        await service.cancel(execution.id)

    durable = await tool_calls.get(intent.id)
    assert durable is not None and durable.status is terminal
    await database.dispose()


async def test_completed_resubmission_returns_durable_result(tmp_path: Path) -> None:
    database, service, runner, repos = await build_service(tmp_path)
    execution = await service.submit(request(tmp_path))
    wait_result = await service.wait(execution.id)
    completed = wait_result.execution
    duplicate = await service.submit(request(tmp_path))

    assert completed.status is ExecutionStatus.COMPLETED
    assert duplicate.id == execution.id
    assert duplicate.status is ExecutionStatus.COMPLETED
    assert runner.launches == 1
    intent = await repos["tool_calls"].get("tool-call-1")
    assert intent.status is ToolCallStatus.COMPLETED
    await database.dispose()


async def test_failed_execution_requires_new_attempt_group_for_retry(tmp_path: Path) -> None:
    database, service, runner, repos = await build_service(tmp_path)
    first = await service.submit(request(tmp_path))
    first.transition_to(ExecutionStatus.FAILED)
    await repos["executions"].save(first)

    same_attempt = await service.submit(request(tmp_path))
    retry = await service.submit(request(tmp_path, attempt_group="retry-1"))

    assert same_attempt.id == first.id
    assert same_attempt.status is ExecutionStatus.FAILED
    assert retry.id != first.id
    assert retry.attempt_group == "retry-1"
    assert retry.status is ExecutionStatus.RUNNING
    assert runner.launches == 2
    intent = await repos["tool_calls"].get("tool-call-1")
    assert intent is not None and intent.status is ToolCallStatus.EXECUTING
    await database.dispose()


@pytest.mark.parametrize(
    "terminal",
    [
        ToolCallStatus.COMPLETED,
        ToolCallStatus.FAILED,
        ToolCallStatus.CANCELLED,
    ],
)
async def test_new_attempt_group_can_reopen_the_observed_terminal_once(
    tmp_path: Path,
    terminal: ToolCallStatus,
) -> None:
    database, service, runner, repos = await build_service(tmp_path)
    first = await service.submit(request(tmp_path))
    tool_calls = repos["tool_calls"]
    assert isinstance(tool_calls, SQLAlchemyToolCallIntentRepository)
    intent = await tool_calls.get("tool-call-1")
    assert intent is not None
    authoritative, changed = await tool_calls.compare_and_set_status(
        intent.id,
        expected={ToolCallStatus.EXECUTING},
        target=terminal,
    )
    assert changed is True and authoritative.status is terminal

    retry = await service.submit(request(tmp_path, attempt_group=f"retry-{terminal.value}"))

    assert retry.id != first.id
    assert retry.status is ExecutionStatus.RUNNING
    assert runner.launches == 2
    durable = await tool_calls.get(intent.id)
    assert durable is not None and durable.status is ToolCallStatus.EXECUTING
    await database.dispose()


async def test_cancelled_execution_does_not_restart_by_default(tmp_path: Path) -> None:
    database, service, runner, _ = await build_service(tmp_path)
    first = await service.submit(request(tmp_path))
    cancelled = await service.cancel(first.id, reason="user requested")
    duplicate = await service.submit(request(tmp_path))

    assert cancelled.status is ExecutionStatus.CANCELLED
    assert duplicate.id == first.id
    assert duplicate.status is ExecutionStatus.CANCELLED
    assert runner.launches == 1
    await database.dispose()


async def test_execution_is_queryable_after_repository_reload(tmp_path: Path) -> None:
    database_path = tmp_path / "execution.db"
    database, service, _, _ = await build_service(tmp_path)
    submitted = await service.submit(request(tmp_path))
    await database.dispose()

    reopened = Database(f"sqlite+aiosqlite:///{database_path}")
    restored = await SQLAlchemyExecutionRepository(reopened.session_factory).get(submitted.id)
    assert restored is not None
    assert restored.session_id == "session-1"
    assert restored.tool_call_id == "tool-call-1"
    assert restored.attempt_group == "initial"
    assert restored.execution_key == submitted.execution_key
    await reopened.dispose()


def test_execution_status_exposes_post_v2_lifecycle() -> None:
    assert {
        ExecutionStatus.QUEUED,
        ExecutionStatus.STARTING,
        ExecutionStatus.RUNNING,
        ExecutionStatus.COMPLETED,
        ExecutionStatus.FAILED,
        ExecutionStatus.CANCELLED,
        ExecutionStatus.HARD_TIMEOUT,
        ExecutionStatus.LOST,
    } <= set(ExecutionStatus)


async def test_wait_distinguishes_lost_execution(tmp_path: Path) -> None:
    database, service, _, repos = await build_service(tmp_path)
    execution = await service.submit(request(tmp_path))
    execution.transition_to(ExecutionStatus.LOST)
    await repos["executions"].save(execution)

    result = await service.wait(execution.id, timeout_seconds=0.1)

    assert result.wait_status.value == "execution_lost"
    assert result.execution.status is ExecutionStatus.LOST
    await database.dispose()
