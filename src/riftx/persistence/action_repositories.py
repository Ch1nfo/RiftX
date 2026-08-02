"""SQLAlchemy implementation of the durable, read-only Action projection."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import select
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncSession

from riftx.application.actions import (
    ActionAggregateRead,
    ActionApprovalRead,
    ActionCorrelationQuality,
    ActionCoverage,
    ActionEventRead,
    ActionExecutionRead,
    ActionIntentRead,
    ActionListAggregateRead,
    ActionListApprovalRead,
    ActionListExecutionRead,
    ActionListIntentRead,
    ActionListResultRead,
    ActionPageKey,
    ActionPartialReason,
    ActionReadPage,
    ActionResultRead,
)
from riftx.domain import ApprovalLevel, ApprovalStatus, ExecutionStatus
from riftx.runtime.types import ToolCallStatus

from .action_read_queries import (
    build_action_artifact_query,
    build_action_detail_approval_query,
    build_action_detail_event_query,
    build_action_detail_execution_query,
    build_action_detail_root_query,
    build_action_finding_query,
    build_action_list_approval_query,
    build_action_list_event_query,
    build_action_list_execution_query,
    build_action_list_root_query,
)
from .orm import RunRecord, ToolCallIntentRecord
from .repositories import SessionFactory

_EXECUTION_LIMIT = 100
_ARTIFACT_LIMIT = 100
_FINDING_LIMIT = 100
_EVENT_LIMIT = 200


@dataclass(frozen=True, slots=True)
class _RootRow:
    action_id: str
    run_id: str
    session_id: str
    cycle_id: str
    step_id: str
    engine_call_id: str | None
    tool_id: str | None
    skill_id: str | None
    reason: str
    target_summary: str | None
    approval_level: object
    status: object
    claimed_execution_key: str | None
    claimed_attempt_group: str | None
    created_at: datetime
    updated_at: datetime
    arguments: dict[str, object] | None


@dataclass(frozen=True, slots=True)
class _ApprovalRow:
    action_id: str
    runtime_id: str
    runtime_run_id: str
    runtime_session_id: str
    runtime_cycle_id: str
    runtime_status: object | None
    runtime_decided_by: str | None
    runtime_created_at: datetime
    runtime_decided_at: datetime | None
    public_id: str | None
    public_run_id: str | None
    public_status: object | None
    public_decided_by: str | None
    public_created_at: datetime | None
    public_decided_at: datetime | None
    runtime_feedback: str | None


@dataclass(frozen=True, slots=True)
class _ExecutionRow:
    execution_id: str
    attempt_group: str | None
    status: object
    exit_code: int | None
    created_at: datetime | None
    started_at: datetime | None
    finished_at: datetime | None
    physical_stop_confirmed_at: datetime | None
    node_id: str | None


@dataclass(frozen=True, slots=True)
class _ExecutionSummary:
    count: int
    current_match_count: int
    current_id: str | None
    max_created_at: datetime | None
    max_started_at: datetime | None
    max_finished_at: datetime | None
    max_physical_stop_confirmed_at: datetime | None
    max_updated_at: datetime | None
    scope_mismatch: bool
    session_mismatch: bool


@dataclass(frozen=True, slots=True)
class _ArtifactRow:
    artifact_id: str
    run_id: str
    execution_id: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class _ArtifactSummary:
    count: int
    max_created_at: datetime | None
    scope_mismatch: bool


@dataclass(frozen=True, slots=True)
class _FindingRow:
    finding_id: str
    run_id: str
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class _FindingSummary:
    count: int
    max_created_at: datetime | None
    max_updated_at: datetime | None
    partial: bool


@dataclass(frozen=True, slots=True)
class _EventRow:
    event_id: str
    run_id: str
    created_at: datetime
    sequence: int
    event_type: str


@dataclass(frozen=True, slots=True)
class _EventSummary:
    count: int
    max_created_at: datetime | None
    partial: bool


@dataclass(slots=True)
class _ActionState:
    root: _RootRow
    reasons: list[ActionPartialReason] = field(default_factory=list)
    clocks: list[datetime] = field(default_factory=list)
    approval_count: int = 0
    approval_rows: list[_ApprovalRow] = field(default_factory=list)
    execution_rows: list[_ExecutionRow] = field(default_factory=list)
    execution_summary: _ExecutionSummary | None = None
    current_execution_id: str | None = None
    execution_quality: ActionCorrelationQuality = ActionCorrelationQuality.EXACT
    artifact_rows: list[_ArtifactRow] = field(default_factory=list)
    artifact_summary: _ArtifactSummary | None = None
    finding_rows: list[_FindingRow] = field(default_factory=list)
    finding_summary: _FindingSummary | None = None
    event_rows: list[_EventRow] = field(default_factory=list)
    event_summary: _EventSummary | None = None

    def __post_init__(self) -> None:
        self.clocks.extend((self.root.created_at, self.root.updated_at))


class SQLAlchemyActionReadRepository:
    """Hydrate Actions in six bounded-shape SELECT phases per nonempty call."""

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    async def resolve_run(self, run_id: str) -> str | None:
        async with self._session_factory() as session:
            return await session.scalar(select(RunRecord.id).where(RunRecord.id == run_id))

    async def resolve_action_run(self, run_id: str, action_id: str) -> str | None:
        del run_id
        async with self._session_factory() as session:
            return await session.scalar(
                select(ToolCallIntentRecord.run_id).where(ToolCallIntentRecord.id == action_id)
            )

    async def list_page(
        self,
        run_id: str,
        *,
        limit: int,
        after: ActionPageKey | None,
        snapshot: ActionPageKey | None,
    ) -> ActionReadPage:
        async with self._session_factory() as session, session.begin():
            _require_sqlite_json_projection(session)
            root_mappings = await _rows(
                session,
                build_action_list_root_query(
                    run_id,
                    limit=limit,
                    after=after,
                    snapshot=snapshot,
                ).execution_options(riftx_action_phase="root"),
            )
            roots = tuple(_root_from_mapping(row, detail=False) for row in root_mappings)
            if not roots:
                return ActionReadPage(items=(), has_more=False, snapshot=snapshot)
            states = await _hydrate_states(session, roots, detail=False)

        page_snapshot = snapshot or ActionPageKey(roots[0].created_at, roots[0].action_id)
        return ActionReadPage(
            items=tuple(_assemble_list(state) for state in states),
            has_more=len(roots) > limit,
            snapshot=page_snapshot,
        )

    async def get(self, run_id: str, action_id: str) -> ActionAggregateRead | None:
        async with self._session_factory() as session, session.begin():
            _require_sqlite_json_projection(session)
            root_mappings = await _rows(
                session,
                build_action_detail_root_query(run_id, action_id).execution_options(
                    riftx_action_phase="root"
                ),
            )
            roots = tuple(_root_from_mapping(row, detail=True) for row in root_mappings)
            if not roots:
                return None
            states = await _hydrate_states(session, roots, detail=True)
        return _assemble_detail(states[0])


async def _hydrate_states(
    session: AsyncSession,
    roots: Sequence[_RootRow],
    *,
    detail: bool,
) -> tuple[_ActionState, ...]:
    """Run the five child phases after the caller's root SELECT."""

    action_ids = tuple(root.action_id for root in roots)
    claimed_keys = tuple(
        root.claimed_execution_key for root in roots if root.claimed_execution_key is not None
    )
    states = {root.action_id: _ActionState(root) for root in roots}

    approval_statement = (
        build_action_detail_approval_query(action_ids)
        if detail
        else build_action_list_approval_query(action_ids)
    ).execution_options(riftx_action_phase="approval")
    for mapping in await _rows(session, approval_statement):
        state = _state_for_mapping(states, mapping)
        approval_count = _required_nonnegative_int(mapping["approval_count"])
        if state.approval_rows or state.approval_count:
            raise ValueError("Action approval projection returned duplicate summary rows")
        state.approval_count = approval_count
        state.approval_rows.append(_approval_from_mapping(mapping, detail=detail))

    execution_statement = (
        build_action_detail_execution_query(action_ids, claimed_keys)
        if detail
        else build_action_list_execution_query(action_ids, claimed_keys)
    ).execution_options(riftx_action_phase="execution")
    for mapping in await _rows(session, execution_statement):
        state = _state_for_mapping(states, mapping)
        summary = _execution_summary_from_mapping(mapping)
        if state.execution_summary is None:
            state.execution_summary = summary
        elif state.execution_summary != summary:
            raise ValueError("Action execution projection returned inconsistent summaries")
        row = _execution_from_mapping(mapping, detail=detail)
        if row is not None:
            state.execution_rows.append(row)

    artifact_statement = build_action_artifact_query(action_ids).execution_options(
        riftx_action_phase="artifact"
    )
    for mapping in await _rows(session, artifact_statement):
        state = _state_for_mapping(states, mapping)
        summary = _artifact_summary_from_mapping(mapping)
        if state.artifact_summary is None:
            state.artifact_summary = summary
        elif state.artifact_summary != summary:
            raise ValueError("Action artifact projection returned inconsistent summaries")
        row = _artifact_from_mapping(mapping)
        if row is not None:
            state.artifact_rows.append(row)

    finding_statement = build_action_finding_query(
        action_ids,
        detail=detail,
    ).execution_options(riftx_action_phase="finding")
    for mapping in await _rows(session, finding_statement):
        state = _state_for_mapping(states, mapping)
        summary = _finding_summary_from_mapping(mapping)
        if state.finding_summary is None:
            state.finding_summary = summary
        elif state.finding_summary != summary:
            raise ValueError("Action finding projection returned inconsistent summaries")
        if detail:
            row = _finding_from_mapping(mapping)
            if row is not None:
                state.finding_rows.append(row)

    event_statement = (
        build_action_detail_event_query(action_ids)
        if detail
        else build_action_list_event_query(action_ids)
    ).execution_options(riftx_action_phase="event")
    for mapping in await _rows(session, event_statement):
        state = _state_for_mapping(states, mapping)
        summary = _event_summary_from_mapping(mapping)
        if state.event_summary is None:
            state.event_summary = summary
        elif state.event_summary != summary:
            raise ValueError("Action event projection returned inconsistent summaries")
        if detail:
            row = _event_from_mapping(mapping)
            if row is not None:
                state.event_rows.append(row)

    for state in states.values():
        _finalize_bounded_state(state, detail=detail)
    return tuple(states[root.action_id] for root in roots)


