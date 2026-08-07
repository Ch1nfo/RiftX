from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

import riftx.cli.app as cli_module
from riftx.application.errors import ApplicationConflictError
from riftx.application.services import (
    PentestCapabilityResolver,
    PentestCapabilitySelection,
)
from riftx.capabilities import CapabilityVersionStatus
from riftx.capability_management import (
    CapabilityManagementError,
    activate_operator_skill,
    disable_operator_skill,
    inspect_operator_skills,
    register_operator_skill,
    rollback_operator_skill,
    validate_operator_skills,
)
from riftx.config import RiftXConfig
from riftx.database_maintenance import repair_sqlite_database
from riftx.domain import ApprovalLevel
from riftx.packs import OfficialPackCatalog, bootstrap_official_packs
from riftx.persistence import Database, SQLAlchemyCapabilityRepository
from riftx.skills import create_default_skill_registry
from riftx.tools import ToolRegistry

_SKILL_ID = "minimal-service-verification"
_CLI_RUNNER = CliRunner()


def _config(tmp_path: Path) -> RiftXConfig:
    return RiftXConfig.model_validate(
        {
            "database": {
                "url": f"sqlite+aiosqlite:///{tmp_path / 'riftx.db'}",
            },
            "skills": {"path": tmp_path / "skills"},
        }
    )


def _bootstrap(config: RiftXConfig, tmp_path: Path) -> None:
    repair_sqlite_database(config.database.url, cwd=tmp_path)

    async def operation() -> None:
        database = Database(config.database.url)
        try:
            await bootstrap_official_packs(
                SQLAlchemyCapabilityRepository(database.session_factory)
            )
        finally:
            await database.dispose()

    asyncio.run(operation())


def _write_skill(
    root: Path,
    *,
    version: str,
    marker: str,
) -> Path:
    directory = root / _SKILL_ID
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "SKILL.md").write_text(
        f"""---
name: Minimal Service Verification
description: Select the smallest evidence-producing verification step ({marker})
version: {version}
source: operator
required_capabilities:
  - evidence_ledger
preferred_tools:
  - operator-only-tool
approval_level: never
---

## When to use

Use after an authorized service observation needs bounded verification.

## Preconditions

The target and proposed interaction are inside the current Scope.

## Procedure

Choose one minimal verification step and preserve its raw result.

## Decision points

Do not broaden the interaction when the first result is conclusive.

## Stop conditions

Stop after evidence, a conclusive negative result, or any safety gate.

## Expected output

Return evidence references and the remaining uncertainty.

## Error handling

Record execution failures without converting them into target negatives.
"""
    )
    (directory / "REFERENCES.md").write_text(f"method-version:{marker}\n")
    return directory


