"""End-to-end tests for the shared FastAPI control plane."""

from __future__ import annotations

import asyncio
import sys
from collections.abc import AsyncIterator
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from riftx.api import APISettings, ControlPlane, create_app
from riftx.application.services import (
    ApprovalApplicationService,
    ArtifactApplicationService,
    EventApplicationService,
    FindingApplicationService,
    RunApplicationService,
    TerminalApplicationService,
    ToolApplicationService,
)
from riftx.domain import Approval, Finding, FindingSeverity, ToolCall
from riftx.persistence import (
    Database,
    SQLAlchemyApprovalRepository,
    SQLAlchemyArtifactRepository,
    SQLAlchemyEngagementRepository,
    SQLAlchemyExecutionRepository,
    SQLAlchemyFindingRepository,
    SQLAlchemyRunEventRepository,
    SQLAlchemyRunRepository,
    SQLAlchemyTerminalRepository,
)
from riftx.runner import RunnerPaths, TerminalSupervisor
from riftx.tools import ToolRegistry


@dataclass
class FakeWorkflowClient:
    fail: bool = False
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

    async def append_user_message(self, run_id: str, message: str) -> None:
        self._record("message", run_id, message)

    def workflow_id(self, run_id: str) -> str:
        return f"test-workflow-{run_id}"

    def _record(self, action: str, run_id: str, detail: str | None = None) -> None:
        if self.fail:
            raise RuntimeError("Temporal test outage")
        self.calls.append((action, run_id, detail))


@dataclass
class RuntimeFixture:
    control_plane: ControlPlane
    workflow: FakeWorkflowClient
    finding_repository: SQLAlchemyFindingRepository
    approval_repository: SQLAlchemyApprovalRepository
    artifact_repository: SQLAlchemyArtifactRepository


async def _build_runtime(
    tmp_path: Path,
    *,
    database_path: Path | None = None,
    workflow: FakeWorkflowClient | None = None,
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
    artifact_repository = SQLAlchemyArtifactRepository(database.session_factory)
    approval_repository = SQLAlchemyApprovalRepository(database.session_factory)
    execution_repository = SQLAlchemyExecutionRepository(database.session_factory)
    terminal_repository = SQLAlchemyTerminalRepository(database.session_factory)
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
                workspace_root=settings.workspace_root,
            ),
            event_service=EventApplicationService(
                run_repository=run_repository,
                event_repository=event_repository,
            ),
            finding_service=FindingApplicationService(
                run_repository=run_repository,
                finding_repository=finding_repository,
                artifact_repository=artifact_repository,
                execution_repository=execution_repository,
                event_repository=event_repository,
            ),
            tool_service=ToolApplicationService(registry),
            approval_service=ApprovalApplicationService(
                approval_repository=approval_repository,
                run_repository=run_repository,
                event_repository=event_repository,
                workflow_client=workflow_client,
            ),
            artifact_service=ArtifactApplicationService(
                run_repository=run_repository,
                execution_repository=execution_repository,
                artifact_repository=artifact_repository,
                event_repository=event_repository,
                paths=RunnerPaths(settings.runner_state_path),
            ),
            terminal_service=TerminalApplicationService(
                run_repository=run_repository,
                supervisor=terminal_supervisor,
            ),
            terminal_supervisor=terminal_supervisor,
        ),
        workflow=workflow_client,
        finding_repository=finding_repository,
        approval_repository=approval_repository,
        artifact_repository=artifact_repository,
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
            assert created["temporal_workflow_id"] == f"test-workflow-{run_id}"

            listed = await client.get("/api/v1/runs")
            assert listed.status_code == 200
            assert [item["id"] for item in listed.json()["items"]] == [run_id]

            shown = await client.get(f"/api/v1/runs/{run_id}")
            assert shown.status_code == 200
            assert shown.json()["objective"]["description"] == "Inspect the local service"

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

            assert [call[0] for call in runtime.workflow.calls] == [
                "start",
                "pause",
                "resume",
                "cancel_current_execution",
                "message",
            ]
            events = await client.get(f"/api/v1/runs/{run_id}/events", params={"limit": 20})
            assert events.status_code == 200
            event_types = [item["event_type"] for item in events.json()["items"]]
            assert event_types == [
                "run.created",
                "workflow.started",
                "run.pause_requested",
                "run.resume_requested",
                "execution.cancel_requested",
                "user.message_queued",
            ]
            assert events.json()["items"][-1]["payload"]["message"] == (
                "Focus on the HTTP endpoint"
            )
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
        assert ("approve", str(run["id"]), "sdk-call-1") in runtime.workflow.calls

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
        assert ("reject", str(run["id"]), "sdk-call-2") in runtime.workflow.calls

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
                _receive_ws_state(websocket, owner="agent")

            closed = client.delete(f"/api/v1/terminals/{session_id}")
            assert closed.status_code == 200
            assert closed.json()["status"] == "closed"

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
            assert [item["event_type"] for item in events.json()["items"]][-3:] == [
                "artifact.registered",
                "finding.created",
                "finding.updated",
            ]
    finally:
        await runtime.control_plane.close()