def _state_for_mapping(
    states: Mapping[str, _ActionState],
    mapping: RowMapping,
) -> _ActionState:
    action_id = _required_str(mapping["action_id"])
    try:
        return states[action_id]
    except KeyError:
        raise ValueError("Action child projection returned an unselected owner") from None


def _finalize_bounded_state(state: _ActionState, *, detail: bool) -> None:
    execution = state.execution_summary
    artifact = state.artifact_summary
    finding = state.finding_summary
    event = state.event_summary
    if execution is None or artifact is None or finding is None or event is None:
        raise ValueError("Action child projection omitted a selected root summary")

    _require_bounded_unique_rows(
        state.execution_rows,
        count=execution.count,
        limit=_EXECUTION_LIMIT,
        expected=min(execution.count, _EXECUTION_LIMIT),
        identity=lambda row: row.execution_id,
    )
    _require_bounded_unique_rows(
        state.artifact_rows,
        count=artifact.count,
        limit=_ARTIFACT_LIMIT,
        expected=min(artifact.count, _ARTIFACT_LIMIT),
        identity=lambda row: row.artifact_id,
    )
    _require_bounded_unique_rows(
        state.finding_rows,
        count=finding.count,
        limit=_FINDING_LIMIT,
        expected=min(finding.count, _FINDING_LIMIT) if detail else 0,
        identity=lambda row: row.finding_id,
    )
    _require_bounded_unique_rows(
        state.event_rows,
        count=event.count,
        limit=_EVENT_LIMIT,
        expected=min(event.count, _EVENT_LIMIT) if detail else 0,
        identity=lambda row: row.event_id,
    )

    state.execution_rows.sort(key=_attempt_display_key)
    claim_key = state.root.claimed_execution_key
    claim_group = state.root.claimed_attempt_group
    claim_complete = claim_key is not None and claim_group is not None
    claim_absent = claim_key is None and claim_group is None
    if state.execution_rows and not claim_complete:
        state.execution_quality = ActionCorrelationQuality.LEGACY
    returned_execution_ids = {row.execution_id for row in state.execution_rows}
    if claim_complete:
        if (
            execution.current_match_count == 1
            and execution.current_id is not None
            and execution.current_id in returned_execution_ids
        ):
            state.current_execution_id = execution.current_id
        else:
            state.reasons.append(ActionPartialReason.EXECUTION_CURRENT_CORRELATION_PARTIAL)
    elif not claim_absent or execution.count > 1:
        state.reasons.append(ActionPartialReason.EXECUTION_CURRENT_CORRELATION_PARTIAL)
    if execution.scope_mismatch:
        state.reasons.append(ActionPartialReason.EXECUTION_SCOPE_MISMATCH)
    if execution.session_mismatch:
        state.reasons.append(ActionPartialReason.EXECUTION_SESSION_MISMATCH)
    if len(state.execution_rows) < execution.count:
        state.reasons.append(ActionPartialReason.EXECUTION_ATTEMPTS_TRUNCATED)
    if state.reasons:
        state.execution_quality = ActionCorrelationQuality.PARTIAL
    state.clocks.extend(
        _present_datetimes(
            execution.max_created_at,
            execution.max_started_at,
            execution.max_finished_at,
            execution.max_physical_stop_confirmed_at,
            execution.max_updated_at,
        )
    )

    state.artifact_rows.sort(key=lambda row: (row.created_at, row.artifact_id))
    if artifact.scope_mismatch:
        state.reasons.append(ActionPartialReason.ARTIFACT_SCOPE_MISMATCH)
    state.clocks.extend(_present_datetimes(artifact.max_created_at))

    state.finding_rows.sort(key=lambda row: (row.created_at, row.finding_id))
    if finding.partial:
        state.reasons.append(ActionPartialReason.FINDING_EVIDENCE_UNRESOLVED)
    state.clocks.extend(_present_datetimes(finding.max_created_at, finding.max_updated_at))

    state.event_rows.sort(key=lambda row: (row.sequence, row.event_id))
    if event.partial:
        state.reasons.append(ActionPartialReason.EVENT_CORRELATION_PARTIAL)
    state.clocks.extend(_present_datetimes(event.max_created_at))


