"""Fail-closed authorization ordering for generic reads of Audit-owned children."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import APIRouter, FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from riftx.api import APISettings, create_app
from riftx.api.auth import get_authenticated_local_principal
from riftx.api.dependencies import (
    RunReadAuthorizer,
    get_action_service,
    get_approval_service,
    get_artifact_service,
    get_browser_service,
    get_connector_service,
    get_context_service,
    get_event_service,
    get_execution_service,
    get_finding_service,
    get_graph_service,
    get_memory_service,
    get_report_service,
    get_run_read_authorizer,
    get_runtime_observability_service,
    get_terminal_service,
    get_traffic_metadata_service,
)
from riftx.api.errors import install_error_handlers
from riftx.api.routes.actions import router as actions_router
from riftx.api.routes.approvals import router as approvals_router
from riftx.api.routes.artifacts import router as artifacts_router
from riftx.api.routes.browser import router as browser_router
from riftx.api.routes.connectors import router as connectors_router
from riftx.api.routes.context import router as context_router
from riftx.api.routes.events import router as events_router
from riftx.api.routes.executions import router as executions_router
from riftx.api.routes.findings import router as findings_router
from riftx.api.routes.graphs import router as graphs_router
from riftx.api.routes.memories import router as memories_router
from riftx.api.routes.observability import router as observability_router
from riftx.api.routes.reports import router as reports_router
from riftx.api.routes.terminals import router as terminals_router
from riftx.api.routes.traffic import router as traffic_router
from riftx.application.errors import EntityNotFoundError, resource_not_accessible
from riftx.domain import (
    Artifact,
    Execution,
    ExecutorType,
    LocalPrincipal,
    OperatorCapability,
    RunKind,
    TrustProfile,
)
from riftx.memory import MemoryScope, MemoryScopeType

AUTHORIZED_RUN_ID = "audit-run-authorized"
FOREIGN_RUN_ID = "audit-run-foreign"


@dataclass(frozen=True, slots=True)
class _ReadCase:
    name: str
    router: APIRouter
    dependency: object
    method: str
    path: str
    kind: str


_OPAQUE_CASES = (
    _ReadCase(
        "artifact",
        artifacts_router,
        get_artifact_service,
        "GET",
        "/api/v1/artifacts/artifact-1",
        "artifact",
    ),
    _ReadCase(
        "artifact-content",
        artifacts_router,
        get_artifact_service,
        "GET",
        "/api/v1/artifacts/artifact-1/content",
        "artifact-content",
    ),
    _ReadCase(
        "finding",
        findings_router,
        get_finding_service,
        "GET",
        "/api/v1/findings/finding-1",
        "finding",
    ),
    _ReadCase(
        "report",
        reports_router,
        get_report_service,
        "GET",
        "/api/v1/reports/report-1",
        "report",
    ),
    _ReadCase(
        "execution",
        executions_router,
        get_execution_service,
        "GET",
        "/api/v1/executions/execution-1",
        "execution",
    ),
    _ReadCase(
        "execution-output",
        executions_router,
        get_execution_service,
        "GET",
        "/api/v1/executions/execution-1/output",
        "execution-output",
    ),
    _ReadCase(
        "execution-wait",
        executions_router,
        get_execution_service,
        "POST",
        "/api/v1/executions/execution-1/wait",
        "execution-wait",
    ),
    _ReadCase(
        "browser",
        browser_router,
        get_browser_service,
        "GET",
        "/api/v1/browser/sessions/browser-1",
        "browser",
    ),
    _ReadCase(
        "terminal",
        terminals_router,
        get_terminal_service,
        "GET",
        "/api/v1/terminals/terminal-1",
        "terminal",
    ),
    _ReadCase(
        "context",
        context_router,
        get_context_service,
        "GET",
        "/api/v1/context-compilations/context-1",
        "context",
    ),
    _ReadCase(
        "session-context",
        context_router,
        get_context_service,
        "GET",
        "/api/v1/sessions/session-1/context",
        "session-context",
    ),
    _ReadCase(
        "memory",
        memories_router,
        get_memory_service,
        "GET",
        "/api/v1/memories/memory-1",
        "memory",
    ),
)

_MISMATCH_CASES = tuple(
    case
    for case in _OPAQUE_CASES
    if case.kind
    in {
        "artifact",
        "finding",
        "report",
        "execution",
        "browser",
        "terminal",
        "context",
        "session-context",
        "memory",
    }
)

_M1_GENERAL_ONLY_KINDS = frozenset(
    {
        "browser",
        "context",
        "finding",
        "memory",
        "report",
        "session-context",
        "terminal",
    }
)

_M1_GENERAL_ONLY_READ_CASES = tuple(
    case for case in _OPAQUE_CASES if case.kind in _M1_GENERAL_ONLY_KINDS
) + (
    _ReadCase(
        "finding-list",
        findings_router,
        get_finding_service,
        "GET",
        f"/api/v1/runs/{AUTHORIZED_RUN_ID}/findings",
        "finding-list",
    ),
    _ReadCase(
        "report-list",
        reports_router,
        get_report_service,
        "GET",
        f"/api/v1/runs/{AUTHORIZED_RUN_ID}/reports",
        "report-list",
    ),
    _ReadCase(
        "approval-list",
        approvals_router,
        get_approval_service,
        "GET",
        f"/api/v1/runs/{AUTHORIZED_RUN_ID}/approvals",
        "approval-list",
    ),
    _ReadCase(
        "action-list",
        actions_router,
        get_action_service,
        "GET",
        f"/api/v1/runs/{AUTHORIZED_RUN_ID}/actions",
        "action-list",
    ),
    _ReadCase(
        "action-detail",
        actions_router,
        get_action_service,
        "GET",
        f"/api/v1/runs/{AUTHORIZED_RUN_ID}/actions/action-1",
        "action-detail",
    ),
    _ReadCase(
        "graph",
        graphs_router,
        get_graph_service,
        "GET",
        f"/api/v1/runs/{AUTHORIZED_RUN_ID}/graph?view=task",
        "graph",
    ),
    _ReadCase(
        "metrics",
        observability_router,
        get_runtime_observability_service,
        "GET",
        f"/api/v1/runs/{AUTHORIZED_RUN_ID}/metrics",
        "metrics",
    ),
    _ReadCase(
        "target-http-list",
        traffic_router,
        get_traffic_metadata_service,
        "GET",
        f"/api/v1/runs/{AUTHORIZED_RUN_ID}/target-http/exchanges",
        "target-http-list",
    ),
    _ReadCase(
        "target-http-detail",
        traffic_router,
        get_traffic_metadata_service,
        "GET",
        (
            f"/api/v1/runs/{AUTHORIZED_RUN_ID}/target-http/exchanges/"
            "exchange-1"
        ),
        "target-http-detail",
    ),
    _ReadCase(
        "run-context",
        context_router,
        get_context_service,
        "GET",
        f"/api/v1/runs/{AUTHORIZED_RUN_ID}/context",
        "run-context",
    ),
    _ReadCase(
        "memory-list-run-scope",
        memories_router,
        get_memory_service,
        "GET",
        (
            "/api/v1/memories?scope_type=run"
            f"&scope_id={AUTHORIZED_RUN_ID}"
        ),
        "memory-list",
    ),
    _ReadCase(
        "memory-search-run-scope",
        memories_router,
        get_memory_service,
        "GET",
        f"/api/v1/memories/search?q=audit&run_id={AUTHORIZED_RUN_ID}",
        "memory-search",
    ),
    _ReadCase(
        "connector-events",
        connectors_router,
        get_connector_service,
        "GET",
        f"/api/v1/connectors/runs/{AUTHORIZED_RUN_ID}/events",
        "connector-events",
    ),
    _ReadCase(
        "connector-webui",
        connectors_router,
        get_connector_service,
        "GET",
        f"/api/v1/connectors/runs/{AUTHORIZED_RUN_ID}/webui",
        "connector-webui",
    ),
)

_M1_GENERAL_ONLY_READ_ROUTE_NAMES = frozenset(
    {
        "connector_events",
        "connector_webui",
        "get_browser",
        "get_context_compilation",
        "get_finding",
        "get_memory",
        "get_report",
        "get_run_action",
        "get_run_context",
        "get_run_graph",
        "get_run_metrics",
        "get_session_context",
        "get_target_http_exchange",
        "get_terminal",
        "list_approvals",
        "list_findings",
        "list_memories",
        "list_reports",
        "list_run_actions",
        "list_target_http_exchanges",
        "search_memories",
    }
)


class _AuthorizerSpy:
    def __init__(
        self,
        *,
        deny: bool,
        kind: RunKind = RunKind.CODE_AUDIT,
    ) -> None:
        self.deny = deny
        self.kind = kind
        self.calls: list[str] = []

    async def require(self, run_id: str) -> object:
        self.calls.append(run_id)
        if self.deny:
            raise resource_not_accessible()
        return SimpleNamespace(id=run_id, kind=self.kind)


class _ReadServiceSpy:
    def __init__(
        self,
        kind: str,
        *,
        missing: bool = False,
        mismatch: bool = False,
        disappeared: bool = False,
    ) -> None:
        self.kind = kind
        self.missing = missing
        self.mismatch = mismatch
        self.disappeared = disappeared
        self.resolver_calls = 0
        self.full_calls = 0
        self.io_calls = 0

    def _resolved_run_id(self) -> str:
        self.resolver_calls += 1
        if self.missing:
            raise resource_not_accessible()
        return AUTHORIZED_RUN_ID

    async def resolve_run_id(self, _resource_id: str) -> str:
        return self._resolved_run_id()

    async def resolve_scope(self, _memory_id: str) -> MemoryScope:
        return MemoryScope(
            scope_type=MemoryScopeType.RUN,
            scope_id=self._resolved_run_id(),
        )

    async def resolve_latest_for_session(self, _session_id: str) -> tuple[str, str]:
        return "context-1", self._resolved_run_id()

    async def get(self, resource_id: str, **_: object) -> object:
        self.full_calls += 1
        if self.disappeared:
            raise EntityNotFoundError("SensitiveChild", "canary-resource-id")
        if not self.mismatch:
            raise AssertionError("full child getter ran before authorization")
        if self.kind == "browser":
            return SimpleNamespace(session=SimpleNamespace(run_id=FOREIGN_RUN_ID))
        if self.kind == "terminal":
            return SimpleNamespace(
                terminal=SimpleNamespace(run_id=FOREIGN_RUN_ID),
                execution=SimpleNamespace(run_id=FOREIGN_RUN_ID),
            )
        if self.kind in {"context", "session-context"}:
            return SimpleNamespace(
                id=resource_id,
                run_id=FOREIGN_RUN_ID,
                session_id="session-1",
                manifest=SimpleNamespace(
                    run_id=FOREIGN_RUN_ID,
                    session_id="session-1",
                ),
            )
        if self.kind == "memory":
            return SimpleNamespace(
                scope=MemoryScope(
                    scope_type=MemoryScopeType.RUN,
                    scope_id=FOREIGN_RUN_ID,
                )
            )
        return SimpleNamespace(run_id=FOREIGN_RUN_ID)

    async def get_finding(self, _finding_id: str) -> object:
        self.full_calls += 1
        if self.disappeared:
            raise EntityNotFoundError("SensitiveFinding", "canary-finding-id")
        if not self.mismatch:
            raise AssertionError("Finding evidence loaded before authorization")
        return SimpleNamespace(run_id=FOREIGN_RUN_ID)

    async def open_public_content(self, *_: object, **__: object) -> object:
        self.io_calls += 1
        if self.disappeared:
            raise EntityNotFoundError("SensitiveArtifact", "canary-artifact-id")
        raise AssertionError("Artifact file/hash I/O ran before authorization")

    async def output(self, *_: object, **__: object) -> object:
        self.io_calls += 1
        if self.disappeared:
            raise EntityNotFoundError("SensitiveExecution", "canary-execution-id")
        raise AssertionError("Runner output I/O ran before authorization")

    async def wait(self, *_: object, **__: object) -> object:
        self.io_calls += 1
        if self.disappeared:
            raise EntityNotFoundError("SensitiveExecution", "canary-execution-id")
        raise AssertionError("Runner wait/output I/O ran before authorization")

    async def latest_for_run(self, *_: object, **__: object) -> object:
        self.full_calls += 1
        raise AssertionError("Context Manifest loaded before the M1 RunKind fence")

    async def list_scope(self, *_: object, **__: object) -> list[object]:
        self.full_calls += 1
        raise AssertionError("Memory content loaded before the M1 RunKind fence")

    async def retrieve(self, *_: object, **__: object) -> list[object]:
        self.full_calls += 1
        raise AssertionError("Memory retrieval ran before the M1 RunKind fence")

    async def list(self, *_: object, **__: object) -> list[object]:
        self.full_calls += 1
        raise AssertionError("Generic content list ran before the M1 RunKind fence")

    async def list_findings(self, *_: object, **__: object) -> list[object]:
        self.full_calls += 1
        raise AssertionError("Finding list ran before the M1 RunKind fence")

    async def get_view(self, *_: object, **__: object) -> object:
        self.full_calls += 1
        raise AssertionError("Graph projection ran before the M1 RunKind fence")

    async def snapshot(self, *_: object, **__: object) -> object:
        self.full_calls += 1
        raise AssertionError("Metrics snapshot ran before the M1 RunKind fence")


def _app(case: _ReadCase, service: object, authorizer: object) -> FastAPI:
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(case.router, prefix="/api/v1")
    app.dependency_overrides[case.dependency] = lambda: service
    app.dependency_overrides[get_run_read_authorizer] = lambda: authorizer
    app.dependency_overrides[get_authenticated_local_principal] = lambda: LocalPrincipal(
        id="authorized-local-operator",
        capabilities=frozenset({OperatorCapability.READ}),
    )
    return app


async def _request(app: FastAPI, case: _ReadCase) -> httpx.Response:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        return await client.request(case.method, case.path)


@pytest.mark.parametrize("case", _OPAQUE_CASES, ids=lambda case: case.name)
async def test_child_missing_and_denied_are_byte_identical_before_full_read_or_io(
    case: _ReadCase,
) -> None:
    missing_service = _ReadServiceSpy(case.kind, missing=True)
    missing_authorizer = _AuthorizerSpy(deny=False)
    missing = await _request(
        _app(case, missing_service, missing_authorizer),
        case,
    )

    denied_service = _ReadServiceSpy(case.kind)
    denied_authorizer = _AuthorizerSpy(deny=True)
    denied = await _request(
        _app(case, denied_service, denied_authorizer),
        case,
    )

    assert missing.status_code == denied.status_code == 404
    assert missing.content == denied.content
    assert missing.json() == {
        "error": {
            "code": "resource_not_accessible",
            "message": "The requested resource was not found",
            "details": {},
        }
    }
    assert missing_authorizer.calls == []
    assert denied_authorizer.calls == [AUTHORIZED_RUN_ID]
    assert missing_service.full_calls == denied_service.full_calls == 0
    assert missing_service.io_calls == denied_service.io_calls == 0


@pytest.mark.parametrize("case", _MISMATCH_CASES, ids=lambda case: case.name)
async def test_resolver_and_full_object_owner_mismatch_fails_closed(
    case: _ReadCase,
) -> None:
    service = _ReadServiceSpy(case.kind, mismatch=True)
    authorizer = _AuthorizerSpy(
        deny=False,
        kind=(
            RunKind.GENERAL
            if case.kind in _M1_GENERAL_ONLY_KINDS
            else RunKind.CODE_AUDIT
        ),
    )

    response = await _request(_app(case, service, authorizer), case)

    assert response.status_code == 404
    assert response.json()["error"] == {
        "code": "resource_not_accessible",
        "message": "The requested resource was not found",
        "details": {},
    }
    assert authorizer.calls == [AUTHORIZED_RUN_ID]
    assert service.full_calls == 1
    assert service.io_calls == 0


@pytest.mark.parametrize("case", _OPAQUE_CASES, ids=lambda case: case.name)
async def test_child_disappearing_after_authorization_stays_opaque(
    case: _ReadCase,
) -> None:
    service = _ReadServiceSpy(case.kind, disappeared=True)
    authorizer = _AuthorizerSpy(
        deny=False,
        kind=(
            RunKind.GENERAL
            if case.kind in _M1_GENERAL_ONLY_KINDS
            else RunKind.CODE_AUDIT
        ),
    )

    response = await _request(_app(case, service, authorizer), case)

    assert response.status_code == 404
    assert response.json()["error"] == {
        "code": "resource_not_accessible",
        "message": "The requested resource was not found",
        "details": {},
    }
    assert "canary" not in response.text
    assert authorizer.calls == [AUTHORIZED_RUN_ID]
    assert service.full_calls + service.io_calls == 1


@pytest.mark.parametrize(
    "case",
    _M1_GENERAL_ONLY_READ_CASES,
    ids=lambda case: case.name,
)
async def test_code_audit_generic_reads_not_approved_for_m1_default_deny_before_content(
    case: _ReadCase,
) -> None:
    service = _ReadServiceSpy(case.kind)
    authorizer = _AuthorizerSpy(deny=False, kind=RunKind.CODE_AUDIT)

    response = await _request(_app(case, service, authorizer), case)

    assert response.status_code == 409
    assert response.json()["error"] == {
        "code": "run_kind_operation_unsupported",
        "message": "The requested operation is not supported for this Run kind",
        "details": {},
    }
    assert authorizer.calls == [AUTHORIZED_RUN_ID]
    assert service.full_calls == 0
    assert service.io_calls == 0


def test_m1_general_only_read_routes_publish_the_kind_conflict_contract() -> None:
    routes = {
        route.name: route
        for case in _M1_GENERAL_ONLY_READ_CASES
        for route in case.router.routes
        if isinstance(route, APIRoute)
    }

    assert _M1_GENERAL_ONLY_READ_ROUTE_NAMES <= routes.keys()
    for route_name in _M1_GENERAL_ONLY_READ_ROUTE_NAMES:
        assert 409 in routes[route_name].responses, route_name


async def test_disabled_audit_feature_keeps_authorized_child_read_available() -> None:
    artifact = Artifact(
        id="artifact-1",
        run_id=AUTHORIZED_RUN_ID,
        name="safe.txt",
        path="/server-owned/not-returned/safe.txt",
        mime_type="text/plain",
        sha256="a" * 64,
        size=4,
    )

    class _RunReads:
        async def resolve_kind(self, run_id: str) -> RunKind:
            assert run_id == AUTHORIZED_RUN_ID
            return RunKind.CODE_AUDIT

        async def get_run(self, _run_id: str) -> object:
            raise AssertionError("Code Audit read bypassed the Audit authorization root")

    class _DisabledAuditReads:
        feature_enabled = False

        async def get_by_run_authorized(self, run_id: str, **_: object) -> object:
            assert run_id == AUTHORIZED_RUN_ID
            return SimpleNamespace(
                run=SimpleNamespace(id=run_id, kind=RunKind.CODE_AUDIT)
            )

    class _Artifacts:
        async def resolve_run_id(self, artifact_id: str) -> str:
            assert artifact_id == artifact.id
            return AUTHORIZED_RUN_ID

        async def get(self, artifact_id: str) -> Artifact:
            assert artifact_id == artifact.id
            return artifact

    principal = LocalPrincipal(
        id="local-operator",
        capabilities=frozenset({OperatorCapability.READ}),
    )
    authorizer = RunReadAuthorizer(
        run_service=_RunReads(),  # type: ignore[arg-type]
        audit_service=_DisabledAuditReads(),  # type: ignore[arg-type]
        principal=principal,
        audit_authorizer=object(),  # type: ignore[arg-type]
    )
    case = _OPAQUE_CASES[0]
    response = await _request(_app(case, _Artifacts(), authorizer), case)

    assert response.status_code == 200, response.text
    assert response.json()["id"] == artifact.id
    assert "path" not in response.json()


async def test_code_audit_event_read_remains_in_the_m1_allowlist() -> None:
    class _Events:
        def __init__(self) -> None:
            self.calls: list[tuple[str, int, int]] = []

        async def list_events(
            self,
            run_id: str,
            *,
            after_sequence: int,
            limit: int,
        ) -> list[object]:
            self.calls.append((run_id, after_sequence, limit))
            return []

    service = _Events()
    case = _ReadCase(
        "events-allowed",
        events_router,
        get_event_service,
        "GET",
        f"/api/v1/runs/{AUTHORIZED_RUN_ID}/events",
        "events",
    )
    response = await _request(
        _app(
            case,
            service,
            _AuthorizerSpy(deny=False, kind=RunKind.CODE_AUDIT),
        ),
        case,
    )

    assert response.status_code == 200, response.text
    assert response.json() == {"items": [], "after_sequence": 0}
    assert service.calls == [(AUTHORIZED_RUN_ID, 0, 100)]


async def test_code_audit_execution_detail_and_list_use_positive_allowlist_projection() -> None:
    secret = "RIFTX_AUDIT_EXECUTION_SECRET_CANARY"
    execution = Execution(
        id="execution-1",
        execution_key=f"{secret}-key",
        run_id=AUTHORIZED_RUN_ID,
        node_id="local",
        executor_type=ExecutorType.PROCESS,
        argv=["sh", "-c", f"echo {secret}"],
        command_text=f"echo {secret}",
        tool_id="audit-validator",
        tool_version="1",
        executable_path=f"/private/{secret}/sh",
        cwd=f"/private/{secret}/cwd",
        env_diff={"SECRET": secret},
        stdout_path=f"/private/{secret}/stdout",
        stderr_path=f"/private/{secret}/stderr",
    )

    class _Executions:
        async def resolve_run_id(self, execution_id: str) -> str:
            assert execution_id == execution.id
            return execution.run_id

        async def get(self, execution_id: str) -> Execution:
            assert execution_id == execution.id
            return execution

        async def list(self, run_id: str, **_: object) -> list[Execution]:
            assert run_id == execution.run_id
            return [execution]

    authorizer = _AuthorizerSpy(deny=False)
    case = _ReadCase(
        "execution-safe-projection",
        executions_router,
        get_execution_service,
        "GET",
        f"/api/v1/executions/{execution.id}",
        "execution",
    )
    app = _app(case, _Executions(), authorizer)

    detail = await _request(app, case)
    list_case = _ReadCase(
        "execution-safe-list-projection",
        executions_router,
        get_execution_service,
        "GET",
        f"/api/v1/runs/{execution.run_id}/executions",
        "execution",
    )
    listed = await _request(app, list_case)

    assert detail.status_code == listed.status_code == 200
    assert listed.json()["items"] == [detail.json()]
    assert set(detail.json()) == {
        "kind",
        "id",
        "run_id",
        "node_id",
        "executor_type",
        "tool_id",
        "tool_version",
        "status",
        "exit_code",
        "started_at",
        "finished_at",
        "physical_stop_confirmed_at",
    }
    assert detail.json()["kind"] == "code_audit"
    assert secret not in detail.text
    assert secret not in listed.text


async def test_session_context_revalidates_resolved_compilation_identity() -> None:
    class _Contexts:
        async def resolve_latest_for_session(self, session_id: str) -> tuple[str, str]:
            assert session_id == "session-1"
            return "expected-compilation", AUTHORIZED_RUN_ID

        async def get(self, compilation_id: str) -> object:
            assert compilation_id == "expected-compilation"
            return SimpleNamespace(
                id="same-run-foreign-compilation",
                run_id=AUTHORIZED_RUN_ID,
                session_id="session-1",
                manifest=SimpleNamespace(
                    run_id=AUTHORIZED_RUN_ID,
                    session_id="session-1",
                ),
            )

    case = next(case for case in _OPAQUE_CASES if case.kind == "session-context")
    response = await _request(
        _app(
            case,
            _Contexts(),
            _AuthorizerSpy(deny=False, kind=RunKind.GENERAL),
        ),
        case,
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "resource_not_accessible"
    assert "same-run-foreign-compilation" not in response.text


@pytest.mark.parametrize(
    "path",
    (
        "/api/v1/browser/sessions/session-1/stream",
        "/api/v1/terminals/session-1/ws",
    ),
)
def test_websocket_post_authorization_disappearance_is_opaque(
    tmp_path: Path,
    path: str,
) -> None:
    canary = "RIFTX_DISAPPEARING_CHILD_CANARY"

    class _DisappearingService:
        async def resolve_run_id(self, session_id: str) -> str:
            assert session_id == "session-1"
            return AUTHORIZED_RUN_ID

        async def get(self, session_id: str, **_: object) -> object:
            assert session_id == "session-1"
            raise EntityNotFoundError("SensitiveSession", canary)

    class _GeneralRunReads:
        async def resolve_kind(self, run_id: str) -> RunKind:
            assert run_id == AUTHORIZED_RUN_ID
            return RunKind.GENERAL

        async def get_run(self, run_id: str) -> object:
            assert run_id == AUTHORIZED_RUN_ID
            return SimpleNamespace(id=run_id, kind=RunKind.GENERAL)

    service = _DisappearingService()
    settings = APISettings(
        trust_profile=TrustProfile.LOCAL_SINGLE_OPERATOR,
        local_principal_path=tmp_path / "local-principal.json",
        admin_token="test-only-audit-child-read-token-0001",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'unused.db'}",
        web_dist_path=tmp_path / "missing-web",
        cors_origins=(),
    )
    control_plane = SimpleNamespace(
        settings=settings,
        run_service=_GeneralRunReads(),
        audit_service=object(),
        browser_service=service,
        terminal_service=service,
    )
    app = create_app(control_plane=control_plane)  # type: ignore[arg-type]

    with TestClient(
        app,
        headers={"Authorization": "Bearer test-only-audit-child-read-token-0001"},
    ) as client:
        with client.websocket_connect(path) as websocket:
            error = websocket.receive_json()
            with pytest.raises(WebSocketDisconnect) as closed:
                websocket.receive_json()

    assert error == {
        "type": "error",
        "code": "resource_not_accessible",
        "message": "The requested resource was not found",
    }
    assert canary not in str(error)
    assert closed.value.code == 4409
