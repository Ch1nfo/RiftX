from __future__ import annotations

from pathlib import Path

import pytest

from riftx.domain import (
    BrowserMode,
    BrowserSession,
    BrowserSessionStatus,
    Engagement,
    Execution,
    ExecutionStatus,
    ExecutorType,
    Objective,
    Run,
    TerminalSession,
    TerminalStatus,
)
from riftx.persistence import (
    Database,
    SQLAlchemyActiveTakeoverReader,
    SQLAlchemyAgentSessionRepository,
    SQLAlchemyBrowserRepository,
    SQLAlchemyEngagementRepository,
    SQLAlchemyExecutionRepository,
    SQLAlchemyRunRepository,
    SQLAlchemyTerminalRepository,
)
from riftx.runtime.types import AgentSession


async def test_active_takeovers_are_bounded_and_run_scoped(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'takeovers.db'}")
    await database.create_schema()
    engagements = SQLAlchemyEngagementRepository(database.session_factory)
    runs = SQLAlchemyRunRepository(database.session_factory)
    sessions = SQLAlchemyAgentSessionRepository(database.session_factory)
    executions = SQLAlchemyExecutionRepository(database.session_factory)
    terminals = SQLAlchemyTerminalRepository(database.session_factory)
    browsers = SQLAlchemyBrowserRepository(database.session_factory)
    await engagements.create(Engagement(id="engagement-1", name="Observer takeovers"))
    for suffix in ("1", "2"):
        run_id = f"run-{suffix}"
        session_id = f"session-{suffix}"
        await runs.create(
            Run(
                kind="general",
                id=run_id,
                engagement_id="engagement-1",
                node_id="node-1",
                objective=Objective(description="Observe active takeovers"),
                workspace_path=f"/tmp/{run_id}",
            )
        )
        await sessions.create(
            AgentSession(id=session_id, run_id=run_id, model_profile="test")
        )
        execution = Execution(
            id=f"execution-{suffix}",
            execution_key=f"execution-key-{suffix}",
            run_id=run_id,
            node_id="node-1",
            executor_type=ExecutorType.PTY,
            argv=["sh"],
            cwd="/tmp",
            stdout_path=f"/tmp/{suffix}.stdout",
            stderr_path=f"/tmp/{suffix}.stderr",
        )
        execution.transition_to(ExecutionStatus.STARTING)
        await executions.create_if_absent(execution)
        terminal = TerminalSession(
            id=f"terminal-{suffix}",
            run_id=run_id,
            execution_id=execution.id,
        )
        terminal.transition_to(TerminalStatus.OPEN)
        terminal.take_over()
        await terminals.create(terminal)
        browser = BrowserSession(
            id=f"browser-{suffix}",
            run_id=run_id,
            agent_session_id=session_id,
            node_id="node-1",
            mode=BrowserMode.MANAGED_EPHEMERAL,
        )
        browser.transition_to(BrowserSessionStatus.STARTING)
        browser.transition_to(BrowserSessionStatus.ACTIVE)
        browser.take_over(observation_version=0)
        await browsers.create_session(browser)

    reader = SQLAlchemyActiveTakeoverReader(database.session_factory)
    assert await reader.active_for_run("run-1") == (
        "browser:browser-1",
        "terminal:terminal-1",
    )
    assert await reader.active_for_run("run-1", limit=1) == (
        "browser:browser-1",
    )
    with pytest.raises(ValueError, match="between 1 and 100"):
        await reader.active_for_run("run-1", limit=0)
    await database.dispose()
