"""Offline Doctor contract tests."""

from __future__ import annotations

from pathlib import Path

import yaml

from riftx.config import RiftXConfig
from riftx.doctor import (
    DOCTOR_CHECK_IDS,
    DoctorStatus,
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


def test_doctor_fails_when_operator_skill_is_invalid(tmp_path: Path) -> None:
    config = _write_runtime_configs(tmp_path)
    skill_root = config.skills.path / "broken"
    skill_root.mkdir(parents=True)
    (skill_root / "SKILL.md").write_text("not valid front matter\n", encoding="utf-8")

    report = run_local_doctor(config, environment={}, cwd=tmp_path)

    assert report.by_id("skills").status is DoctorStatus.FAILED
    assert report.failed


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
