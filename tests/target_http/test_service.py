from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from riftx.application.errors import ApplicationConflictError
from riftx.application.services import RunSafetyStopService
from riftx.domain import (
    Engagement,
    EntryPoint,
    EntryPointKind,
    Objective,
    PentestAdmission,
    PentestBudget,
    Run,
    RunKind,
    RunnerCommand,
    RunnerCommandKind,
    RunnerCommandOrigin,
    RunnerCommandOwnership,
    RunnerCommandOwnershipState,
    RunnerCommandStatus,
    RunnerEffectBinding,
    RunnerOperationFamily,
    RunnerOutputContract,
    RunnerPrincipal,
    RunnerResourceKind,
    RunStatus,
    Scope,
    runner_payload_digest,
)
from riftx.execution import build_execution_key
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
from riftx.persistence.target_http_repositories import (
    SQLAlchemyTargetHttpRequestRepository,
)
from riftx.runner import ProcessSupervisor, RunnerPaths
from riftx.runner.target_http import RemoteTargetHttpClient, RunnerTargetHttpClient
from riftx.runtime.types import (
    AgentCycle,
    AgentSession,
    AgentStep,
    AgentStepType,
    ToolCallIntent,
    ToolCallStatus,
)
from riftx.scope import ScopeViolationError
from riftx.target_http import (
    TargetHttpExchange,
    TargetHttpRequest,
    TargetHttpResult,
    TargetHttpRunnerExecutionCancelledError,
    TargetHttpRunnerExecutionUncertainError,
    TargetHttpRunnerStopOutcome,
    TargetHttpSubmission,
)
from riftx.target_http.service import TargetHttpApplicationService


class FakeArtifacts:
    def __init__(self) -> None:
        self.commands = []

    async def register_content(self, run_id: str, command):
        self.commands.append((run_id, command))
        return SimpleNamespace(id=f"artifact-{len(self.commands)}")


class RecordingRunner:
    def __init__(self) -> None:
        self.launches = []

    async def execute(self, launch, *, effect_guard=None) -> TargetHttpExchange:
        self.launches.append(launch)
        if effect_guard is not None:
            await effect_guard()
        await asyncio.sleep(0)
        body = b'{"authorized":true}'
        return TargetHttpExchange(
            result=TargetHttpResult(
                request_id="request-1",
                execution_key=launch.request.execution_key,
                request_hash=launch.request.fingerprint,
                status_code=200,
                response_headers={"content-type": "application/json"},
                elapsed_ms=2,
                content_type="application/json",
                content_length=len(body),
                body_excerpt=body.decode(),
                final_url=launch.request.url,
            ),
            response_body=body,
        )

    async def stop_run(self, run_id, *, node_id, tool_call_ids):
        return [
            TargetHttpRunnerStopOutcome(
                tool_call_id=tool_call_id,
                confirmed=False,
                reason="recording_runner_has_no_active_task",
            )
            for tool_call_id in tool_call_ids
        ]


class BlockingExchangeRunner(RecordingRunner):
    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def execute(self, launch, *, effect_guard=None) -> TargetHttpExchange:
        self.launches.append(launch)
        if effect_guard is not None:
            await effect_guard()
        self.started.set()
        await self.release.wait()
        body = b'{"late":true}'
        return TargetHttpExchange(
            result=TargetHttpResult(
                request_id=f"late-request-{launch.tool_call_id}",
                execution_key=launch.request.execution_key,
                request_hash=launch.request.fingerprint,
                status_code=200,
                elapsed_ms=1,
                content_type="application/json",
                content_length=len(body),
                body_excerpt=body.decode(),
                final_url=launch.request.url,
            ),
            response_body=body,
        )


class UncertainExecutionRunner(RecordingRunner):
    def __init__(self, *, execute_stop_confirmed: bool) -> None:
        super().__init__()
        self.execute_stop_confirmed = execute_stop_confirmed
        self.retry_stop_confirmed = execute_stop_confirmed
        self.stop_calls = 0

    async def execute(self, launch, *, effect_guard=None) -> TargetHttpExchange:
        self.launches.append(launch)
        if effect_guard is not None:
            await effect_guard()
        raise TargetHttpRunnerExecutionUncertainError(
            "remote command wait timed out",
            stop_outcome=TargetHttpRunnerStopOutcome(
                tool_call_id=launch.tool_call_id,
                confirmed=self.execute_stop_confirmed,
                reason=(
                    "target_http_local_task_terminated"
                    if self.execute_stop_confirmed
                    else "target_http_remote_stop_unconfirmed"
                ),
            ),
        )

    async def stop_run(self, run_id, *, node_id, tool_call_ids):
        self.stop_calls += 1
        return [
            TargetHttpRunnerStopOutcome(
                tool_call_id=tool_call_id,
                confirmed=self.retry_stop_confirmed,
                reason=(
                    "target_http_local_task_terminated"
                    if self.retry_stop_confirmed
                    else "target_http_remote_stop_unconfirmed"
                ),
            )
            for tool_call_id in tool_call_ids
        ]


