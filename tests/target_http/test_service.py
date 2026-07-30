from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from riftx.application.errors import ApplicationConflictError
from riftx.domain import Engagement, Objective, Run, Scope
from riftx.execution import build_execution_key
from riftx.persistence import (
    Database,
    SQLAlchemyAgentCycleRepository,
    SQLAlchemyAgentSessionRepository,
    SQLAlchemyAgentStepRepository,
    SQLAlchemyEngagementRepository,
    SQLAlchemyRunRepository,
    SQLAlchemyToolCallIntentRepository,
)
from riftx.persistence.target_http_repositories import (
    SQLAlchemyTargetHttpRequestRepository,
)
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

    async def execute(self, launch) -> TargetHttpExchange:
        self.launches.append(launch)
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


class Events:
    def __init__(self) -> None:
        self.types = []

    async def append(self, run_id: str, event_type: str, payload=None):
        self.types.append((run_id, event_type, payload))


async def build_service(tmp_path: Path, *, status=ToolCallStatus.READY):
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'target-http.db'}")
    await database.create_schema()
    await SQLAlchemyEngagementRepository(database.session_factory).create(
        Engagement(id="engagement-1", name="Authorized")
    )
    runs = SQLAlchemyRunRepository(database.session_factory)
    await runs.create(
        Run(
            id="run-1",
            engagement_id="engagement-1",
            node_id="node-1",
            objective=Objective(description="Test authorized HTTP target"),
            scope=Scope(domains=["target.internal"]),
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
            tool_id="request_target_url",
            status=status,
        )
    )
    runner = RecordingRunner()
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


def submission(url: str = "https://target.internal/api") -> TargetHttpSubmission:
    key = build_execution_key(
        run_id="run-1",
        session_id="session-1",
        tool_call_id="tool-call-1",
        attempt_group="initial",
    )
    return TargetHttpSubmission(
        run_id="run-1",
        session_id="session-1",
        tool_call_id="tool-call-1",
        node_id="node-1",
        request=TargetHttpRequest(execution_key=key, method="GET", url=url),
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
        assert [item[1] for item in events.types] == [
            "target_http.request_started",
            "target_http.response_received",
        ]
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