def _require_bounded_unique_rows[RowT](
    rows: Sequence[RowT],
    *,
    count: int,
    limit: int,
    expected: int,
    identity: Any,
) -> None:
    identities = tuple(identity(row) for row in rows)
    if (
        count < len(rows)
        or len(rows) > limit
        or len(rows) != expected
        or len(set(identities)) != len(identities)
    ):
        raise ValueError("Action child projection violated its bounded row contract")


def _present_datetimes(*values: datetime | None) -> tuple[datetime, ...]:
    return tuple(value for value in values if value is not None)


def _assemble_list(state: _ActionState) -> ActionListAggregateRead:
    execution_summary, artifact_summary, finding_summary, event_summary = _summaries(state)
    approval, approval_quality, approval_reasons = _list_approval(state)
    reasons = _aggregate_reasons(state, approval_reasons)
    quality = _aggregate_quality(state, approval_quality, reasons)
    artifacts = _sorted_artifacts(state)
    executions = tuple(_list_execution(state, row) for row in state.execution_rows)
    return ActionListAggregateRead(
        intent=ActionListIntentRead(
            action_id=state.root.action_id,
            run_id=state.root.run_id,
            session_id=state.root.session_id,
            cycle_id=state.root.cycle_id,
            step_id=state.root.step_id,
            engine_call_id=state.root.engine_call_id,
            tool_id=state.root.tool_id,
            skill_id=state.root.skill_id,
            reason=state.root.reason,
            target_summary=state.root.target_summary,
            approval_level=_enum_or_raw(state.root.approval_level, ApprovalLevel),
            status=_enum_or_raw(state.root.status, ToolCallStatus),
            created_at=state.root.created_at,
        ),
        approval=approval,
        executions=executions,
        current_execution_id=state.current_execution_id,
        execution_count=execution_summary.count,
        execution_coverage=_coverage(len(executions), execution_summary.count, _EXECUTION_LIMIT),
        result=ActionListResultRead(
            artifact_ids=tuple(row.artifact_id for row in artifacts),
            artifact_count=artifact_summary.count,
            output_size=0,
            output_available=False,
            artifacts_truncated=len(artifacts) < artifact_summary.count,
        ),
        finding_count=finding_summary.count,
        event_count=event_summary.count,
        finding_coverage=_summary_coverage(finding_summary.count, _FINDING_LIMIT),
        event_coverage=_summary_coverage(event_summary.count, _EVENT_LIMIT),
        updated_at=_high_water(state),
        correlation_quality=quality,
        partial_reasons=reasons,
    )


