"""End-to-end tests for the shared FastAPI control plane."""

from __future__ import annotations

import asyncio
import base64
import json
import sys
from collections.abc import AsyncIterator
from dataclasses import dataclass, field, replace
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import pytest
import yaml
from agents import Model, ModelResponse, Usage
from fastapi.testclient import TestClient
from openai.types.responses import (
    ResponseFunctionToolCall,
    ResponseOutputMessage,
    ResponseOutputText,
)
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from riftx.agent import (
    AgentCycle,
    AgentCycleOutput,
    AgentRuntimeServices,
    SQLAlchemyCheckpointStore,
)
from riftx.api import APISettings, ControlPlane, create_app
from riftx.application.errors import ApplicationConflictError
from riftx.application.services import (
    ApprovalApplicationService,
    ApprovalRequestRecorder,
    ArtifactApplicationService,
    EventApplicationService,
    ExecutionApplicationService,
    FindingApplicationService,
    NodeApplicationService,
    NodeRegistration,
    ReportApplicationService,
    RunApplicationService,
    RunnerControlService,
    TerminalApplicationService,
    ToolApplicationService,
)
from riftx.context import ContextApplicationService
from riftx.domain import (
    Approval,
    Execution,
    ExecutionStatus,
    ExecutorType,
    Finding,
    FindingSeverity,
    RunnerCommandKind,
    RunStatus,
    TerminalSession,
    TerminalStatus,
    ToolCall,
)
from riftx.memory import MemoryService, MemoryWriter
from riftx.observability import RuntimeMetricName, RuntimeObservabilityService
from riftx.persistence import (
    Database,
    SQLAlchemyApprovalRepository,
    SQLAlchemyArtifactRepository,
    SQLAlchemyEngagementRepository,
    SQLAlchemyExecutionRepository,
    SQLAlchemyFindingRepository,
    SQLAlchemyNodeRepository,
    SQLAlchemyReportRepository,
    SQLAlchemyRunEventRepository,
    SQLAlchemyRunnerCommandRepository,
    SQLAlchemyRunnerCredentialRepository,
    SQLAlchemyRunRepository,
    SQLAlchemyTerminalRepository,
)
from riftx.persistence.context_repositories import SQLAlchemyContextCompilationRepository
from riftx.persistence.memory_repositories import SQLAlchemyMemoryRepository
from riftx.persistence.observability_repository import (
    SQLAlchemyRuntimeObservabilityRepository,
)
from riftx.runner import ProcessSupervisor, RunnerPaths, TerminalSupervisor
from riftx.runner.remote_terminal import NodeTerminalRouter, RemoteTerminalSupervisor
from riftx.skills import create_default_skill_registry
from riftx.temporal import RiftXRunWorkflow, WorkflowPhase
from riftx.temporal.activities import RiftXActivities
from riftx.temporal.runtime import TemporalRunClient, TemporalRuntimeConfig
from riftx.tools import ToolRegistry

FAKE_TOOL_FIXTURE = Path(__file__).parents[2] / "tools" / "fixtures" / "fake_tool.py"


class FullRunModel(Model):
    """Deterministic model that drives the complete host-native E2E lifecycle."""

    def __init__(self) -> None:
        self.calls = 0

    async def get_response(self, *args: Any, **kwargs: Any) -> ModelResponse:
        outputs = [
            _e2e_tool_call(
                "run_registered_tool",
                {
                    "tool_id": "fake-success",
                    "args": ["e2e"],
                    "timeout_seconds": None,
                    "reason": "Run the deterministic authorized verification fixture.",
                },
                call_id="tool-call-e2e",
            ),
            _e2e_tool_call(
                "create_finding",
                {
                    "title": "Deterministic test service exposure",
                    "severity": "high",
                    "affected_assets": ["127.0.0.1"],
                    "description": "The authorized fixture returned a successful service response.",
                    "evidence": [
                        {
                            "artifact_id": None,
                            "execution_id": None,
                            "description": "Observed fake-success output in the host execution.",
                            "location": "agent.tool_completed",
                        }
                    ],
                    "reproduction_steps": ["Run fake-success with argument e2e"],
                    "impact": "Confirms the end-to-end finding pipeline.",
                    "recommendation": "Retain this fixture only for automated tests.",
                },
                call_id="finding-call-e2e",
            ),
            _e2e_tool_call(
                "complete_run",
                {"run_summary": "Verification executed and one supported finding was recorded."},
                call_id="complete-call-e2e",
            ),
            _e2e_message(
                AgentCycleOutput(
                    assistant_message="The deterministic verification run completed.",
                    plan_summary="Execute the fixture, record the finding, and generate reports.",
                    run_summary="Verification executed and one supported finding was recorded.",
                )
            ),
        ]
        output = outputs[self.calls]
        self.calls += 1
        return ModelResponse(
            output=output,
            usage=Usage(),
            response_id=f"e2e-response-{self.calls}",
        )

    def stream_response(self, *args: Any, **kwargs: Any) -> AsyncIterator[Any]:
        async def generate() -> AsyncIterator[Any]:
            if False:
                yield None

        return generate()


def _e2e_tool_call(
    name: str,
    arguments: dict[str, object],
    *,
    call_id: str,
) -> list[Any]:
    return [
        ResponseFunctionToolCall(
            arguments=json.dumps(arguments),
            call_id=call_id,
            name=name,
            type="function_call",
            status="completed",
        )
    ]


def _e2e_message(output: AgentCycleOutput) -> list[Any]:
    return [
        ResponseOutputMessage(
            id="e2e-message",
            role="assistant",
            status="completed",
            type="message",
            content=[
                ResponseOutputText(
                    annotations=[],
                    text=output.model_dump_json(),
                    type="output_text",
                )
            ],
        )
    ]


@dataclass
class FakeWorkflowClient:
    fail: bool = False
    error: Exception | None = None
    calls: list[tuple[str, str, str | None]] = field(default_factory=list)

    async def start_run(self, run_id: str) -> object:
        self._record("start", run_id)
        return object()

    async def pause(self, run_id: str) -> None:
        self._record("pause", run_id)

    async def resume(self, run_id: str) -> None:
        self._record("resume", run_id)

    async def approve(self, run_id: str, call_id: str) -> None:
        self._record("approve", run_id, call_id)

    async def reject(self, run_id: str, call_id: str) -> None:
        self._record("reject", run_id, call_id)

    async def cancel_current_execution(self, run_id: str) -> None:
        self._record("cancel_current_execution", run_id)

    async def cancel(self, run_id: str) -> None:
        self._record("cancel", run_id)

    async def compact(self, run_id: str, max_history_items: int = 100) -> None:
        self._record("compact", run_id, str(max_history_items))

    async def switch_model(self, run_id: str, model_profile: str) -> None:
        self._record("switch_model", run_id, model_profile)

    async def append_user_message(self, run_id: str, message: str) -> None:
        self._record("message", run_id, message)

    def workflow_id(self, run_id: str) -> str:
        return f"test-workflow-{run_id}"

    def _record(self, action: str, run_id: str, detail: str | None = None) -> None:
        if self.error is not None:
            raise self.error
        if self.fail:
            raise RuntimeError("Temporal test outage")
        self.calls.append((action, run_id, detail))


@dataclass
class RuntimeFixture:
    control_plane: ControlPlane
    workflow: FakeWorkflowClient | TemporalRunClient
    finding_repository: SQLAlchemyFindingRepository
    approval_repository: SQLAlchemyApprovalRepository
    artifact_repository: SQLAlchemyArtifactRepository
    report_repository: SQLAlchemyReportRepository
    run_repository: SQLAlchemyRunRepository
    execution_repository: SQLAlchemyExecutionRepository
    terminal_repository: SQLAlchemyTerminalRepository


