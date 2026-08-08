from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from tests.integration.persistence.test_capability_repository import version

from riftx.capabilities import (
    CapabilityKind,
    CapabilityVersionStatus,
    SessionCapabilityManifestReader,
    TechniqueContextManager,
)
from riftx.context import ContextCompiler
from riftx.domain import Engagement, Objective, Run
from riftx.domain.base import utc_now
from riftx.persistence import (
    Database,
    SQLAlchemyAgentSessionRepository,
    SQLAlchemyCapabilityRepository,
    SQLAlchemyCapabilitySelectionStore,
    SQLAlchemyEngagementRepository,
    SQLAlchemyRunRepository,
)
from riftx.runtime.lifecycle import ContextCompileRequest
from riftx.runtime.types import AgentSession


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
    await SQLAlchemyAgentSessionRepository(database.session_factory).create(
        AgentSession(id="session-primary", run_id="run-1", model_profile="default")
    )


async def test_technique_selection_reload_restart_and_unified_manifest(
    tmp_path: Path,
) -> None:
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'riftx.db'}"
    database = Database(database_url)
    await database.create_schema()
    await _build_runtime(database)
    catalog = SQLAlchemyCapabilityRepository(database.session_factory)
    capability, first = version("1.0.0")
    await catalog.register_version(capability, first)
    selections = SQLAlchemyCapabilitySelectionStore(database.session_factory)
    techniques = TechniqueContextManager(catalog, selections)

    selected = await techniques.select_technique(
        capability.capability_id,
        run_id="run-1",
        session_id="session-primary",
        agent_id="primary",
        reason="analyze bounded request differences",
    )
    compiled = await ContextCompiler(
        technique_context=techniques,
        capability_manifest_reader=SessionCapabilityManifestReader(selections),
    ).compile(
        ContextCompileRequest(
            run_id="run-1",
            session_id="session-primary",
            agent_id="primary",
            model_profile="default",
        )
    )

    assert selected == first
    assert compiled.context_manifest["loaded_techniques"][0]["stale"] is False
    manifest = compiled.context_manifest["session_capability_manifest"]
    assert manifest["selections"] == [
        {
            "kind": "technique",
            "capability_id": capability.capability_id,
            "version": "1.0.0",
            "digest": first.manifest_digest,
            "source": "official",
            "reason": "analyze bounded request differences",
            "active": True,
        }
    ]

    changed_at = utc_now() + timedelta(seconds=1)
    await catalog.set_version_status(
        first.version_id,
        CapabilityVersionStatus.DISABLED,
        changed_at=changed_at,
    )
    _, second = version("2.0.0")
    await catalog.register_version(capability, second)
    stale = await techniques.visibility(
        run_id="run-1",
        session_id="session-primary",
        agent_id="primary",
    )
    assert stale.loaded_techniques[0].stale is True

    reloaded = await techniques.reload_technique(
        capability.capability_id,
        run_id="run-1",
        session_id="session-primary",
        agent_id="primary",
        reason="use the current approved procedure",
    )
    assert reloaded == second
    await database.dispose()

    reopened = Database(database_url)
    restarted = TechniqueContextManager(
        SQLAlchemyCapabilityRepository(reopened.session_factory),
        SQLAlchemyCapabilitySelectionStore(reopened.session_factory),
    )
    recovered = await restarted.visibility(
        run_id="run-1",
        session_id="session-primary",
        agent_id="primary",
    )
    assert recovered.loaded_techniques[0].version == "2.0.0"
    assert recovered.loaded_techniques[0].stale is False
    await restarted.unload_technique(
        capability.capability_id,
        run_id="run-1",
        session_id="session-primary",
        agent_id="primary",
    )
    inactive = await SessionCapabilityManifestReader(
        SQLAlchemyCapabilitySelectionStore(reopened.session_factory)
    ).read(
        run_id="run-1",
        session_id="session-primary",
        agent_id="primary",
    )
    assert inactive.selections[0].kind is CapabilityKind.TECHNIQUE
    assert inactive.selections[0].active is False
    await reopened.dispose()
