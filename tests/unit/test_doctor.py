"""Offline Doctor contract tests."""

from __future__ import annotations

from pathlib import Path

import yaml

from riftx.config import RiftXConfig
from riftx.doctor import DOCTOR_CHECK_IDS, DoctorStatus, run_local_doctor
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
