from __future__ import annotations

from pathlib import Path

import pytest

from riftx.application.errors import RepositoryConflictError
from riftx.capabilities import (
    CapabilityKind,
    CapabilitySource,
    SessionCapabilitySelection,
)
from riftx.context import ContextCompiler
from riftx.domain import Engagement, Objective, Run
from riftx.domain.base import utc_now
from riftx.persistence import (
    Database,
    SQLAlchemyAgentSessionRepository,
    SQLAlchemyCapabilitySelectionStore,
    SQLAlchemyEngagementRepository,
    SQLAlchemyRunRepository,
    SQLAlchemySkillSelectionStore,
)
from riftx.runtime.lifecycle import ContextCompileRequest
from riftx.runtime.types import AgentSession
from riftx.skills import ProgressiveSkillContextManager, SkillRegistry

_BODY = """
## When to use
Use for an authorized candidate.

## Preconditions
The target is in scope.

## Procedure
Collect bounded evidence.

## Decision points
Choose the least invasive verification.

## Stop conditions
Stop after proof or a conclusive negative result.

## Expected output
Return evidence references.

## Error handling
Preserve the error and raw artifacts.
"""


def _write_skill(root: Path, skill_id: str, *, marker: str) -> Path:
    directory = root / skill_id
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "SKILL.md").write_text(
        f"""---
name: {skill_id}
description: {marker}
version: 1.0.0
source: operator
required_capabilities:
  - http_request
---
{_BODY}
"""
    )
    (directory / "REFERENCES.md").write_text(f"reference:{marker}")
    return directory


async def _build_runtime(database: Database) -> None:
    await SQLAlchemyEngagementRepository(database.session_factory).create(
        Engagement(id="engagement-1", name="Authorized")
    )
    await SQLAlchemyRunRepository(database.session_factory).create(
        Run(
            kind="general",
            id="run-1",
            engagement_id="engagement-1",
            node_id="node-1",
            objective=Objective(description="Assess the authorized target"),
            workspace_path="/tmp/riftx/run-1",
        )
    )
    sessions = SQLAlchemyAgentSessionRepository(database.session_factory)
    await sessions.create(
        AgentSession(id="session-primary", run_id="run-1", model_profile="default")
    )
    await sessions.create(
        AgentSession(
            id="session-child",
            run_id="run-1",
            parent_session_id="session-primary",
            agent_type="subagent:recon",
            model_profile="default",
        )
    )


async def test_skill_selection_survives_restart_and_pins_original_package(
    tmp_path: Path,
) -> None:
    skill_root = tmp_path / "skills"
    directory = _write_skill(skill_root, "ssrf-validation", marker="original")
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'riftx.db'}"
    database = Database(database_url)
    await database.create_schema()
    await _build_runtime(database)
    context = ProgressiveSkillContextManager(
        SkillRegistry(skill_root),
        SQLAlchemySkillSelectionStore(database.session_factory),
    )

    selected = await context.select_skill(
        "ssrf-validation",
        run_id="run-1",
        session_id="session-primary",
        agent_id="primary",
        reason="validate the SSRF hypothesis",
    )
    await context.load_references(
        "ssrf-validation",
        run_id="run-1",
        session_id="session-primary",
        agent_id="primary",
    )
    original_digest = selected.digest
    await database.dispose()

    path = directory / "SKILL.md"
    path.write_text(path.read_text().replace("description: original", "description: updated"))
    reopened = Database(database_url)
    await reopened.create_schema()
    restarted = ProgressiveSkillContextManager(
        SkillRegistry(skill_root),
        SQLAlchemySkillSelectionStore(reopened.session_factory),
    )
    visibility = await restarted.visibility(
        run_id="run-1",
        session_id="session-primary",
        agent_id="primary",
    )

    assert visibility.loaded_skill_documents[0].description == "original"
    assert visibility.loaded_skill_documents[0].digest == original_digest
    assert visibility.loaded_skill_references[0].content == "reference:original"
    assert visibility.loaded_skills[0].reason == "validate the SSRF hypothesis"
    assert visibility.loaded_skills[0].stale is True
    assert visibility.available_skills[0].digest != original_digest
    compiled = await ContextCompiler(skill_context=restarted).compile(
        ContextCompileRequest(
            run_id="run-1",
            session_id="session-primary",
            agent_id="primary",
            model_profile="default",
        )
    )
    assert compiled.loaded_skill_documents[0]["description"] == "original"
    assert compiled.context_manifest["loaded_skills"] == [
        {
            "id": "ssrf-validation",
            "version": "1.0.0",
            "digest": original_digest,
            "source": "operator",
            "reason": "validate the SSRF hypothesis",
            "references_loaded": True,
            "stale": True,
        }
    ]
    await reopened.dispose()