class CancelledExecutionRunner(RecordingRunner):
    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.stop_confirmed = False
        self.stop_calls = 0

    async def execute(self, launch, *, effect_guard=None) -> TargetHttpExchange:
        self.launches.append(launch)
        if effect_guard is not None:
            await effect_guard()
        self.started.set()
        await asyncio.Event().wait()
        raise AssertionError("cancelled Target HTTP execution unexpectedly resumed")

    async def stop_run(self, run_id, *, node_id, tool_call_ids):
        self.stop_calls += 1
        return [
            TargetHttpRunnerStopOutcome(
                tool_call_id=tool_call_id,
                confirmed=self.stop_confirmed,
                reason=(
                    "target_http_local_task_terminated"
                    if self.stop_confirmed
                    else "target_http_remote_stop_unconfirmed"
                ),
            )
            for tool_call_id in tool_call_ids
        ]


class ServiceBlockingClient:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.closed = asyncio.Event()

    def build_request(self, method, url, **kwargs) -> httpx.Request:
        return httpx.Request(
            method,
            url,
            headers=kwargs.get("headers"),
            content=kwargs.get("content"),
        )

    async def send(self, request, *, stream, follow_redirects):
        self.started.set()
        await asyncio.Event().wait()
        raise AssertionError("stopped service request unexpectedly resumed")

    async def aclose(self) -> None:
        self.closed.set()


class ServiceCloseFailureClient:
    def __init__(self) -> None:
        self.close_attempts = 0

    def build_request(self, method, url, **kwargs) -> httpx.Request:
        return httpx.Request(
            method,
            url,
            headers=kwargs.get("headers"),
            content=kwargs.get("content"),
        )

    async def send(self, request, *, stream, follow_redirects):
        return httpx.Response(
            200,
            request=request,
            headers={"content-type": "text/plain"},
            content=b"response",
        )

    async def aclose(self) -> None:
        self.close_attempts += 1
        raise OSError("client close remains unconfirmed")


class ObservedRunnerTargetHttpClient(RunnerTargetHttpClient):
    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.stop_attempted = asyncio.Event()

    async def stop_run(self, run_id, *, node_id, tool_call_ids):
        self.stop_attempted.set()
        return await super().stop_run(
            run_id,
            node_id=node_id,
            tool_call_ids=tool_call_ids,
        )


class EmptyRunResourceStopper:
    async def stop_run(self, run_id: str):
        assert run_id
        return SimpleNamespace(
            attempted_ids=(),
            node_ids={},
            observed_statuses={},
            confirmed_statuses={},
            failures={},
        )


_REMOTE_OWNER = RunnerPrincipal(instance_id="runner-target-http-service", epoch=1)