def _assemble_detail(state: _ActionState) -> ActionAggregateRead:
    execution_summary, artifact_summary, finding_summary, event_summary = _summaries(state)
    approval, approval_quality, approval_reasons = _detail_approval(state)
    reasons = _aggregate_reasons(state, approval_reasons)
    quality = _aggregate_quality(state, approval_quality, reasons)
    artifacts = _sorted_artifacts(state)
    findings = _sorted_findings(state)
    events = _sorted_events(state)
    executions = tuple(_detail_execution(state, row) for row in state.execution_rows)
    return ActionAggregateRead(
        intent=ActionIntentRead(
            action_id=state.root.action_id,
            run_id=state.root.run_id,
            session_id=state.root.session_id,
            cycle_id=state.root.cycle_id,
            step_id=state.root.step_id,
            engine_call_id=state.root.engine_call_id,
            tool_id=state.root.tool_id,
            skill_id=state.root.skill_id,
            reason=state.root.reason,
            target_summary=state.root.target_summary,
            approval_level=_enum_or_raw(state.root.approval_level, ApprovalLevel),
            status=_enum_or_raw(state.root.status, ToolCallStatus),
            arguments=state.root.arguments or {},
            created_at=state.root.created_at,
        ),
        approval=approval,
        executions=executions,
        current_execution_id=state.current_execution_id,
        execution_count=execution_summary.count,
        execution_coverage=_coverage(len(executions), execution_summary.count, _EXECUTION_LIMIT),
        result=ActionResultRead(
            artifact_ids=tuple(row.artifact_id for row in artifacts),
            artifact_count=artifact_summary.count,
            output_size=0,
            output_available=False,
            artifacts_truncated=len(artifacts) < artifact_summary.count,
        ),
        finding_ids=tuple(row.finding_id for row in findings),
        finding_count=finding_summary.count,
        events=tuple(
            ActionEventRead(
                event_id=row.event_id,
                sequence=row.sequence,
                event_type=row.event_type,
                created_at=row.created_at,
            )
            for row in events
        ),
        event_count=event_summary.count,
        finding_coverage=_coverage(len(findings), finding_summary.count, _FINDING_LIMIT),
        event_coverage=_coverage(len(events), event_summary.count, _EVENT_LIMIT),
        correlation_quality=quality,
        partial_reasons=reasons,
        updated_at=_high_water(state),
    )


