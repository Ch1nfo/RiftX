from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

import riftx.application.services.runs as run_service_module
from riftx.api.auth import get_authenticated_local_principal
from riftx.api.dependencies import (
    get_artifact_service,
    get_finding_service,
    get_memory_service,
    get_report_service,
    get_run_service,
    get_terminal_service,
    get_tool_service,
)
from riftx.api.errors import install_error_handlers
from riftx.api.routes.artifacts import router as artifacts_router
from riftx.api.routes.connectors import router as connectors_router
from riftx.api.routes.findings import router as findings_router
from riftx.api.routes.memories import router as memories_router
from riftx.api.routes.reports import router as reports_router
from riftx.api.routes.runs import router as runs_router
from riftx.api.routes.terminals import router as terminals_router
from riftx.application.run_kind_effects import (
    PolicyDenialReason,
    RunKindEffectPolicyDenied,
)
from riftx.domain import (
    EntryPoint,
    EntryPointKind,
    LocalPrincipal,
    Objective,
    OperatorCapability,
    PentestAdmission,
    PentestBudget,
    Run,
    RunKind,
    Scope,
)


@dataclass
class FakeRunService:
    run: Run
    mutation_calls: list[tuple[str, str]] = field(default_factory=list)

    async def get_run(self, run_id: str) -> Run:
        assert run_id == self.run.id
        return self.run

    async def create_run(self, command: object, *, principal: object) -> Run:
        del command, principal
        return self._record("create_run", self.run.id)

    async def resolve_kind(self, run_id: str) -> RunKind:
        assert run_id == self.run.id
        return self.run.kind

    async def list_runs(self, **filters: object) -> list[Run]:
        kind = filters.get("kind")
        return [self.run] if kind is None or kind is self.run.kind else []

    async def pause(self, run_id: str) -> Run:
        return self._record("pause", run_id)

    async def resume(self, run_id: str) -> Run:
        return self._record("resume", run_id)

    async def cancel(self, run_id: str) -> Run:
        return self._record("cancel", run_id)

    async def cancel_current_execution(self, run_id: str) -> Run:
        return self._record("cancel_current_execution", run_id)

    async def compact(self, run_id: str, *, max_history_items: int = 100) -> Run:
        del max_history_items
        return self._record("compact", run_id)

    async def switch_model(self, run_id: str, model_profile: str) -> Run:
        del model_profile
        return self._record("switch_model", run_id)

    async def append_user_message(
        self,
        run_id: str,
        message: str,
        *,
        message_event_id: str | None = None,
    ) -> Run:
        del message, message_event_id
        return self._record("append_user_message", run_id)

    def _record(self, operation: str, run_id: str) -> Run:
        self.mutation_calls.append((operation, run_id))
        return self.run


@dataclass
class FakeEffectService:
    calls: list[str] = field(default_factory=list)

    async def create(self, *_: object, **__: object) -> object:
        self.calls.append("create")
        return object()

    async def create_finding(self, *_: object, **__: object) -> object:
        self.calls.append("create_finding")
        return object()

    async def register(self, *_: object, **__: object) -> object:
        self.calls.append("register")
        return object()

    async def generate(self, *_: object, **__: object) -> list[object]:
        self.calls.append("generate")
        return []


def _run(kind: RunKind, tmp_path: Path) -> Run:
    pentest = kind is RunKind.PENTEST
    return Run(
        kind=kind,
        id=f"{kind.value}-run",
        engagement_id="engagement-1",
        node_id="local",
        objective=Objective(description=f"{kind.value} projection"),
        entry_points=(
            [EntryPoint(kind=EntryPointKind.DOMAIN, value="example.test")]
            if pentest
            else []
        ),
        scope=Scope(domains=["example.test"] if pentest else []),
        pentest_admission=(
            PentestAdmission(
                budget=PentestBudget(
                    max_duration_seconds=3600,
                    max_model_calls=100,
                    max_tokens=100_000,
                    max_tool_calls=200,
                    max_target_interactions=50,
                    max_concurrent_target_interactions=2,
                )
            )
            if pentest
            else None
        ),
        workspace_path=str(tmp_path / f"{kind.value}-workspace-sensitive"),
        temporal_workflow_id=f"{kind.value}-workflow-sensitive",
    )


