"""Fixed-query SQLAlchemy aggregation for run-scoped runtime metrics."""

from __future__ import annotations

import json
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from riftx.domain import ApprovalStatus, BrowserActionStatus, ExecutionStatus, RunStatus
from riftx.observability import RuntimeMetricsEvidence
from riftx.runtime.types import ToolCallStatus

from .orm import (
    AgentMessageRecord,
    AgentSessionRecord,
    BrowserActionRecord,
    BrowserSessionRecord,
    ContextCheckpointRecord,
    ContextCompilationRecord,
    ExecutionRecord,
    RunEventRecord,
    RunRecord,
    RuntimeApprovalRequestRecord,
    ToolCallIntentRecord,
    WebResearchPacketRecord,
)

SessionFactory = async_sessionmaker[AsyncSession]

_USEFUL_CONTEXT_CATEGORIES = {
    "run_contract",
    "working_memory",
    "conversation",
    "tool_results",
    "retrieved_memory",
    "subagent_results",
}
_RESUMED_TOOL_STATUSES = {
    ToolCallStatus.EXECUTING.value,
    ToolCallStatus.COMPLETED.value,
    ToolCallStatus.FAILED.value,
    ToolCallStatus.CANCELLED.value,
}
_USEFUL_SUBAGENT_STATUSES = {"completed", "partial"}


class SQLAlchemyRuntimeObservabilityRepository:
    """Collect all eleven QA-02 metrics with a fixed number of bounded queries."""

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    async def collect(self, run_id: str) -> RuntimeMetricsEvidence | None:
        async with self._session_factory() as session:
            run = await session.get(RunRecord, run_id)
            if run is None:
                return None
            intents = await _rows(
                session,
                select(ToolCallIntentRecord).where(ToolCallIntentRecord.run_id == run_id),
            )
            executions = await _rows(
                session,
                select(ExecutionRecord).where(ExecutionRecord.run_id == run_id),
            )
            recovery_events = await _rows(
                session,
                select(RunEventRecord).where(
                    RunEventRecord.run_id == run_id,
                    RunEventRecord.event_type == "execution.reconciled",
                ),
            )
            checkpoints = await _rows(
                session,
                select(ContextCheckpointRecord).where(
                    ContextCheckpointRecord.run_id == run_id
                ),
            )
            compilations = await _rows(
                session,
                select(ContextCompilationRecord).where(
                    ContextCompilationRecord.run_id == run_id
                ),
            )
            child_sessions = await _rows(
                session,
                select(AgentSessionRecord).where(
                    AgentSessionRecord.run_id == run_id,
                    AgentSessionRecord.parent_session_id.is_not(None),
                ),
            )
            subagent_messages = await _rows(
                session,
                select(AgentMessageRecord).where(
                    AgentMessageRecord.run_id == run_id,
                    AgentMessageRecord.message_type == "subagent_result",
                ),
            )
            approval_rows = await _rows(
                session,
                select(RuntimeApprovalRequestRecord).where(
                    RuntimeApprovalRequestRecord.run_id == run_id,
                    RuntimeApprovalRequestRecord.status == ApprovalStatus.APPROVED.value,
                ),
            )
            browser_actions = await _rows(
                session,
                select(BrowserActionRecord)
                .join(
                    BrowserSessionRecord,
                    BrowserSessionRecord.id == BrowserActionRecord.browser_session_id,
                )
                .where(BrowserSessionRecord.run_id == run_id),
            )
            research_packets = await _rows(
                session,
                select(WebResearchPacketRecord).where(
                    WebResearchPacketRecord.run_id == run_id
                ),
            )

        repeated_tool_calls = _repeated_tool_calls(intents)
        invalid_tool_calls = sum(
            not (row.tool_id or row.skill_id) or row.execution_spec_json is None
            for row in intents
        )
        execution_keys = [row.execution_key for row in executions if row.execution_key]
        duplicate_executions = len(execution_keys) - len(set(execution_keys))
        recovery_successes = sum(
            event.payload_json.get("status") != ExecutionStatus.LOST.value
            for event in recovery_events
        )
        preserved_dimensions = sum(
            _checkpoint_dimensions(row.snapshot_json, run.objective, run.scope_json)
            for row in checkpoints
        )
        useful_tokens, total_tokens = _context_tokens(compilations)
        useful_subagents = _useful_subagents(child_sessions, subagent_messages)
        intent_statuses = {row.id: row.status for row in intents}
        approval_resumes = sum(
            intent_statuses.get(row.tool_call_intent_id) in _RESUMED_TOOL_STATUSES
            for row in approval_rows
        )
        cited_claims, total_claims = _citation_counts(research_packets)
        return RuntimeMetricsEvidence(
            completed_tasks=int(run.status == RunStatus.COMPLETED.value),
            total_tasks=1,
            repeated_tool_calls=repeated_tool_calls,
            total_tool_calls=len(intents),
            invalid_tool_calls=invalid_tool_calls,
            recovery_successes=recovery_successes,
            recovery_attempts=len(recovery_events),
            duplicate_executions=duplicate_executions,
            total_executions=len(executions),
            preserved_compaction_dimensions=preserved_dimensions,
            total_compaction_dimensions=len(checkpoints) * 5,
            useful_context_tokens=useful_tokens,
            total_context_tokens=total_tokens,
            useful_subagent_results=useful_subagents,
            total_subagent_results=len(child_sessions),
            approval_resume_successes=approval_resumes,
            resolved_approvals=len(approval_rows),
            failed_browser_actions=sum(
                row.status == BrowserActionStatus.FAILED.value for row in browser_actions
            ),
            total_browser_actions=len(browser_actions),
            cited_claims=cited_claims,
            total_claims=total_claims,
        )