def _list_approval(
    state: _ActionState,
) -> tuple[
    ActionListApprovalRead | None,
    ActionCorrelationQuality,
    tuple[ActionPartialReason, ...],
]:
    row, quality, reasons = _approval_data(state)
    if row is None:
        return None, quality, reasons
    return (
        ActionListApprovalRead(
            approval_id=row.runtime_id,
            runtime_status=_enum_or_raw(row.runtime_status, ApprovalStatus),
            public_status=_enum_or_raw(row.public_status, ApprovalStatus),
            runtime_decided_by=row.runtime_decided_by,
            public_decided_by=row.public_decided_by,
            runtime_decided_at=row.runtime_decided_at,
            public_decided_at=row.public_decided_at,
            bridge_correlation_quality=quality,
            bridge_partial_reasons=reasons,
        ),
        quality,
        reasons,
    )


def _detail_approval(
    state: _ActionState,
) -> tuple[
    ActionApprovalRead | None,
    ActionCorrelationQuality,
    tuple[ActionPartialReason, ...],
]:
    row, quality, reasons = _approval_data(state)
    if row is None:
        return None, quality, reasons
    return (
        ActionApprovalRead(
            approval_id=row.runtime_id,
            runtime_status=_enum_or_raw(row.runtime_status, ApprovalStatus),
            public_status=_enum_or_raw(row.public_status, ApprovalStatus),
            runtime_decided_by=row.runtime_decided_by,
            public_decided_by=row.public_decided_by,
            runtime_decided_at=row.runtime_decided_at,
            public_decided_at=row.public_decided_at,
            feedback=row.runtime_feedback,
            bridge_correlation_quality=quality,
            bridge_partial_reasons=reasons,
        ),
        quality,
        reasons,
    )


def _approval_data(
    state: _ActionState,
) -> tuple[
    _ApprovalRow | None,
    ActionCorrelationQuality,
    tuple[ActionPartialReason, ...],
]:
    if not state.approval_rows:
        status = _enum_or_raw(state.root.status, ToolCallStatus)
        reasons: tuple[ActionPartialReason, ...] = ()
        if status in {ToolCallStatus.WAITING_APPROVAL, ToolCallStatus.REJECTED}:
            reasons = (
                ActionPartialReason.APPROVAL_RUNTIME_MISSING,
                ActionPartialReason.APPROVAL_PUBLIC_MISSING,
            )
        return (
            None,
            ActionCorrelationQuality.PARTIAL if reasons else ActionCorrelationQuality.EXACT,
            reasons,
        )

    row = state.approval_rows[0]
    reasons: list[ActionPartialReason] = []
    single = state.approval_count == 1
    if not single:
        reasons.append(ActionPartialReason.APPROVAL_SHARED_ID_MISMATCH)
    runtime_valid = single and _runtime_approval_scope_valid(state, row)
    if not runtime_valid and single:
        reasons.append(ActionPartialReason.APPROVAL_SCOPE_MISMATCH)
    if row.public_id is None:
        reasons.append(ActionPartialReason.APPROVAL_PUBLIC_MISSING)
    else:
        if row.public_id != row.runtime_id:
            reasons.append(ActionPartialReason.APPROVAL_SHARED_ID_MISMATCH)
        if row.public_run_id != state.root.run_id:
            reasons.append(ActionPartialReason.APPROVAL_SCOPE_MISMATCH)
    public_valid = single and runtime_valid and _public_bridge_scope_valid(state, row)

    if runtime_valid:
        state.clocks.extend(_runtime_approval_clocks(row))
    if public_valid:
        state.clocks.extend(_public_approval_clocks(row))
    safe_row = replace(
        row,
        runtime_status=row.runtime_status if runtime_valid else None,
        runtime_decided_by=row.runtime_decided_by if runtime_valid else None,
        runtime_decided_at=row.runtime_decided_at if runtime_valid else None,
        runtime_feedback=row.runtime_feedback if runtime_valid else None,
        public_status=row.public_status if public_valid else None,
        public_decided_by=row.public_decided_by if public_valid else None,
        public_decided_at=row.public_decided_at if public_valid else None,
    )
    deduplicated = _dedupe(reasons)
    quality = ActionCorrelationQuality.PARTIAL if deduplicated else ActionCorrelationQuality.EXACT
    return safe_row, quality, deduplicated