def _app(service: FakeRunService) -> FastAPI:
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(runs_router, prefix="/api/v1")
    app.include_router(connectors_router, prefix="/api/v1")
    app.dependency_overrides[get_run_service] = lambda: service
    app.dependency_overrides[get_tool_service] = lambda: SimpleNamespace(node_id="local")
    app.dependency_overrides[get_authenticated_local_principal] = lambda: LocalPrincipal(
        id="principal-1",
        capabilities=frozenset(OperatorCapability),
    )
    return app


def _client(service: FakeRunService) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(service)),
        base_url="http://test",
    )


def _effect_client(
    service: FakeRunService,
    effects: FakeEffectService,
) -> httpx.AsyncClient:
    app = FastAPI()
    install_error_handlers(app)
    for route in (
        terminals_router,
        findings_router,
        artifacts_router,
        reports_router,
        memories_router,
    ):
        app.include_router(route, prefix="/api/v1")
    app.dependency_overrides[get_run_service] = lambda: service
    app.dependency_overrides[get_terminal_service] = lambda: effects
    app.dependency_overrides[get_finding_service] = lambda: effects
    app.dependency_overrides[get_artifact_service] = lambda: effects
    app.dependency_overrides[get_report_service] = lambda: effects
    app.dependency_overrides[get_memory_service] = lambda: effects
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    )


@pytest.mark.asyncio
async def test_code_audit_generic_reads_are_retired_and_fail_closed(
    tmp_path: Path,
) -> None:
    run = _run(RunKind.CODE_AUDIT, tmp_path)
    service = FakeRunService(run)

    async with _client(service) as client:
        detail = await client.get(f"/api/v1/runs/{run.id}")
        listed = await client.get("/api/v1/runs", params={"kind": "code_audit"})

    assert detail.status_code == 404, detail.text
    assert detail.json()["error"]["code"] == "resource_not_accessible"
    assert listed.status_code == 410, listed.text
    assert listed.json()["error"]["code"] == "code_audit_retired"
    assert run.workspace_path not in detail.text + listed.text
    assert run.temporal_workflow_id not in detail.text + listed.text


@pytest.mark.asyncio
async def test_pentest_generic_reads_preserve_discriminated_admission_projection(
    tmp_path: Path,
) -> None:
    run = _run(RunKind.PENTEST, tmp_path)
    service = FakeRunService(run)

    async with _client(service) as client:
        detail = await client.get(f"/api/v1/runs/{run.id}")
        listed = await client.get("/api/v1/runs", params={"kind": "pentest"})

    assert detail.status_code == 200, detail.text
    assert listed.status_code == 200, listed.text
    for payload in (detail.json(), listed.json()["items"][0]):
        assert payload["kind"] == RunKind.PENTEST.value
        assert payload["pentest_admission"] == run.pentest_admission.model_dump(mode="json")


@pytest.mark.asyncio
async def test_pentest_generic_controls_use_the_interactive_service_path(
    tmp_path: Path,
) -> None:
    run = _run(RunKind.PENTEST, tmp_path)
    service = FakeRunService(run)

    async with _client(service) as client:
        responses = [
            await client.post(f"/api/v1/runs/{run.id}/pause"),
            await client.post(f"/api/v1/runs/{run.id}/resume"),
            await client.post(f"/api/v1/runs/{run.id}/cancel"),
            await client.post(f"/api/v1/runs/{run.id}/cancel-current-execution"),
        ]

    assert [response.status_code for response in responses] == [202, 202, 202, 202]
    assert service.mutation_calls == [
        ("pause", run.id),
        ("resume", run.id),
        ("cancel", run.id),
        ("cancel_current_execution", run.id),
    ]


@pytest.mark.asyncio
async def test_code_audit_generic_run_api_bridge_rejects_before_service_mutation(
    tmp_path: Path,
) -> None:
    run = _run(RunKind.CODE_AUDIT, tmp_path)
    service = FakeRunService(run)
    requests: tuple[tuple[str, dict[str, object] | None], ...] = (
        (f"/api/v1/runs/{run.id}/pause", None),
        (f"/api/v1/runs/{run.id}/resume", None),
        (f"/api/v1/runs/{run.id}/cancel", None),
        (f"/api/v1/runs/{run.id}/cancel-current-execution", None),
        (f"/api/v1/runs/{run.id}/compact", {"max_history_items": 1}),
        (f"/api/v1/runs/{run.id}/model", {"model_profile": "fast"}),
        (f"/api/v1/runs/{run.id}/message", {"message": "bypass"}),
        (f"/api/v1/connectors/runs/{run.id}/cancel", None),
    )

    async with _client(service) as client:
        responses = [await client.post(path, json=body) for path, body in requests]

    for response in responses:
        assert response.status_code == 409, response.text
        assert response.json() == {
            "error": {
                "code": "run_kind_operation_unsupported",
                "message": "The requested operation is not supported for this Run kind",
                "details": {},
            }
        }
    assert service.mutation_calls == []