def _verified_remote_command(
    *,
    command_id: str,
    node_id: str,
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
    status: RunnerCommandStatus,
    result: dict[str, object] | None = None,
    error: str = "",
) -> RunnerCommand:
    resolved_target = target or _REMOTE_OWNER
    binding = RunnerEffectBinding(
        id=f"binding-{command_id}",
        run_id=run_id,
        run_kind=RunKind.GENERAL,
        node_id=node_id,
        target=resolved_target,
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
    return RunnerCommand(
        id=command_id,
        node_id=node_id,
        target=resolved_target,
        kind=kind,
        idempotency_key=idempotency_key,
        ownership=ownership,
        ownership_state=RunnerCommandOwnershipState.VERIFIED,
        quarantine_reason="",
        payload=payload,
        status=status,
        result=result or {},
        error=error,
    )


class FailedDeliveryControl:
    def __init__(self, *, acknowledge_cancel: bool) -> None:
        self.acknowledge_cancel = acknowledge_cancel
        self.cancel_commands = 0
        self.enqueued: list[tuple[str, RunnerCommandKind]] = []
        self.commands: dict[str, RunnerCommand] = {}

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
        self.enqueued.append((node_id, kind))
        if kind is RunnerCommandKind.TARGET_HTTP:
            command = _verified_remote_command(
                command_id="failed-delivery-command",
                node_id=node_id,
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
                status=RunnerCommandStatus.FAILED,
                error="delivery claim replay suppressed after a possible send",
            )
        else:
            self.cancel_commands += 1
            intent_id = str(payload["tool_call_ids"][0])  # type: ignore[index]
            command = _verified_remote_command(
                command_id=f"cancel-command-{self.cancel_commands}",
                node_id=node_id,
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
                status=RunnerCommandStatus.COMPLETED,
                result={
                    "outcomes": [
                        {
                            "tool_call_id": intent_id,
                            "confirmed": True,
                            "reason": "target_http_local_task_terminated",
                        }
                    ]
                },
            )
        self.commands[command.id] = command
        return command, True

    async def wait_command(
        self,
        command_id: str,
        *,
        timeout_seconds: float,
        poll_interval_seconds: float = 0.1,
    ) -> RunnerCommand:
        assert poll_interval_seconds == 0.1
        if command_id == "failed-delivery-command":
            assert timeout_seconds == 60
            return self.commands[command_id]
        assert timeout_seconds == 0.01
        if not self.acknowledge_cancel:
            raise TimeoutError("cancel ACK unavailable")
        return self.commands[command_id]

    async def read_command_output(self, command_id: str) -> bytes:
        return b""


class Events:
    def __init__(self) -> None:
        self.types = []

    async def append(self, run_id: str, event_type: str, payload=None):
        self.types.append((run_id, event_type, payload))


class KindSwitchingRuns:
    """Expose a defensive recheck that changes after initial admission."""

    def __init__(self, delegate: SQLAlchemyRunRepository) -> None:
        self.delegate = delegate
        self.reads = 0

    async def get(self, run_id: str) -> Run | None:
        run = await self.delegate.get(run_id)
        if run is None:
            return None
        self.reads += 1
        if self.reads > 1:
            return run.model_copy(update={"kind": RunKind.CODE_AUDIT})
        return run


async def build_service(
    tmp_path: Path,
    *,
    status=ToolCallStatus.READY,
    run_status=RunStatus.CREATED,
    run_kind=RunKind.GENERAL,
    runner=None,
    tool_id: str = "request_target_url",
    pentest_budget: PentestBudget | None = None,
):
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'target-http.db'}")
    await database.create_schema()
    await SQLAlchemyEngagementRepository(database.session_factory).create(
        Engagement(
            id="engagement-1",
            name="Authorized",
            authorization_reference="authorization:test-target",
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
            objective=Objective(description="Test authorized HTTP target"),
            entry_points=(
                [EntryPoint(kind=EntryPointKind.DOMAIN, value="target.internal")]
                if is_pentest
                else []
            ),
            scope=Scope(domains=["target.internal"]),
            pentest_admission=(
                PentestAdmission(
                    budget=pentest_budget
                    or PentestBudget(
                        max_duration_seconds=3600,
                        max_model_calls=100,
                        max_tokens=100_000,
                        max_tool_calls=100,
                        max_target_interactions=50,
                        max_concurrent_target_interactions=2,
                    )
                )
                if is_pentest
                else None
            ),
            status=run_status,
            workspace_path=str(tmp_path),
        )
    )
    await SQLAlchemyAgentSessionRepository(database.session_factory).create(
        AgentSession(id="session-1", run_id="run-1", model_profile="test")
    )
    await SQLAlchemyAgentCycleRepository(database.session_factory).create(
        AgentCycle(id="cycle-1", run_id="run-1", session_id="session-1", sequence=1)
    )
    await SQLAlchemyAgentStepRepository(database.session_factory).create(
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
            tool_id=tool_id,
            status=status,
        )
    )
    runner = runner or RecordingRunner()
    artifacts = FakeArtifacts()
    events = Events()
    repository = SQLAlchemyTargetHttpRequestRepository(database.session_factory)
    service = TargetHttpApplicationService(
        runs=runs,
        tool_calls=tool_calls,
        requests=repository,
        runner=runner,
        artifacts=artifacts,
        events=events,
    )
    return database, service, runner, artifacts, events, tool_calls, repository


def submission(
    url: str = "https://target.internal/api",
    *,
    tool_call_id: str = "tool-call-1",
) -> TargetHttpSubmission:
    key = build_execution_key(
        run_id="run-1",
        session_id="session-1",
        tool_call_id=tool_call_id,
        attempt_group="initial",
    )
    return TargetHttpSubmission(
        run_id="run-1",
        session_id="session-1",
        tool_call_id=tool_call_id,
        node_id="node-1",
        request=TargetHttpRequest(execution_key=key, method="GET", url=url),
    )


async def add_ready_intent(
    database: Database,
    *,
    intent_id: str,
    sequence: int,
    tool_id: str = "request_target_url",
) -> None:
    step_id = f"step-{sequence}"
    await SQLAlchemyAgentStepRepository(database.session_factory).create(
        AgentStep(
            id=step_id,
            cycle_id="cycle-1",
            sequence=sequence,
            step_type=AgentStepType.TOOL_PROPOSAL,
        )
    )
    await SQLAlchemyToolCallIntentRepository(database.session_factory).create(
        ToolCallIntent(
            id=intent_id,
            run_id="run-1",
            session_id="session-1",
            cycle_id="cycle-1",
            step_id=step_id,
            tool_id=tool_id,
            status=ToolCallStatus.READY,
        )
    )