async def test_skill_selection_isolated_and_subagent_allowlist_fails_closed(
    tmp_path: Path,
) -> None:
    skill_root = tmp_path / "skills"
    _write_skill(skill_root, "allowed-skill", marker="allowed")
    _write_skill(skill_root, "blocked-skill", marker="blocked")
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'riftx.db'}")
    await database.create_schema()
    await _build_runtime(database)
    context = ProgressiveSkillContextManager(
        SkillRegistry(skill_root),
        SQLAlchemySkillSelectionStore(database.session_factory),
    )
    await context.restrict_skills(
        ["allowed-skill"],
        run_id="run-1",
        session_id="session-child",
        agent_id="subagent:recon",
    )

    assert [
        item.id
        for item in await context.list_skills(session_id="session-child")
    ] == ["allowed-skill"]
    with pytest.raises(PermissionError, match="outside the Session allowlist"):
        await context.select_skill(
            "blocked-skill",
            run_id="run-1",
            session_id="session-child",
            agent_id="subagent:recon",
            reason="not delegated",
        )
    await context.select_skill(
        "allowed-skill",
        run_id="run-1",
        session_id="session-child",
        agent_id="subagent:recon",
        reason="delegated reconnaissance",
    )
    primary = await context.visibility(
        run_id="run-1",
        session_id="session-primary",
        agent_id="primary",
    )
    child = await context.visibility(
        run_id="run-1",
        session_id="session-child",
        agent_id="subagent:recon",
    )
    assert primary.loaded_skill_documents == []
    assert [document.id for document in child.loaded_skill_documents] == ["allowed-skill"]
    await database.dispose()


async def test_unified_capability_selections_pin_versions_and_survive_restart(
    tmp_path: Path,
) -> None:
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'riftx.db'}"
    database = Database(database_url)
    await database.create_schema()
    await _build_runtime(database)
    store = SQLAlchemyCapabilitySelectionStore(database.session_factory)
    now = utc_now()
    tool = SessionCapabilitySelection(
        run_id="run-1",
        session_id="session-primary",
        agent_id="primary",
        kind=CapabilityKind.TOOL,
        capability_id="shared-id",
        version="tool 1.0",
        digest="a" * 64,
        source=CapabilitySource.OPERATOR,
        reason="selected for validation",
        snapshot={"schema": {"type": "object"}},
        selected_at=now,
        updated_at=now,
    )
    technique = tool.model_copy(
        update={
            "kind": CapabilityKind.TECHNIQUE,
            "version": "1.0.0",
            "digest": "b" * 64,
            "snapshot": {"manifest": {"title": "Validate"}},
        }
    )
    await store.save_selection(tool)
    await store.save_selection(technique)

    with pytest.raises(RepositoryConflictError, match="cannot replace"):
        await store.save_selection(tool.model_copy(update={"digest": "c" * 64}))
    with pytest.raises(RepositoryConflictError, match="digest no longer matches"):
        await store.replace_selection(
            tool.model_copy(update={"digest": "c" * 64, "updated_at": utc_now()}),
            expected_digest="d" * 64,
        )

    replacement = tool.model_copy(
        update={
            "version": "tool 2.0",
            "digest": "c" * 64,
            "snapshot": {"schema": {"type": "string"}},
            "updated_at": utc_now(),
        }
    )
    await store.replace_selection(replacement, expected_digest=tool.digest)
    await database.dispose()

    reopened = Database(database_url)
    restarted = SQLAlchemyCapabilitySelectionStore(reopened.session_factory)
    loaded = await restarted.list_selections("session-primary")
    by_kind = {item.kind: item for item in loaded}
    assert set(by_kind) == {CapabilityKind.TOOL, CapabilityKind.TECHNIQUE}
    assert by_kind[CapabilityKind.TOOL].digest == replacement.digest
    assert by_kind[CapabilityKind.TOOL].snapshot == replacement.snapshot
    assert by_kind[CapabilityKind.TECHNIQUE] == technique
    await reopened.dispose()


async def test_explicit_skill_reload_replaces_stale_snapshot(tmp_path: Path) -> None:
    skill_root = tmp_path / "skills"
    directory = _write_skill(skill_root, "reloadable", marker="original")
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'riftx.db'}")
    await database.create_schema()
    await _build_runtime(database)
    context = ProgressiveSkillContextManager(
        SkillRegistry(skill_root),
        SQLAlchemySkillSelectionStore(database.session_factory),
    )
    original = await context.select_skill(
        "reloadable",
        run_id="run-1",
        session_id="session-primary",
        agent_id="primary",
        reason="initial",
    )
    path = directory / "SKILL.md"
    path.write_text(path.read_text().replace("description: original", "description: updated"))

    reloaded = await context.reload_skill(
        "reloadable",
        run_id="run-1",
        session_id="session-primary",
        agent_id="primary",
        reason="operator requested refresh",
    )
    visibility = await context.visibility(
        run_id="run-1",
        session_id="session-primary",
        agent_id="primary",
    )

    assert reloaded.description == "updated"
    assert reloaded.digest != original.digest
    assert visibility.loaded_skill_documents == [reloaded]
    assert visibility.loaded_skill_references == []
    assert visibility.loaded_skills[0].reason == "operator requested refresh"
    assert visibility.loaded_skills[0].stale is False
    await database.dispose()