def test_operator_skill_lifecycle_is_immutable_explicit_and_source_bound(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    _bootstrap(config, tmp_path)
    root = config.skills.path
    directory = _write_skill(root, version="1.0.0", marker="v1")

    assert validate_operator_skills(config, _SKILL_ID)[0].version == "1.0.0"
    registered_v1 = register_operator_skill(config, _SKILL_ID)
    assert registered_v1.status is CapabilityVersionStatus.APPROVED
    assert registered_v1.manifest.permission.approval_level is ApprovalLevel.ALWAYS
    assert registered_v1.manifest.permission.requires_scope is True
    assert registered_v1.manifest.permission.credential_references == ()
    assert register_operator_skill(config, _SKILL_ID) == registered_v1

    _write_skill(root, version="1.0.0", marker="drifted")
    with pytest.raises(CapabilityManagementError, match="increase the Skill version"):
        register_operator_skill(config, _SKILL_ID)

    _write_skill(root, version="1.0.0", marker="v1")
    active_v1 = activate_operator_skill(config, _SKILL_ID, "1.0.0")
    assert active_v1.status is CapabilityVersionStatus.ACTIVE

    _write_skill(root, version="2.0.0", marker="v2")
    registered_v2 = register_operator_skill(config, _SKILL_ID)
    assert registered_v2.status is CapabilityVersionStatus.APPROVED
    with pytest.raises(CapabilityManagementError, match="disable it before activating"):
        activate_operator_skill(config, _SKILL_ID, "2.0.0")

    assert disable_operator_skill(config, _SKILL_ID, "1.0.0").status is (
        CapabilityVersionStatus.DISABLED
    )
    assert activate_operator_skill(config, _SKILL_ID, "2.0.0").status is (
        CapabilityVersionStatus.ACTIVE
    )

    shutil.rmtree(directory)
    inventory = inspect_operator_skills(config, _SKILL_ID)
    assert {(item.version, item.capability_status, item.source_status) for item in inventory} == {
        ("1.0.0", "disabled", "missing"),
        ("2.0.0", "active", "missing"),
    }
    assert disable_operator_skill(config, _SKILL_ID).status is (
        CapabilityVersionStatus.DISABLED
    )
    assert {item.version for item in inspect_operator_skills(config)} == {
        "1.0.0",
        "2.0.0",
    }
    with pytest.raises(CapabilityManagementError, match="was not found"):
        rollback_operator_skill(config, _SKILL_ID, "1.0.0")

    _write_skill(root, version="1.0.0", marker="v1")
    rolled_back = rollback_operator_skill(config, _SKILL_ID, "1.0.0")
    assert rolled_back.version_id == registered_v1.version_id
    assert rolled_back.status is CapabilityVersionStatus.ACTIVE


def test_pentest_requires_active_operator_skill_and_preserves_the_snapshot(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    _bootstrap(config, tmp_path)
    directory = _write_skill(config.skills.path, version="1.0.0", marker="original")
    register_operator_skill(config, _SKILL_ID)
    catalog = OfficialPackCatalog()
    tools_path = tmp_path / "tools.yaml"
    tools_path.write_text("version: 1\nexecution_policy: registered_only\ntools: {}\n")

    async def resolve():
        tools = ToolRegistry(tools_path, node_id="local")
        await tools.refresh()
        database = Database(config.database.url)
        try:
            resolver = PentestCapabilityResolver(
                tools=tools,
                skills=create_default_skill_registry(
                    config.skills.path,
                    official_skill_roots=catalog.skill_roots(),
                ),
                capabilities=SQLAlchemyCapabilityRepository(database.session_factory),
                packs=catalog,
            )
            return await resolver.resolve(
                PentestCapabilitySelection(skill_ids=(_SKILL_ID,)),
                run_id="00000000-0000-4000-8000-000000000101",
                session_id="00000000-0000-4000-8000-000000000101:primary",
                selected_at=registered_at,
            )
        finally:
            await database.dispose()

    registered_at = register_operator_skill(config, _SKILL_ID).created_at
    with pytest.raises(ApplicationConflictError, match="is not active"):
        asyncio.run(resolve())

    activate_operator_skill(config, _SKILL_ID, "1.0.0")
    resolved = asyncio.run(resolve())
    selection = next(
        item for item in resolved.selections if item.capability_id == _SKILL_ID
    )
    assert selection.version == "1.0.0"
    assert selection.snapshot["document"]["description"].endswith("(original)")
    assert "operator-only-tool" not in resolved.tool_allowlist

    _write_skill(config.skills.path, version="1.0.0", marker="drifted")
    with pytest.raises(ApplicationConflictError, match="does not match its active version"):
        asyncio.run(resolve())

    shutil.rmtree(directory)
    with pytest.raises(ApplicationConflictError, match="unavailable"):
        asyncio.run(resolve())
    assert selection.snapshot["document"]["description"].endswith("(original)")


def test_operator_skill_cli_exposes_the_complete_local_lifecycle(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _bootstrap(config, tmp_path)
    _write_skill(config.skills.path, version="1.0.0", marker="cli")
    config_path = tmp_path / "riftx.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "database": {"url": config.database.url},
                "skills": {"path": str(config.skills.path)},
            }
        )
    )

    commands = (
        (["skills", "validate", _SKILL_ID], "Validated 1 Operator Skill"),
        (["skills", "register", _SKILL_ID], "as approved"),
        (["skills", "activate", _SKILL_ID, "1.0.0"], "Activated"),
        (["skills", "list", _SKILL_ID], "active"),
        (["skills", "disable", _SKILL_ID], "existing Run snapshots"),
        (["skills", "rollback", _SKILL_ID, "1.0.0"], "Rolled back"),
    )
    for arguments, expected in commands:
        result = _CLI_RUNNER.invoke(
            cli_module.app,
            ["--config", str(config_path), *arguments],
        )
        assert result.exit_code == 0, result.output
        assert expected in result.output
