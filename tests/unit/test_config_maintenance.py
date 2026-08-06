"""Safe runtime configuration migration tests."""

from __future__ import annotations

import stat
from pathlib import Path

import pytest
import yaml

import riftx.config_maintenance as maintenance
from riftx.config_maintenance import (
    RuntimeConfigMigrationError,
    RuntimeConfigMigrationStatus,
    inspect_runtime_config_migration,
    repair_runtime_config,
)

_LEGACY_CONFIG = """\
# operator comment
server:
  port: 9000
audit:
  enabled: false
  # Real repository Preflight remains unavailable until an operator supplies
  # a reviewed, immutable image digest for the local Linux SourceIngest worker.
  source_ingest:
    backend_id: linux_container
    runtime: docker
    image_digest: null
    policy_version: riftx.audit-source-ingest-policy/v1
    max_wall_seconds: 120
    max_memory_mib: 512
    max_pids: 32
    max_result_bytes: 262144
    max_output_bytes: 1048576
    lease_seconds: 120
    job_ttl_seconds: 3600
  validation:
    default_policy: static_only
"""


def test_runtime_config_inspection_only_marks_exact_legacy_defaults_fixable(
    tmp_path: Path,
) -> None:
    ready = tmp_path / "ready.yaml"
    ready.write_text("server:\n  port: 9000\n", encoding="utf-8")
    legacy = tmp_path / "legacy.yaml"
    legacy.write_text(_LEGACY_CONFIG, encoding="utf-8")
    customized = tmp_path / "customized.yaml"
    customized.write_text(
        _LEGACY_CONFIG.replace("image_digest: null", f"image_digest: {'a' * 64}"),
        encoding="utf-8",
    )
    inline = tmp_path / "inline.yaml"
    legacy_payload = yaml.safe_load(_LEGACY_CONFIG)
    inline.write_text(
        yaml.safe_dump(
            {
                "audit": {
                    "source_ingest": legacy_payload["audit"]["source_ingest"],
                }
            },
            default_flow_style=True,
        ),
        encoding="utf-8",
    )

    assert inspect_runtime_config_migration(ready).status is RuntimeConfigMigrationStatus.READY
    assert (
        inspect_runtime_config_migration(legacy).status
        is RuntimeConfigMigrationStatus.MIGRATABLE
    )
    manual = inspect_runtime_config_migration(customized)
    assert manual.status is RuntimeConfigMigrationStatus.MANUAL
    assert not manual.fixable
    assert (
        inspect_runtime_config_migration(inline).status
        is RuntimeConfigMigrationStatus.MANUAL
    )


def test_runtime_config_repair_backs_up_and_preserves_unrelated_yaml(
    tmp_path: Path,
) -> None:
    path = tmp_path / "riftx.yaml"
    path.write_text(_LEGACY_CONFIG, encoding="utf-8")
    path.chmod(0o640)

    result = repair_runtime_config(path)

    migrated = path.read_text(encoding="utf-8")
    assert "# operator comment" in migrated
    assert "server:\n  port: 9000\n" in migrated
    assert "source_ingest" not in migrated
    assert "Real repository Preflight" not in migrated
    assert "validation:\n    default_policy: static_only\n" in migrated
    assert stat.S_IMODE(path.stat().st_mode) == 0o640
    assert result.backup_path.read_text(encoding="utf-8") == _LEGACY_CONFIG
    assert stat.S_IMODE(result.backup_path.stat().st_mode) == 0o600
    assert inspect_runtime_config_migration(path).status is RuntimeConfigMigrationStatus.READY


def test_runtime_config_repair_refuses_customized_or_symlinked_files(
    tmp_path: Path,
) -> None:
    customized = tmp_path / "customized.yaml"
    customized.write_text(
        _LEGACY_CONFIG.replace("max_pids: 32", "max_pids: 16"),
        encoding="utf-8",
    )
    target = tmp_path / "target.yaml"
    target.write_text(_LEGACY_CONFIG, encoding="utf-8")
    link = tmp_path / "link.yaml"
    link.symlink_to(target)

    with pytest.raises(RuntimeConfigMigrationError, match="manual"):
        repair_runtime_config(customized)
    with pytest.raises(RuntimeConfigMigrationError, match="symbolic"):
        repair_runtime_config(link)

    assert customized.read_text(encoding="utf-8").endswith("default_policy: static_only\n")
    assert target.read_text(encoding="utf-8") == _LEGACY_CONFIG


def test_runtime_config_repair_restores_original_when_verification_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "riftx.yaml"
    path.write_text(_LEGACY_CONFIG, encoding="utf-8")
    original_identity = (path.stat().st_dev, path.stat().st_ino)
    monkeypatch.setattr(
        maintenance,
        "_verify_migrated_config",
        lambda _path: (_ for _ in ()).throw(ValueError("verification failed")),
    )

    with pytest.raises(RuntimeConfigMigrationError, match="restored"):
        repair_runtime_config(path)

    assert path.read_text(encoding="utf-8") == _LEGACY_CONFIG
    assert (path.stat().st_dev, path.stat().st_ino) != original_identity


def test_runtime_config_repair_revalidates_content_after_inspection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "riftx.yaml"
    customized = _LEGACY_CONFIG.replace("max_pids: 32", "max_pids: 16")
    path.write_text(customized, encoding="utf-8")
    monkeypatch.setattr(
        maintenance,
        "inspect_runtime_config_migration",
        lambda _path: maintenance.RuntimeConfigMigrationState(
            path=path,
            status=RuntimeConfigMigrationStatus.MIGRATABLE,
            detail="stale inspection",
            fixable=True,
        ),
    )

    with pytest.raises(RuntimeConfigMigrationError, match="manual"):
        repair_runtime_config(path)

    assert path.read_text(encoding="utf-8") == customized
    assert not (tmp_path / ".riftx-backups").exists()