async def _build_runtime(
    tmp_path: Path,
    *,
    database_path: Path | None = None,
    workflow: FakeWorkflowClient | TemporalRunClient | None = None,
) -> RuntimeFixture:
    db_path = database_path or (tmp_path / "riftx.db")
    database = Database(f"sqlite+aiosqlite:///{db_path}")
    await database.create_schema()

    tools_path = tmp_path / "tools.yaml"
    if not tools_path.exists():
        tools_path.write_text(
            """\
version: 1
execution_policy: registered_only
tools:
  python:
    command: [python]
    capabilities: [scripting]
"""
        )
    registry = ToolRegistry(tools_path, node_id="local")
    await registry.refresh()

    engagement_repository = SQLAlchemyEngagementRepository(database.session_factory)
    run_repository = SQLAlchemyRunRepository(database.session_factory)
    event_repository = SQLAlchemyRunEventRepository(database.session_factory)
    finding_repository = SQLAlchemyFindingRepository(database.session_factory)
    node_repository = SQLAlchemyNodeRepository(database.session_factory)
    artifact_repository = SQLAlchemyArtifactRepository(database.session_factory)
    report_repository = SQLAlchemyReportRepository(database.session_factory)
    approval_repository = SQLAlchemyApprovalRepository(database.session_factory)
    execution_repository = SQLAlchemyExecutionRepository(database.session_factory)
    terminal_repository = SQLAlchemyTerminalRepository(database.session_factory)
    runner_credential_repository = SQLAlchemyRunnerCredentialRepository(database.session_factory)
    runner_command_repository = SQLAlchemyRunnerCommandRepository(database.session_factory)
    context_repository = SQLAlchemyContextCompilationRepository(database.session_factory)
    memory_repository = SQLAlchemyMemoryRepository(database.session_factory)
    node_service = NodeApplicationService(node_repository)
    terminal_supervisor = TerminalSupervisor(
        terminal_repository=terminal_repository,
        execution_repository=execution_repository,
        event_repository=event_repository,
        paths=RunnerPaths(tmp_path / "runner"),
        termination_grace_seconds=0.1,
    )
    workflow_client = workflow or FakeWorkflowClient()
    settings = APISettings(
        database_url=database.url,
        tools_config_path=tools_path,
        workspace_root=tmp_path / "workspaces",
        runner_state_path=tmp_path / "runner",
        sse_poll_interval_seconds=0.001,
        sse_heartbeat_seconds=0.005,
        runner_registration_token="test-bootstrap",
        runner_command_lease_seconds=0.05,
    )
    runner_paths = RunnerPaths(settings.runner_state_path)
    process_supervisor = ProcessSupervisor(execution_repository, runner_paths)
    artifact_service = ArtifactApplicationService(
        run_repository=run_repository,
        execution_repository=execution_repository,
        artifact_repository=artifact_repository,
        event_repository=event_repository,
        paths=runner_paths,
    )
    runner_control_service = RunnerControlService(
        credentials=runner_credential_repository,
        commands=runner_command_repository,
        nodes=node_service,
        executions=execution_repository,
        paths=runner_paths,
        registration_token=settings.runner_registration_token,
        terminals=terminal_repository,
        events=event_repository,
        lease_duration=timedelta(seconds=settings.runner_command_lease_seconds),
    )
    remote_terminal_supervisor = RemoteTerminalSupervisor(
        terminal_repository=terminal_repository,
        execution_repository=execution_repository,
        event_repository=event_repository,
        control=runner_control_service,
        paths=runner_paths,
    )
    terminal_controller = NodeTerminalRouter(
        local_node_id=settings.node_id,
        terminal_repository=terminal_repository,
        execution_repository=execution_repository,
        local=terminal_supervisor,
        remote=remote_terminal_supervisor,
    )
    return RuntimeFixture(
        control_plane=ControlPlane(
            settings=settings,
            database=database,
            run_service=RunApplicationService(
                engagement_repository=engagement_repository,
                run_repository=run_repository,
                event_repository=event_repository,
                workflow_client=workflow_client,
                execution_repository=execution_repository,
                execution_runner=process_supervisor,
                workspace_root=settings.workspace_root,
                execution_cancel_timeout_seconds=0.2,
                execution_cancel_poll_seconds=0.01,
            ),
            event_service=EventApplicationService(
                run_repository=run_repository,
                event_repository=event_repository,
            ),
            execution_service=ExecutionApplicationService(
                run_repository=run_repository,
                execution_repository=execution_repository,
                event_repository=event_repository,
                runner=process_supervisor,
            ),
            node_service=node_service,
            runner_control_service=runner_control_service,
            finding_service=FindingApplicationService(
                run_repository=run_repository,
                finding_repository=finding_repository,
                artifact_repository=artifact_repository,
                execution_repository=execution_repository,
                event_repository=event_repository,
                memory_writer=MemoryWriter(memory_repository),
            ),
            report_service=ReportApplicationService(
                run_repository=run_repository,
                finding_repository=finding_repository,
                artifact_repository=artifact_repository,
                report_repository=report_repository,
                event_repository=event_repository,
                artifact_service=artifact_service,
            ),
            tool_service=ToolApplicationService(registry),
            approval_service=ApprovalApplicationService(
                approval_repository=approval_repository,
                run_repository=run_repository,
                event_repository=event_repository,
                workflow_client=workflow_client,
            ),
            artifact_service=artifact_service,
            context_service=ContextApplicationService(context_repository),
            memory_service=MemoryService(memory_repository),
            runtime_observability_service=RuntimeObservabilityService(
                SQLAlchemyRuntimeObservabilityRepository(database.session_factory)
            ),
            terminal_service=TerminalApplicationService(
                run_repository=run_repository,
                supervisor=terminal_controller,
                artifact_service=artifact_service,
                event_repository=event_repository,
            ),
            terminal_supervisor=terminal_supervisor,
            process_supervisor=process_supervisor,
        ),
        workflow=workflow_client,
        finding_repository=finding_repository,
        approval_repository=approval_repository,
        artifact_repository=artifact_repository,
        report_repository=report_repository,
        run_repository=run_repository,
        execution_repository=execution_repository,
        terminal_repository=terminal_repository,
    )


async def _client(control_plane: ControlPlane) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(control_plane=control_plane)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            yield client


