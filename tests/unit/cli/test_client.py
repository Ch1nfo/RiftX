"""Unit tests for the CLI HTTP and SSE client."""

from __future__ import annotations

import json

import httpx
import pytest

from riftx.cli.client import APIClient, RiftXAPIError, parse_sse_lines


def test_api_client_reads_control_plane_health() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"status": "ok", "trust_profile": "local_trusted"})

    with APIClient(
        "http://control-plane",
        transport=httpx.MockTransport(handler),
    ) as client:
        health = client.health()

    assert health["status"] == "ok"
    assert requests[0].url.path == "/healthz"


def test_api_client_uses_shared_run_endpoints() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/api/v1/runs" and request.method == "POST":
            return httpx.Response(201, json={"id": "run-1", "status": "created"})
        if request.url.path.endswith("/message"):
            return httpx.Response(202, json={"accepted": True, "run": {"id": "run-1"}})
        if request.url.path.endswith("/compact"):
            return httpx.Response(202, json={"accepted": True, "run": {"id": "run-1"}})
        return httpx.Response(404, json={"error": {"code": "missing", "message": "missing"}})

    with APIClient(
        "http://control-plane/",
        transport=httpx.MockTransport(handler),
    ) as client:
        created = client.create_run({"objective": "test"})
        queued = client.append_message(
            "run-1",
            "continue",
            message_event_id="742ffacf-68f2-47bc-9a73-8705db475385",
        )
        compacted = client.compact_run("run-1", max_history_items=25)

    assert created["id"] == "run-1"
    assert queued["accepted"] is True
    assert compacted["accepted"] is True
    assert requests[0].url == httpx.URL("http://control-plane/api/v1/runs")
    assert json.loads(requests[0].content) == {"objective": "test"}
    assert requests[1].url.path == "/api/v1/runs/run-1/message"
    assert json.loads(requests[1].content) == {
        "message": "continue",
        "message_event_id": "742ffacf-68f2-47bc-9a73-8705db475385",
    }
    assert requests[2].url.path == "/api/v1/runs/run-1/compact"
    assert json.loads(requests[2].content) == {"max_history_items": 25}


def test_api_client_combines_run_status_and_kind_filters() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"items": [], "limit": 25, "offset": 5})

    with APIClient(
        "http://control-plane",
        transport=httpx.MockTransport(handler),
    ) as client:
        client.list_runs(
            status="running",
            kind="code_audit",
            limit=25,
            offset=5,
        )

    assert dict(requests[0].url.params) == {
        "limit": "25",
        "offset": "5",
        "status": "running",
        "kind": "code_audit",
    }


def test_local_audit_client_uses_minimal_job_endpoints_and_text_report() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/report"):
            return httpx.Response(200, text="# Audit\n")
        return httpx.Response(200, json={"audit_id": "audit-1", "items": []})

    with APIClient(
        "http://control-plane",
        transport=httpx.MockTransport(handler),
    ) as client:
        client.create_local_audit(
            "/workspace/project",
            include_patterns=("src/**",),
            exclude_patterns=("vendor/**",),
        )
        client.start_local_audit("audit-1")
        client.get_local_audit("audit-1")
        client.list_local_audit_findings(
            "audit-1",
            severity="high",
            category="secret",
            file="src/app.py",
            limit=25,
            offset=5,
        )
        report = client.get_local_audit_report("audit-1", format="markdown")
        client.cancel_local_audit("audit-1")

    assert report == "# Audit\n"
    assert [(request.method, request.url.path) for request in requests] == [
        ("POST", "/api/v1/audits"),
        ("POST", "/api/v1/audits/audit-1/start"),
        ("GET", "/api/v1/audits/audit-1"),
        ("GET", "/api/v1/audits/audit-1/findings"),
        ("GET", "/api/v1/audits/audit-1/report"),
        ("POST", "/api/v1/audits/audit-1/cancel"),
    ]
    assert json.loads(requests[0].content) == {
        "source_path": "/workspace/project",
        "include_patterns": ["src/**"],
        "exclude_patterns": ["vendor/**"],
    }
    assert dict(requests[3].url.params) == {
        "limit": "25",
        "offset": "5",
        "severity": "high",
        "category": "secret",
        "file": "src/app.py",
    }
    assert requests[4].url.params["format"] == "markdown"