@pytest.mark.asyncio
async def test_code_audit_direct_effect_routes_reject_before_child_service(
    tmp_path: Path,
) -> None:
    run = _run(RunKind.CODE_AUDIT, tmp_path)
    runs = FakeRunService(run)
    effects = FakeEffectService()
    requests: tuple[tuple[str, dict[str, object]], ...] = (
        (
            f"/api/v1/runs/{run.id}/terminals",
            {"argv": ["sh", "-c", "touch must-not-exist"]},
        ),
        (
            f"/api/v1/runs/{run.id}/findings",
            {"title": "forged audit fact", "severity": "critical"},
        ),
        (
            f"/api/v1/runs/{run.id}/artifacts",
            {"source_path": "/sensitive/host/file"},
        ),
        (
            f"/api/v1/runs/{run.id}/reports",
            {"formats": ["json"]},
        ),
        (
            "/api/v1/memories",
            {
                "memory_type": "semantic",
                "scope_type": "run",
                "scope_id": run.id,
                "title": "forged memory",
                "content": "must not persist",
                "summary": "must not persist",
                "source_refs": ["user://forged"],
            },
        ),
    )

    async with _effect_client(runs, effects) as client:
        responses = [await client.post(path, json=body) for path, body in requests]

    for response in responses:
        assert response.status_code == 409, response.text
        assert response.json()["error"]["code"] == "run_kind_operation_unsupported"
    assert effects.calls == []


@pytest.mark.asyncio
async def test_general_run_read_and_mutation_contract_remains_compatible(tmp_path: Path) -> None:
    run = _run(RunKind.GENERAL, tmp_path)
    service = FakeRunService(run)

    async with _client(service) as client:
        detail = await client.get(f"/api/v1/runs/{run.id}")
        paused = await client.post(f"/api/v1/runs/{run.id}/pause")

    assert detail.status_code == 200, detail.text
    assert detail.json()["workspace_path"] == run.workspace_path
    assert detail.json()["temporal_workflow_id"] == run.temporal_workflow_id
    assert paused.status_code == 202, paused.text
    assert paused.json()["run"]["workspace_path"] == run.workspace_path
    assert service.mutation_calls == [("pause", run.id)]


@pytest.mark.asyncio
async def test_general_run_api_catalog_denial_precedes_every_service_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def deny_policy(*_: object, **__: object) -> None:
        raise RunKindEffectPolicyDenied(PolicyDenialReason.OWNERSHIP_CLAIM_MISSING)

    monkeypatch.setattr(
        run_service_module,
        "require_run_kind_effect_policy",
        deny_policy,
    )
    run = _run(RunKind.GENERAL, tmp_path)
    service = FakeRunService(run)
    requests: tuple[tuple[str, dict[str, object] | None], ...] = (
        ("/api/v1/runs", {"objective": "must-not-create"}),
        (f"/api/v1/runs/{run.id}/pause", None),
        (f"/api/v1/runs/{run.id}/resume", None),
        (f"/api/v1/runs/{run.id}/cancel", None),
        (f"/api/v1/runs/{run.id}/cancel-current-execution", None),
        (f"/api/v1/runs/{run.id}/compact", {"max_history_items": 1}),
        (f"/api/v1/runs/{run.id}/model", {"model_profile": "fast"}),
        (f"/api/v1/runs/{run.id}/message", {"message": "must-not-dispatch"}),
    )

    async with _client(service) as client:
        responses = [await client.post(path, json=body) for path, body in requests]

    for response in responses:
        assert response.status_code == 409, response.text
        assert response.json()["error"]["code"] == "run_kind_effect_policy_denied"
    assert service.mutation_calls == []