async def _create_run(client: httpx.AsyncClient) -> dict[str, object]:
    response = await client.post(
        "/api/v1/runs",
        json={
            "objective": "Inspect the local service",
            "model_profile": "fast",
            "engagement": {"name": "Local authorized test"},
            "success_criteria": [{"description": "Identify the exposed service", "required": True}],
            "entry_points": [{"kind": "url", "value": "http://127.0.0.1"}],
            "scope": {"ips": ["127.0.0.1"]},
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


@pytest.mark.asyncio
async def test_run_crud_control_and_message_timeline(tmp_path: Path) -> None:
    runtime = await _build_runtime(tmp_path)
    try:
        async for client in _client(runtime.control_plane):
            created = await _create_run(client)
            run_id = str(created["id"])
            assert created["node_id"] == "local"
            assert created["status"] == "created"
            assert created["model_profile"] == "fast"
            assert created["temporal_workflow_id"] == f"test-workflow-{run_id}"

            listed = await client.get("/api/v1/runs")
            assert listed.status_code == 200
            assert [item["id"] for item in listed.json()["items"]] == [run_id]

            shown = await client.get(f"/api/v1/runs/{run_id}")
            assert shown.status_code == 200
            assert shown.json()["objective"]["description"] == "Inspect the local service"

            metrics = await client.get(f"/api/v1/runs/{run_id}/metrics")
            assert metrics.status_code == 200
            metric_payload = metrics.json()
            assert set(metric_payload["metrics"]) == {metric.value for metric in RuntimeMetricName}
            assert metric_payload["metrics"]["task_completion_rate"] == {
                "name": "task_completion_rate",
                "numerator": 0,
                "denominator": 1,
                "value": 0.0,
                "available": True,
                "direction": "higher_is_better",
                "description": "Completed tasks divided by observed tasks.",
            }

            assert (await client.post(f"/api/v1/runs/{run_id}/pause")).status_code == 202
            assert (await client.post(f"/api/v1/runs/{run_id}/resume")).status_code == 202
            assert (
                await client.post(f"/api/v1/runs/{run_id}/cancel-current-execution")
            ).status_code == 202
            message = await client.post(
                f"/api/v1/runs/{run_id}/message",
                json={"message": "Focus on the HTTP endpoint"},
            )
            assert message.status_code == 202
            compact = await client.post(
                f"/api/v1/runs/{run_id}/compact",
                json={"max_history_items": 25},
            )
            assert compact.status_code == 202
            assert ("compact", run_id, "25") in runtime.workflow.calls
            switched = await client.post(
                f"/api/v1/runs/{run_id}/model",
                json={"model_profile": "deep"},
            )
            assert switched.status_code == 202
            assert ("switch_model", run_id, "deep") in runtime.workflow.calls
            assert (await client.post(f"/api/v1/runs/{run_id}/cancel")).status_code == 202

            assert [call[0] for call in runtime.workflow.calls] == [
                "start",
                "pause",
                "resume",
                "cancel_current_execution",
                "message",
                "compact",
                "switch_model",
                "cancel",
            ]
            events = await client.get(f"/api/v1/runs/{run_id}/events", params={"limit": 20})
            assert events.status_code == 200
            event_types = [item["event_type"] for item in events.json()["items"]]
            assert event_types == [
                "run.created",
                "workflow.started",
                "run.status_changed",
                "run.pause_requested",
                "run.status_changed",
                "run.status_changed",
                "run.resume_requested",
                "execution.cancel_requested",
                "user.message_queued",
                "agent.context_compaction_requested",
                "agent.model_switch_requested",
                "run.status_changed",
                "run.cancel_requested",
                "run.status_changed",
            ]
            message_event = next(
                item
                for item in events.json()["items"]
                if item["event_type"] == "user.message_queued"
            )
            assert message_event["payload"]["message"] == "Focus on the HTTP endpoint"
            assert ("message", run_id, message_event["id"]) in runtime.workflow.calls
    finally:
        await runtime.control_plane.close()


@pytest.mark.asyncio
async def test_manual_memory_management_scope_search_and_lifecycle(tmp_path: Path) -> None:
    runtime = await _build_runtime(tmp_path)
    try:
        async for client in _client(runtime.control_plane):
            missing_source = await client.post(
                "/api/v1/memories",
                json={
                    "memory_type": "semantic",
                    "scope_type": "engagement",
                    "scope_id": "engagement-1",
                    "title": "Invalid",
                    "content": "No source",
                    "summary": "No source",
                    "source_refs": [],
                },
            )
            assert missing_source.status_code == 422
            first = await client.post(
                "/api/v1/memories",
                json={
                    "memory_type": "semantic",
                    "scope_type": "engagement",
                    "scope_id": "engagement-1",
                    "title": "Staging proxy",
                    "content": "Use SOCKS5 on 127.0.0.1:1080.",
                    "summary": "Staging SOCKS5 proxy",
                    "retrieval_keywords": ["proxy", "socks5"],
                    "confidence": 0.9,
                    "importance": 0.8,
                    "source_refs": ["user://messages/message-1"],
                },
            )
            assert first.status_code == 201, first.text
            first_id = first.json()["id"]
            other = await client.post(
                "/api/v1/memories",
                json={
                    "memory_type": "semantic",
                    "scope_type": "engagement",
                    "scope_id": "engagement-2",
                    "title": "Other customer proxy",
                    "content": "Use another proxy.",
                    "summary": "Other proxy",
                    "retrieval_keywords": ["proxy"],
                    "source_refs": ["user://messages/message-2"],
                },
            )
            assert other.status_code == 201

            scoped = await client.get(
                "/api/v1/memories",
                params={"scope_type": "engagement", "scope_id": "engagement-1"},
            )
            assert [item["id"] for item in scoped.json()["items"]] == [first_id]
            edited = await client.patch(
                f"/api/v1/memories/{first_id}",
                json={"summary": "Updated staging proxy"},
            )
            assert edited.status_code == 200
            assert edited.json()["summary"] == "Updated staging proxy"
            pinned = await client.post(
                f"/api/v1/memories/{first_id}/pin",
                json={"pinned": True},
            )
            assert pinned.json()["pinned"] is True
            search = await client.get(
                "/api/v1/memories/search",
                params={"q": "unrelated", "engagement_id": "engagement-1"},
            )
            assert [item["id"] for item in search.json()["items"]] == [first_id]

            replacement = await client.post(
                "/api/v1/memories",
                json={
                    "memory_type": "semantic",
                    "scope_type": "engagement",
                    "scope_id": "engagement-1",
                    "title": "Replacement proxy",
                    "content": "Use SOCKS5 on 127.0.0.1:2080.",
                    "summary": "Replacement SOCKS5 proxy",
                    "retrieval_keywords": ["proxy", "socks5"],
                    "source_refs": ["artifact://runs/run-1/executions/ex-1/stdout"],
                    "supersedes": first_id,
                },
            )
            assert replacement.status_code == 201, replacement.text
            replacement_id = replacement.json()["id"]
            deleted = await client.delete(f"/api/v1/memories/{replacement_id}")
            assert deleted.status_code == 200
            assert deleted.json()["status"] == "deleted"
            inactive = await client.get(
                "/api/v1/memories",
                params={
                    "scope_type": "engagement",
                    "scope_id": "engagement-1",
                    "include_inactive": True,
                },
            )
            assert {item["status"] for item in inactive.json()["items"]} == {
                "superseded",
                "deleted",
            }
    finally:
        await runtime.control_plane.close()


@pytest.mark.asyncio
async def test_sse_resumes_from_last_event_id(tmp_path: Path) -> None:
    runtime = await _build_runtime(tmp_path)
    try:
        async for client in _client(runtime.control_plane):
            created = await _create_run(client)
            run_id = str(created["id"])
            await client.post(
                f"/api/v1/runs/{run_id}/message",
                json={"message": "Continue from here"},
            )

            response = await client.get(
                f"/api/v1/runs/{run_id}/events/stream",
                headers={"Last-Event-ID": "2"},
                params={"follow": "false"},
            )
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("text/event-stream")
            assert "id: 3" in response.text
            assert "event: user.message_queued" in response.text
            assert "id: 1" not in response.text
            assert "id: 2" not in response.text

            invalid = await client.get(
                f"/api/v1/runs/{run_id}/events/stream",
                headers={"Last-Event-ID": "not-a-sequence"},
                params={"follow": "false"},
            )
            assert invalid.status_code == 400
            assert invalid.json()["error"]["code"] == "invalid_last_event_id"
    finally:
        await runtime.control_plane.close()


@pytest.mark.asyncio
async def test_tools_and_findings_share_persisted_control_plane(tmp_path: Path) -> None:
    runtime = await _build_runtime(tmp_path)
    try:
        async for client in _client(runtime.control_plane):
            tools = await client.get("/api/v1/nodes/local/tools")
            assert tools.status_code == 200
            assert tools.json()["execution_policy"] == "registered_only"
            assert tools.json()["tools"][0]["definition"]["id"] == "python"
            assert tools.json()["tools"][0]["state"]["availability"] == "available"

            refreshed = await client.post("/api/v1/nodes/local/refresh-tools")
            assert refreshed.status_code == 200
            assert refreshed.json()["generation"] == tools.json()["generation"] + 1

            updated = await client.put(
                "/api/v1/nodes/local/tools/python",
                json={
                    "enabled": False,
                    "command": ["python"],
                    "executor": "process",
                    "capabilities": ["scripting", "edited"],
                    "approval": "sensitive",
                    "timeout": 45,
                    "environment": {"RIFTX_EDITED": "1"},
                },
            )
            assert updated.status_code == 200
            updated_payload = updated.json()
            assert updated_payload["generation"] == refreshed.json()["generation"] + 1
            definition = updated_payload["tools"][0]["definition"]
            assert definition["enabled"] is False
            assert definition["capabilities"] == ["scripting", "edited"]
            assert updated_payload["tools"][0]["state"]["availability"] == "disabled"
            persisted_text = await asyncio.to_thread(
                runtime.control_plane.settings.tools_config_path.read_text
            )
            persisted_tools = yaml.safe_load(persisted_text)
            assert persisted_tools["tools"]["python"]["approval"] == "sensitive"

            missing_node = await client.get("/api/v1/nodes/remote/tools")
            assert missing_node.status_code == 404
            assert missing_node.json()["error"]["code"] == "node_not_found"

            created = await _create_run(client)
            run_id = str(created["id"])
            finding = Finding(
                run_id=run_id,
                title="Exposed development service",
                severity=FindingSeverity.MEDIUM,
                description="The endpoint exposes development metadata.",
            )
            await runtime.finding_repository.create(finding)

            findings = await client.get(f"/api/v1/runs/{run_id}/findings")
            assert findings.status_code == 200
            assert findings.json()["items"][0]["id"] == finding.id
    finally:
        await runtime.control_plane.close()


@pytest.mark.asyncio
async def test_unified_errors_and_temporal_outage(tmp_path: Path) -> None:
    runtime = await _build_runtime(tmp_path, workflow=FakeWorkflowClient(fail=True))
    try:
        async for client in _client(runtime.control_plane):
            missing = await client.get("/api/v1/runs/missing")
            assert missing.status_code == 404
            assert missing.json() == {
                "error": {
                    "code": "run_not_found",
                    "message": "Run 'missing' was not found",
                    "details": {"entity": "Run", "entity_id": "missing"},
                }
            }

            invalid = await client.post("/api/v1/runs", json={"objective": ""})
            assert invalid.status_code == 422
            assert invalid.json()["error"]["code"] == "validation_error"

            missing_route = await client.get("/api/v1/not-a-route")
            assert missing_route.status_code == 404
            assert missing_route.json()["error"]["code"] == "route_not_found"

            unavailable = await client.post(
                "/api/v1/runs",
                json={"objective": "Saved despite outage"},
            )
            assert unavailable.status_code == 503
            assert unavailable.json()["error"]["code"] == "temporal_unavailable"
            run_id = unavailable.json()["error"]["details"]["run_id"]

            persisted = await client.get(f"/api/v1/runs/{run_id}")
            assert persisted.status_code == 200
            events = await client.get(f"/api/v1/runs/{run_id}/events")
            assert [item["event_type"] for item in events.json()["items"]] == [
                "run.created",
                "workflow.start_failed",
            ]
    finally:
        await runtime.control_plane.close()


@pytest.mark.asyncio
async def test_api_restart_recovers_runs_from_sqlite(tmp_path: Path) -> None:
    database_path = tmp_path / "durable.db"
    first = await _build_runtime(tmp_path, database_path=database_path)
    async for client in _client(first.control_plane):
        created = await _create_run(client)
        run_id = str(created["id"])
    await first.control_plane.close()

    second = await _build_runtime(tmp_path, database_path=database_path)
    try:
        async for client in _client(second.control_plane):
            recovered = await client.get(f"/api/v1/runs/{run_id}")
            assert recovered.status_code == 200
            assert recovered.json()["id"] == run_id
            assert recovered.json()["objective"]["description"] == "Inspect the local service"
    finally:
        await second.control_plane.close()


def test_api_settings_load_web_dist_from_environment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    web_dist = tmp_path / "web-dist"
    monkeypatch.setenv("RIFTX_WEB_DIST", str(web_dist))

    assert APISettings.from_environment().web_dist_path == web_dist


@pytest.mark.asyncio
async def test_built_web_ui_uses_spa_fallback(tmp_path: Path) -> None:
    runtime = await _build_runtime(tmp_path)
    web_dist = tmp_path / "web-dist"
    web_dist.mkdir()
    (web_dist / "index.html").write_text("<html><body>RiftX WebUI</body></html>")
    runtime.control_plane.settings = replace(
        runtime.control_plane.settings,
        web_dist_path=web_dist,
    )
    try:
        async for client in _client(runtime.control_plane):
            response = await client.get("/runs/example-run")
            assert response.status_code == 200
            assert "RiftX WebUI" in response.text
    finally:
        await runtime.control_plane.close()


@pytest.mark.asyncio
async def test_approval_endpoints_decide_and_recover_after_restart(tmp_path: Path) -> None:
    database_path = tmp_path / "approval-control-plane.db"
    runtime = await _build_runtime(tmp_path, database_path=database_path)
    async for client in _client(runtime.control_plane):
        run = await _create_run(client)
        tool_call = ToolCall(
            id="tool-call-1",
            sdk_call_id="sdk-call-1",
            run_id=str(run["id"]),
            agent_step_id="step-1",
            tool_id="python",
            arguments={"args": ["--version"]},
        )
        approval = Approval(
            id="approval-1",
            run_id=str(run["id"]),
            tool_call_id=tool_call.id,
            tool_name="python",
            command=["python", "--version"],
            cwd=str(tmp_path),
            target_summary="ip:127.0.0.1",
            env_diff={"RIFTX_TEST": "1"},
            reason="Verify the local runtime.",
        )
        await runtime.approval_repository.create_request(tool_call, approval)

        listed = await client.get(f"/api/v1/runs/{run['id']}/approvals")
        assert listed.status_code == 200
        assert listed.json()["items"][0]["command"] == ["python", "--version"]

        approved = await client.post(
            "/api/v1/approvals/approval-1/approve",
            json={"decided_by": "api-user", "approve_for_run": True},
        )
        assert approved.status_code == 200
        assert approved.json()["status"] == "approved"
        assert await runtime.approval_repository.is_granted(str(run["id"]), "python")
        assert ("approve", str(run["id"]), "approval-1") in runtime.workflow.calls

        rejected_call = ToolCall(
            id="tool-call-2",
            sdk_call_id="sdk-call-2",
            run_id=str(run["id"]),
            agent_step_id="step-1",
            tool_id="python",
            arguments={"args": ["unsafe.py"]},
        )
        rejected_request = Approval(
            id="approval-2",
            run_id=str(run["id"]),
            tool_call_id=rejected_call.id,
            tool_name="python",
            command=["python", "unsafe.py"],
            cwd=str(tmp_path),
            target_summary="ip:127.0.0.1",
            reason="Execute a follow-up action.",
        )
        await runtime.approval_repository.create_request(rejected_call, rejected_request)
        rejected = await client.post(
            "/api/v1/approvals/approval-2/reject",
            json={"decided_by": "api-user", "reason": "Outside authorized scope"},
        )
        assert rejected.status_code == 200
        assert rejected.json()["reason"] == "Outside authorized scope"
        assert ("reject", str(run["id"]), "approval-2") in runtime.workflow.calls

    await runtime.control_plane.close()

    restarted = await _build_runtime(tmp_path, database_path=database_path)
    async for client in _client(restarted.control_plane):
        response = await client.get(f"/api/v1/runs/{run['id']}/approvals?status=approved")
        assert response.status_code == 200
        assert [item["id"] for item in response.json()["items"]] == ["approval-1"]
    await restarted.control_plane.close()


@pytest.mark.asyncio
async def test_approval_decision_remains_durable_when_temporal_signal_fails(
    tmp_path: Path,
) -> None:
    runtime = await _build_runtime(tmp_path)
    async for client in _client(runtime.control_plane):
        run = await _create_run(client)
        tool_call = ToolCall(
            id="tool-call-outage",
            sdk_call_id="sdk-call-outage",
            run_id=str(run["id"]),
            agent_step_id="step-outage",
            tool_id="python",
        )
        approval = Approval(
            id="approval-outage",
            run_id=str(run["id"]),
            tool_call_id=tool_call.id,
            tool_name="python",
        )
        await runtime.approval_repository.create_request(tool_call, approval)
        runtime.workflow.fail = True

        response = await client.post(
            "/api/v1/approvals/approval-outage/approve",
            json={"decided_by": "api-user"},
        )
        assert response.status_code == 503
        persisted = await runtime.approval_repository.get("approval-outage")
        assert persisted is not None
        assert persisted.status.value == "approved"
    await runtime.control_plane.close()


@pytest.mark.asyncio
async def test_approval_preserves_workflow_error_classification(tmp_path: Path) -> None:
    runtime = await _build_runtime(tmp_path)
    async for client in _client(runtime.control_plane):
        run = await _create_run(client)
        tool_call = ToolCall(
            id="tool-call-closed-workflow",
            sdk_call_id="sdk-call-closed-workflow",
            run_id=str(run["id"]),
            agent_step_id="step-closed-workflow",
            tool_id="python",
        )
        approval = Approval(
            id="approval-closed-workflow",
            run_id=str(run["id"]),
            tool_call_id=tool_call.id,
            tool_name="python",
        )
        await runtime.approval_repository.create_request(tool_call, approval)
        assert isinstance(runtime.workflow, FakeWorkflowClient)
        runtime.workflow.error = ApplicationConflictError(
            "workflow_not_running",
            "The Workflow is no longer running",
        )

        response = await client.post(
            "/api/v1/approvals/approval-closed-workflow/approve",
            json={"decided_by": "api-user"},
        )

        assert response.status_code == 409
        assert response.json()["error"]["code"] == "workflow_not_running"
        assert response.json()["error"]["details"]["approval_saved"] is True
    await runtime.control_plane.close()


@pytest.mark.asyncio
async def test_terminal_run_approval_is_not_actionable(tmp_path: Path) -> None:
    runtime = await _build_runtime(tmp_path)
    async for client in _client(runtime.control_plane):
        run = await _create_run(client)
        run_id = str(run["id"])
        tool_call = ToolCall(
            id="tool-call-terminal-run",
            sdk_call_id="sdk-call-terminal-run",
            run_id=run_id,
            agent_step_id="step-terminal-run",
            tool_id="python",
        )
        approval = Approval(
            id="approval-terminal-run",
            run_id=run_id,
            tool_call_id=tool_call.id,
            tool_name="python",
        )
        await runtime.approval_repository.create_request(tool_call, approval)
        await runtime.run_repository.update_status(run_id, RunStatus.PREPARING)
        await runtime.run_repository.update_status(run_id, RunStatus.FAILED)

        response = await client.post(
            "/api/v1/approvals/approval-terminal-run/approve",
            json={"decided_by": "api-user"},
        )

        assert response.status_code == 409
        assert response.json()["error"]["code"] == "approval_not_actionable"
        persisted = await runtime.approval_repository.get("approval-terminal-run")
        assert persisted is not None
        assert persisted.status.value == "pending"
        assert isinstance(runtime.workflow, FakeWorkflowClient)
        assert not any(call[0] == "approve" for call in runtime.workflow.calls)
    await runtime.control_plane.close()


_TERMINAL_SCRIPT = r"""
import signal
import sys

def interrupted(signum, frame):
    print("INTERRUPTED", flush=True)

signal.signal(signal.SIGINT, interrupted)
print("READY", flush=True)
for line in sys.stdin:
    print("ECHO:" + line.rstrip(), flush=True)
"""


@pytest.mark.asyncio
async def test_terminal_rest_lifecycle_and_start_failure(tmp_path: Path) -> None:
    runtime = await _build_runtime(tmp_path)
    try:
        async for client in _client(runtime.control_plane):
            run = await _create_run(client)
            created = await client.post(
                f"/api/v1/runs/{run['id']}/terminals",
                json={
                    "argv": [sys.executable, "-u", "-c", _TERMINAL_SCRIPT],
                    "owner": "agent",
                    "cols": 100,
                    "rows": 30,
                },
            )
            assert created.status_code == 201, created.text
            session = created.json()
            assert session["status"] == "open"
            assert session["owner"] == "agent"
            assert session["execution_status"] == "running"

            fetched = await client.get(f"/api/v1/terminals/{session['id']}")
            assert fetched.status_code == 200
            assert fetched.json()["id"] == session["id"]

            closed = await client.delete(f"/api/v1/terminals/{session['id']}")
            assert closed.status_code == 200
            assert closed.json()["status"] == "closed"
            assert closed.json()["execution_status"] == "cancelled"

            missing = await client.get("/api/v1/terminals/missing")
            assert missing.status_code == 404
            assert missing.json()["error"]["code"] == "terminal_session_not_found"

            failed = await client.post(
                f"/api/v1/runs/{run['id']}/terminals",
                json={"argv": [str(tmp_path / "does-not-exist")]},
            )
            assert failed.status_code == 409
            assert failed.json()["error"]["code"] == "terminal_start_failed"
    finally:
        await runtime.control_plane.close()


def _receive_ws_message(websocket: Any, expected_type: str) -> dict[str, object]:
    for _ in range(100):
        message = websocket.receive_json()
        if message.get("type") == expected_type:
            return message
    raise AssertionError(f"did not receive websocket message type {expected_type!r}")


def _receive_ws_state(websocket: Any, **expected: object) -> dict[str, object]:
    for _ in range(100):
        message = _receive_ws_message(websocket, "state")
        session = message["session"]
        if isinstance(session, dict) and all(
            session.get(key) == value for key, value in expected.items()
        ):
            return session
    raise AssertionError(f"did not receive terminal state matching {expected!r}")


def _receive_ws_output(websocket: Any, expected: str) -> str:
    content = ""
    for _ in range(100):
        message = _receive_ws_message(websocket, "output")
        content += str(message.get("data", ""))
        if expected in content:
            return content
    raise AssertionError(f"did not receive terminal output {expected!r}; output={content!r}")


def test_terminal_websocket_takeover_io_resize_interrupt_and_release(tmp_path: Path) -> None:
    runtime = asyncio.run(_build_runtime(tmp_path))
    app = create_app(control_plane=runtime.control_plane)
    try:
        with TestClient(app) as client:
            run_response = client.post(
                "/api/v1/runs",
                json={"objective": "Exercise the local PTY"},
            )
            assert run_response.status_code == 201
            run = run_response.json()
            created = client.post(
                f"/api/v1/runs/{run['id']}/terminals",
                json={
                    "argv": [sys.executable, "-u", "-c", _TERMINAL_SCRIPT],
                    "owner": "agent",
                },
            )
            assert created.status_code == 201, created.text
            session_id = created.json()["id"]

            with client.websocket_connect(f"/api/v1/terminals/{session_id}/ws") as websocket:
                _receive_ws_state(websocket, owner="agent", status="open")
                _receive_ws_output(websocket, "READY")

                websocket.send_json({"type": "input", "data": "blocked\n"})
                error = _receive_ws_message(websocket, "error")
                assert error["code"] == "terminal_not_owned"

                websocket.send_json({"type": "takeover"})
                _receive_ws_state(websocket, owner="user")
                websocket.send_json({"type": "input", "data": "你好 RiftX\n"})
                assert "ECHO:你好 RiftX" in _receive_ws_output(websocket, "ECHO:你好 RiftX")

                websocket.send_json({"type": "resize", "cols": 132, "rows": 48})
                _receive_ws_state(websocket, cols=132, rows=48)
                websocket.send_json({"type": "interrupt"})
                _receive_ws_output(websocket, "INTERRUPTED")

                websocket.send_json({"type": "ping"})
                _receive_ws_message(websocket, "pong")
                websocket.send_json({"type": "release"})
                summary = _receive_ws_message(websocket, "terminal_takeover_summary")
                assert summary["summary"]["byte_count"] > 0
                assert summary["summary"]["artifact_id"]
                assert "ECHO:你好 RiftX" in summary["summary"]["summary"]
                _receive_ws_state(websocket, owner="agent")

            closed = client.delete(f"/api/v1/terminals/{session_id}")
            assert closed.status_code == 200
            assert closed.json()["status"] == "closed"
            assert closed.json()["transcript_artifact_id"]

            with client.websocket_connect("/api/v1/terminals/missing/ws") as websocket:
                error = websocket.receive_json()
                assert error["type"] == "error"
                assert error["code"] == "terminal_session_not_found"
    finally:
        asyncio.run(runtime.control_plane.close())


@pytest.mark.asyncio
async def test_artifact_registration_snapshots_and_recovers_content(tmp_path: Path) -> None:
    database_path = tmp_path / "artifact.db"
    runtime = await _build_runtime(tmp_path, database_path=database_path)
    artifact_id = ""
    run_id = ""
    try:
        async for client in _client(runtime.control_plane):
            created = await _create_run(client)
            run_id = str(created["id"])
            source = Path(str(created["workspace_path"])) / "scan output.txt"
            source.write_text("original evidence\n")

            response = await client.post(
                f"/api/v1/runs/{run_id}/artifacts",
                json={
                    "source_path": str(source),
                    "name": "scan.txt",
                    "description": "Service scan output",
                },
            )
            assert response.status_code == 201
            artifact = response.json()
            artifact_id = str(artifact["id"])
            assert artifact["mime_type"] == "text/plain"
            assert artifact["size"] == len(b"original evidence\n")
            assert artifact["sha256"] == (
                "62b0c0767a0d936528c5e12e349eb572c57ff271930d84715d43ad1e4599d279"
            )
            assert artifact["content_url"] == (f"/api/v1/artifacts/{artifact_id}/content")
            assert "path" not in artifact

            source.write_text("mutated source\n")
            content = await client.get(artifact["content_url"])
            assert content.status_code == 200
            assert content.content == b"original evidence\n"
            assert content.headers["etag"] == f'"sha256:{artifact["sha256"]}"'
            assert "attachment" in content.headers["content-disposition"]

            listed = await client.get(f"/api/v1/runs/{run_id}/artifacts")
            assert listed.status_code == 200
            assert listed.json()["items"] == [artifact]
            fetched = await client.get(f"/api/v1/artifacts/{artifact_id}")
            assert fetched.json() == artifact

            events = await client.get(f"/api/v1/runs/{run_id}/events")
            assert events.json()["items"][-1]["event_type"] == "artifact.registered"
    finally:
        await runtime.control_plane.close()

    restarted = await _build_runtime(tmp_path, database_path=database_path)
    try:
        async for client in _client(restarted.control_plane):
            content = await client.get(f"/api/v1/artifacts/{artifact_id}/content")
            assert content.status_code == 200
            assert content.content == b"original evidence\n"
            listed = await client.get(f"/api/v1/runs/{run_id}/artifacts")
            assert listed.json()["items"][0]["id"] == artifact_id
    finally:
        await restarted.control_plane.close()


@pytest.mark.asyncio
async def test_artifact_registration_rejects_escaped_and_invalid_sources(tmp_path: Path) -> None:
    runtime = await _build_runtime(tmp_path)
    try:
        async for client in _client(runtime.control_plane):
            created = await _create_run(client)
            run_id = str(created["id"])
            workspace = Path(str(created["workspace_path"]))
            outside = tmp_path / "outside.txt"
            outside.write_text("not run evidence")

            escaped = await client.post(
                f"/api/v1/runs/{run_id}/artifacts",
                json={"source_path": str(outside)},
            )
            assert escaped.status_code == 409
            assert escaped.json()["error"]["code"] == "artifact_source_outside_run"

            symlink = workspace / "escaped-link.txt"
            symlink.symlink_to(outside)
            linked = await client.post(
                f"/api/v1/runs/{run_id}/artifacts",
                json={"source_path": str(symlink)},
            )
            assert linked.status_code == 409
            assert linked.json()["error"]["code"] == "artifact_source_outside_run"

            missing = await client.post(
                f"/api/v1/runs/{run_id}/artifacts",
                json={"source_path": str(workspace / "missing.txt")},
            )
            assert missing.status_code == 409
            assert missing.json()["error"]["code"] == "artifact_source_unavailable"

            directory = await client.post(
                f"/api/v1/runs/{run_id}/artifacts",
                json={"source_path": str(workspace)},
            )
            assert directory.status_code == 409
            assert directory.json()["error"]["code"] == "artifact_source_not_file"

            source = workspace / "valid.txt"
            source.write_text("valid")
            invalid_name = await client.post(
                f"/api/v1/runs/{run_id}/artifacts",
                json={"source_path": str(source), "name": "../escape.txt"},
            )
            assert invalid_name.status_code == 409
            assert invalid_name.json()["error"]["code"] == "invalid_artifact_name"
    finally:
        await runtime.control_plane.close()


@pytest.mark.asyncio
async def test_artifact_download_detects_snapshot_tampering(tmp_path: Path) -> None:
    runtime = await _build_runtime(tmp_path)
    try:
        async for client in _client(runtime.control_plane):
            created = await _create_run(client)
            run_id = str(created["id"])
            source = Path(str(created["workspace_path"])) / "evidence.txt"
            source.write_text("trusted")
            response = await client.post(
                f"/api/v1/runs/{run_id}/artifacts",
                json={"source_path": str(source)},
            )
            artifact_id = str(response.json()["id"])
            artifact = await runtime.artifact_repository.get(artifact_id)
            assert artifact is not None
            snapshot = Path(artifact.path)
            await asyncio.to_thread(snapshot.chmod, 0o644)
            await asyncio.to_thread(snapshot.write_text, "tampered")

            content = await client.get(f"/api/v1/artifacts/{artifact_id}/content")
            assert content.status_code == 409
            assert content.json()["error"]["code"] == "artifact_integrity_mismatch"
    finally:
        await runtime.control_plane.close()


@pytest.mark.asyncio
async def test_findings_are_editable_and_validate_artifact_evidence(tmp_path: Path) -> None:
    runtime = await _build_runtime(tmp_path)
    try:
        async for client in _client(runtime.control_plane):
            first = await _create_run(client)
            first_run_id = str(first["id"])
            source = Path(str(first["workspace_path"])) / "proof.txt"
            source.write_text("service exposed")
            artifact_response = await client.post(
                f"/api/v1/runs/{first_run_id}/artifacts",
                json={"source_path": str(source), "description": "raw proof"},
            )
            artifact_id = str(artifact_response.json()["id"])

            created = await client.post(
                f"/api/v1/runs/{first_run_id}/findings",
                json={
                    "title": "  Exposed service  ",
                    "severity": "high",
                    "affected_assets": [" 127.0.0.1 ", "127.0.0.1"],
                    "description": "  Development service is reachable.  ",
                    "evidence": [
                        {
                            "artifact_id": artifact_id,
                            "description": "Banner capture",
                            "location": "line:1",
                        }
                    ],
                    "reproduction_steps": [" curl localhost ", "curl localhost"],
                },
            )
            assert created.status_code == 201
            finding = created.json()
            finding_id = str(finding["id"])
            assert finding["title"] == "Exposed service"
            assert finding["status"] == "draft"
            assert finding["affected_assets"] == ["127.0.0.1"]
            assert finding["reproduction_steps"] == ["curl localhost"]
            assert finding["evidence"][0]["artifact_id"] == artifact_id
            before_confirmation = await client.get(
                "/api/v1/memories",
                params={
                    "scope_type": "asset",
                    "scope_id": f"{first['engagement_id']}::127.0.0.1",
                },
            )
            assert before_confirmation.json()["items"] == []

            updated = await client.patch(
                f"/api/v1/findings/{finding_id}",
                json={
                    "status": "confirmed",
                    "severity": "critical",
                    "impact": "Administrative metadata is exposed.",
                },
            )
            assert updated.status_code == 200
            assert updated.json()["status"] == "confirmed"
            assert updated.json()["severity"] == "critical"
            assert updated.json()["title"] == "Exposed service"
            assert updated.json()["updated_at"] > finding["updated_at"]

            promoted = await client.get(
                "/api/v1/memories",
                params={
                    "scope_type": "asset",
                    "scope_id": f"{first['engagement_id']}::127.0.0.1",
                },
            )
            assert promoted.status_code == 200
            assert len(promoted.json()["items"]) == 1
            promoted_memory = promoted.json()["items"][0]
            assert promoted_memory["memory_type"] == "episodic"
            assert promoted_memory["title"] == "Exposed service"
            assert promoted_memory["source_refs"] == [
                f"finding://{finding_id}",
                f"artifact://{artifact_id}",
            ]

            fetched = await client.get(f"/api/v1/findings/{finding_id}")
            assert fetched.json() == updated.json()
            listed = await client.get(
                f"/api/v1/runs/{first_run_id}/findings",
                params={"status": "confirmed", "severity": "critical"},
            )
            assert [item["id"] for item in listed.json()["items"]] == [finding_id]

            second = await _create_run(client)
            second_run_id = str(second["id"])
            mismatch = await client.post(
                f"/api/v1/runs/{second_run_id}/findings",
                json={
                    "title": "Invalid evidence",
                    "severity": "low",
                    "evidence": [{"artifact_id": artifact_id}],
                },
            )
            assert mismatch.status_code == 409
            assert mismatch.json()["error"]["code"] == "finding_artifact_run_mismatch"

            missing = await client.patch(
                f"/api/v1/findings/{finding_id}",
                json={"evidence": [{"artifact_id": "missing"}]},
            )
            assert missing.status_code == 404
            assert missing.json()["error"]["code"] == "artifact_not_found"

            no_changes = await client.patch(
                f"/api/v1/findings/{finding_id}",
                json={},
            )
            assert no_changes.status_code == 409
            assert no_changes.json()["error"]["code"] == "empty_finding_update"

            empty = await client.post(
                f"/api/v1/runs/{first_run_id}/findings",
                json={
                    "title": "Empty evidence",
                    "severity": "info",
                    "evidence": [{}],
                },
            )
            assert empty.status_code == 409
            assert empty.json()["error"]["code"] == "empty_finding_evidence"

            events = await client.get(f"/api/v1/runs/{first_run_id}/events")
            assert [item["event_type"] for item in events.json()["items"]][-4:] == [
                "artifact.registered",
                "finding.created",
                "finding.updated",
                "memory.promotion_evaluated",
            ]
    finally:
        await runtime.control_plane.close()


@pytest.mark.asyncio
async def test_reports_generate_list_get_and_link_finding_evidence(tmp_path: Path) -> None:
    runtime = await _build_runtime(tmp_path)
    try:
        async for client in _client(runtime.control_plane):
            run = await _create_run(client)
            run_id = str(run["id"])
            premature = await client.post(
                f"/api/v1/runs/{run_id}/reports",
                json={"formats": ["markdown"]},
            )
            assert premature.status_code == 409
            assert premature.json()["error"]["code"] == "run_not_reportable"
            await runtime.run_repository.update_status(run_id, RunStatus.PREPARING)
            await runtime.run_repository.update_status(run_id, RunStatus.RUNNING)
            await runtime.run_repository.update_status(run_id, RunStatus.COMPLETED)
            source = Path(str(run["workspace_path"])) / "proof.txt"
            source.write_text("service exposed")
            artifact_response = await client.post(
                f"/api/v1/runs/{run_id}/artifacts",
                json={"source_path": str(source), "description": "Banner proof"},
            )
            assert artifact_response.status_code == 201
            evidence_artifact_id = str(artifact_response.json()["id"])
            finding_response = await client.post(
                f"/api/v1/runs/{run_id}/findings",
                json={
                    "title": "Exposed service",
                    "severity": "high",
                    "description": "A development endpoint is reachable.",
                    "evidence": [
                        {
                            "artifact_id": evidence_artifact_id,
                            "description": "Open banner proof",
                        }
                    ],
                },
            )
            assert finding_response.status_code == 201
            finding_id = str(finding_response.json()["id"])

            generated = await client.post(
                f"/api/v1/runs/{run_id}/reports",
                json={"formats": ["markdown", "html", "json"]},
            )
            assert generated.status_code == 201, generated.text
            reports = generated.json()["items"]
            assert [item["format"] for item in reports] == ["markdown", "html", "json"]
            assert all(item["finding_ids"] == [finding_id] for item in reports)
            assert all(item["content_url"].startswith("/api/v1/artifacts/") for item in reports)

            listed = await client.get(f"/api/v1/runs/{run_id}/reports")
            assert {item["id"] for item in listed.json()["items"]} == {
                item["id"] for item in reports
            }
            markdown_report = reports[0]
            fetched = await client.get(f"/api/v1/reports/{markdown_report['id']}")
            assert fetched.json() == markdown_report
            content = await client.get(markdown_report["content_url"])
            assert content.status_code == 200
            assert f"/api/v1/artifacts/{evidence_artifact_id}/content" in content.text

            events = await client.get(f"/api/v1/runs/{run_id}/events")
            event_types = [item["event_type"] for item in events.json()["items"]]
            assert event_types.count("report.generated") == 3
    finally:
        await runtime.control_plane.close()


@pytest.mark.asyncio
async def test_runner_registration_heartbeat_and_node_management(tmp_path: Path) -> None:
    runtime = await _build_runtime(tmp_path)
    async for client in _client(runtime.control_plane):
        missing_auth = await client.post(
            "/api/v1/nodes/register",
            json={
                "node_id": "missing-auth",
                "name": "Missing Auth",
                "platform": "linux",
                "architecture": "x86_64",
            },
        )
        assert missing_auth.status_code == 401

        denied = await client.post(
            "/api/v1/nodes/register",
            headers={"Authorization": "Bearer wrong"},
            json={
                "node_id": "windows-a",
                "name": "Windows Runner A",
                "platform": "windows",
                "architecture": "amd64",
            },
        )
        assert denied.status_code == 401
        assert denied.json()["error"]["code"] == "runner_registration_denied"

        registered = await client.post(
            "/api/v1/nodes/register",
            headers={"Authorization": "Bearer test-bootstrap"},
            json={
                "node_id": "windows-a",
                "name": "Windows Runner A",
                "platform": "windows",
                "architecture": "amd64",
                "runner_version": "2.0.0",
                "capabilities": ["powershell", "conpty"],
                "labels": {"zone": "internal"},
            },
        )
        assert registered.status_code == 200
        assert registered.json()["created"] is True
        assert registered.json()["node"]["status"] == "online"
        first_token = registered.json()["runner_token"]

        repeated = await client.post(
            "/api/v1/nodes/register",
            headers={"Authorization": "Bearer test-bootstrap"},
            json={
                "node_id": "windows-a",
                "name": "Windows Runner Primary",
                "platform": "windows",
                "architecture": "amd64",
                "runner_version": "2.0.1",
                "capabilities": ["powershell"],
            },
        )
        assert repeated.status_code == 200
        assert repeated.json()["created"] is False
        runner_token = repeated.json()["runner_token"]
        assert runner_token != first_token

        stale_token = await client.post(
            "/api/v1/nodes/windows-a/heartbeat",
            headers={"Authorization": f"Bearer {first_token}"},
            json={},
        )
        assert stale_token.status_code == 401

        runner_headers = {
            "Authorization": f"Bearer {runner_token}",
            "X-RiftX-Node-ID": "windows-a",
        }
        unauthenticated_heartbeat = await client.post(
            "/api/v1/nodes/windows-a/heartbeat",
            json={},
        )
        assert unauthenticated_heartbeat.status_code == 401

        invalid_heartbeat = await client.post(
            "/api/v1/nodes/windows-a/heartbeat",
            headers=runner_headers,
            json={"status": "offline"},
        )
        assert invalid_heartbeat.status_code == 422
        assert invalid_heartbeat.json()["error"]["code"] == "validation_error"

        heartbeat = await client.post(
            "/api/v1/nodes/windows-a/heartbeat",
            headers=runner_headers,
            json={"status": "degraded", "labels": {"load": "high"}},
        )
        assert heartbeat.status_code == 200
        assert heartbeat.json()["status"] == "degraded"
        assert heartbeat.json()["labels"] == {"load": "high"}

        listed = await client.get("/api/v1/nodes?status=degraded")
        assert listed.status_code == 200
        assert [node["id"] for node in listed.json()["items"]] == ["windows-a"]

        disconnected = await client.post("/api/v1/nodes/windows-a/disconnect")
        assert disconnected.status_code == 200
        assert disconnected.json()["status"] == "offline"

        missing = await client.get("/api/v1/nodes/missing")
        assert missing.status_code == 404
        assert missing.json()["error"]["code"] == "node_not_found"
    await runtime.control_plane.close()


@pytest.mark.asyncio
async def test_node_api_exposes_runtime_and_active_execution_state(tmp_path: Path) -> None:
    runtime = await _build_runtime(tmp_path)
    try:
        await runtime.control_plane.node_service.register(
            NodeRegistration(
                node_id="local",
                name="Local Runner",
                platform="linux",
                architecture="x86_64",
                runner_version="2.0.0",
                capabilities=("scripting",),
                labels={
                    "shell": "/bin/zsh",
                    "working_directory": str(tmp_path),
                    "tool_count": "99",
                },
            )
        )
        async for client in _client(runtime.control_plane):
            run = await _create_run(client)
            execution = Execution(
                id="execution-active",
                execution_key="active-key",
                run_id=str(run["id"]),
                node_id="local",
                executor_type=ExecutorType.PROCESS,
                argv=["python", "-V"],
                cwd=str(run["workspace_path"]),
                stdout_path=str(tmp_path / "stdout.log"),
                stderr_path=str(tmp_path / "stderr.log"),
            )
            execution.transition_to(ExecutionStatus.STARTING)
            execution.transition_to(ExecutionStatus.RUNNING)
            await runtime.execution_repository.create_if_absent(execution)

            fetched = await client.get("/api/v1/nodes/local")
            listed = await client.get("/api/v1/nodes")

            assert fetched.status_code == 200
            node = fetched.json()
            assert node["platform"] == "linux"
            assert node["architecture"] == "x86_64"
            assert node["shell"] == "/bin/zsh"
            assert node["working_directory"] == str(tmp_path)
            assert node["tool_count"] == 1
            assert node["active_execution_ids"] == [execution.id]
            assert node["current_run_ids"] == [execution.run_id]
            assert listed.json()["items"] == [node]
    finally:
        await runtime.control_plane.close()


@pytest.mark.asyncio
async def test_remote_runner_control_channel_reconnects_and_bounds_updates(
    tmp_path: Path,
) -> None:
    runtime = await _build_runtime(tmp_path)
    async for client in _client(runtime.control_plane):
        registration = await client.post(
            "/api/v1/nodes/register",
            headers={"Authorization": "Bearer test-bootstrap"},
            json={
                "node_id": "runner-a",
                "name": "Runner A",
                "platform": "linux",
                "architecture": "x86_64",
            },
        )
        token = registration.json()["runner_token"]
        headers = {
            "Authorization": f"Bearer {token}",
            "X-RiftX-Node-ID": "runner-a",
        }
        other_registration = await client.post(
            "/api/v1/nodes/register",
            headers={"Authorization": "Bearer test-bootstrap"},
            json={
                "node_id": "runner-b",
                "name": "Runner B",
                "platform": "linux",
                "architecture": "x86_64",
            },
        )
        other_headers = {
            "Authorization": f"Bearer {other_registration.json()['runner_token']}",
            "X-RiftX-Node-ID": "runner-b",
        }
        run_response = await client.post(
            "/api/v1/runs",
            json={
                "objective": "Remote execution channel test",
                "node_id": "runner-a",
                "engagement": {"name": "Authorized remote test"},
            },
        )
        assert run_response.status_code == 201
        run = run_response.json()
        paths = RunnerPaths(runtime.control_plane.settings.runner_state_path)
        output_paths = paths.execution(str(run["id"]), "execution-remote")
        execution = Execution(
            id="execution-remote",
            execution_key="remote-key",
            run_id=str(run["id"]),
            node_id="runner-a",
            executor_type=ExecutorType.PROCESS,
            argv=["echo", "hello"],
            cwd=str(run["workspace_path"]),
            stdout_path=str(output_paths.stdout),
            stderr_path=str(output_paths.stderr),
        )
        execution.transition_to(ExecutionStatus.STARTING)
        _, created = await runtime.execution_repository.create_if_absent(execution)
        assert created is True

        command, command_created = await runtime.control_plane.runner_control_service.enqueue(
            "runner-a",
            kind=RunnerCommandKind.EXECUTE,
            idempotency_key="execute:remote-key",
            payload={"execution_id": execution.id, "argv": execution.argv},
        )
        repeated, repeated_created = await runtime.control_plane.runner_control_service.enqueue(
            "runner-a",
            kind=RunnerCommandKind.EXECUTE,
            idempotency_key="execute:remote-key",
            payload={"execution_id": execution.id, "argv": ["ignored"]},
        )
        assert command_created is True
        assert repeated_created is False
        assert repeated.id == command.id

        leased = await client.get("/api/v1/runner/commands/next", headers=headers)
        leased_command = leased.json()["command"]
        assert leased_command["id"] == command.id
        assert leased_command["kind"] == "execute"
        first_lease = leased_command["lease_id"]

        await asyncio.sleep(0.06)
        re_leased = await client.get("/api/v1/runner/commands/next", headers=headers)
        re_leased_command = re_leased.json()["command"]
        assert re_leased_command["id"] == command.id
        assert re_leased_command["attempts"] == 2
        assert re_leased_command["lease_id"] != first_lease

        stale_finish = await client.post(
            f"/api/v1/runner/commands/{command.id}/finish",
            headers=headers,
            json={"lease_id": first_lease, "succeeded": True},
        )
        assert stale_finish.status_code == 409

        running = await client.post(
            f"/api/v1/runner/executions/{execution.id}/status",
            headers=headers,
            json={
                "status": "running",
                "pid": 4242,
                "process_group_id": 4242,
                "executable_path": "/usr/bin/echo",
                "tool_id": "echo",
                "tool_version": "9.1",
                "platform_system": "linux",
                "platform_release": "6.10",
                "platform_architecture": "x86_64",
                "process_created_at": "2026-07-30T00:00:00Z",
            },
        )
        assert running.status_code == 200
        assert running.json()["status"] == "running"
        assert running.json()["executable_path"] == "/usr/bin/echo"
        assert running.json()["tool_version"] == "9.1"
        assert running.json()["platform_architecture"] == "x86_64"

        cross_node_status = await client.post(
            f"/api/v1/runner/executions/{execution.id}/status",
            headers=other_headers,
            json={"status": "running", "pid": 4243},
        )
        assert cross_node_status.status_code == 401
        assert cross_node_status.json()["error"]["code"] == ("runner_execution_scope_mismatch")

        output = await client.post(
            f"/api/v1/runner/executions/{execution.id}/output",
            headers=headers,
            json={"stream": "stdout", "offset": 0, "data": "aGVsbG8K"},
        )
        assert output.status_code == 200
        assert output.json()["next_offset"] == 6
        assert output_paths.stdout.read_bytes() == b"hello\n"

        replay = await client.post(
            f"/api/v1/runner/executions/{execution.id}/output",
            headers=headers,
            json={"stream": "stdout", "offset": 0, "data": "aGVsbG8K"},
        )
        assert replay.status_code == 409
        assert replay.json()["error"]["code"] == "runner_output_offset_mismatch"

        exited = await client.post(
            f"/api/v1/runner/executions/{execution.id}/status",
            headers=headers,
            json={"status": "exited", "exit_code": 0},
        )
        assert exited.status_code == 200
        assert exited.json()["exit_code"] == 0

        terminal_paths = paths.terminal(str(run["id"]), "terminal-remote")
        terminal_execution = Execution(
            id="execution-terminal-remote",
            execution_key="terminal:terminal-remote",
            run_id=str(run["id"]),
            node_id="runner-a",
            executor_type=ExecutorType.PTY,
            argv=["pwsh.exe"],
            cwd=str(run["workspace_path"]),
            stdout_path=str(terminal_paths.transcript),
            stderr_path=str(terminal_paths.transcript),
        )
        terminal_execution.transition_to(ExecutionStatus.STARTING)
        await runtime.execution_repository.create_if_absent(terminal_execution)
        await runtime.terminal_repository.create(
            TerminalSession(
                id="terminal-remote",
                run_id=str(run["id"]),
                execution_id=terminal_execution.id,
            )
        )
        terminal_running = await client.post(
            f"/api/v1/runner/executions/{terminal_execution.id}/status",
            headers=headers,
            json={"status": "running", "pid": 5150, "process_group_id": 5150},
        )
        assert terminal_running.status_code == 200
        persisted_terminal = await runtime.terminal_repository.get("terminal-remote")
        assert persisted_terminal is not None
        assert persisted_terminal.status is TerminalStatus.OPEN

        terminal_output = await client.post(
            f"/api/v1/runner/executions/{terminal_execution.id}/output",
            headers=headers,
            json={"stream": "stdout", "offset": 0, "data": "UkVBRFkK"},
        )
        assert terminal_output.status_code == 200
        assert terminal_paths.transcript.read_bytes() == b"READY\n"

        terminal_exited = await client.post(
            f"/api/v1/runner/executions/{terminal_execution.id}/status",
            headers=headers,
            json={"status": "exited", "exit_code": 0},
        )
        assert terminal_exited.status_code == 200
        persisted_terminal = await runtime.terminal_repository.get("terminal-remote")
        assert persisted_terminal is not None
        assert persisted_terminal.status is TerminalStatus.CLOSED

        oversized_result = await client.post(
            f"/api/v1/runner/commands/{command.id}/finish",
            headers=headers,
            json={
                "lease_id": re_leased_command["lease_id"],
                "succeeded": True,
                "result": {"value": "x" * (64 * 1024)},
            },
        )
        assert oversized_result.status_code == 409
        assert oversized_result.json()["error"]["code"] == "runner_result_too_large"

        finished = await client.post(
            f"/api/v1/runner/commands/{command.id}/finish",
            headers=headers,
            json={
                "lease_id": re_leased_command["lease_id"],
                "succeeded": True,
                "result": {"execution_id": execution.id},
            },
        )
        assert finished.status_code == 200
        assert finished.json()["status"] == "completed"

        empty = await client.get(
            "/api/v1/runner/commands/next?wait_seconds=0.01",
            headers=headers,
        )
        assert empty.status_code == 200
        assert empty.json()["command"] is None
    await runtime.control_plane.close()


@pytest.mark.asyncio
async def test_remote_target_http_command_uploads_bounded_response_body(
    tmp_path: Path,
) -> None:
    runtime = await _build_runtime(tmp_path)
    try:
        async for client in _client(runtime.control_plane):
            registration = await client.post(
                "/api/v1/nodes/register",
                headers={"Authorization": "Bearer test-bootstrap"},
                json={
                    "node_id": "runner-http",
                    "name": "Runner HTTP",
                    "platform": "linux",
                    "architecture": "x86_64",
                    "capabilities": ["target_http"],
                },
            )
            headers = {
                "Authorization": f"Bearer {registration.json()['runner_token']}",
                "X-RiftX-Node-ID": "runner-http",
            }
            command, created = await runtime.control_plane.runner_control_service.enqueue(
                "runner-http",
                kind=RunnerCommandKind.TARGET_HTTP,
                idempotency_key="target-http:integration-key",
                payload={"max_response_bytes": 32},
            )
            assert created is True
            leased = await client.get("/api/v1/runner/commands/next", headers=headers)
            lease_id = leased.json()["command"]["lease_id"]

            uploaded = await client.post(
                f"/api/v1/runner/commands/{command.id}/output",
                headers=headers,
                json={
                    "lease_id": lease_id,
                    "offset": 0,
                    "data": base64.b64encode(b"remote body").decode(),
                },
            )
            assert uploaded.status_code == 200
            assert uploaded.json()["next_offset"] == 11

            replay = await client.post(
                f"/api/v1/runner/commands/{command.id}/output",
                headers=headers,
                json={
                    "lease_id": lease_id,
                    "offset": 0,
                    "data": base64.b64encode(b"remote body").decode(),
                },
            )
            assert replay.status_code == 409
            assert replay.json()["error"]["code"] == "runner_output_offset_mismatch"

            oversized = await client.post(
                f"/api/v1/runner/commands/{command.id}/output",
                headers=headers,
                json={
                    "lease_id": lease_id,
                    "offset": 11,
                    "data": base64.b64encode(b"x" * 22).decode(),
                },
            )
            assert oversized.status_code == 409
            assert oversized.json()["error"]["code"] == "runner_command_output_too_large"

            finished = await client.post(
                f"/api/v1/runner/commands/{command.id}/finish",
                headers=headers,
                json={
                    "lease_id": lease_id,
                    "succeeded": True,
                    "result": {"result": {"status_code": 200}},
                },
            )
            assert finished.status_code == 200
            assert (
                await runtime.control_plane.runner_control_service.read_command_output(command.id)
                == b"remote body"
            )
    finally:
        await runtime.control_plane.close()


@pytest.mark.asyncio
async def test_remote_terminal_api_routes_commands_and_streams_transcript(
    tmp_path: Path,
) -> None:
    runtime = await _build_runtime(tmp_path)
    try:
        async for client in _client(runtime.control_plane):
            registration = await client.post(
                "/api/v1/nodes/register",
                headers={"Authorization": "Bearer test-bootstrap"},
                json={
                    "node_id": "windows-a",
                    "name": "Windows Runner A",
                    "platform": "windows",
                    "architecture": "amd64",
                    "capabilities": ["powershell", "conpty"],
                },
            )
            token = registration.json()["runner_token"]
            headers = {
                "Authorization": f"Bearer {token}",
                "X-RiftX-Node-ID": "windows-a",
            }
            run_response = await client.post(
                "/api/v1/runs",
                json={
                    "objective": "Remote Windows terminal",
                    "node_id": "windows-a",
                    "engagement": {"name": "Remote terminal E2E"},
                },
            )
            run = run_response.json()
            created = await client.post(
                f"/api/v1/runs/{run['id']}/terminals",
                json={
                    "argv": ["pwsh.exe", "-NoLogo", "-NoProfile"],
                    "cols": 132,
                    "rows": 48,
                },
            )
            assert created.status_code == 201, created.text
            terminal = created.json()
            assert terminal["status"] == "open"
            assert terminal["execution_status"] == "running"

            leased = await client.get("/api/v1/runner/commands/next", headers=headers)
            start_command = leased.json()["command"]
            assert start_command["kind"] == "terminal_start"
            assert start_command["payload"]["session_id"] == terminal["id"]
            assert start_command["payload"]["execution_id"] == terminal["execution_id"]
            assert start_command["payload"]["request"]["node_id"] == "windows-a"

            running = await client.post(
                f"/api/v1/runner/executions/{terminal['execution_id']}/status",
                headers=headers,
                json={"status": "running", "pid": 5150, "process_group_id": 5150},
            )
            assert running.status_code == 200
            assert running.json()["pid"] == 5150
            await client.post(
                f"/api/v1/runner/commands/{start_command['id']}/finish",
                headers=headers,
                json={
                    "lease_id": start_command["lease_id"],
                    "succeeded": True,
                    "result": {"session_id": terminal["id"]},
                },
            )

            uploaded = await client.post(
                f"/api/v1/runner/executions/{terminal['execution_id']}/output",
                headers=headers,
                json={"stream": "stdout", "offset": 0, "data": "UkVBRFkNCg=="},
            )
            assert uploaded.status_code == 200
            output = await runtime.control_plane.terminal_service.read(terminal["id"])
            assert output.data == b"READY\r\n"

            closed = await client.delete(f"/api/v1/terminals/{terminal['id']}")
            assert closed.status_code == 200
            assert closed.json()["status"] == "closed"
            assert closed.json()["execution_status"] == "cancelled"
            close_lease = await client.get("/api/v1/runner/commands/next", headers=headers)
            close_command = close_lease.json()["command"]
            assert close_command["kind"] == "terminal_close"
            assert close_command["payload"]["operation_id"] == (f"terminal-close:{terminal['id']}")

            cancelled = await client.post(
                f"/api/v1/runner/executions/{terminal['execution_id']}/status",
                headers=headers,
                json={"status": "cancelled", "exit_code": 130},
            )
            assert cancelled.status_code == 200
            assert cancelled.json()["exit_code"] == 130
            fetched = await client.get(f"/api/v1/terminals/{terminal['id']}")
            assert fetched.json()["pid"] == 5150
            assert fetched.json()["exit_code"] == 130
    finally:
        await runtime.control_plane.close()


@pytest.mark.asyncio
async def test_execution_api_exposes_provenance_and_cursor_output(tmp_path: Path) -> None:
    runtime = await _build_runtime(tmp_path)
    try:
        async for client in _client(runtime.control_plane):
            run = await _create_run(client)
            run_id = str(run["id"])
            paths = RunnerPaths(runtime.control_plane.settings.runner_state_path).execution(
                run_id, "execution-public"
            )
            paths.stdout.parent.mkdir(parents=True, exist_ok=True)
            paths.stdout.write_bytes(b"hello execution\n")
            paths.stderr.write_bytes(b"diagnostic\n")
            execution = Execution(
                id="execution-public",
                execution_key="public-key",
                run_id=run_id,
                node_id="local",
                executor_type=ExecutorType.PROCESS,
                argv=["/usr/bin/printf", "hello"],
                tool_id="printf",
                tool_version="coreutils 9",
                executable_path="/usr/bin/printf",
                cwd=str(run["workspace_path"]),
                env_diff={"LANG": "C.UTF-8"},
                platform_system="linux",
                platform_release="6.10",
                platform_architecture="x86_64",
                stdout_path=str(paths.stdout),
                stderr_path=str(paths.stderr),
            )
            execution.transition_to(ExecutionStatus.STARTING)
            execution.transition_to(ExecutionStatus.RUNNING)
            execution.transition_to(ExecutionStatus.EXITED, exit_code=0)
            await runtime.execution_repository.create_if_absent(execution)

            listed = await client.get(f"/api/v1/runs/{run_id}/executions")
            fetched = await client.get(f"/api/v1/executions/{execution.id}")
            waited = await client.post(
                f"/api/v1/executions/{execution.id}/wait",
                params={"timeout_seconds": 0.1, "max_bytes": 5},
            )
            output = await client.get(
                f"/api/v1/executions/{execution.id}/output",
                params={"max_bytes": 5},
            )

            assert listed.status_code == 200
            assert [item["id"] for item in listed.json()["items"]] == [execution.id]
            assert fetched.status_code == 200
            assert fetched.json()["tool_id"] == "printf"
            assert fetched.json()["tool_version"] == "coreutils 9"
            assert fetched.json()["executable_path"] == "/usr/bin/printf"
            assert fetched.json()["platform_system"] == "linux"
            assert fetched.json()["platform_architecture"] == "x86_64"
            assert waited.status_code == 200
            assert waited.json()["wait_status"] == "execution_completed"
            assert waited.json()["execution_status"] == "exited"
            assert waited.json()["partial_output"] == "hellodiagn"
            assert output.status_code == 200
            assert output.json()["stdout"]["data"] == "aGVsbG8="
            assert output.json()["stdout"]["next_cursor"] == 5
            assert output.json()["stdout"]["eof"] is False
    finally:
        await runtime.control_plane.close()


@pytest.mark.asyncio
async def test_complete_agent_runner_sse_finding_report_lifecycle(tmp_path: Path) -> None:
    """Exercise the exact V2 design §28.5 chain through public API boundaries."""

    tools_path = tmp_path / "tools.yaml"
    tools_path.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "execution_policy": "registered_only",
                "tools": {
                    "fake-success": {
                        "command": [sys.executable, str(FAKE_TOOL_FIXTURE)],
                        "executor": "process",
                        "capabilities": ["deterministic_verification"],
                        "approval": "never",
                        "timeout": 30,
                        "version_probe": {
                            "command": [
                                sys.executable,
                                str(FAKE_TOOL_FIXTURE),
                                "--version",
                            ]
                        },
                    }
                },
            },
            sort_keys=False,
        )
    )
    environment = await WorkflowEnvironment.start_time_skipping()
    runtime: RuntimeFixture | None = None
    task_queue = f"riftx-e2e-{uuid4()}"
    temporal_config = TemporalRuntimeConfig(
        task_queue=task_queue,
        workflow_id_prefix="e2e-workflow",
    )
    workflow_client = TemporalRunClient(environment.client, temporal_config)

    try:
        runtime = await _build_runtime(tmp_path, workflow=workflow_client)
        registry = ToolRegistry(tools_path, node_id="local")
        await registry.refresh()
        event_repository = SQLAlchemyRunEventRepository(
            runtime.control_plane.database.session_factory
        )
        supervisor = runtime.control_plane.process_supervisor
        assert supervisor is not None
        model = FullRunModel()
        agent_cycle = AgentCycle(
            services=AgentRuntimeServices(
                tool_registry=registry,
                skill_registry=create_default_skill_registry(),
                supervisor=supervisor,
                finding_repository=runtime.finding_repository,
                event_repository=event_repository,
                finding_service=runtime.control_plane.finding_service,
                artifact_service=runtime.control_plane.artifact_service,
                terminal_service=runtime.control_plane.terminal_service,
                approval_repository=runtime.approval_repository,
            ),
            session_factory=runtime.control_plane.database.session_factory,
            checkpoint_store=SQLAlchemyCheckpointStore(
                runtime.control_plane.database.session_factory
            ),
            model=model,
        )
        activities = RiftXActivities(
            run_repository=runtime.run_repository,
            event_repository=event_repository,
            execution_repository=runtime.execution_repository,
            tool_registry=registry,
            supervisor=supervisor,
            agent_cycle=agent_cycle,
            approval_recorder=ApprovalRequestRecorder(
                approval_repository=runtime.approval_repository,
                event_repository=event_repository,
                tool_registry=registry,
            ),
            report_service=runtime.control_plane.report_service,
            session_factory=runtime.control_plane.database.session_factory,
        )

        async with Worker(
            environment.client,
            task_queue=task_queue,
            workflows=[RiftXRunWorkflow],
            activities=activities.registered(),
            max_cached_workflows=0,
        ):
            async for client in _client(runtime.control_plane):
                created_response = await client.post(
                    "/api/v1/runs",
                    json={
                        "objective": "Execute the deterministic full-run verifier",
                        "engagement": {"name": "Authorized E2E fixture"},
                        "success_criteria": [
                            {
                                "description": "Execute the fixture and record a finding",
                                "required": True,
                            }
                        ],
                        "entry_points": [{"kind": "ip", "value": "127.0.0.1"}],
                        "scope": {"ips": ["127.0.0.1"]},
                        "approval_mode": "balanced",
                    },
                )
                assert created_response.status_code == 201, created_response.text
                created = created_response.json()
                run_id = str(created["id"])
                handle = workflow_client.get_handle(run_id)
                workflow_result = await handle.result()

                assert workflow_result["phase"] == WorkflowPhase.COMPLETED.value
                assert model.calls == 4

                run_response = await client.get(f"/api/v1/runs/{run_id}")
                executions_response = await client.get(f"/api/v1/runs/{run_id}/executions")
                findings_response = await client.get(f"/api/v1/runs/{run_id}/findings")
                artifacts_response = await client.get(f"/api/v1/runs/{run_id}/artifacts")
                reports_response = await client.get(f"/api/v1/runs/{run_id}/reports")
                events_response = await client.get(
                    f"/api/v1/runs/{run_id}/events",
                    params={"limit": 1000},
                )
                sse_response = await client.get(
                    f"/api/v1/runs/{run_id}/events/stream",
                    params={"follow": "false"},
                )

                assert run_response.json()["status"] == "completed"
                executions = executions_response.json()["items"]
                assert len(executions) == 1
                assert executions[0]["tool_id"] == "fake-success"
                assert executions[0]["status"] == "exited"
                assert executions[0]["exit_code"] == 0
                output_response = await client.get(
                    f"/api/v1/executions/{executions[0]['id']}/output"
                )
                stdout = base64.b64decode(output_response.json()["stdout"]["data"])
                assert stdout.decode().strip() == "args=e2e"

                findings = findings_response.json()["items"]
                assert len(findings) == 1
                assert findings[0]["title"] == "Deterministic test service exposure"
                assert findings[0]["severity"] == "high"

                reports = reports_response.json()["items"]
                assert {item["format"] for item in reports} == {"markdown", "html", "json"}
                assert workflow_result["report_id"] in {item["id"] for item in reports}
                assert all(item["finding_ids"] == [findings[0]["id"]] for item in reports)
                assert len(artifacts_response.json()["items"]) == 3
                markdown_report = next(item for item in reports if item["format"] == "markdown")
                report_content = await client.get(markdown_report["content_url"])
                assert "Deterministic test service exposure" in report_content.text

                events = events_response.json()["items"]
                event_types = [item["event_type"] for item in events]
                ordered_types = [
                    "run.created",
                    "run.prepared",
                    "agent.tool_completed",
                    "finding.created",
                    "agent.completion_requested",
                    "agent.cycle_completed",
                    "report.generated",
                    "run.cleaned_up",
                ]
                positions = [event_types.index(event_type) for event_type in ordered_types]
                assert positions == sorted(positions)
                assert sse_response.status_code == 200
                sse_event_types = [
                    line.removeprefix("event: ")
                    for line in sse_response.text.splitlines()
                    if line.startswith("event: ")
                ]
                assert sse_event_types == event_types
    finally:
        if runtime is not None:
            await runtime.control_plane.close()
        await environment.shutdown()