async def test_service_requires_scope_and_ready_intent_then_saves_artifacts(
    tmp_path: Path,
) -> None:
    database, service, runner, artifacts, events, tool_calls, repository = await build_service(
        tmp_path
    )
    try:
        result = await service.execute(submission())

        assert result.request_artifact_id == "artifact-1"
        assert result.response_artifact_id == "artifact-2"
        assert len(runner.launches) == 1
        assert runner.launches[0].node_id == "node-1"
        assert runner.launches[0].scope.domains == ["target.internal"]
        assert len(artifacts.commands) == 2
        assert artifacts.commands[0][1].name.endswith("-request.json")
        assert artifacts.commands[1][1].content == b'{"authorized":true}'
        intent = await tool_calls.get("tool-call-1")
        assert intent is not None and intent.status is ToolCallStatus.COMPLETED
        assert await repository.get_by_execution_key(result.execution_key) == result
        assert await service.get_result("run-1", result.request_id) == result
        assert [item[1] for item in events.types] == [
            "target_http.request_started",
            "target_http.response_received",
        ]
        assert events.types[0][2] == {
            "url_summary": {
                "scheme": "https",
                "origin": "https://target.internal",
                "path_shape": "/…",
                "path_segment_count": 1,
            },
            "url_redacted": True,
        }
        assert events.types[1][2] == {
            "response_recorded": True,
            "status_code": 200,
        }
    finally:
        await database.dispose()


async def test_runtime_target_http_tool_id_uses_existing_execution_boundary(
    tmp_path: Path,
) -> None:
    database, service, runner, *_ = await build_service(
        tmp_path,
        tool_id="target_http_request",
    )
    try:
        result = await service.execute(submission())
        assert result.request_id == "request-1"
        assert len(runner.launches) == 1
    finally:
        await database.dispose()


async def test_duplicate_execution_key_runs_only_once_under_concurrency(
    tmp_path: Path,
) -> None:
    database, service, runner, *_ = await build_service(tmp_path)
    try:
        first, second = await asyncio.gather(
            service.execute(submission()), service.execute(submission())
        )
        assert first == second
        assert len(runner.launches) == 1
    finally:
        await database.dispose()


async def test_pentest_target_interaction_total_budget_survives_service_restart(
    tmp_path: Path,
) -> None:
    database, service, runner, *_ = await build_service(
        tmp_path,
        run_kind=RunKind.PENTEST,
        pentest_budget=PentestBudget(
            max_duration_seconds=3600,
            max_model_calls=100,
            max_tokens=100_000,
            max_tool_calls=100,
            max_target_interactions=1,
            max_concurrent_target_interactions=1,
        ),
    )
    try:
        await service.execute(submission())
        assert len(runner.launches) == 1
        await add_ready_intent(
            database,
            intent_id="tool-call-2",
            sequence=2,
        )

        restarted_runner = RecordingRunner()
        restarted_events = SQLAlchemyRunEventRepository(database.session_factory)
        restarted_tool_calls = SQLAlchemyToolCallIntentRepository(
            database.session_factory
        )
        restarted = TargetHttpApplicationService(
            runs=SQLAlchemyRunRepository(database.session_factory),
            tool_calls=restarted_tool_calls,
            requests=SQLAlchemyTargetHttpRequestRepository(database.session_factory),
            runner=restarted_runner,
            artifacts=FakeArtifacts(),
            events=restarted_events,
        )

        with pytest.raises(ApplicationConflictError) as caught:
            await restarted.execute(submission(tool_call_id="tool-call-2"))

        assert caught.value.code == "pentest_budget_exhausted"
        assert caught.value.details == {
            "run_id": "run-1",
            "budget_name": "max_target_interactions",
            "limit": 1,
            "used": 1,
        }
        assert restarted_runner.launches == []
        second = await restarted_tool_calls.get("tool-call-2")
        assert second is not None and second.status is ToolCallStatus.READY
        timeline = await restarted_events.list_after(
            "run-1",
            after_sequence=0,
            limit=100,
        )
        assert (timeline[-1].event_type, timeline[-1].payload) == (
            "pentest.budget_exhausted",
            caught.value.details,
        )
    finally:
        await database.dispose()


