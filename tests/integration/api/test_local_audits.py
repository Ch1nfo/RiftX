"""API contract for simplified same-machine local Code Audit jobs."""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import httpx

from riftx.api import APISettings, create_app
from riftx.api.dependencies import (
    get_audit_control_service,
    get_audit_service,
    get_local_audit_job_service,
    get_optional_local_audit_job_service,
)
from riftx.audit import LocalAuditJobService, LocalAuditWorker, LocalAuditWorkerConfig
from riftx.config import AuditConfig
from riftx.domain import TrustProfile
from riftx.persistence import Database, SQLAlchemyLocalAuditJobRepository

LOCAL_TOKEN = "test-only-local-audit-api-token-0001"


@asynccontextmanager
async def _local_audit_api(
    tmp_path: Path,
    *,
    name: str,
) -> AsyncIterator[
    tuple[
        Path,
        Database,
        LocalAuditJobService,
        object,
        httpx.AsyncClient,
    ]
]:
    source_root = tmp_path / name / "sources"
    state_root = tmp_path / name / "state"
    source_root.mkdir(parents=True)
    state_root.mkdir(parents=True)
    state_root.chmod(0o700)
    database = Database(f"sqlite+aiosqlite:///{state_root / 'riftx.db'}")
    await database.create_schema()
    repository = SQLAlchemyLocalAuditJobRepository(database.session_factory)
    service = LocalAuditJobService(
        repository,
        LocalAuditWorker(
            repository,
            LocalAuditWorkerConfig(
                allowed_roots=(source_root,),
                protected_paths=(state_root,),
                staging_root=state_root / "staging",
                snapshot_root=state_root / "snapshots",
            ),
        ),
        auto_dispatch=True,
    )
    settings = APISettings(
        trust_profile=TrustProfile.LOCAL_SINGLE_OPERATOR,
        local_principal_path=state_root / "principal.json",
        admin_token=LOCAL_TOKEN,
        cors_origins=(),
        web_dist_path=state_root / "missing-web-dist",
        audit=AuditConfig(
            enabled=True,
            source_roots=(source_root,),
            snapshot_root=state_root / "configured-snapshots",
            temp_root=state_root / "configured-temp",
            fix_root=state_root / "configured-fixes",
        ),
    )
    control_plane = SimpleNamespace(settings=settings)
    app = create_app(control_plane=control_plane)  # type: ignore[arg-type]
    app.dependency_overrides[get_audit_service] = lambda: SimpleNamespace()
    app.dependency_overrides[get_audit_control_service] = lambda: SimpleNamespace()
    app.dependency_overrides[get_local_audit_job_service] = lambda: service
    app.dependency_overrides[get_optional_local_audit_job_service] = lambda: service
    try:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://test",
                headers={"Authorization": f"Bearer {LOCAL_TOKEN}"},
            ) as client:
                yield source_root, database, service, app, client
    finally:
        await service.close()
        await database.dispose()


def _tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


async def test_local_audit_api_runs_filters_reports_and_survives_read_restart(
    tmp_path: Path,
) -> None:
    async with _local_audit_api(tmp_path, name="complete") as (
        source_root,
        database,
        service,
        app,
        client,
    ):
        project = source_root / "project"
        project.mkdir()
        (project / "app.py").write_text(
            'password = "correct-horse-battery-staple"\nresult = eval(input())\n',
            encoding="utf-8",
        )
        before = _tree_digest(project)

        created = await client.post(
            "/api/v1/audits",
            json={"source_path": str(project)},
        )
        assert created.status_code == 201, created.text
        assert "source_path" not in created.text
        audit_id = created.json()["audit_id"]

        started = await client.post(f"/api/v1/audits/{audit_id}/start")
        assert started.status_code == 200, started.text
        completed = await service.wait(audit_id)
        assert completed is not None
        assert completed.status.value == "completed"

        status = await client.get(f"/api/v1/audits/{audit_id}")
        findings = await client.get(f"/api/v1/audits/{audit_id}/findings")
        secrets = await client.get(
            f"/api/v1/audits/{audit_id}/findings",
            params={"severity": "high", "category": "secret", "file": "app.py"},
        )
        assert status.status_code == findings.status_code == secrets.status_code == 200
        assert status.json()["status"] == "completed"
        assert status.json()["finding_count"] == 2
        assert findings.json()["total"] == 2
        assert secrets.json()["total"] == 1
        secret = secrets.json()["items"][0]
        assert secret["category"] == "secret"
        assert secret["evidence_excerpt"].endswith('"[REDACTED]"')

        detail = await client.get(
            f"/api/v1/audits/{audit_id}/findings/{secret['finding_id']}"
        )
        json_report = await client.get(
            f"/api/v1/audits/{audit_id}/report",
            params={"format": "json"},
        )
        markdown_report = await client.get(
            f"/api/v1/audits/{audit_id}/report",
            params={"format": "markdown"},
        )
        assert detail.status_code == 200
        assert detail.json() == secret
        assert json_report.headers["content-type"].startswith("application/json")
        assert json.loads(json_report.text)["summary"]["finding_count"] == 2
        assert markdown_report.headers["content-type"].startswith("text/markdown")
        assert "# RiftX Local Code Audit Report" in markdown_report.text
        assert _tree_digest(project) == before

        readback = LocalAuditJobService(
            SQLAlchemyLocalAuditJobRepository(database.session_factory),
            None,
        )
        app.dependency_overrides[get_local_audit_job_service] = lambda: readback
        app.dependency_overrides[get_optional_local_audit_job_service] = lambda: readback
        restarted_status = await client.get(f"/api/v1/audits/{audit_id}")
        restarted_report = await client.get(f"/api/v1/audits/{audit_id}/report")
        assert restarted_status.json() == status.json()
        assert restarted_report.text == json_report.text


async def test_local_audit_api_cancel_and_unavailable_report_are_stable(
    tmp_path: Path,
) -> None:
    async with _local_audit_api(tmp_path, name="cancel") as (
        source_root,
        _database,
        _service,
        _app,
        client,
    ):
        project = source_root / "project"
        project.mkdir()
        (project / "safe.py").write_text("answer = 42\n", encoding="utf-8")
        invalid = await client.post(
            "/api/v1/audits",
            json={"source_path": "relative/project"},
        )
        created = await client.post(
            "/api/v1/audits",
            json={"source_path": str(project)},
        )
        audit_id = created.json()["audit_id"]

        unavailable = await client.get(f"/api/v1/audits/{audit_id}/report")
        cancelled = await client.post(f"/api/v1/audits/{audit_id}/cancel")
        started = await client.post(f"/api/v1/audits/{audit_id}/start")

        assert invalid.status_code == 422
        assert invalid.json()["error"]["code"] == "audit_source_absolute_path_invalid"
        assert unavailable.status_code == 409
        assert unavailable.json()["error"]["code"] == "local_audit_report_unavailable"
        assert cancelled.status_code == started.status_code == 200
        assert cancelled.json()["status"] == started.json()["status"] == "cancelled"
        findings = await client.get(f"/api/v1/audits/{audit_id}/findings")
        assert findings.json()["items"] == []
        assert findings.json()["total"] == 0