def _runtime_approval_scope_valid(state: _ActionState, row: _ApprovalRow) -> bool:
    return (
        row.runtime_run_id == state.root.run_id
        and row.runtime_session_id == state.root.session_id
        and row.runtime_cycle_id == state.root.cycle_id
    )


def _public_bridge_scope_valid(state: _ActionState, row: _ApprovalRow) -> bool:
    return row.public_id == row.runtime_id and row.public_run_id == state.root.run_id


def _list_execution(state: _ActionState, row: _ExecutionRow) -> ActionListExecutionRead:
    return ActionListExecutionRead(
        execution_id=row.execution_id,
        attempt_group=row.attempt_group,
        node_id=row.node_id or "",
        status=_enum_or_raw(row.status, ExecutionStatus),
        created_at=row.created_at,
        started_at=row.started_at,
        finished_at=row.finished_at,
        exit_code=row.exit_code,
        correlation_quality=_execution_row_quality(state),
        physical_stop_confirmed_at=row.physical_stop_confirmed_at,
    )


def _detail_execution(state: _ActionState, row: _ExecutionRow) -> ActionExecutionRead:
    return ActionExecutionRead(
        execution_id=row.execution_id,
        attempt_group=row.attempt_group,
        node_id=row.node_id or "",
        status=_enum_or_raw(row.status, ExecutionStatus),
        created_at=row.created_at,
        started_at=row.started_at,
        finished_at=row.finished_at,
        exit_code=row.exit_code,
        correlation_quality=_execution_row_quality(state),
        error_summary=None,
        physical_stop_confirmed_at=row.physical_stop_confirmed_at,
    )


def _aggregate_reasons(
    state: _ActionState,
    approval_reasons: Sequence[ActionPartialReason],
) -> tuple[ActionPartialReason, ...]:
    return _dedupe((*state.reasons, *approval_reasons))


def _aggregate_quality(
    state: _ActionState,
    approval_quality: ActionCorrelationQuality,
    reasons: Sequence[ActionPartialReason],
) -> ActionCorrelationQuality:
    if reasons or approval_quality is ActionCorrelationQuality.PARTIAL:
        return ActionCorrelationQuality.PARTIAL
    if (
        state.execution_quality is ActionCorrelationQuality.LEGACY
        or approval_quality is ActionCorrelationQuality.LEGACY
    ):
        return ActionCorrelationQuality.LEGACY
    return ActionCorrelationQuality.EXACT


def _coverage(scanned: int, count: int, limit: int) -> ActionCoverage:
    return ActionCoverage(scanned=scanned, limit=limit, truncated=scanned < count)


def _summary_coverage(count: int, limit: int) -> ActionCoverage:
    scanned = min(count, limit)
    return _coverage(scanned, count, limit)


def _summaries(
    state: _ActionState,
) -> tuple[_ExecutionSummary, _ArtifactSummary, _FindingSummary, _EventSummary]:
    values = (
        state.execution_summary,
        state.artifact_summary,
        state.finding_summary,
        state.event_summary,
    )
    if any(value is None for value in values):
        raise ValueError("Action child projection summary is unavailable")
    execution, artifact, finding, event = values
    assert execution is not None
    assert artifact is not None
    assert finding is not None
    assert event is not None
    return execution, artifact, finding, event


def _execution_row_quality(state: _ActionState) -> ActionCorrelationQuality:
    if (
        state.root.claimed_execution_key is not None
        and state.root.claimed_attempt_group is not None
    ):
        return ActionCorrelationQuality.EXACT
    return ActionCorrelationQuality.LEGACY


def _sorted_artifacts(state: _ActionState) -> tuple[_ArtifactRow, ...]:
    return tuple(sorted(state.artifact_rows, key=lambda row: (row.created_at, row.artifact_id)))


def _sorted_findings(state: _ActionState) -> tuple[_FindingRow, ...]:
    unique = {row.finding_id: row for row in state.finding_rows}
    return tuple(sorted(unique.values(), key=lambda row: (row.created_at, row.finding_id)))


def _sorted_events(state: _ActionState) -> tuple[_EventRow, ...]:
    unique = {row.event_id: row for row in state.event_rows}
    return tuple(sorted(unique.values(), key=lambda row: (row.sequence, row.event_id)))


def _high_water(state: _ActionState) -> datetime:
    aware = tuple(clock.astimezone(UTC) for clock in state.clocks if _is_aware(clock))
    return max(aware, default=state.root.updated_at)