async def test_pentest_target_interaction_concurrency_is_claimed_atomically(
    tmp_path: Path,
) -> None:
    runner = BlockingExchangeRunner()
    database, service, _, _, events, tool_calls, _ = await build_service(
        tmp_path,
        run_kind=RunKind.PENTEST,
        runner=runner,
        pentest_budget=PentestBudget(
            max_duration_seconds=3600,
            max_model_calls=100,
            max_tokens=100_000,
            max_tool_calls=100,
            max_target_interactions=2,
            max_concurrent_target_interactions=1,
        ),
    )
    await add_ready_intent(database, intent_id="tool-call-2", sequence=2)
    first = asyncio.create_task(service.execute(submission()))
    try:
        await runner.started.wait()
        with pytest.raises(ApplicationConflictError) as caught:
            await service.execute(submission(tool_call_id="tool-call-2"))

        assert caught.value.code == "pentest_budget_exhausted"
        assert caught.value.details == {
            "run_id": "run-1",
            "budget_name": "max_concurrent_target_interactions",
            "limit": 1,
            "used": 1,
        }
        assert len(runner.launches) == 1
        second = await tool_calls.get("tool-call-2")
        assert second is not None and second.status is ToolCallStatus.READY

        runner.release.set()
        await first
        await service.execute(submission(tool_call_id="tool-call-2"))
        assert len(runner.launches) == 2
        assert any(
            event_type == "pentest.budget_exhausted"
            and payload == caught.value.details
            for _, event_type, payload in events.types
        )
    finally:
        runner.release.set()
        if not first.done():
            first.cancel()
            await asyncio.gather(first, return_exceptions=True)
        await database.dispose()


async def test_same_key_with_different_request_is_rejected(tmp_path: Path) -> None:
    database, service, *_ = await build_service(tmp_path)
    try:
        await service.execute(submission())
        with pytest.raises(ApplicationConflictError) as caught:
            await service.execute(submission("https://target.internal/other"))
        assert caught.value.code == "target_http_idempotency_conflict"
    finally:
        await database.dispose()


async def test_out_of_scope_request_never_reaches_runner(tmp_path: Path) -> None:
    database, service, runner, *_ = await build_service(tmp_path)
    try:
        with pytest.raises(ScopeViolationError):
            await service.execute(submission("http://outside.internal/admin"))
        assert runner.launches == []
    finally:
        await database.dispose()


async def test_unapproved_tool_intent_never_reaches_runner(tmp_path: Path) -> None:
    database, service, runner, *_ = await build_service(
        tmp_path, status=ToolCallStatus.WAITING_APPROVAL
    )
    try:
        with pytest.raises(ApplicationConflictError) as caught:
            await service.execute(submission())
        assert caught.value.code == "target_http_not_approved"
        assert runner.launches == []
    finally:
        await database.dispose()


@pytest.mark.parametrize(
    "run_status",
    [
        RunStatus.PAUSING,
        RunStatus.PAUSED,
        RunStatus.CANCELLING,
        RunStatus.CANCELLED,
        RunStatus.COMPLETING,
        RunStatus.COMPLETED,
        RunStatus.FAILED,
    ],
)
async def test_stopped_run_status_blocks_before_target_http_effect(
    tmp_path: Path,
    run_status: RunStatus,
) -> None:
    database, service, runner, *_ = await build_service(tmp_path, run_status=run_status)
    try:
        with pytest.raises(ApplicationConflictError) as caught:
            await service.execute(submission())

        assert caught.value.code == "run_target_http_blocked"
        assert runner.launches == []
    finally:
        await database.dispose()


async def test_code_audit_target_http_rejects_before_intent_or_external_effect(
    tmp_path: Path,
) -> None:
    database, service, runner, artifacts, events, tool_calls, repository = await build_service(
        tmp_path,
        run_kind=RunKind.CODE_AUDIT,
    )
    try:
        with pytest.raises(ApplicationConflictError) as caught:
            await service.execute(submission())

        assert caught.value.code == "run_kind_operation_unsupported"
        intent = await tool_calls.get("tool-call-1")
        assert intent is not None and intent.status is ToolCallStatus.READY
        assert runner.launches == []
        assert artifacts.commands == []
        assert events.types == []
        assert await repository.get_by_execution_key(
            submission().request.execution_key
        ) is None
    finally:
        await database.dispose()


async def test_code_audit_target_http_rejects_before_existing_result_replay(
    tmp_path: Path,
) -> None:
    database, service, runner, artifacts, events, tool_calls, repository = await build_service(
        tmp_path,
        run_kind=RunKind.CODE_AUDIT,
    )
    request = submission()
    replayed = TargetHttpResult(
        request_id="existing-request",
        execution_key=request.request.execution_key,
        request_hash=request.request.fingerprint,
        status_code=200,
        elapsed_ms=1,
        content_type="text/plain",
        content_length=8,
        body_excerpt="existing",
        final_url=request.request.url,
    )
    await repository.create(request, replayed)
    try:
        with pytest.raises(ApplicationConflictError) as caught:
            await service.execute(request)

        assert caught.value.code == "run_kind_operation_unsupported"
        intent = await tool_calls.get("tool-call-1")
        assert intent is not None and intent.status is ToolCallStatus.READY
        assert runner.launches == []
        assert artifacts.commands == []
        assert events.types == []
        assert await repository.get_by_execution_key(request.request.execution_key) == replayed
    finally:
        await database.dispose()


