from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from riftx.api.runtime import APISettings
from riftx.config import AuditConfig, RiftXConfig, RiftXConfigError, load_riftx_config


def _load(
    tmp_path: Path,
    *,
    payload: dict[str, object] | None = None,
    environment: dict[str, str] | None = None,
    cli_overrides: dict[str, object] | None = None,
) -> RiftXConfig:
    config_path = tmp_path / "riftx.yaml"
    if payload is not None:
        config_path.write_text(yaml.safe_dump(payload), encoding="utf-8")
    return load_riftx_config(
        system_path=tmp_path / "missing-system.yaml",
        user_path=tmp_path / "missing-user.yaml",
        explicit_path=config_path if payload is not None else None,
        environment=environment or {},
        cli_overrides=cli_overrides,
    )


def test_audit_config_keeps_only_historical_snapshot_compatibility() -> None:
    audit = AuditConfig()

    assert set(AuditConfig.model_fields) == {
        "enabled",
        "snapshot_root",
        "max_repository_bytes",
        "max_file_bytes",
    }
    assert audit.enabled is False
    assert audit.snapshot_root == Path("/var/lib/riftx/audit/snapshots")
    assert audit.max_repository_bytes == 2_147_483_648
    assert audit.max_file_bytes == 5_242_880


def test_legacy_audit_yaml_is_ignored_except_snapshot_compatibility(
    tmp_path: Path,
) -> None:
    snapshot_root = tmp_path / "history" / "snapshots"
    config = _load(
        tmp_path,
        payload={
            "audit": {
                "enabled": True,
                "snapshot_root": str(snapshot_root),
                "max_repository_bytes": 1_000,
                "max_file_bytes": 100,
                "source_roots": [str(tmp_path)],
                "default_mode": "deep",
                "model_egress": {"default_mode": "remote_redacted"},
                "budget": {"max_model_calls": 1},
                "source_ingest": {"runtime": "docker"},
                "validation": {"default_policy": "isolated_build"},
            }
        },
        environment={
            "RIFTX_TRUST_PROFILE": "local_single_operator",
            "RIFTX_ADMIN_TOKEN": "test-only-audit-compat-admin-token-0001",
        },
    )
    settings = APISettings.from_config(config)

    assert config.audit == AuditConfig(
        enabled=True,
        snapshot_root=snapshot_root,
        max_repository_bytes=1_000,
        max_file_bytes=100,
    )
    assert settings.audit == config.audit
    assert settings.audit is not config.audit
    assert not hasattr(config.audit, "source_roots")
    assert not hasattr(config.audit, "source_ingest")


def test_retired_audit_environment_variables_are_ignored(tmp_path: Path) -> None:
    snapshot_root = tmp_path / "snapshots"
    config = _load(
        tmp_path,
        environment={
            "RIFTX_AUDIT_ENABLED": "true",
            "RIFTX_AUDIT_SNAPSHOT_ROOT": str(snapshot_root),
            "RIFTX_AUDIT_MAX_REPOSITORY_BYTES": "1000",
            "RIFTX_AUDIT_MAX_FILE_BYTES": "100",
            "RIFTX_AUDIT_DEFAULT_MODE": "deep",
            "RIFTX_AUDIT_REGISTRY_TOKEN": "retired-canary",
        },
    )

    assert config.audit == AuditConfig(
        enabled=True,
        snapshot_root=snapshot_root,
        max_repository_bytes=1_000,
        max_file_bytes=100,
    )


def test_snapshot_compatibility_limits_remain_fail_closed(tmp_path: Path) -> None:
    with pytest.raises(RiftXConfigError, match="max_file_bytes"):
        _load(
            tmp_path,
            payload={
                "audit": {
                    "snapshot_root": str(tmp_path / "snapshots"),
                    "max_repository_bytes": 100,
                    "max_file_bytes": 101,
                }
            },
        )

    with pytest.raises(RiftXConfigError, match="must be an absolute path"):
        _load(tmp_path, payload={"audit": {"snapshot_root": "relative/snapshots"}})


def test_audit_compatibility_cannot_be_changed_by_cli_override(tmp_path: Path) -> None:
    with pytest.raises(RiftXConfigError, match="cannot be changed through CLI overrides"):
        _load(tmp_path, cli_overrides={"audit": {"enabled": True}})