def _attempt_display_key(row: _ExecutionRow) -> tuple[bool, datetime, str]:
    created_at = _aware_utc(row.created_at)
    return (
        created_at is None,
        created_at or datetime.max.replace(tzinfo=UTC),
        row.execution_id,
    )


def _aware_utc(value: datetime | None) -> datetime | None:
    if value is None or not _is_aware(value):
        return None
    return value.astimezone(UTC)


def _is_aware(value: datetime) -> bool:
    try:
        return value.tzinfo is not None and value.utcoffset() is not None
    except (AttributeError, OverflowError, ValueError):
        return False


def _enum_or_raw[EnumT: StrEnum](
    value: object,
    enum_type: type[EnumT],
) -> EnumT | str | None:
    if isinstance(value, enum_type):
        return value
    if not isinstance(value, str):
        return None
    try:
        return enum_type(value)
    except ValueError:
        return value


def _dedupe(values: Sequence[ActionPartialReason]) -> tuple[ActionPartialReason, ...]:
    return tuple(dict.fromkeys(values))


def _runtime_approval_clocks(row: _ApprovalRow) -> tuple[datetime, ...]:
    return tuple(
        value
        for value in (
            row.runtime_created_at,
            row.runtime_decided_at,
        )
        if value is not None
    )


def _public_approval_clocks(row: _ApprovalRow) -> tuple[datetime, ...]:
    return tuple(
        value for value in (row.public_created_at, row.public_decided_at) if value is not None
    )


def _root_from_mapping(row: RowMapping, *, detail: bool) -> _RootRow:
    arguments: dict[str, object] | None = None
    if detail:
        raw_arguments = row["intent_arguments"]
        arguments = dict(raw_arguments) if isinstance(raw_arguments, Mapping) else {}
    return _RootRow(
        action_id=_required_str(row["action_id"]),
        run_id=_required_str(row["intent_run_id"]),
        session_id=_required_str(row["intent_session_id"]),
        cycle_id=_required_str(row["intent_cycle_id"]),
        step_id=_required_str(row["intent_step_id"]),
        engine_call_id=_optional_str(row["intent_engine_call_id"]),
        tool_id=_optional_str(row["intent_tool_id"]),
        skill_id=_optional_str(row["intent_skill_id"]),
        reason=_required_str(row["intent_reason"]),
        target_summary=_optional_str(row["intent_target_summary"]),
        approval_level=row["intent_approval_level"],
        status=row["intent_status"],
        claimed_execution_key=_optional_str(row["claimed_execution_key"]),
        claimed_attempt_group=_optional_str(row["claimed_attempt_group"]),
        created_at=_required_datetime(row["intent_created_at"]),
        updated_at=_required_datetime(row["intent_updated_at"]),
        arguments=arguments,
    )


def _approval_from_mapping(row: RowMapping, *, detail: bool) -> _ApprovalRow:
    return _ApprovalRow(
        action_id=_required_str(row["action_id"]),
        runtime_id=_required_str(row["runtime_approval_id"]),
        runtime_run_id=_required_str(row["runtime_run_id"]),
        runtime_session_id=_required_str(row["runtime_session_id"]),
        runtime_cycle_id=_required_str(row["runtime_cycle_id"]),
        runtime_status=row["runtime_status"],
        runtime_decided_by=_optional_str(row["runtime_decided_by"]),
        runtime_created_at=_required_datetime(row["runtime_created_at"]),
        runtime_decided_at=_optional_datetime(row["runtime_decided_at"]),
        public_id=_optional_str(row["public_approval_id"]),
        public_run_id=_optional_str(row["public_run_id"]),
        public_status=row["public_status"],
        public_decided_by=_optional_str(row["public_decided_by"]),
        public_created_at=_optional_datetime(row["public_created_at"]),
        public_decided_at=_optional_datetime(row["public_decided_at"]),
        runtime_feedback=(_optional_str(row["runtime_feedback"]) if detail else None),
    )


def _execution_summary_from_mapping(row: RowMapping) -> _ExecutionSummary:
    count = _required_nonnegative_int(row["execution_count"])
    current_match_count = _required_nonnegative_int(row["execution_current_match_count"])
    current_id = _optional_str(row["execution_current_id"])
    if current_match_count > count or (current_match_count > 0) != (current_id is not None):
        raise ValueError("Action execution projection contains an invalid current summary")
    return _ExecutionSummary(
        count=count,
        current_match_count=current_match_count,
        current_id=current_id,
        max_created_at=_nullable_datetime(row["execution_max_created_at"]),
        max_started_at=_nullable_datetime(row["execution_max_started_at"]),
        max_finished_at=_nullable_datetime(row["execution_max_finished_at"]),
        max_physical_stop_confirmed_at=_nullable_datetime(
            row["execution_max_physical_stop_confirmed_at"]
        ),
        max_updated_at=_nullable_datetime(row["execution_max_updated_at"]),
        scope_mismatch=_required_bool(row["execution_scope_mismatch"]),
        session_mismatch=_required_bool(row["execution_session_mismatch"]),
    )