async def test_code_audit_target_http_stop_run_remains_available_for_safety(
    tmp_path: Path,
) -> None:
    database, service, _, _, _, tool_calls, _ = await build_service(
        tmp_path,
        run_kind=RunKind.CODE_AUDIT,
    )
    try:
        result = await service.stop_run("run-1")

        assert result.succeeded is True
        assert result.confirmed_statuses == {"tool-call-1": "cancelled"}
        intent = await tool_calls.get("tool-call-1")
        assert intent is not None and intent.status is ToolCallStatus.CANCELLED
    finally:
        await database.dispose()


async def test_code_audit_target_http_stop_run_can_converge_executing_intent(
    tmp_path: Path,
) -> None:
    runner = UncertainExecutionRunner(execute_stop_confirmed=True)
    database, service, _, _, _, tool_calls, _ = await build_service(
        tmp_path,
        status=ToolCallStatus.EXECUTING,
        run_kind=RunKind.CODE_AUDIT,
        runner=runner,
    )
    try:
        result = await service.stop_run("run-1")

        assert result.succeeded is True
        assert result.confirmed_statuses == {"tool-call-1": "cancelled"}
        assert runner.stop_calls == 1
        intent = await tool_calls.get("tool-call-1")
        assert intent is not None and intent.status is ToolCallStatus.CANCELLED
    finally:
        await database.dispose()


async def test_target_http_effect_guard_rechecks_run_kind_before_event_or_runner(
    tmp_path: Path,
) -> None:
    database, service, runner, artifacts, events, tool_calls, repository = await build_service(
        tmp_path
    )
    service._runs = KindSwitchingRuns(  # type: ignore[assignment]
        SQLAlchemyRunRepository(database.session_factory)
    )
    try:
        with pytest.raises(ApplicationConflictError) as caught:
            await service.execute(submission())

        assert caught.value.code == "run_kind_operation_unsupported"
        intent = await tool_calls.get("tool-call-1")
        assert intent is not None and intent.status is ToolCallStatus.EXECUTING
        assert runner.launches == []
        assert artifacts.commands == []
        assert events.types == []
        assert await repository.get_by_execution_key(
            submission().request.execution_key
        ) is None
    finally:
        await database.dispose()


async def test_run_stop_after_runner_await_cannot_revive_intent_or_save_result(
    tmp_path: Path,
) -> None:
    runner = BlockingExchangeRunner()
    database, service, _, artifacts, _, tool_calls, repository = await build_service(
        tmp_path,
        runner=runner,
    )
    execution = asyncio.create_task(service.execute(submission()))
    try:
        await asyncio.wait_for(runner.started.wait(), timeout=1)
        runs = SQLAlchemyRunRepository(database.session_factory)
        await runs.update_status("run-1", RunStatus.PAUSING)
        runner.release.set()

        with pytest.raises(ApplicationConflictError) as caught:
            await execution

        assert caught.value.code == "run_target_http_blocked"
        intent = await tool_calls.get("tool-call-1")
        assert intent is not None and intent.status is ToolCallStatus.CANCELLED
        assert artifacts.commands == []
        assert await repository.get_by_execution_key(submission().request.execution_key) is None
    finally:
        runner.release.set()
        if not execution.done():
            execution.cancel()
            await asyncio.gather(execution, return_exceptions=True)
        await database.dispose()


async def test_stop_run_persistently_cancels_ready_intent(tmp_path: Path) -> None:
    database, service, _, _, _, tool_calls, _ = await build_service(tmp_path)
    try:
        result = await service.stop_run("run-1")

        assert result.succeeded is True
        assert result.run_id == "run-1"
        assert result.attempted_ids == ("tool-call-1",)
        assert result.node_ids == {"tool-call-1": "node-1"}
        assert result.initial_statuses == {"tool-call-1": "ready"}
        assert result.observed_statuses == {"tool-call-1": "cancelled"}
        assert result.confirmed_ids == ("tool-call-1",)
        assert result.confirmed_statuses == {"tool-call-1": "cancelled"}
        assert result.failures == {}
        intent = await tool_calls.get("tool-call-1")
        assert intent is not None and intent.status is ToolCallStatus.CANCELLED
    finally:
        await database.dispose()


