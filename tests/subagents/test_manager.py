from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

from riftx.config import SubagentConfig
from riftx.domain import Engagement, Objective, Run
from riftx.persistence import (
    Database,
    SQLAlchemyAgentSessionRepository,
    SQLAlchemyEngagementRepository,
    SQLAlchemyProviderStateRepository,
    SQLAlchemyRunEventRepository,
    SQLAlchemyRunRepository,
    SQLAlchemyTranscriptRepository,
)
from riftx.runtime.session import SessionManager
from riftx.subagents import (
    DelegationPacket,
    SubagentLimitError,
    SubagentManager,
    SubagentResult,
    SubagentStatus,
)
from riftx.tools import ToolContextManager, ToolRegistry


async def build_manager(
    tmp_path: Path,
    *,
    limits: SubagentConfig | None = None,
) -> tuple[Database, SessionManager, SubagentManager, SQLAlchemyTranscriptRepository]:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'subagents.db'}")
    await database.create_schema()
    runs = SQLAlchemyRunRepository(database.session_factory)
    await SQLAlchemyEngagementRepository(database.session_factory).create(
        Engagement(id="engagement-1", name="Authorized")
    )
    await runs.create(
        Run(
            kind="general",
            id="run-1",
            engagement_id="engagement-1",
            node_id="node-1",
            objective=Objective(description="Inspect the local service"),
            workspace_path=str(tmp_path / "workspace"),
        )
    )
    session_repository = SQLAlchemyAgentSessionRepository(database.session_factory)
    transcript = SQLAlchemyTranscriptRepository(database.session_factory)
    sessions = SessionManager(
        run_repository=runs,
        session_repository=session_repository,
        transcript_repository=transcript,
        provider_state_repository=SQLAlchemyProviderStateRepository(
            database.session_factory
        ),
    )
    tools_path = tmp_path / "tools.yaml"
    tools_path.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "tools": {
                    "probe": {
                        "command": [sys.executable],
                        "capabilities": ["recon"],
                    },
                    "unassigned": {
                        "command": [sys.executable],
                        "capabilities": ["other"],
                    },
                },
            }
        )
    )
    registry = ToolRegistry(tools_path, node_id="node-1")
    await registry.refresh()
    manager = SubagentManager(
        sessions=sessions,
        session_repository=session_repository,
        tool_context=ToolContextManager(registry),
        limits=limits,
        events=SQLAlchemyRunEventRepository(database.session_factory),
    )
    return database, sessions, manager, transcript


def delegation(task_id: str) -> DelegationPacket:
    return DelegationPacket(
        task_id=task_id,
        subagent_type="recon",
        task="Probe the HTTPS endpoint",
        run_contract_summary="Authorized local assessment",
        relevant_scope=["127.0.0.1"],
        available_tool_ids=["probe"],
        workspace="/workspace",
    )


async def test_subagent_uses_independent_session_and_primary_gets_only_merge_packet(
    tmp_path: Path,
) -> None:
    database, sessions, manager, transcript = await build_manager(tmp_path)
    await sessions.create_session(
        run_id="run-1", model_profile="test-model", session_id="primary"
    )
    handle = await manager.start(
        parent_session_id="primary",
        delegation=delegation("task-1"),
        session_id="subagent-1",
    )
    result = SubagentResult(
        task_id="task-1",
        status=SubagentStatus.COMPLETED,
        summary="HTTPS endpoint responds.",
        failed_approaches=["UDP probe"],
        unresolved_questions=["TLS policy"],
        recommended_next_actions=["Inspect TLS configuration"],
    )
    await manager.complete(
        handle.session.id,
        result,
    )
    await manager.complete(handle.session.id, result)

    child_messages = await transcript.list_by_session("subagent-1")
    parent_messages = await transcript.list_by_session("primary")

    assert handle.session.parent_session_id == "primary"
    assert [item.sequence for item in child_messages] == [1, 2]
    assert child_messages[0].structured_content["available_tool_ids"] == ["probe"]
    assert child_messages[1].structured_content["failed_approaches"] == ["UDP probe"]
    assert len(parent_messages) == 1
    assert parent_messages[0].structured_content == {
        "task_id": "task-1",
        "status": "completed",
        "summary": "HTTPS endpoint responds.",
        "confirmed_fact_candidates": [],
        "hypothesis_updates": [],
        "finding_candidates": [],
        "evidence_refs": [],
        "recommended_next_actions": ["Inspect TLS configuration"],
    }
    await database.dispose()


async def test_scheduler_enforces_depth_parallel_and_total_limits(tmp_path: Path) -> None:
    database, sessions, manager, _ = await build_manager(
        tmp_path,
        limits=SubagentConfig(max_parallel_per_run=2, max_total_per_run=3),
    )
    await sessions.create_session(
        run_id="run-1", model_profile="test-model", session_id="primary"
    )
    first = await manager.start(
        parent_session_id="primary",
        delegation=delegation("task-1"),
        session_id="subagent-1",
    )
    await manager.start(
        parent_session_id="primary",
        delegation=delegation("task-2"),
        session_id="subagent-2",
    )

    with pytest.raises(SubagentLimitError, match="active Subagents"):
        await manager.start(
            parent_session_id="primary",
            delegation=delegation("parallel-overflow"),
        )
    with pytest.raises(SubagentLimitError, match="cannot delegate"):
        await manager.start(
            parent_session_id=first.session.id,
            delegation=delegation("nested"),
        )

    await manager.complete(
        first.session.id,
        SubagentResult(
            task_id="task-1",
            status=SubagentStatus.COMPLETED,
            summary="Done",
        ),
    )
    third = await manager.start(
        parent_session_id="primary",
        delegation=delegation("task-3"),
        session_id="subagent-3",
    )
    await manager.complete(
        third.session.id,
        SubagentResult(
            task_id="task-3",
            status=SubagentStatus.COMPLETED,
            summary="Done",
        ),
    )
    with pytest.raises(SubagentLimitError, match="already created 3"):
        await manager.start(
            parent_session_id="primary",
            delegation=delegation("total-overflow"),
        )
    await database.dispose()