def _execution_from_mapping(
    row: RowMapping,
    *,
    detail: bool,
) -> _ExecutionRow | None:
    execution_id = _optional_str(row["execution_id"])
    if execution_id is None:
        return None
    return _ExecutionRow(
        execution_id=execution_id,
        attempt_group=_optional_str(row["execution_attempt_group"]),
        status=row["execution_status"],
        exit_code=row["execution_exit_code"]
        if isinstance(row["execution_exit_code"], int)
        else None,
        created_at=_optional_datetime(row["execution_created_at"]),
        started_at=_optional_datetime(row["execution_started_at"]),
        finished_at=_optional_datetime(row["execution_finished_at"]),
        physical_stop_confirmed_at=_optional_datetime(row["execution_physical_stop_confirmed_at"]),
        node_id=_optional_str(row["execution_node_id"]),
    )


def _artifact_summary_from_mapping(row: RowMapping) -> _ArtifactSummary:
    return _ArtifactSummary(
        count=_required_nonnegative_int(row["artifact_count"]),
        max_created_at=_nullable_datetime(row["artifact_max_created_at"]),
        scope_mismatch=_required_bool(row["artifact_scope_mismatch"]),
    )


def _artifact_from_mapping(row: RowMapping) -> _ArtifactRow | None:
    artifact_id = _optional_str(row["artifact_id"])
    if artifact_id is None:
        return None
    return _ArtifactRow(
        artifact_id=artifact_id,
        run_id=_required_str(row["artifact_run_id"]),
        execution_id=_required_str(row["artifact_execution_id"]),
        created_at=_required_datetime(row["artifact_created_at"]),
    )


def _finding_summary_from_mapping(row: RowMapping) -> _FindingSummary:
    return _FindingSummary(
        count=_required_nonnegative_int(row["finding_count"]),
        max_created_at=_nullable_datetime(row["finding_max_created_at"]),
        max_updated_at=_nullable_datetime(row["finding_max_updated_at"]),
        partial=_required_bool(row["finding_partial"]),
    )


def _finding_from_mapping(row: RowMapping) -> _FindingRow | None:
    finding_id = _optional_str(row["finding_id"])
    if finding_id is None:
        return None
    return _FindingRow(
        finding_id=finding_id,
        run_id=_required_str(row["finding_run_id"]),
        created_at=_required_datetime(row["finding_created_at"]),
        updated_at=_required_datetime(row["finding_updated_at"]),
    )


def _event_summary_from_mapping(row: RowMapping) -> _EventSummary:
    return _EventSummary(
        count=_required_nonnegative_int(row["event_count"]),
        max_created_at=_nullable_datetime(row["event_max_created_at"]),
        partial=_required_bool(row["event_partial"]),
    )


def _event_from_mapping(row: RowMapping) -> _EventRow | None:
    event_id = _optional_str(row["event_id"])
    if event_id is None:
        return None
    return _EventRow(
        event_id=event_id,
        run_id=_required_str(row["event_run_id"]),
        created_at=_required_datetime(row["event_created_at"]),
        sequence=_required_nonnegative_int(row["event_sequence"]),
        event_type=_required_str(row["event_type"]),
    )


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _required_str(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("Action read row contains an invalid durable identifier")
    return value


def _required_bool(value: object) -> bool:
    if not isinstance(value, bool):
        raise ValueError("Action read row contains an invalid projection flag")
    return value


def _required_nonnegative_int(value: object) -> int:
    if type(value) is not int or value < 0:
        raise ValueError("Action read row contains an invalid projection count")
    return value


def _required_datetime(value: object) -> datetime:
    if not isinstance(value, datetime):
        raise ValueError("Action read row contains an invalid durable timestamp")
    return value


def _optional_datetime(value: object) -> datetime | None:
    return value if isinstance(value, datetime) else None


def _nullable_datetime(value: object) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return value
    raise ValueError("Action read row contains an invalid summary timestamp")


async def _rows(session: AsyncSession, statement: Any) -> tuple[RowMapping, ...]:
    result = await session.execute(statement)
    return tuple(result.mappings())


def _require_sqlite_json_projection(session: AsyncSession) -> None:
    if session.get_bind().dialect.name != "sqlite":
        raise RuntimeError(
            "Action reference-only JSON correlation is unavailable for this database dialect"
        )


__all__ = ["SQLAlchemyActionReadRepository"]