async def test_stop_run_cancels_local_inflight_request_and_returns_confirmation(
    tmp_path: Path,
) -> None:
    client = ServiceBlockingClient()
    runner = RunnerTargetHttpClient(
        node_id="node-1",
        client_factory=lambda **_: client,
        stop_timeout_seconds=1,
    )
    database, service, _, artifacts, _, tool_calls, repository = await build_service(
        tmp_path,
        runner=runner,
    )
    execution = asyncio.create_task(service.execute(submission()))
    try:
        await asyncio.wait_for(client.started.wait(), timeout=1)
        runs = SQLAlchemyRunRepository(database.session_factory)
        await runs.update_status("run-1", RunStatus.PAUSING)

        result = await service.stop_run("run-1")

        assert result.succeeded is True
        assert result.initial_statuses == {"tool-call-1": "executing"}
        assert result.confirmed_statuses == {"tool-call-1": "cancelled"}
        assert result.failures == {}
        assert client.closed.is_set()
        with pytest.raises(asyncio.CancelledError):
            await execution
        intent = await tool_calls.get("tool-call-1")
        assert intent is not None and intent.status is ToolCallStatus.CANCELLED
        assert artifacts.commands == []
        assert await repository.get_by_execution_key(submission().request.execution_key) is None
    finally:
        if not execution.done():
            execution.cancel()
            await asyncio.gather(execution, return_exceptions=True)
        await database.dispose()


async def test_two_target_http_client_instances_converge_on_owner_stop_ack(
    tmp_path: Path,
) -> None:
    """A foreign Worker re-enumerates after the real owner closes its client."""

    client = ServiceBlockingClient()
    owner_runner = RunnerTargetHttpClient(
        node_id="node-1",
        client_factory=lambda **_: client,
        stop_timeout_seconds=1,
    )
    database, owner_service, _, artifacts, events, tool_calls, repository = await build_service(
        tmp_path, runner=owner_runner
    )
    runs = SQLAlchemyRunRepository(database.session_factory)
    foreign_runner = ObservedRunnerTargetHttpClient(
        node_id="node-1",
        stop_timeout_seconds=0.05,
    )
    foreign_service = TargetHttpApplicationService(
        runs=runs,
        tool_calls=tool_calls,
        requests=repository,
        runner=foreign_runner,
        artifacts=artifacts,
        events=events,
    )
    executions = SQLAlchemyExecutionRepository(database.session_factory)
    supervisor = ProcessSupervisor(executions, RunnerPaths(tmp_path / "runner"))
    empty = EmptyRunResourceStopper()

    def safety(target_http: TargetHttpApplicationService) -> RunSafetyStopService:
        return RunSafetyStopService(
            execution_repository=executions,
            execution_runner=supervisor,
            resource_stoppers={
                "browser_sessions": empty,
                "target_http_requests": target_http,
            },
            resource_stop_poll_seconds=0.001,
            resource_stop_max_passes=100,
        )

    execution = asyncio.create_task(owner_service.execute(submission()))
    try:
        await asyncio.wait_for(client.started.wait(), timeout=1)
        await runs.update_status("run-1", RunStatus.COMPLETING)
        foreign_stop = asyncio.create_task(safety(foreign_service).stop_run("run-1"))
        await asyncio.wait_for(foreign_runner.stop_attempted.wait(), timeout=1)

        owner_result = await safety(owner_service).stop_run("run-1", drain=False)
        foreign_result = await asyncio.wait_for(foreign_stop, timeout=1)
        intent = await tool_calls.get("tool-call-1")

        assert owner_result.succeeded is True
        assert foreign_result.succeeded is True
        assert intent is not None and intent.status is ToolCallStatus.CANCELLED
        assert client.closed.is_set()
        with pytest.raises(TargetHttpRunnerExecutionCancelledError):
            await execution
    finally:
        if not execution.done():
            execution.cancel()
            await asyncio.gather(execution, return_exceptions=True)
        await supervisor.close()
        await database.dispose()


async def test_stop_run_keeps_remote_executing_intent_unconfirmed(tmp_path: Path) -> None:
    database, service, _, _, _, tool_calls, _ = await build_service(
        tmp_path,
        status=ToolCallStatus.EXECUTING,
    )
    try:
        result = await service.stop_run("run-1")

        assert result.succeeded is False
        assert result.confirmed_ids == ()
        assert result.observed_statuses == {"tool-call-1": "executing"}
        assert result.failures == {"tool-call-1": "recording_runner_has_no_active_task"}
        intent = await tool_calls.get("tool-call-1")
        assert intent is not None and intent.status is ToolCallStatus.EXECUTING
    finally:
        await database.dispose()


async def test_remote_wait_timeout_keeps_intent_executing_until_stop_ack(
    tmp_path: Path,
) -> None:
    runner = UncertainExecutionRunner(execute_stop_confirmed=False)
    database, service, _, _, _, tool_calls, _ = await build_service(
        tmp_path,
        runner=runner,
    )
    try:
        with pytest.raises(TargetHttpRunnerExecutionUncertainError):
            await service.execute(submission())

        intent = await tool_calls.get("tool-call-1")
        assert intent is not None and intent.status is ToolCallStatus.EXECUTING

        runner.retry_stop_confirmed = True
        result = await service.stop_run("run-1")

        assert result.succeeded is True
        assert result.confirmed_statuses == {"tool-call-1": "cancelled"}
        intent = await tool_calls.get("tool-call-1")
        assert intent is not None and intent.status is ToolCallStatus.CANCELLED
    finally:
        await database.dispose()