async def _rows(session: AsyncSession, statement: object) -> Sequence[object]:
    return (await session.scalars(statement)).all()  # type: ignore[arg-type,no-any-return]


def _repeated_tool_calls(intents: Sequence[ToolCallIntentRecord]) -> int:
    fingerprints = [
        json.dumps(
            [row.tool_id or row.skill_id, row.arguments_json],
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        for row in intents
    ]
    return len(fingerprints) - len(set(fingerprints))


def _checkpoint_dimensions(
    snapshot: dict[str, object],
    objective: str,
    scope: dict[str, object],
) -> int:
    return sum(
        (
            snapshot.get("objective") == objective,
            snapshot.get("scope") == scope,
            isinstance(snapshot.get("plan"), dict) and bool(snapshot["plan"]),
            isinstance(snapshot.get("user_decisions"), list),
            isinstance(snapshot.get("retained_message_ids"), list),
        )
    )


def _context_tokens(
    compilations: Sequence[ContextCompilationRecord],
) -> tuple[int, int]:
    useful = 0
    total = 0
    for compilation in compilations:
        categories = compilation.manifest_json.get("categories")
        if not isinstance(categories, dict):
            continue
        for name, raw_usage in categories.items():
            if not isinstance(raw_usage, dict):
                continue
            tokens = raw_usage.get("estimated_tokens", 0)
            if not isinstance(tokens, int) or isinstance(tokens, bool) or tokens < 0:
                continue
            total += tokens
            if name in _USEFUL_CONTEXT_CATEGORIES:
                useful += tokens
    return useful, total


def _useful_subagents(
    child_sessions: Sequence[AgentSessionRecord],
    messages: Sequence[AgentMessageRecord],
) -> int:
    child_ids = {row.id for row in child_sessions}
    useful_ids: set[str] = set()
    for message in messages:
        if message.session_id not in child_ids:
            continue
        payload = message.structured_content_json
        if not isinstance(payload, dict):
            continue
        status = payload.get("status")
        summary = payload.get("summary")
        if status in _USEFUL_SUBAGENT_STATUSES and isinstance(summary, str) and summary.strip():
            useful_ids.add(message.session_id)
    return len(useful_ids)


def _citation_counts(
    packets: Sequence[WebResearchPacketRecord],
) -> tuple[int, int]:
    cited = 0
    total = 0
    for packet in packets:
        source_ids = set(packet.source_ids_json)
        for claim in packet.claims_json:
            total += 1
            evidence = claim.get("evidence") if isinstance(claim, dict) else None
            if not isinstance(evidence, list) or not evidence:
                continue
            if all(
                isinstance(span, dict)
                and isinstance(span.get("source_id"), str)
                and span["source_id"] in source_ids
                for span in evidence
            ):
                cited += 1
    return cited, total
