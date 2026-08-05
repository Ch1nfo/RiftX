from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

from riftx.domain import Engagement, Objective, Run
from riftx.persistence import (
    Database,
    SQLAlchemyAgentSessionRepository,
    SQLAlchemyCapabilitySelectionStore,
    SQLAlchemyEngagementRepository,
    SQLAlchemyRunRepository,
)
from riftx.runtime.types import AgentSession
from riftx.tools import (
    ToolContextManager,
    ToolNotFoundError,
    ToolRegistry,
    ToolSearchRequest,
    ToolUnavailableError,
)


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


def _write_tools(path: Path, *, description: str) -> None:
    path.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "tools": {
                    "scanner": {
                        "command": [sys.executable, "--safe-prefix"],
                        "description": description,
                    },
                    "blocked": {"command": [sys.executable]},
                },
            },
            sort_keys=False,
        )
    )


async def _context(database: Database, config: Path) -> ToolContextManager:
    registry = ToolRegistry(config, node_id="node-1")
    await registry.refresh()
    return ToolContextManager(
        registry,
        store=SQLAlchemyCapabilitySelectionStore(database.session_factory),
    )


async def test_tool_selection_survives_restart_and_requires_explicit_reload(
    tmp_path: Path,
) -> None:
    config = tmp_path / "tools.yaml"
    _write_tools(config, description="Original scanner")
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'riftx.db'}"
    database = Database(database_url)
    await database.create_schema()
    await _build_runtime(database)
    context = await _context(database, config)

    selected = await context.get_tool(
        "scanner",
        run_id="run-1",
        session_id="session-primary",
        agent_id="primary",
    )
    assert selected.stale is False
    assert selected.full_schema["description"] == "Original scanner"
    await database.dispose()

    reopened = Database(database_url)
    restarted = await _context(reopened, config)
    recovered = await restarted.visibility(
        run_id="run-1",
        session_id="session-primary",
        agent_id="primary",
    )
    assert recovered.dynamically_loaded_tools == ["scanner"]
    assert recovered.loaded_tools[0].digest == selected.digest

    _write_tools(config, description="Updated scanner")
    await restarted.registry.reload_if_changed()
    stale = await restarted.visibility(
        run_id="run-1",
        session_id="session-primary",
        agent_id="primary",
    )
    assert stale.dynamically_loaded_tools == []
    assert stale.stale_loaded_tools == ["scanner"]
    with pytest.raises(ToolUnavailableError, match="explicit reload"):
        await restarted.assert_selected(
            "scanner",
            run_id="run-1",
            session_id="session-primary",
            agent_id="primary",
        )

    reloaded = await restarted.reload_tool(
        "scanner",
        run_id="run-1",
        session_id="session-primary",
        agent_id="primary",
    )
    assert reloaded.digest != selected.digest
    assert reloaded.full_schema["description"] == "Updated scanner"
    await restarted.unload_tool(
        "scanner",
        run_id="run-1",
        session_id="session-primary",
        agent_id="primary",
    )
    await reopened.dispose()

    reopened_again = Database(database_url)
    inactive = await (await _context(reopened_again, config)).visibility(
        run_id="run-1",
        session_id="session-primary",
        agent_id="primary",
    )
    assert inactive.dynamically_loaded_tools == []
    assert inactive.loaded_tools == []
    assert "scanner" in inactive.hidden_available_tools
    await reopened_again.dispose()


async def test_tool_allowlist_is_durable_and_fails_closed_after_restart(
    tmp_path: Path,
) -> None:
    config = tmp_path / "tools.yaml"
    _write_tools(config, description="Scanner")
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'riftx.db'}"
    database = Database(database_url)
    await database.create_schema()
    await _build_runtime(database)
    context = await _context(database, config)
    await context.restrict_tools(
        ["search_tools", "get_tool", "scanner"],
        run_id="run-1",
        session_id="session-child",
        agent_id="subagent:recon",
    )
    await context.load_tool(
        "scanner",
        run_id="run-1",
        session_id="session-child",
        agent_id="subagent:recon",
    )
    await database.dispose()

    reopened = Database(database_url)
    restarted = await _context(reopened, config)
    results = await restarted.search_tools(
        run_id="run-1",
        session_id="session-child",
        agent_id="subagent:recon",
        request=ToolSearchRequest(query=""),
    )
    assert [item.tool.id for item in results] == ["scanner"]
    visibility = await restarted.visibility(
        run_id="run-1",
        session_id="session-child",
        agent_id="subagent:recon",
    )
    assert visibility.dynamically_loaded_tools == ["scanner"]
    with pytest.raises(ToolNotFoundError):
        await restarted.load_tool(
            "blocked",
            run_id="run-1",
            session_id="session-child",
            agent_id="subagent:recon",
        )
    await reopened.dispose()