async def test_remote_wait_timeout_cancels_intent_only_with_runner_ack(tmp_path: Path) -> None:
    runner = UncertainExecutionRunner(execute_stop_confirmed=True)
    database, service, _, _, _, tool_calls, _ = await build_service(
        tmp_path,
        runner=runner,
    )
    try:
        with pytest.raises(TargetHttpRunnerExecutionUncertainError):
            await service.execute(submission())

        intent = await tool_calls.get("tool-call-1")
        assert intent is not None and intent.status is ToolCallStatus.CANCELLED
    finally:
        await database.dispose()


async def test_control_coroutine_cancel_without_runner_ack_keeps_intent_retryable(
    tmp_path: Path,
) -> None:
    runner = CancelledExecutionRunner()
    database, service, _, _, _, tool_calls, _ = await build_service(
        tmp_path,
        runner=runner,
    )
    execution = asyncio.create_task(service.execute(submission()))
    try:
        await asyncio.wait_for(runner.started.wait(), timeout=1)
        execution.cancel()
        with pytest.raises(asyncio.CancelledError):
            await execution

        assert runner.stop_calls == 1
        intent = await tool_calls.get("tool-call-1")
        assert intent is not None and intent.status is ToolCallStatus.EXECUTING

        runner.stop_confirmed = True
        result = await service.stop_run("run-1")
        assert result.succeeded is True
        intent = await tool_calls.get("tool-call-1")
        assert intent is not None and intent.status is ToolCallStatus.CANCELLED
    finally:
        if not execution.done():
            execution.cancel()
            await asyncio.gather(execution, return_exceptions=True)
        await database.dispose()


async def test_unconfirmed_local_client_close_keeps_run_fenced_and_intent_retryable(
    tmp_path: Path,
) -> None:
    client = ServiceCloseFailureClient()
    runner = RunnerTargetHttpClient(
        node_id="node-1",
        client_factory=lambda **_: client,
        stop_timeout_seconds=0.05,
    )
    database, service, _, artifacts, _, tool_calls, repository = await build_service(
        tmp_path,
        runner=runner,
    )
    runs = SQLAlchemyRunRepository(database.session_factory)
    try:
        with pytest.raises(TargetHttpRunnerExecutionUncertainError) as caught:
            await service.execute(submission())

        assert caught.value.stop_outcome.confirmed is False
        intent = await tool_calls.get("tool-call-1")
        assert intent is not None and intent.status is ToolCallStatus.EXECUTING
        assert artifacts.commands == []
        assert await repository.get_by_execution_key(submission().request.execution_key) is None

        await runs.update_status("run-1", RunStatus.CANCELLING)
        result = await service.stop_run("run-1")

        assert result.succeeded is False
        assert result.confirmed_ids == ()
        assert "client close remains unconfirmed" in result.failures["tool-call-1"]
        intent = await tool_calls.get("tool-call-1")
        assert intent is not None and intent.status is ToolCallStatus.EXECUTING
        run = await runs.get("run-1")
        assert run is not None and run.status is RunStatus.CANCELLING
        assert client.close_attempts == 2
    finally:
        await database.dispose()


@pytest.mark.parametrize("acknowledge_cancel", [False, True])
async def test_failed_remote_delivery_claim_requires_stop_ack_before_terminal_intent(
    tmp_path: Path,
    acknowledge_cancel: bool,
) -> None:
    control = FailedDeliveryControl(acknowledge_cancel=acknowledge_cancel)
    runner = RemoteTargetHttpClient(control, stop_timeout_seconds=0.01)
    database, service, _, artifacts, events, tool_calls, repository = await build_service(
        tmp_path,
        runner=runner,
    )
    try:
        with pytest.raises(TargetHttpRunnerExecutionUncertainError) as caught:
            await service.execute(submission())

        assert "delivery claim replay suppressed" in str(caught.value)
        assert caught.value.stop_outcome.confirmed is acknowledge_cancel
        intent = await tool_calls.get("tool-call-1")
        expected = ToolCallStatus.CANCELLED if acknowledge_cancel else ToolCallStatus.EXECUTING
        assert intent is not None and intent.status is expected
        assert artifacts.commands == []
        assert await repository.get_by_execution_key(submission().request.execution_key) is None
        assert all(item[1] != "target_http.request_failed" for item in events.types)
        assert [kind for _, kind in control.enqueued] == [
            RunnerCommandKind.TARGET_HTTP,
            RunnerCommandKind.TARGET_HTTP_CANCEL,
        ]

        if not acknowledge_cancel:
            control.acknowledge_cancel = True
            result = await service.stop_run("run-1")
            assert result.succeeded is True
            intent = await tool_calls.get("tool-call-1")
            assert intent is not None and intent.status is ToolCallStatus.CANCELLED
    finally:
        await database.dispose()
