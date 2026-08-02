from __future__ import annotations

import asyncio
from pathlib import Path

from riftx.config import SubagentConfig
from riftx.domain import MessageRole, MessageType, MessageVisibility, TranscriptMessageDraft
from riftx.subagents import (
    SubagentHandle,
    SubagentOrchestrator,
    SubagentResult,
    SubagentStatus,
)

from .test_manager import build_manager, delegation


class IndependentToolRunner:
    def __init__(self, sessions) -> None:
        self._sessions = sessions
        self.active = 0
        self.max_active = 0

    async def run(self, handle: SubagentHandle) -> SubagentResult:
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(0.02)
            await self._sessions.append_message(
                handle.session.id,
                TranscriptMessageDraft(
                    agent_id=handle.session.agent_type,
                    role=MessageRole.TOOL,
                    message_type=MessageType.TOOL_RESULT_REFERENCE,
                    content=f"probe completed for {handle.delegation.task_id}",
                    execution_id=f"execution-{handle.delegation.task_id}",
                    visibility=MessageVisibility.SUBAGENT_PRIVATE,
                ),
            )
            return SubagentResult(
                task_id=handle.delegation.task_id,
                status=SubagentStatus.COMPLETED,
                summary=f"Completed {handle.delegation.task_id}",
                evidence_refs=[f"execution://{handle.delegation.task_id}"],
            )
        finally:
            self.active -= 1


async def test_three_subagents_execute_tools_in_parallel_and_return_only_packets(
    tmp_path: Path,
) -> None:
    database, sessions, manager, transcript = await build_manager(
        tmp_path,
        limits=SubagentConfig(max_parallel_per_run=4, max_total_per_run=20),
    )
    await sessions.create_session(
        run_id="run-1", model_profile="test-model", session_id="primary"
    )
    runner = IndependentToolRunner(sessions)
    orchestrator = SubagentOrchestrator(manager, runner)

    results = await orchestrator.execute_many(
        parent_session_id="primary",
        delegations=[delegation(f"task-{index}") for index in range(1, 4)],
    )

    parent_messages = await transcript.list_by_session("primary")
    child_sessions = await manager.list_sessions("run-1")
    assert runner.max_active == 3
    assert [result.status for result in results] == [SubagentStatus.COMPLETED] * 3
    assert len(parent_messages) == 3
    assert all(item.structured_content is not None for item in parent_messages)
    assert all(
        "failed_approaches" not in (item.structured_content or {})
        for item in parent_messages
    )
    assert len(child_sessions) == 3
    for child in child_sessions:
        messages = await transcript.list_by_session(child.id)
        assert [item.message_type for item in messages] == [
            MessageType.SUBAGENT_DELEGATION,
            MessageType.TOOL_RESULT_REFERENCE,
            MessageType.SUBAGENT_RESULT,
        ]
    await database.dispose()
