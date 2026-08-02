"""Explicit SQL projections used by the durable Action read repository.

The list and detail plans intentionally have separate SELECT lists.  Keeping
the projections here makes it possible to audit that command, filesystem, and
execution-output columns never enter the Action hydration path.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from sqlalchemy import (
    Select,
    and_,
    case,
    false,
    func,
    literal,
    or_,
    select,
    true,
    tuple_,
    union_all,
)
from sqlalchemy.sql.selectable import CTE

from riftx.application.actions import ActionPageKey

from .artifact_visibility import artifact_is_not_target_http_sensitive
from .orm import (
    ApprovalRecord,
    ArtifactRecord,
    ExecutionRecord,
    FindingRecord,
    RunEventRecord,
    RuntimeApprovalRequestRecord,
    ToolCallIntentRecord,
)


class _KeyedColumn(Protocol):
    key: str


def _keys(*columns: _KeyedColumn) -> frozenset[str]:
    return frozenset(str(column.key) for column in columns)


_LIST_ROOT_KEYS = _keys(
    ToolCallIntentRecord.id,
    ToolCallIntentRecord.run_id,
    ToolCallIntentRecord.session_id,
    ToolCallIntentRecord.cycle_id,
    ToolCallIntentRecord.step_id,
    ToolCallIntentRecord.engine_call_id,
    ToolCallIntentRecord.tool_id,
    ToolCallIntentRecord.skill_id,
    ToolCallIntentRecord.reason,
    ToolCallIntentRecord.target_summary,
    ToolCallIntentRecord.approval_level,
    ToolCallIntentRecord.status,
    ToolCallIntentRecord.claimed_execution_key,
    ToolCallIntentRecord.claimed_attempt_group,
    ToolCallIntentRecord.created_at,
    ToolCallIntentRecord.updated_at,
)
_DETAIL_ROOT_KEYS = _LIST_ROOT_KEYS | _keys(ToolCallIntentRecord.arguments_json)

_LIST_APPROVAL_KEYS = _keys(
    RuntimeApprovalRequestRecord.tool_call_intent_id,
    RuntimeApprovalRequestRecord.id,
    RuntimeApprovalRequestRecord.run_id,
    RuntimeApprovalRequestRecord.session_id,
    RuntimeApprovalRequestRecord.cycle_id,
    RuntimeApprovalRequestRecord.status,
    RuntimeApprovalRequestRecord.decided_by,
    RuntimeApprovalRequestRecord.created_at,
    RuntimeApprovalRequestRecord.decided_at,
    ApprovalRecord.id,
    ApprovalRecord.run_id,
    ApprovalRecord.status,
    ApprovalRecord.decided_by,
    ApprovalRecord.created_at,
    ApprovalRecord.decided_at,
) | frozenset({"approval_count"})
_DETAIL_APPROVAL_KEYS = _LIST_APPROVAL_KEYS | _keys(
    RuntimeApprovalRequestRecord.feedback,
)

_EXECUTION_SUMMARY_KEYS = frozenset(
    {
        "action_id",
        "execution_count",
        "execution_current_match_count",
        "execution_current_id",
        "execution_max_created_at",
        "execution_max_started_at",
        "execution_max_finished_at",
        "execution_max_physical_stop_confirmed_at",
        "execution_max_updated_at",
        "execution_scope_mismatch",
        "execution_session_mismatch",
    }
)
_LIST_EXECUTION_KEYS = _EXECUTION_SUMMARY_KEYS | frozenset(
    {
        "execution_id",
        "execution_attempt_group",
        "execution_status",
        "execution_exit_code",
        "execution_created_at",
        "execution_started_at",
        "execution_finished_at",
        "execution_physical_stop_confirmed_at",
        "execution_node_id",
    }
)
_DETAIL_EXECUTION_KEYS = _LIST_EXECUTION_KEYS

_ARTIFACT_KEYS = frozenset(
    {
        "action_id",
        "artifact_count",
        "artifact_max_created_at",
        "artifact_scope_mismatch",
        "artifact_id",
        "artifact_run_id",
        "artifact_execution_id",
        "artifact_created_at",
    }
)
_LIST_FINDING_KEYS = frozenset(
    {
        "action_id",
        "finding_count",
        "finding_max_created_at",
        "finding_max_updated_at",
        "finding_partial",
    }
)
_DETAIL_FINDING_KEYS = _LIST_FINDING_KEYS | frozenset(
    {
        "finding_id",
        "finding_run_id",
        "finding_created_at",
        "finding_updated_at",
    }
)
_LIST_EVENT_KEYS = frozenset(
    {
        "action_id",
        "event_count",
        "event_max_created_at",
        "event_partial",
    }
)
_DETAIL_EVENT_KEYS = _LIST_EVENT_KEYS | frozenset(
    {
        "event_id",
        "event_run_id",
        "event_created_at",
        "event_sequence",
        "event_type",
    }
)

LIST_SELECTED_COLUMN_KEYS = {
    "roots": _LIST_ROOT_KEYS,
    "approvals": _LIST_APPROVAL_KEYS,
    "executions": _LIST_EXECUTION_KEYS,
    "artifacts": _ARTIFACT_KEYS,
    "findings": _LIST_FINDING_KEYS,
    "events": _LIST_EVENT_KEYS,
}

DETAIL_SELECTED_COLUMN_KEYS = {
    "roots": _DETAIL_ROOT_KEYS,
    "approvals": _DETAIL_APPROVAL_KEYS,
    "executions": _DETAIL_EXECUTION_KEYS,
    "artifacts": _ARTIFACT_KEYS,
    "findings": _DETAIL_FINDING_KEYS,
    "events": _DETAIL_EVENT_KEYS,
}


def _list_root_columns() -> tuple[object, ...]:
    return (
        ToolCallIntentRecord.id.label("action_id"),
        ToolCallIntentRecord.run_id.label("intent_run_id"),
        ToolCallIntentRecord.session_id.label("intent_session_id"),
        ToolCallIntentRecord.cycle_id.label("intent_cycle_id"),
        ToolCallIntentRecord.step_id.label("intent_step_id"),
        ToolCallIntentRecord.engine_call_id.label("intent_engine_call_id"),
        ToolCallIntentRecord.tool_id.label("intent_tool_id"),
        ToolCallIntentRecord.skill_id.label("intent_skill_id"),
        ToolCallIntentRecord.reason.label("intent_reason"),
        ToolCallIntentRecord.target_summary.label("intent_target_summary"),
        ToolCallIntentRecord.approval_level.label("intent_approval_level"),
        ToolCallIntentRecord.status.label("intent_status"),
        ToolCallIntentRecord.claimed_execution_key.label("claimed_execution_key"),
        ToolCallIntentRecord.claimed_attempt_group.label("claimed_attempt_group"),
        ToolCallIntentRecord.created_at.label("intent_created_at"),
        ToolCallIntentRecord.updated_at.label("intent_updated_at"),
    )


def build_action_list_root_query(
    run_id: str,
    *,
    limit: int,
    after: ActionPageKey | None,
    snapshot: ActionPageKey | None,
) -> Select[tuple[object, ...]]:
    """Build the descending list keyset query, including one sentinel row."""

    statement = select(*_list_root_columns()).where(ToolCallIntentRecord.run_id == run_id)
    if snapshot is not None:
        statement = statement.where(
            tuple_(ToolCallIntentRecord.created_at, ToolCallIntentRecord.id) <= snapshot.as_tuple()
        )
    if after is not None:
        statement = statement.where(
            tuple_(ToolCallIntentRecord.created_at, ToolCallIntentRecord.id) < after.as_tuple()
        )
    return statement.order_by(
        ToolCallIntentRecord.created_at.desc(),
        ToolCallIntentRecord.id.desc(),
    ).limit(limit + 1)


def build_action_detail_root_query(
    run_id: str,
    action_id: str,
) -> Select[tuple[object, ...]]:
    return select(
        *_list_root_columns(),
        ToolCallIntentRecord.arguments_json.label("intent_arguments"),
    ).where(
        ToolCallIntentRecord.run_id == run_id,
        ToolCallIntentRecord.id == action_id,
    )


def _approval_columns(*, detail: bool) -> tuple[object, ...]:
    columns: tuple[object, ...] = (
        RuntimeApprovalRequestRecord.tool_call_intent_id.label("action_id"),
        RuntimeApprovalRequestRecord.id.label("runtime_approval_id"),
        RuntimeApprovalRequestRecord.run_id.label("runtime_run_id"),
        RuntimeApprovalRequestRecord.session_id.label("runtime_session_id"),
        RuntimeApprovalRequestRecord.cycle_id.label("runtime_cycle_id"),
        RuntimeApprovalRequestRecord.status.label("runtime_status"),
        RuntimeApprovalRequestRecord.decided_by.label("runtime_decided_by"),
        RuntimeApprovalRequestRecord.created_at.label("runtime_created_at"),
        RuntimeApprovalRequestRecord.decided_at.label("runtime_decided_at"),
        ApprovalRecord.id.label("public_approval_id"),
        ApprovalRecord.run_id.label("public_run_id"),
        ApprovalRecord.status.label("public_status"),
        ApprovalRecord.decided_by.label("public_decided_by"),
        ApprovalRecord.created_at.label("public_created_at"),
        ApprovalRecord.decided_at.label("public_decided_at"),
    )
    if detail:
        columns += (RuntimeApprovalRequestRecord.feedback.label("runtime_feedback"),)
    return columns


def _approval_query(
    action_ids: Sequence[str],
    *,
    detail: bool,
) -> Select[tuple[object, ...]]:
    roots = _selected_action_roots(action_ids, name="approval_selected_roots")
    ranked = (
        select(
            *_approval_columns(detail=detail),
            func.count()
            .over(
                partition_by=RuntimeApprovalRequestRecord.tool_call_intent_id,
            )
            .label("approval_count"),
            func.row_number()
            .over(
                partition_by=RuntimeApprovalRequestRecord.tool_call_intent_id,
                order_by=RuntimeApprovalRequestRecord.id,
            )
            .label("approval_rank"),
        )
        .select_from(RuntimeApprovalRequestRecord)
        .join(
            roots,
            roots.c.action_id == RuntimeApprovalRequestRecord.tool_call_intent_id,
        )
        .outerjoin(ApprovalRecord, ApprovalRecord.id == RuntimeApprovalRequestRecord.id)
        .cte("approval_ranked")
    )
    columns: tuple[object, ...] = (
        ranked.c.approval_count,
        ranked.c.action_id,
        ranked.c.runtime_approval_id,
        ranked.c.runtime_run_id,
        ranked.c.runtime_session_id,
        ranked.c.runtime_cycle_id,
        ranked.c.runtime_status,
        ranked.c.runtime_decided_by,
        ranked.c.runtime_created_at,
        ranked.c.runtime_decided_at,
        ranked.c.public_approval_id,
        ranked.c.public_run_id,
        ranked.c.public_status,
        ranked.c.public_decided_by,
        ranked.c.public_created_at,
        ranked.c.public_decided_at,
    )
    if detail:
        columns += (ranked.c.runtime_feedback,)
    return select(*columns).where(ranked.c.approval_rank == 1).order_by(ranked.c.action_id)


def build_action_list_approval_query(
    action_ids: Sequence[str],
) -> Select[tuple[object, ...]]:
    return _approval_query(action_ids, detail=False)


def build_action_detail_approval_query(
    action_ids: Sequence[str],
) -> Select[tuple[object, ...]]:
    return _approval_query(action_ids, detail=True)


def _selected_action_roots(
    action_ids: Sequence[str],
    *,
    name: str,
) -> CTE:
    """Project only durable ownership fields for the requested roots."""

    return (
        select(
            ToolCallIntentRecord.id.label("action_id"),
            ToolCallIntentRecord.run_id.label("run_id"),
            ToolCallIntentRecord.session_id.label("session_id"),
            ToolCallIntentRecord.claimed_execution_key.label("claimed_execution_key"),
            ToolCallIntentRecord.claimed_attempt_group.label("claimed_attempt_group"),
        )
        .where(ToolCallIntentRecord.id.in_(tuple(action_ids)))
        .cte(name)
    )


def _global_execution_ownership(*, prefix: str) -> tuple[CTE, CTE]:
    """Return global candidate and exact durable owners for every execution.

    Candidate ownership includes both the execution's direct durable intent and
    every durable intent claiming its execution key.  Computing this globally
    is important: a claim outside the requested page must still make reference
    correlation partial for an owner inside the page.
    """

    direct_candidates = (
        select(
            ExecutionRecord.id.label("execution_id"),
            ToolCallIntentRecord.id.label("action_id"),
        )
        .select_from(ExecutionRecord)
        .join(ToolCallIntentRecord, ToolCallIntentRecord.id == ExecutionRecord.tool_call_id)
    )
    claimed_candidates = (
        select(
            ExecutionRecord.id.label("execution_id"),
            ToolCallIntentRecord.id.label("action_id"),
        )
        .select_from(ExecutionRecord)
        .join(
            ToolCallIntentRecord,
            ToolCallIntentRecord.claimed_execution_key == ExecutionRecord.execution_key,
        )
    )
    candidate_rows = union_all(direct_candidates, claimed_candidates).cte(
        f"{prefix}_execution_candidate_rows"
    )
    candidates = (
        select(candidate_rows.c.execution_id, candidate_rows.c.action_id)
        .distinct()
        .cte(f"{prefix}_execution_candidates")
    )
    exact = (
        select(
            ExecutionRecord.id.label("execution_id"),
            ToolCallIntentRecord.id.label("action_id"),
        )
        .select_from(ExecutionRecord)
        .join(
            ToolCallIntentRecord,
            and_(
                ToolCallIntentRecord.id == ExecutionRecord.tool_call_id,
                ToolCallIntentRecord.run_id == ExecutionRecord.run_id,
                ToolCallIntentRecord.session_id == ExecutionRecord.session_id,
            ),
        )
        .cte(f"{prefix}_execution_exact")
    )
    return candidates, exact


def _execution_query(
    action_ids: Sequence[str],
    claimed_execution_keys: Sequence[str],
    *,
    detail: bool,
) -> Select[tuple[object, ...]]:
    # Claims are deliberately derived from durable intents globally.  Retain
    # the parameter for compatibility with callers built against the previous
    # query-builder surface, but never let a page-local claim list define
    # ownership.
    del claimed_execution_keys

    roots = _selected_action_roots(action_ids, name="execution_selected_roots")
    candidates, exact = _global_execution_ownership(prefix="execution")
    exact_columns: tuple[object, ...] = (
        ExecutionRecord.id.label("execution_id"),
        ExecutionRecord.execution_key,
        ExecutionRecord.attempt_group.label("execution_attempt_group"),
        ExecutionRecord.status.label("execution_status"),
        ExecutionRecord.exit_code.label("execution_exit_code"),
        ExecutionRecord.created_at.label("execution_created_at"),
        ExecutionRecord.started_at.label("execution_started_at"),
        ExecutionRecord.finished_at.label("execution_finished_at"),
        ExecutionRecord.physical_stop_confirmed_at.label("execution_physical_stop_confirmed_at"),
        ExecutionRecord.updated_at.label("execution_updated_at"),
        ExecutionRecord.node_id.label("execution_node_id"),
    )
    exact_selected = (
        select(
            roots.c.action_id,
            roots.c.claimed_execution_key,
            roots.c.claimed_attempt_group,
            *exact_columns,
        )
        .select_from(roots)
        .join(exact, exact.c.action_id == roots.c.action_id)
        .join(ExecutionRecord, ExecutionRecord.id == exact.c.execution_id)
        .cte("execution_exact_selected")
    )
    candidate_selected = (
        select(
            roots.c.action_id,
            roots.c.run_id.label("root_run_id"),
            roots.c.session_id.label("root_session_id"),
            ExecutionRecord.tool_call_id.label("execution_action_id"),
            ExecutionRecord.run_id.label("execution_run_id"),
            ExecutionRecord.session_id.label("execution_session_id"),
        )
        .select_from(roots)
        .join(candidates, candidates.c.action_id == roots.c.action_id)
        .join(ExecutionRecord, ExecutionRecord.id == candidates.c.execution_id)
        .cte("execution_candidates_selected")
    )
    current_match = and_(
        roots.c.claimed_execution_key.is_not(None),
        roots.c.claimed_attempt_group.is_not(None),
        exact_selected.c.execution_key == roots.c.claimed_execution_key,
        exact_selected.c.execution_attempt_group == roots.c.claimed_attempt_group,
    )
    exact_summary = (
        select(
            roots.c.action_id,
            func.count(exact_selected.c.execution_id).label("execution_count"),
            func.sum(case((current_match, 1), else_=0)).label("execution_current_match_count"),
            func.max(case((current_match, exact_selected.c.execution_id), else_=None)).label(
                "execution_current_id"
            ),
            func.max(exact_selected.c.execution_created_at).label("execution_max_created_at"),
            func.max(exact_selected.c.execution_started_at).label("execution_max_started_at"),
            func.max(exact_selected.c.execution_finished_at).label("execution_max_finished_at"),
            func.max(exact_selected.c.execution_physical_stop_confirmed_at).label(
                "execution_max_physical_stop_confirmed_at"
            ),
            func.max(exact_selected.c.execution_updated_at).label("execution_max_updated_at"),
        )
        .select_from(roots)
        .outerjoin(exact_selected, exact_selected.c.action_id == roots.c.action_id)
        .group_by(roots.c.action_id)
        .cte("execution_exact_summary")
    )
    scope_mismatch = or_(
        candidate_selected.c.execution_action_id.is_distinct_from(candidate_selected.c.action_id),
        candidate_selected.c.execution_run_id.is_distinct_from(candidate_selected.c.root_run_id),
    )
    mismatch_summary = (
        select(
            roots.c.action_id,
            func.max(case((scope_mismatch, true()), else_=false())).label(
                "execution_scope_mismatch"
            ),
            func.max(
                case(
                    (
                        candidate_selected.c.execution_session_id.is_distinct_from(
                            candidate_selected.c.root_session_id
                        ),
                        true(),
                    ),
                    else_=false(),
                )
            ).label("execution_session_mismatch"),
        )
        .select_from(roots)
        .outerjoin(candidate_selected, candidate_selected.c.action_id == roots.c.action_id)
        .group_by(roots.c.action_id)
        .cte("execution_mismatch_summary")
    )
    summary = (
        select(
            exact_summary.c.action_id,
            exact_summary.c.execution_count,
            exact_summary.c.execution_current_match_count,
            exact_summary.c.execution_current_id,
            exact_summary.c.execution_max_created_at,
            exact_summary.c.execution_max_started_at,
            exact_summary.c.execution_max_finished_at,
            exact_summary.c.execution_max_physical_stop_confirmed_at,
            exact_summary.c.execution_max_updated_at,
            mismatch_summary.c.execution_scope_mismatch,
            mismatch_summary.c.execution_session_mismatch,
        )
        .join(mismatch_summary, mismatch_summary.c.action_id == exact_summary.c.action_id)
        .cte("execution_summary")
    )
    ranked = (
        select(
            exact_selected,
            current_match.label("is_current_match"),
            func.row_number()
            .over(
                partition_by=exact_selected.c.action_id,
                order_by=(
                    exact_selected.c.execution_created_at.is_not(None).desc(),
                    exact_selected.c.execution_created_at.desc(),
                    exact_selected.c.execution_id.desc(),
                ),
            )
            .label("recency_rank"),
        )
        .select_from(exact_selected)
        .join(roots, roots.c.action_id == exact_selected.c.action_id)
        .cte("execution_ranked")
    )
    current_rank = (
        select(
            ranked.c.action_id,
            func.max(
                case((ranked.c.is_current_match.is_(true()), ranked.c.recency_rank), else_=None)
            ).label("current_rank"),
        )
        .group_by(ranked.c.action_id)
        .cte("execution_current_rank")
    )
    retained_rank_limit = case(
        (
            and_(
                summary.c.execution_current_match_count == 1,
                current_rank.c.current_rank > 100,
            ),
            99,
        ),
        else_=100,
    )
    bounded = (
        select(ranked)
        .select_from(ranked)
        .join(summary, summary.c.action_id == ranked.c.action_id)
        .outerjoin(current_rank, current_rank.c.action_id == ranked.c.action_id)
        .where(
            or_(
                ranked.c.recency_rank <= retained_rank_limit,
                and_(
                    summary.c.execution_current_match_count == 1,
                    ranked.c.is_current_match.is_(true()),
                ),
            )
        )
        .cte("execution_bounded")
    )

    columns: tuple[object, ...] = (
        summary.c.action_id,
        summary.c.execution_count,
        summary.c.execution_current_match_count,
        summary.c.execution_current_id,
        summary.c.execution_max_created_at,
        summary.c.execution_max_started_at,
        summary.c.execution_max_finished_at,
        summary.c.execution_max_physical_stop_confirmed_at,
        summary.c.execution_max_updated_at,
        summary.c.execution_scope_mismatch,
        summary.c.execution_session_mismatch,
        bounded.c.execution_id,
        bounded.c.execution_attempt_group,
        bounded.c.execution_status,
        bounded.c.execution_exit_code,
        bounded.c.execution_created_at,
        bounded.c.execution_started_at,
        bounded.c.execution_finished_at,
        bounded.c.execution_physical_stop_confirmed_at,
        bounded.c.execution_node_id,
    )
    return (
        select(*columns)
        .select_from(summary)
        .outerjoin(bounded, bounded.c.action_id == summary.c.action_id)
        .order_by(
            summary.c.action_id,
            bounded.c.execution_created_at.is_(None),
            bounded.c.execution_created_at,
            bounded.c.execution_id,
        )
    )


def build_action_list_execution_query(
    action_ids: Sequence[str],
    claimed_execution_keys: Sequence[str],
) -> Select[tuple[object, ...]]:
    return _execution_query(action_ids, claimed_execution_keys, detail=False)


def build_action_detail_execution_query(
    action_ids: Sequence[str],
    claimed_execution_keys: Sequence[str],
) -> Select[tuple[object, ...]]:
    return _execution_query(action_ids, claimed_execution_keys, detail=True)


def build_action_artifact_query(
    action_ids: Sequence[str],
) -> Select[tuple[object, ...]]:
    """Return exact artifact metadata with at most 100 rows per selected root."""

    roots = _selected_action_roots(action_ids, name="artifact_selected_roots")
    _, exact_executions = _global_execution_ownership(prefix="artifact")
    exact_artifacts = (
        select(
            roots.c.action_id,
            ArtifactRecord.id.label("artifact_id"),
            ArtifactRecord.run_id.label("artifact_run_id"),
            ArtifactRecord.execution_id.label("artifact_execution_id"),
            ArtifactRecord.created_at.label("artifact_created_at"),
        )
        .select_from(roots)
        .join(exact_executions, exact_executions.c.action_id == roots.c.action_id)
        .join(ArtifactRecord, ArtifactRecord.execution_id == exact_executions.c.execution_id)
        .where(
            ArtifactRecord.run_id == roots.c.run_id,
            artifact_is_not_target_http_sensitive(),
        )
        .cte("artifact_exact_selected")
    )
    mismatched_artifacts = (
        select(
            roots.c.action_id,
            ArtifactRecord.id.label("artifact_id"),
        )
        .select_from(roots)
        .join(exact_executions, exact_executions.c.action_id == roots.c.action_id)
        .join(ArtifactRecord, ArtifactRecord.execution_id == exact_executions.c.execution_id)
        .where(
            ArtifactRecord.run_id != roots.c.run_id,
            artifact_is_not_target_http_sensitive(),
        )
        .cte("artifact_mismatched_selected")
    )
    exact_summary = (
        select(
            roots.c.action_id,
            func.count(exact_artifacts.c.artifact_id).label("artifact_count"),
            func.max(exact_artifacts.c.artifact_created_at).label("artifact_max_created_at"),
        )
        .select_from(roots)
        .outerjoin(exact_artifacts, exact_artifacts.c.action_id == roots.c.action_id)
        .group_by(roots.c.action_id)
        .cte("artifact_exact_summary")
    )
    mismatch_summary = (
        select(
            roots.c.action_id,
            case(
                (func.count(mismatched_artifacts.c.artifact_id) > 0, true()),
                else_=false(),
            ).label("artifact_scope_mismatch"),
        )
        .select_from(roots)
        .outerjoin(
            mismatched_artifacts,
            mismatched_artifacts.c.action_id == roots.c.action_id,
        )
        .group_by(roots.c.action_id)
        .cte("artifact_mismatch_summary")
    )
    summary = (
        select(
            exact_summary.c.action_id,
            exact_summary.c.artifact_count,
            exact_summary.c.artifact_max_created_at,
            mismatch_summary.c.artifact_scope_mismatch,
        )
        .join(mismatch_summary, mismatch_summary.c.action_id == exact_summary.c.action_id)
        .cte("artifact_summary")
    )
    ranked = select(
        exact_artifacts,
        func.row_number()
        .over(
            partition_by=exact_artifacts.c.action_id,
            order_by=(
                exact_artifacts.c.artifact_created_at,
                exact_artifacts.c.artifact_id,
            ),
        )
        .label("artifact_rank"),
    ).cte("artifact_ranked")
    bounded = select(ranked).where(ranked.c.artifact_rank <= 100).cte("artifact_bounded")
    return (
        select(
            summary.c.action_id,
            summary.c.artifact_count,
            summary.c.artifact_max_created_at,
            summary.c.artifact_scope_mismatch,
            bounded.c.artifact_id,
            bounded.c.artifact_run_id,
            bounded.c.artifact_execution_id,
            bounded.c.artifact_created_at,
        )
        .select_from(summary)
        .outerjoin(bounded, bounded.c.action_id == summary.c.action_id)
        .order_by(
            summary.c.action_id,
            bounded.c.artifact_created_at,
            bounded.c.artifact_id,
        )
    )


def _global_artifact_ownership(
    execution_candidates: CTE,
    execution_exact: CTE,
    *,
    prefix: str,
) -> tuple[CTE, CTE]:
    candidate_rows = (
        select(
            ArtifactRecord.id.label("artifact_id"),
            execution_candidates.c.action_id,
        )
        .select_from(ArtifactRecord)
        .join(
            execution_candidates,
            execution_candidates.c.execution_id == ArtifactRecord.execution_id,
        )
        .where(artifact_is_not_target_http_sensitive())
        .cte(f"{prefix}_artifact_candidate_rows")
    )
    candidates = (
        select(candidate_rows.c.artifact_id, candidate_rows.c.action_id)
        .distinct()
        .cte(f"{prefix}_artifact_candidates")
    )
    exact = (
        select(
            ArtifactRecord.id.label("artifact_id"),
            execution_exact.c.action_id,
        )
        .select_from(ArtifactRecord)
        .join(
            execution_exact,
            execution_exact.c.execution_id == ArtifactRecord.execution_id,
        )
        .join(ToolCallIntentRecord, ToolCallIntentRecord.id == execution_exact.c.action_id)
        .where(
            ArtifactRecord.run_id == ToolCallIntentRecord.run_id,
            artifact_is_not_target_http_sensitive(),
        )
        .cte(f"{prefix}_artifact_exact")
    )
    return candidates, exact


def _global_approval_ownership(*, prefix: str) -> tuple[CTE, CTE]:
    counted = select(
        RuntimeApprovalRequestRecord.id.label("approval_id"),
        RuntimeApprovalRequestRecord.tool_call_intent_id.label("action_id"),
        func.count()
        .over(partition_by=RuntimeApprovalRequestRecord.tool_call_intent_id)
        .label("approval_count"),
    ).cte(f"{prefix}_approval_counted")
    candidates = (
        select(
            counted.c.approval_id,
            counted.c.action_id,
        )
        .select_from(counted)
        .join(ToolCallIntentRecord, ToolCallIntentRecord.id == counted.c.action_id)
        .cte(f"{prefix}_approval_candidates")
    )
    exact = (
        select(
            RuntimeApprovalRequestRecord.id.label("approval_id"),
            ToolCallIntentRecord.id.label("action_id"),
        )
        .select_from(counted)
        .join(
            RuntimeApprovalRequestRecord,
            RuntimeApprovalRequestRecord.id == counted.c.approval_id,
        )
        .join(
            ToolCallIntentRecord,
            and_(
                ToolCallIntentRecord.id == RuntimeApprovalRequestRecord.tool_call_intent_id,
                ToolCallIntentRecord.run_id == RuntimeApprovalRequestRecord.run_id,
                ToolCallIntentRecord.session_id == RuntimeApprovalRequestRecord.session_id,
                ToolCallIntentRecord.cycle_id == RuntimeApprovalRequestRecord.cycle_id,
            ),
        )
        .join(
            ApprovalRecord,
            and_(
                ApprovalRecord.id == RuntimeApprovalRequestRecord.id,
                ApprovalRecord.run_id == ToolCallIntentRecord.run_id,
            ),
        )
        .where(counted.c.approval_count == 1)
        .cte(f"{prefix}_approval_exact")
    )
    return candidates, exact


def _reference_resolution(
    objects: CTE,
    references: CTE,
    exact_owners: CTE,
    candidate_owners: CTE,
    *,
    prefix: str,
) -> tuple[CTE, CTE]:
    """Collapse reference rows into fail-closed per-object ownership metadata."""

    per_reference = (
        select(
            references.c.object_id,
            references.c.reference_kind,
            references.c.reference_id,
            func.min(exact_owners.c.action_id).label("exact_owner_id"),
            func.count(func.distinct(exact_owners.c.action_id)).label("exact_owner_count"),
            func.max(
                case(
                    (
                        and_(
                            candidate_owners.c.action_id.is_not(None),
                            exact_owners.c.action_id.is_distinct_from(candidate_owners.c.action_id),
                        ),
                        true(),
                    ),
                    else_=false(),
                )
            ).label("candidate_conflict"),
        )
        .select_from(references)
        .outerjoin(
            exact_owners,
            and_(
                exact_owners.c.reference_kind == references.c.reference_kind,
                exact_owners.c.reference_id == references.c.reference_id,
            ),
        )
        .outerjoin(
            candidate_owners,
            and_(
                candidate_owners.c.reference_kind == references.c.reference_kind,
                candidate_owners.c.reference_id == references.c.reference_id,
            ),
        )
        .group_by(
            references.c.object_id,
            references.c.reference_kind,
            references.c.reference_id,
        )
        .cte(f"{prefix}_per_reference")
    )
    exact_owner_count = func.count(func.distinct(per_reference.c.exact_owner_id))
    unresolved_reference = and_(
        per_reference.c.reference_id.is_not(None),
        or_(
            per_reference.c.exact_owner_count != 1,
            per_reference.c.candidate_conflict.is_(true()),
        ),
    )
    resolution = (
        select(
            objects.c.object_id,
            func.min(per_reference.c.exact_owner_id).label("exact_owner_id"),
            exact_owner_count.label("exact_owner_count"),
            case(
                (
                    or_(
                        func.max(case((unresolved_reference, true()), else_=false())).is_(true()),
                        exact_owner_count > 1,
                    ),
                    true(),
                ),
                else_=false(),
            ).label("unresolved_identity"),
            objects.c.invalid_shape,
        )
        .select_from(objects)
        .outerjoin(per_reference, per_reference.c.object_id == objects.c.object_id)
        .group_by(objects.c.object_id, objects.c.invalid_shape)
        .cte(f"{prefix}_resolution")
    )
    exact_targets = (
        select(
            references.c.object_id,
            exact_owners.c.action_id,
        )
        .select_from(references)
        .join(
            exact_owners,
            and_(
                exact_owners.c.reference_kind == references.c.reference_kind,
                exact_owners.c.reference_id == references.c.reference_id,
            ),
        )
    )
    candidate_targets = (
        select(
            references.c.object_id,
            candidate_owners.c.action_id,
        )
        .select_from(references)
        .join(
            candidate_owners,
            and_(
                candidate_owners.c.reference_kind == references.c.reference_kind,
                candidate_owners.c.reference_id == references.c.reference_id,
            ),
        )
    )
    target_rows = union_all(exact_targets, candidate_targets).cte(f"{prefix}_target_rows")
    targets = (
        select(target_rows.c.object_id, target_rows.c.action_id).distinct().cte(f"{prefix}_targets")
    )
    return resolution, targets


def build_action_finding_query(
    action_ids: Sequence[str],
    *,
    detail: bool = False,
) -> Select[tuple[object, ...]]:
    roots = _selected_action_roots(action_ids, name="finding_selected_roots")
    evidence_is_valid = func.json_valid(FindingRecord.evidence_json) == 1
    validated_evidence = case(
        (evidence_is_valid, FindingRecord.evidence_json),
        else_=literal("[]"),
    )
    evidence_is_array = func.json_type(validated_evidence) == "array"
    evidence_array = case(
        (evidence_is_array, validated_evidence),
        else_=literal("[]"),
    )
    evidence_item = (
        func.json_each(evidence_array)
        .table_valued("key", "value", "type", joins_implicitly=True)
        .alias("finding_evidence_item")
    )
    evidence_object = case(
        (evidence_item.c.type == "object", evidence_item.c.value),
        else_=literal("{}"),
    )
    refs_invalid = or_(
        evidence_is_valid.is_not(true()),
        evidence_is_array.is_not(true()),
        and_(
            evidence_item.c.key.is_not(None),
            evidence_item.c.type.not_in(("null", "object")),
        ),
        _sqlite_json_reference_type_invalid(evidence_object, "$.execution_id"),
        _sqlite_json_reference_type_invalid(evidence_object, "$.artifact_id"),
    )
    expanded = (
        select(
            FindingRecord.id.label("object_id"),
            _sqlite_json_text(
                evidence_object,
                "$.execution_id",
                "execution_reference_id",
            ),
            _sqlite_json_text(
                evidence_object,
                "$.artifact_id",
                "artifact_reference_id",
            ),
            case((refs_invalid, true()), else_=false()).label("invalid_shape"),
        )
        .select_from(FindingRecord)
        .outerjoin(evidence_item, true())
        .where(FindingRecord.run_id.in_(select(roots.c.run_id).distinct()))
        .cte("finding_expanded")
    )
    objects = (
        select(
            expanded.c.object_id,
            func.max(expanded.c.invalid_shape).label("invalid_shape"),
        )
        .group_by(expanded.c.object_id)
        .cte("finding_objects")
    )
    reference_rows = union_all(
        select(
            expanded.c.object_id,
            literal("execution").label("reference_kind"),
            expanded.c.execution_reference_id.label("reference_id"),
        ).where(expanded.c.execution_reference_id.is_not(None)),
        select(
            expanded.c.object_id,
            literal("artifact").label("reference_kind"),
            expanded.c.artifact_reference_id.label("reference_id"),
        ).where(expanded.c.artifact_reference_id.is_not(None)),
    ).cte("finding_reference_rows")
    references = (
        select(
            reference_rows.c.object_id,
            reference_rows.c.reference_kind,
            reference_rows.c.reference_id,
        )
        .distinct()
        .cte("finding_references")
    )
    execution_candidates, execution_exact = _global_execution_ownership(prefix="finding")
    artifact_candidates, artifact_exact = _global_artifact_ownership(
        execution_candidates,
        execution_exact,
        prefix="finding",
    )
    exact_owners = union_all(
        select(
            literal("execution").label("reference_kind"),
            execution_exact.c.execution_id.label("reference_id"),
            execution_exact.c.action_id,
        ),
        select(
            literal("artifact").label("reference_kind"),
            artifact_exact.c.artifact_id.label("reference_id"),
            artifact_exact.c.action_id,
        ),
    ).cte("finding_exact_owners")
    candidate_owner_rows = union_all(
        select(
            literal("execution").label("reference_kind"),
            execution_candidates.c.execution_id.label("reference_id"),
            execution_candidates.c.action_id,
        ),
        select(
            literal("artifact").label("reference_kind"),
            artifact_candidates.c.artifact_id.label("reference_id"),
            artifact_candidates.c.action_id,
        ),
    ).cte("finding_candidate_owner_rows")
    candidate_owners = (
        select(
            candidate_owner_rows.c.reference_kind,
            candidate_owner_rows.c.reference_id,
            candidate_owner_rows.c.action_id,
        )
        .distinct()
        .cte("finding_candidate_owners")
    )
    resolution, targets = _reference_resolution(
        objects,
        references,
        exact_owners,
        candidate_owners,
        prefix="finding",
    )
    is_attached = and_(
        resolution.c.exact_owner_count == 1,
        resolution.c.unresolved_identity.is_(false()),
        resolution.c.exact_owner_id == roots.c.action_id,
        FindingRecord.run_id == roots.c.run_id,
    )
    is_partial = or_(
        resolution.c.invalid_shape.is_(true()),
        resolution.c.unresolved_identity.is_(true()),
        and_(
            resolution.c.exact_owner_id == roots.c.action_id,
            FindingRecord.run_id.is_distinct_from(roots.c.run_id),
        ),
    )
    targeted = (
        select(
            roots.c.action_id,
            FindingRecord.id.label("finding_id"),
            FindingRecord.run_id.label("finding_run_id"),
            FindingRecord.created_at.label("finding_created_at"),
            FindingRecord.updated_at.label("finding_updated_at"),
            case((is_attached, true()), else_=false()).label("is_attached"),
            case((is_partial, true()), else_=false()).label("is_partial"),
        )
        .select_from(roots)
        .join(targets, targets.c.action_id == roots.c.action_id)
        .join(FindingRecord, FindingRecord.id == targets.c.object_id)
        .join(resolution, resolution.c.object_id == targets.c.object_id)
        .cte("finding_targeted")
    )
    attached = select(targeted).where(targeted.c.is_attached.is_(true())).cte("finding_attached")
    exact_summary = (
        select(
            roots.c.action_id,
            func.count(attached.c.finding_id).label("finding_count"),
            func.max(attached.c.finding_created_at).label("finding_max_created_at"),
            func.max(attached.c.finding_updated_at).label("finding_max_updated_at"),
        )
        .select_from(roots)
        .outerjoin(attached, attached.c.action_id == roots.c.action_id)
        .group_by(roots.c.action_id)
        .cte("finding_exact_summary")
    )
    partial_summary = (
        select(
            roots.c.action_id,
            func.max(case((targeted.c.is_partial.is_(true()), true()), else_=false())).label(
                "finding_partial"
            ),
        )
        .select_from(roots)
        .outerjoin(targeted, targeted.c.action_id == roots.c.action_id)
        .group_by(roots.c.action_id)
        .cte("finding_partial_summary")
    )
    summary = (
        select(
            exact_summary.c.action_id,
            exact_summary.c.finding_count,
            exact_summary.c.finding_max_created_at,
            exact_summary.c.finding_max_updated_at,
            partial_summary.c.finding_partial,
        )
        .join(partial_summary, partial_summary.c.action_id == exact_summary.c.action_id)
        .cte("finding_summary")
    )
    if not detail:
        return select(summary).order_by(summary.c.action_id)

    ranked = select(
        attached,
        func.row_number()
        .over(
            partition_by=attached.c.action_id,
            order_by=(attached.c.finding_created_at, attached.c.finding_id),
        )
        .label("finding_rank"),
    ).cte("finding_ranked")
    bounded = select(ranked).where(ranked.c.finding_rank <= 100).cte("finding_bounded")
    return (
        select(
            summary.c.action_id,
            summary.c.finding_count,
            summary.c.finding_max_created_at,
            summary.c.finding_max_updated_at,
            summary.c.finding_partial,
            bounded.c.finding_id,
            bounded.c.finding_run_id,
            bounded.c.finding_created_at,
            bounded.c.finding_updated_at,
        )
        .select_from(summary)
        .outerjoin(bounded, bounded.c.action_id == summary.c.action_id)
        .order_by(
            summary.c.action_id,
            bounded.c.finding_created_at,
            bounded.c.finding_id,
        )
    )


def _event_query(
    action_ids: Sequence[str],
    *,
    detail: bool,
) -> Select[tuple[object, ...]]:
    roots = _selected_action_roots(action_ids, name="event_selected_roots")
    reference_paths = (
        "$.tool_call_intent_id",
        "$.action_id",
        "$.approval_id",
        "$.execution_id",
        "$.artifact_id",
    )
    refs_invalid = or_(
        func.json_valid(RunEventRecord.payload_json) != 1,
        *(
            _sqlite_json_reference_type_invalid(RunEventRecord.payload_json, path)
            for path in reference_paths
        ),
    )
    raw_columns: tuple[object, ...] = (
        RunEventRecord.id.label("object_id"),
        RunEventRecord.run_id.label("event_run_id"),
        RunEventRecord.created_at.label("event_created_at"),
    )
    if detail:
        raw_columns += (
            RunEventRecord.sequence.label("event_sequence"),
            RunEventRecord.event_type.label("event_type"),
        )
    raw = (
        select(
            *raw_columns,
            _sqlite_json_text(
                RunEventRecord.payload_json,
                "$.tool_call_intent_id",
                "intent_reference_id",
            ),
            _sqlite_json_text(
                RunEventRecord.payload_json,
                "$.action_id",
                "action_reference_id",
            ),
            _sqlite_json_text(
                RunEventRecord.payload_json,
                "$.approval_id",
                "approval_reference_id",
            ),
            _sqlite_json_text(
                RunEventRecord.payload_json,
                "$.execution_id",
                "execution_reference_id",
            ),
            _sqlite_json_text(
                RunEventRecord.payload_json,
                "$.artifact_id",
                "artifact_reference_id",
            ),
            case((refs_invalid, true()), else_=false()).label("invalid_shape"),
        )
        .where(RunEventRecord.run_id.in_(select(roots.c.run_id).distinct()))
        .cte("event_raw")
    )
    objects = select(raw.c.object_id, raw.c.invalid_shape).cte("event_objects")
    reference_rows = union_all(
        select(
            raw.c.object_id,
            literal("intent").label("reference_kind"),
            raw.c.intent_reference_id.label("reference_id"),
        ).where(raw.c.intent_reference_id.is_not(None)),
        select(
            raw.c.object_id,
            literal("action").label("reference_kind"),
            raw.c.action_reference_id.label("reference_id"),
        ).where(raw.c.action_reference_id.is_not(None)),
        select(
            raw.c.object_id,
            literal("approval").label("reference_kind"),
            raw.c.approval_reference_id.label("reference_id"),
        ).where(raw.c.approval_reference_id.is_not(None)),
        select(
            raw.c.object_id,
            literal("execution").label("reference_kind"),
            raw.c.execution_reference_id.label("reference_id"),
        ).where(raw.c.execution_reference_id.is_not(None)),
        select(
            raw.c.object_id,
            literal("artifact").label("reference_kind"),
            raw.c.artifact_reference_id.label("reference_id"),
        ).where(raw.c.artifact_reference_id.is_not(None)),
    ).cte("event_reference_rows")
    references = (
        select(
            reference_rows.c.object_id,
            reference_rows.c.reference_kind,
            reference_rows.c.reference_id,
        )
        .distinct()
        .cte("event_references")
    )
    execution_candidates, execution_exact = _global_execution_ownership(prefix="event")
    artifact_candidates, artifact_exact = _global_artifact_ownership(
        execution_candidates,
        execution_exact,
        prefix="event",
    )
    approval_candidates, approval_exact = _global_approval_ownership(prefix="event")
    exact_owner_rows = union_all(
        select(
            literal("intent").label("reference_kind"),
            ToolCallIntentRecord.id.label("reference_id"),
            ToolCallIntentRecord.id.label("action_id"),
        ),
        select(
            literal("action").label("reference_kind"),
            ToolCallIntentRecord.id.label("reference_id"),
            ToolCallIntentRecord.id.label("action_id"),
        ),
        select(
            literal("approval").label("reference_kind"),
            approval_exact.c.approval_id.label("reference_id"),
            approval_exact.c.action_id,
        ),
        select(
            literal("execution").label("reference_kind"),
            execution_exact.c.execution_id.label("reference_id"),
            execution_exact.c.action_id,
        ),
        select(
            literal("artifact").label("reference_kind"),
            artifact_exact.c.artifact_id.label("reference_id"),
            artifact_exact.c.action_id,
        ),
    ).cte("event_exact_owner_rows")
    exact_owners = (
        select(
            exact_owner_rows.c.reference_kind,
            exact_owner_rows.c.reference_id,
            exact_owner_rows.c.action_id,
        )
        .distinct()
        .cte("event_exact_owners")
    )
    candidate_owner_rows = union_all(
        select(
            literal("intent").label("reference_kind"),
            ToolCallIntentRecord.id.label("reference_id"),
            ToolCallIntentRecord.id.label("action_id"),
        ),
        select(
            literal("action").label("reference_kind"),
            ToolCallIntentRecord.id.label("reference_id"),
            ToolCallIntentRecord.id.label("action_id"),
        ),
        select(
            literal("approval").label("reference_kind"),
            approval_candidates.c.approval_id.label("reference_id"),
            approval_candidates.c.action_id,
        ),
        select(
            literal("execution").label("reference_kind"),
            execution_candidates.c.execution_id.label("reference_id"),
            execution_candidates.c.action_id,
        ),
        select(
            literal("artifact").label("reference_kind"),
            artifact_candidates.c.artifact_id.label("reference_id"),
            artifact_candidates.c.action_id,
        ),
    ).cte("event_candidate_owner_rows")
    candidate_owners = (
        select(
            candidate_owner_rows.c.reference_kind,
            candidate_owner_rows.c.reference_id,
            candidate_owner_rows.c.action_id,
        )
        .distinct()
        .cte("event_candidate_owners")
    )
    resolution, targets = _reference_resolution(
        objects,
        references,
        exact_owners,
        candidate_owners,
        prefix="event",
    )
    is_attached = and_(
        resolution.c.exact_owner_count == 1,
        resolution.c.unresolved_identity.is_(false()),
        resolution.c.exact_owner_id == roots.c.action_id,
        raw.c.event_run_id == roots.c.run_id,
    )
    is_partial = or_(
        resolution.c.invalid_shape.is_(true()),
        resolution.c.unresolved_identity.is_(true()),
        and_(
            resolution.c.exact_owner_id == roots.c.action_id,
            raw.c.event_run_id.is_distinct_from(roots.c.run_id),
        ),
    )
    targeted_columns: tuple[object, ...] = (
        roots.c.action_id,
        raw.c.object_id.label("event_id"),
        raw.c.event_run_id,
        raw.c.event_created_at,
    )
    if detail:
        targeted_columns += (raw.c.event_sequence, raw.c.event_type)
    targeted = (
        select(
            *targeted_columns,
            case((is_attached, true()), else_=false()).label("is_attached"),
            case((is_partial, true()), else_=false()).label("is_partial"),
        )
        .select_from(roots)
        .join(targets, targets.c.action_id == roots.c.action_id)
        .join(raw, raw.c.object_id == targets.c.object_id)
        .join(resolution, resolution.c.object_id == targets.c.object_id)
        .cte("event_targeted")
    )
    attached = select(targeted).where(targeted.c.is_attached.is_(true())).cte("event_attached")
    exact_summary = (
        select(
            roots.c.action_id,
            func.count(attached.c.event_id).label("event_count"),
            func.max(attached.c.event_created_at).label("event_max_created_at"),
        )
        .select_from(roots)
        .outerjoin(attached, attached.c.action_id == roots.c.action_id)
        .group_by(roots.c.action_id)
        .cte("event_exact_summary")
    )
    partial_summary = (
        select(
            roots.c.action_id,
            func.max(case((targeted.c.is_partial.is_(true()), true()), else_=false())).label(
                "event_partial"
            ),
        )
        .select_from(roots)
        .outerjoin(targeted, targeted.c.action_id == roots.c.action_id)
        .group_by(roots.c.action_id)
        .cte("event_partial_summary")
    )
    summary = (
        select(
            exact_summary.c.action_id,
            exact_summary.c.event_count,
            exact_summary.c.event_max_created_at,
            partial_summary.c.event_partial,
        )
        .join(partial_summary, partial_summary.c.action_id == exact_summary.c.action_id)
        .cte("event_summary")
    )
    if not detail:
        return select(summary).order_by(summary.c.action_id)

    ranked = select(
        attached,
        func.row_number()
        .over(
            partition_by=attached.c.action_id,
            order_by=(attached.c.event_sequence, attached.c.event_id),
        )
        .label("event_rank"),
    ).cte("event_ranked")
    bounded = select(ranked).where(ranked.c.event_rank <= 200).cte("event_bounded")
    return (
        select(
            summary.c.action_id,
            summary.c.event_count,
            summary.c.event_max_created_at,
            summary.c.event_partial,
            bounded.c.event_id,
            bounded.c.event_run_id,
            bounded.c.event_created_at,
            bounded.c.event_sequence,
            bounded.c.event_type,
        )
        .select_from(summary)
        .outerjoin(bounded, bounded.c.action_id == summary.c.action_id)
        .order_by(summary.c.action_id, bounded.c.event_sequence, bounded.c.event_id)
    )


def build_action_list_event_query(action_ids: Sequence[str]) -> Select[tuple[object, ...]]:
    return _event_query(action_ids, detail=False)


def build_action_detail_event_query(action_ids: Sequence[str]) -> Select[tuple[object, ...]]:
    return _event_query(action_ids, detail=True)


def _sqlite_json_text(document: object, path: str, label: str) -> object:
    """Extract one JSON string without returning its opaque sibling data."""

    valid_document = case(
        (func.json_valid(document) == 1, document),
        else_=literal("{}"),
    )
    return case(
        (
            func.json_type(valid_document, path) == "text",
            func.json_extract(valid_document, path),
        ),
        else_=None,
    ).label(label)


def _sqlite_json_reference_type_invalid(document: object, path: str) -> object:
    """Return only whether a present reference has an unsafe JSON type."""

    valid_document = case(
        (func.json_valid(document) == 1, document),
        else_=literal("{}"),
    )
    reference_type = func.json_type(valid_document, path)
    return and_(
        reference_type.is_not(None),
        reference_type.not_in(("null", "text")),
    )


__all__ = [
    "DETAIL_SELECTED_COLUMN_KEYS",
    "LIST_SELECTED_COLUMN_KEYS",
    "build_action_artifact_query",
    "build_action_detail_approval_query",
    "build_action_detail_event_query",
    "build_action_detail_execution_query",
    "build_action_detail_root_query",
    "build_action_finding_query",
    "build_action_list_approval_query",
    "build_action_list_event_query",
    "build_action_list_execution_query",
    "build_action_list_root_query",
]
