"""Offline Doctor contract tests."""

from __future__ import annotations

import sqlite3
import stat
from pathlib import Path

import pytest
import yaml

import riftx.doctor as doctor_module
from riftx.application.errors import RepositoryConflictError
from riftx.config import AuditSourceIngestConfig, RiftXConfig
from riftx.database_maintenance import DatabaseRepairResult
from riftx.doctor import (
    DOCTOR_CHECK_IDS,
    DoctorCheck,
    DoctorFixError,
    DoctorStatus,
    apply_local_doctor_fixes,
    run_live_doctor,
    run_local_doctor,
)
from riftx.packs import OfficialPackCatalog


def _write_runtime_configs(root: Path) -> RiftXConfig:
    models_path = root / "models.yaml"
    models_path.write_text(
        yaml.safe_dump(
            {
                "default_profile": "primary",
                "models": {
                    "primary": {
                        "provider": "openai_compatible",
                        "model": "local-model",
                        "base_url": "http://127.0.0.1:11434/v1",
                        "api_key_env": None,
                        "requires_api_key": False,
                    }
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    tools_path = root / "tools.yaml"
    tools_path.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "execution_policy": "registered_only",
                "tools": {},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return RiftXConfig.model_validate(
        {
            "models": {
                "path": models_path,
                "secrets_path": root / "models.json",
            },
            "tools": {"path": tools_path},
            "skills": {"path": root / "operator-skills"},
            "workspace": {"root": root / "workspaces"},
            "runner": {
                "state_path": root / "runner",
                "credential_path": root / "runner-credentials.json",
            },
            "security": {"local_principal_path": root / "principal.json"},
            "database": {"url": f"sqlite+aiosqlite:///{root / 'riftx.db'}"},
        }
    )


def test_doctor_exposes_all_required_checks_and_degrades_optional_components(
    tmp_path: Path,
) -> None:
    report = run_local_doctor(_write_runtime_configs(tmp_path), environment={}, cwd=tmp_path)

    assert tuple(check.id for check in report.checks) == DOCTOR_CHECK_IDS
    assert report.status is DoctorStatus.DEGRADED
    assert not report.failed
    assert {check.id for check in report.checks if check.status is DoctorStatus.DEGRADED} >= {
        "temporal",
        "runner",
        "browser",
        "tools",
        "skills",
        "mcp",
        "lsp",
        "scanner",
        "storage_permissions",
        "pack_integrity",
        "database_migrations",
        "backup_restore",
    }
    assert all(
        check.remediation
        for check in report.checks
        if check.status is not DoctorStatus.READY
    )


def test_doctor_reports_missing_browser_extra_as_optional_degraded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(doctor_module.importlib.util, "find_spec", lambda name: None)

    browser = run_local_doctor(
        _write_runtime_configs(tmp_path), environment={}, cwd=tmp_path
    ).by_id("browser")

    assert browser.status is DoctorStatus.DEGRADED
    assert "optional Playwright" in browser.detail
    assert "riftx[browser]" in browser.remediation
    assert "playwright install chromium" in browser.remediation


def test_doctor_fails_when_enabled_lsp_socket_or_credential_is_missing(
    tmp_path: Path,
) -> None:
    raw = _write_runtime_configs(tmp_path).model_dump(mode="python")
    raw["code"] = {
        "lsp": {
            "enabled": True,
            "socket_path": tmp_path / "missing.sock",
            "backend_id": "clangd",
            "backend_version": "18.1",
            "token_env": "RIFTX_LSP_TOKEN",
        }
    }
    config = RiftXConfig.model_validate(raw)

    report = run_local_doctor(config, environment={}, cwd=tmp_path)
    lsp = report.by_id("lsp")

    assert lsp.status is DoctorStatus.FAILED
    assert report.status is DoctorStatus.FAILED
    assert report.failed


def test_doctor_reports_real_sqlite_backup_restore_readiness(tmp_path: Path) -> None:
    config = _write_runtime_configs(tmp_path)
    doctor_module.repair_sqlite_database(config.database.url, cwd=tmp_path)

    report = run_local_doctor(config, environment={}, cwd=tmp_path)

    backup_restore = report.by_id("backup_restore")
    assert backup_restore.status is DoctorStatus.READY
    assert "receipt-bound restore" in backup_restore.detail
    assert not (tmp_path / "backups").exists()


def test_doctor_fails_when_operator_skill_is_invalid(tmp_path: Path) -> None:
    config = _write_runtime_configs(tmp_path)
    skill_root = config.skills.path / "broken"
    skill_root.mkdir(parents=True)
    (skill_root / "SKILL.md").write_text("not valid front matter\n", encoding="utf-8")

    report = run_local_doctor(config, environment={}, cwd=tmp_path)

    assert report.by_id("skills").status is DoctorStatus.FAILED
    assert report.failed


def test_doctor_rejects_operator_skill_spoofing_an_official_source(
    tmp_path: Path,
) -> None:
    config = _write_runtime_configs(tmp_path)
    skill_root = config.skills.path / "spoofed"
    skill_root.mkdir(parents=True)
    (skill_root / "SKILL.md").write_text(
        """---
name: Spoofed Skill
description: Must not enter the operator root as official content
version: 1.0.0
source: official
---

## When to use
Never.
## Preconditions
None.
## Procedure
Stop.
## Decision points
Stop.
## Stop conditions
Always.
## Expected output
No output.
## Error handling
Fail closed.
""",
        encoding="utf-8",
    )

    report = run_local_doctor(config, environment={}, cwd=tmp_path)

    assert report.by_id("skills").status is DoctorStatus.FAILED
    assert "source=operator" in report.by_id("skills").detail


def test_doctor_fails_when_official_pack_catalog_is_unavailable(tmp_path: Path) -> None:
    report = run_local_doctor(
        _write_runtime_configs(tmp_path),
        environment={},
        cwd=tmp_path,
        official_pack_catalog=OfficialPackCatalog(tmp_path / "missing-packs"),
    )

    assert report.by_id("pack_integrity").status is DoctorStatus.FAILED
    assert report.by_id("skills").status is DoctorStatus.FAILED
    assert report.failed


def test_doctor_fix_creates_supported_directories_and_repairs_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _write_runtime_configs(tmp_path)
    report = run_local_doctor(config, environment={}, cwd=tmp_path)
    database_path = tmp_path / "riftx.db"
    monkeypatch.setattr(
        doctor_module,
        "repair_sqlite_database",
        lambda *_args, **_kwargs: DatabaseRepairResult(
            path=database_path,
            backup_path=None,
            previous_revisions=(),
        ),
    )
    monkeypatch.setattr(
        doctor_module,
        "_repair_official_pack_persistence",
        lambda *_args, **_kwargs: doctor_module.PackPersistenceRepairResult(
            path=database_path,
            backup_path=tmp_path / "pack-backup.db",
        ),
    )

    fixes = apply_local_doctor_fixes(config, report, cwd=tmp_path)

    assert {(fix.check_id, fix.path) for fix in fixes} == {
        ("skills", config.skills.path),
        ("storage_permissions", config.workspace.root),
        ("database_migrations", database_path),
        ("pack_integrity", database_path),
    }
    for fix in fixes[:2]:
        assert fix.path.is_dir()
        assert stat.S_IMODE(fix.path.stat().st_mode) == 0o700


def test_doctor_repairs_exact_legacy_runtime_config_and_rechecks_ready(
    tmp_path: Path,
) -> None:
    config = _write_runtime_configs(tmp_path)
    runtime_config_path = tmp_path / "riftx.yaml"
    runtime_config_path.write_text(
        yaml.safe_dump(
            {
                "server": {"port": 9000},
                "audit": {
                    "source_ingest": AuditSourceIngestConfig().model_dump(mode="json"),
                    "validation": {"default_policy": "static_only"},
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    before = run_local_doctor(
        config,
        environment={},
        cwd=tmp_path,
        runtime_config_path=runtime_config_path,
    )
    migration = before.by_id("config_migrations")

    assert migration.status is DoctorStatus.DEGRADED
    assert migration.fixable

    fixes = apply_local_doctor_fixes(
        config,
        doctor_module.DoctorReport(checks=(migration,)),
        cwd=tmp_path,
        runtime_config_path=runtime_config_path,
    )
    after = run_local_doctor(
        config,
        environment={},
        cwd=tmp_path,
        runtime_config_path=runtime_config_path,
    )

    assert len(fixes) == 1
    assert fixes[0].check_id == "config_migrations"
    assert fixes[0].path == runtime_config_path
    assert fixes[0].backup_path is not None
    assert after.by_id("config_migrations").status is DoctorStatus.READY


def test_doctor_requires_manual_review_for_customized_legacy_config(
    tmp_path: Path,
) -> None:
    runtime_config_path = tmp_path / "riftx.yaml"
    source_ingest = AuditSourceIngestConfig().model_dump(mode="json")
    source_ingest["max_pids"] = 16
    runtime_config_path.write_text(
        yaml.safe_dump({"audit": {"source_ingest": source_ingest}}, sort_keys=False),
        encoding="utf-8",
    )

    report = run_local_doctor(
        _write_runtime_configs(tmp_path),
        environment={},
        cwd=tmp_path,
        runtime_config_path=runtime_config_path,
    )
    migration = report.by_id("config_migrations")

    assert migration.status is DoctorStatus.FAILED
    assert not migration.fixable
    assert "manual" in migration.detail


def test_doctor_fix_refuses_persistence_repair_while_control_plane_is_reachable(
    tmp_path: Path,
) -> None:
    config = _write_runtime_configs(tmp_path)
    report = run_local_doctor(config, environment={}, cwd=tmp_path)

    with pytest.raises(DoctorFixError, match="Control Plane"):
        apply_local_doctor_fixes(
            config,
            report,
            cwd=tmp_path,
            allow_persistence_fix=False,
        )

    assert not config.skills.path.exists()
    assert not config.workspace.root.exists()


def test_doctor_repairs_official_pack_persistence_and_rechecks_ready(
    tmp_path: Path,
) -> None:
    config = _write_runtime_configs(tmp_path)
    doctor_module.repair_sqlite_database(config.database.url, cwd=tmp_path)

    before = run_local_doctor(config, environment={}, cwd=tmp_path)

    assert before.by_id("database_migrations").status is DoctorStatus.READY
    assert before.by_id("pack_integrity").status is DoctorStatus.FAILED
    assert before.by_id("pack_integrity").fixable

    fixes = apply_local_doctor_fixes(config, before, cwd=tmp_path)
    after = run_local_doctor(config, environment={}, cwd=tmp_path)

    pack_fix = next(fix for fix in fixes if fix.check_id == "pack_integrity")
    assert pack_fix.backup_path is not None and pack_fix.backup_path.is_file()
    assert after.by_id("pack_integrity").status is DoctorStatus.READY


def test_doctor_restores_pack_database_when_reconciliation_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _write_runtime_configs(tmp_path)
    database_path = tmp_path / "riftx.db"
    doctor_module.repair_sqlite_database(config.database.url, cwd=tmp_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute("CREATE TABLE pack_marker (value TEXT NOT NULL)")
        connection.execute("INSERT INTO pack_marker VALUES ('before')")

    async def fail_reconciliation(*_args: object, **_kwargs: object) -> None:
        with sqlite3.connect(database_path) as connection:
            connection.execute("UPDATE pack_marker SET value = 'after'")
        raise RepositoryConflictError("injected pack failure")

    monkeypatch.setattr(doctor_module, "bootstrap_official_packs", fail_reconciliation)
    report = run_local_doctor(config, environment={}, cwd=tmp_path)

    with pytest.raises(DoctorFixError, match="restored"):
        apply_local_doctor_fixes(config, report, cwd=tmp_path)

    with sqlite3.connect(database_path) as restored:
        assert restored.execute("SELECT value FROM pack_marker").fetchone() == ("before",)


def test_doctor_fix_rolls_back_all_created_directories_on_failure(tmp_path: Path) -> None:
    config = _write_runtime_configs(tmp_path)
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    raw = config.model_dump(mode="python")
    raw["workspace"] = {"root": blocker / "workspaces"}
    config = RiftXConfig.model_validate(raw)
    report = doctor_module.DoctorReport(
        checks=(
            DoctorCheck(
                id="skills",
                status=DoctorStatus.DEGRADED,
                detail="missing",
                fixable=True,
            ),
            DoctorCheck(
                id="storage_permissions",
                status=DoctorStatus.DEGRADED,
                detail="missing",
                fixable=True,
            ),
        )
    )

    with pytest.raises(DoctorFixError, match="rolled back"):
        apply_local_doctor_fixes(config, report, cwd=tmp_path)

    assert not config.skills.path.exists()


def test_doctor_fix_rejects_symbolic_link_components(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "linked"
    link.symlink_to(real, target_is_directory=True)
    config = _write_runtime_configs(tmp_path)
    raw = config.model_dump(mode="python")
    raw["skills"] = {"path": link / "skills"}
    config = RiftXConfig.model_validate(raw)
    report = doctor_module.DoctorReport(
        checks=(
            DoctorCheck(
                id="skills",
                status=DoctorStatus.DEGRADED,
                detail="missing",
                fixable=True,
            ),
        )
    )

    with pytest.raises(DoctorFixError, match="symbolic links"):
        apply_local_doctor_fixes(config, report, cwd=tmp_path)

    assert not (real / "skills").exists()


class _LiveClient:
    def __init__(
        self,
        *,
        node_status: str = "online",
        tool_availability: str = "available",
    ) -> None:
        self.node_status = node_status
        self.tool_availability = tool_availability

    def health(self) -> dict[str, object]:
        return {"status": "ok"}

    def get_node(self, node_id: str) -> dict[str, object]:
        return {
            "id": node_id,
            "status": self.node_status,
            "runner_version": "3.0.0",
            "capabilities": ["browser_playwright"],
            "labels": {
                "mode": "worker-local",
                "mcp_refresh_status": "ready",
                "mcp_unavailable_server_count": "0",
                "mcp_open_circuit_count": "0",
            },
        }

    def list_tools(self, node_id: str) -> dict[str, object]:
        return {
            "node_id": node_id,
            "tools": [
                {
                    "definition": {
                        "id": "nmap",
                        "enabled": True,
                        "version_probe": {"command": ["nmap", "--version"]},
                    },
                    "state": {
                        "availability": self.tool_availability,
                        "version": "7.95" if self.tool_availability == "available" else None,
                    },
                }
            ],
        }

    def system_diagnostics(self) -> dict[str, object]:
        return {
            "database": {
                "status": "ready",
                "expected_revision": "head-1",
                "current_revisions": ["head-1"],
            },
            "official_packs": {
                "status": "ready",
                "expected_pack_count": 22,
                "installed_pack_count": 22,
                "active_lock_count": 66,
                "issues": [],
            },
        }


def test_live_doctor_promotes_proven_runtime_checks(tmp_path: Path) -> None:
    config = _write_runtime_configs(tmp_path)
    local = run_local_doctor(config, environment={}, cwd=tmp_path)

    report = run_live_doctor(config, local, _LiveClient())

    assert all(
        report.by_id(check_id).status is DoctorStatus.READY
        for check_id in (
            "temporal",
            "runner",
            "browser",
            "tools",
            "pack_integrity",
            "database_migrations",
        )
    )
    assert report.by_id("mcp").status is DoctorStatus.DEGRADED


def test_live_doctor_promotes_enabled_mcp_from_worker_health_labels(
    tmp_path: Path,
) -> None:
    raw = _write_runtime_configs(tmp_path).model_dump(mode="python")
    raw["mcp"] = {
        "servers": {
            "knowledge": {
                "url": "http://127.0.0.1:9010/mcp",
            }
        }
    }
    config = RiftXConfig.model_validate(raw)
    local = run_local_doctor(config, environment={}, cwd=tmp_path)

    report = run_live_doctor(config, local, _LiveClient())

    assert report.by_id("mcp").status is DoctorStatus.READY


def test_live_doctor_fails_closed_for_offline_runner_or_unavailable_tool(
    tmp_path: Path,
) -> None:
    config = _write_runtime_configs(tmp_path)
    local = run_local_doctor(config, environment={}, cwd=tmp_path)

    report = run_live_doctor(
        config,
        local,
        _LiveClient(node_status="offline", tool_availability="unavailable"),
    )

    assert report.by_id("runner").status is DoctorStatus.FAILED
    assert report.by_id("tools").status is DoctorStatus.FAILED
    assert report.failed


def test_live_doctor_marks_runner_failed_when_control_plane_is_unreachable(
    tmp_path: Path,
) -> None:
    class UnreachableClient(_LiveClient):
        def health(self) -> dict[str, object]:
            raise RuntimeError("connection refused")

    config = _write_runtime_configs(tmp_path)
    local = run_local_doctor(config, environment={}, cwd=tmp_path)

    report = run_live_doctor(config, local, UnreachableClient())

    assert report.by_id("runner").status is DoctorStatus.FAILED
    assert "Control Plane" in report.by_id("runner").detail
    assert report.failed