def test_model_profile_client_uses_encoded_endpoints_and_admin_bearer() -> None:
    requests: list[tuple[str, str, object, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(
            (
                request.method,
                request.url.raw_path.decode(),
                json.loads(request.content) if request.content else None,
                request.headers.get("authorization"),
            )
        )
        return httpx.Response(200, json={"profiles": []})

    with APIClient(
        "http://127.0.0.1",
        transport=httpx.MockTransport(handler),
        admin_token="admin-secret",
    ) as client:
        client.list_model_profiles()
        client.get_model_profile("lab profile")
        client.configure_model_profile(
            "lab profile",
            {
                "model": "lab-model",
                "request_mode": "responses",
                "api_key": "request-secret",
            },
        )
        client.set_default_model_profile("lab profile")
        client.delete_model_profile("lab profile")
        client.create_run({"objective": "ordinary request"})

    assert requests == [
        ("GET", "/api/v1/model-profiles/admin", None, "Bearer admin-secret"),
        (
            "GET",
            "/api/v1/model-profiles/lab%20profile",
            None,
            "Bearer admin-secret",
        ),
        (
            "PUT",
            "/api/v1/model-profiles/lab%20profile",
            {
                "model": "lab-model",
                "request_mode": "responses",
                "api_key": "request-secret",
            },
            "Bearer admin-secret",
        ),
        (
            "PUT",
            "/api/v1/model-profiles/default",
            {"profile": "lab profile"},
            "Bearer admin-secret",
        ),
        (
            "DELETE",
            "/api/v1/model-profiles/lab%20profile",
            None,
            "Bearer admin-secret",
        ),
        (
            "POST",
            "/api/v1/runs",
            {"objective": "ordinary request"},
            "Bearer admin-secret",
        ),
    ]


def test_tool_client_authorizes_local_reads_and_admin_refresh() -> None:
    requests: list[tuple[str, str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path, request.headers.get("authorization")))
        return httpx.Response(200, json={"tools": []})

    with APIClient(
        "http://127.0.0.1",
        transport=httpx.MockTransport(handler),
        admin_token="admin-secret",
    ) as client:
        client.list_tools("local")
        client.refresh_tools("local")

    assert requests == [
        ("GET", "/api/v1/nodes/local/tools", "Bearer admin-secret"),
        ("POST", "/api/v1/nodes/local/refresh-tools", "Bearer admin-secret"),
    ]


def test_api_client_preserves_unified_error_details() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            409,
            json={
                "error": {
                    "code": "run_not_controllable",
                    "message": "Run is complete",
                    "details": {"status": "completed"},
                }
            },
        )

    with APIClient("http://control-plane", transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(RiftXAPIError) as captured:
            client.pause_run("run-1")

    assert captured.value.status_code == 409
    assert captured.value.code == "run_not_controllable"
    assert captured.value.details == {"status": "completed"}


def test_api_client_streams_sse_with_resume_header() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["last-event-id"] == "41"
        assert request.headers["authorization"] == "Bearer local-operator-secret"
        assert request.url.params["follow"] == "false"
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            text=(
                ": heartbeat\n\n"
                "id: 42\n"
                "event: run.status_changed\n"
                'data: {"sequence":42,"event_type":"run.status_changed",'
                '"payload":{"to":"running"}}\n\n'
            ),
        )

    with APIClient(
        "http://127.0.0.1",
        transport=httpx.MockTransport(handler),
        admin_token="local-operator-secret",
    ) as client:
        events = list(client.stream_events("run-1", last_event_id="41", follow=False))

    assert len(events) == 1
    assert events[0].id == "42"
    assert events[0].event == "run.status_changed"
    assert events[0].data == {
        "sequence": 42,
        "event_type": "run.status_changed",
        "payload": {"to": "running"},
    }


def test_parse_sse_lines_handles_multiline_and_raw_data() -> None:
    events = list(
        parse_sse_lines(
            iter(
                [
                    "id: 7",
                    "event: note",
                    "data: first",
                    "data: second",
                    "",
                ]
            )
        )
    )
    assert events[0].data == "first\nsecond"


def test_approval_client_uses_control_plane_endpoints() -> None:
    requests: list[tuple[str, str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        requests.append((request.method, request.url.path, body))
        if request.method == "GET":
            return httpx.Response(200, json={"items": []})
        return httpx.Response(200, json={"id": "approval-1", "status": "approved"})

    with APIClient(
        "http://127.0.0.1",
        transport=httpx.MockTransport(handler),
        admin_token="local-operator-secret",
    ) as client:
        client.list_approvals("run-1")
        client.approve("approval-1", approve_for_run=True)
        client.reject("approval-2", reason="Denied")

    assert requests == [
        ("GET", "/api/v1/runs/run-1/approvals", None),
        (
            "POST",
            "/api/v1/approvals/approval-1/approve",
            {"approve_for_run": True},
        ),
        (
            "POST",
            "/api/v1/approvals/approval-2/reject",
            {"reason": "Denied"},
        ),
    ]


def test_api_client_never_sends_local_operator_token_to_remote_host() -> None:
    with pytest.raises(ValueError, match="loopback Control Plane"):
        APIClient("https://control-plane.example", admin_token="must-not-leak")


def test_terminal_client_uses_rest_endpoints_and_builds_websocket_url() -> None:
    requests: list[tuple[str, str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        requests.append((request.method, request.url.path, body))
        return httpx.Response(200, json={"id": "terminal-1", "status": "open"})

    with APIClient(
        "https://control-plane.example/riftx/",
        transport=httpx.MockTransport(handler),
    ) as client:
        client.create_terminal(
            "run-1",
            argv=["python", "-i"],
            cwd="/tmp/run-1",
            cols=132,
            rows=48,
            owner="user",
        )
        client.get_terminal("terminal-1")
        client.close_terminal("terminal-1")
        websocket_url = client.terminal_websocket_url("terminal-1", cursor=42)

    assert requests == [
        (
            "POST",
            "/riftx/api/v1/runs/run-1/terminals",
            {
                "argv": ["python", "-i"],
                "cwd": "/tmp/run-1",
                "cols": 132,
                "rows": 48,
                "owner": "user",
            },
        ),
        ("GET", "/riftx/api/v1/terminals/terminal-1", None),
        ("DELETE", "/riftx/api/v1/terminals/terminal-1", None),
    ]
    assert websocket_url == (
        "wss://control-plane.example/riftx/api/v1/terminals/terminal-1/ws?cursor=42"
    )


def test_report_client_uses_control_plane_endpoints() -> None:
    requests: list[tuple[str, str, object, dict[str, str]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        requests.append(
            (request.method, request.url.path, body, dict(request.url.params.multi_items()))
        )
        if request.method == "GET" and request.url.path.endswith("/reports"):
            return httpx.Response(200, json={"items": []})
        return httpx.Response(200, json={"id": "report-1"})

    with APIClient("http://control-plane", transport=httpx.MockTransport(handler)) as client:
        client.generate_reports("run-1", formats=["markdown", "json"])
        client.list_reports("run-1", format="markdown", limit=20, offset=5)
        client.get_report("report-1")

    assert requests == [
        (
            "POST",
            "/api/v1/runs/run-1/reports",
            {"formats": ["markdown", "json"]},
            {},
        ),
        (
            "GET",
            "/api/v1/runs/run-1/reports",
            None,
            {"limit": "20", "offset": "5", "format": "markdown"},
        ),
        ("GET", "/api/v1/reports/report-1", None, {}),
    ]


def test_artifact_client_uses_control_plane_endpoints() -> None:
    requests: list[tuple[str, str, object, dict[str, str]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        requests.append(
            (request.method, request.url.path, body, dict(request.url.params.multi_items()))
        )
        if request.method == "GET" and request.url.path.endswith("/artifacts"):
            return httpx.Response(200, json={"items": []})
        return httpx.Response(200, json={"id": "artifact-1"})

    with APIClient("http://control-plane", transport=httpx.MockTransport(handler)) as client:
        client.register_artifact(
            "run-1",
            "/tmp/run-1/scan.xml",
            name="scan.xml",
            mime_type="application/xml",
            description="scan output",
            execution_id="execution-1",
        )
        client.list_artifacts("run-1", execution_id="execution-1", limit=20, offset=5)
        client.get_artifact("artifact-1")

    assert requests == [
        (
            "POST",
            "/api/v1/runs/run-1/artifacts",
            {
                "source_path": "/tmp/run-1/scan.xml",
                "description": "scan output",
                "name": "scan.xml",
                "mime_type": "application/xml",
                "execution_id": "execution-1",
            },
            {},
        ),
        (
            "GET",
            "/api/v1/runs/run-1/artifacts",
            None,
            {"limit": "20", "offset": "5", "execution_id": "execution-1"},
        ),
        ("GET", "/api/v1/artifacts/artifact-1", None, {}),
    ]


def test_execution_client_uses_query_and_cancel_endpoints() -> None:
    requests: list[tuple[str, str, dict[str, str]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path, dict(request.url.params)))
        if request.url.path.endswith("/executions"):
            return httpx.Response(200, json={"items": []})
        return httpx.Response(200, json={"id": "execution-1", "status": "running"})

    with APIClient("http://control-plane", transport=httpx.MockTransport(handler)) as client:
        client.get_execution("execution-1")
        client.list_executions("run-1", limit=25, offset=5)
        client.wait_execution(
            "execution-1",
            timeout_seconds=0.5,
            stdout_cursor=2,
            stderr_cursor=3,
            max_bytes=128,
            next_poll_after_seconds=7,
        )
        client.cancel_execution("execution-1")

    assert requests == [
        ("GET", "/api/v1/executions/execution-1", {}),
        ("GET", "/api/v1/runs/run-1/executions", {"limit": "25", "offset": "5"}),
        (
            "POST",
            "/api/v1/executions/execution-1/wait",
            {
                "timeout_seconds": "0.5",
                "stdout_cursor": "2",
                "stderr_cursor": "3",
                "max_bytes": "128",
                "next_poll_after_seconds": "7",
            },
        ),
        ("POST", "/api/v1/executions/execution-1/cancel", {}),
    ]


def test_context_client_uses_inspector_endpoints() -> None:
    requests: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path))
        return httpx.Response(200, json={"id": "compilation-1"})

    with APIClient("http://control-plane", transport=httpx.MockTransport(handler)) as client:
        client.get_run_context("run-1")
        client.get_session_context("session-1")
        client.get_context_compilation("compilation-1")

    assert requests == [
        ("GET", "/api/v1/runs/run-1/context"),
        ("GET", "/api/v1/sessions/session-1/context"),
        ("GET", "/api/v1/context-compilations/compilation-1"),
    ]


def test_metrics_client_uses_run_observability_endpoint() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"run_id": "run-1", "metrics": {}})

    with APIClient(
        "http://control-plane",
        transport=httpx.MockTransport(handler),
    ) as client:
        payload = client.get_run_metrics("run-1")

    assert payload["run_id"] == "run-1"
    assert requests[0].url.path == "/api/v1/runs/run-1/metrics"
