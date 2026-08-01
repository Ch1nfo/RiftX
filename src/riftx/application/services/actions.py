"""Authorized, redacted projection of durable Run Action records."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import islice
from typing import Protocol
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from riftx.application.actions import (
    ActionAggregateRead,
    ActionApprovalRead,
    ActionApprovalView,
    ActionAttemptOrderQuality,
    ActionCorrelationQuality,
    ActionCoverage,
    ActionEventRead,
    ActionEventView,
    ActionEvidenceView,
    ActionExecutionRead,
    ActionExecutionView,
    ActionIntentRead,
    ActionLifecycle,
    ActionListAggregateRead,
    ActionListApprovalRead,
    ActionListAttemptView,
    ActionListExecutionRead,
    ActionPageKey,
    ActionPartialReason,
    ActionReadPage,
    ActionResultRead,
    ActionResultView,
    ActionStopConfirmation,
    InvalidActionCursorError,
    RunActionListItemView,
    RunActionListView,
    RunActionView,
)
from riftx.application.ports import ActionReadRepository
from riftx.domain import (
    ApprovalLevel,
    ApprovalStatus,
    ExecutionStatus,
    LocalPrincipal,
    OperatorCapability,
)
from riftx.runtime.types import ToolCallStatus

_DEFAULT_SORT = "created_at_desc"
_MAX_LIMIT = 100
_MAX_TEXT = 512
_MAX_TEXT_SCAN = 4096
_MAX_REFERENCE_ID_CHARS = _MAX_TEXT
_MAX_COLLECTION = 32
_MAX_DEPTH = 5
_MAX_REDACTION_NODES = 256
_MAX_REDACTION_BYTES = 16 * 1024
_MAX_ARGUMENTS_JSON_BYTES = 16 * 1024
_MAX_URI_QUOTED_VALUE_CHARS = 512
_TRUNCATED = "[TRUNCATED]"
_REDACTED = "[REDACTED]"
_PATH = "[PATH]"
_CURSOR_DOMAIN = b"riftx-action-cursor-v1\0"
_MAX_CURSOR_BYTES = 4096
_MAX_CURSOR_RUN_ID_CHARS = 64
_MAX_CURSOR_ACTION_ID_CHARS = 128
_MAX_CURSOR_TIME_CHARS = 64
_MAX_CURSOR_SORT_CHARS = 64

_SENSITIVE_KEY_PATTERN = (
    r"(?:authorization|proxy[-_ ]?authorization|cookie|set[-_ ]?cookie|password|passwd|"
    r"secret|token|access[-_ ]?token|credential|api[-_ ]?key|access[-_ ]?key|"
    r"private[-_ ]?key|client[-_ ]?secret|signature|sas[-_ ]?token|"
    r"shared[-_ ]?access[-_ ]?signature|x[-_ ]?amz[-_ ]?signature)"
)
_SENSITIVE_KEYS = re.compile(_SENSITIVE_KEY_PATTERN, re.IGNORECASE)
_SENSITIVE_EXACT_KEYS = {"sig"}
_AUTH_VALUE = re.compile(r"\b(?:basic|bearer)\s+[^\s,;]+", re.IGNORECASE)
_SECRET_HEADER = re.compile(
    r"\b(authorization|proxy-authorization|cookie|set-cookie|x-api-key)\s*:\s*[^\r\n]+",
    re.IGNORECASE,
)
_SENSITIVE_ASSIGNMENT_PREFIX = re.compile(
    rf"\b{_SENSITIVE_KEY_PATTERN}\b[\"']?\s*[=:]",
    re.IGNORECASE,
)
_EXACT_SIG_ASSIGNMENT_PREFIX = re.compile(
    r"(?<![\w])[\"']?sig[\"']?\s*[=:]",
    re.IGNORECASE,
)
_HIGH_CONFIDENCE_SECRET = re.compile(
    r"RIFTX_TEST_SECRET_DO_NOT_LEAK_[A-Za-z0-9._~+/-]*",
    re.IGNORECASE,
)
_PRIVATE_KEY_MARKER_OPENERS = ("-----BEGIN ", "-----END ")
_PRIVATE_KEY_MARKER_SUFFIXES = ("PRIVATE KEY", "PRIVATE KEY BLOCK")
_RFC_APOSTROPHE_FOLLOWERS = frozenset(".,!$&'()*+,;=:?@")
_WINDOWS_PATH = re.compile(r"(?<![\w])(?:[A-Za-z]:\\(?:[^\\\s]+\\)*[^\\\s]*)")
_WINDOWS_UNC_PATH = re.compile(r"(?<!\\)\\\\[^\\\s]+\\[^\\\s]+(?:\\[^\\\s]+)*")
_POSIX_PATH = re.compile(r"(?<![:/\w])/(?!/)(?:[^\s,;]+/)*[^\s,;]+")
_URI_START = re.compile(r"\b[a-z][a-z0-9+.-]*://", re.IGNORECASE)
_ACTIVE_EXECUTION_STATUSES = {
    ExecutionStatus.CREATED,
    ExecutionStatus.QUEUED,
    ExecutionStatus.STARTING,
    ExecutionStatus.RUNNING,
}


class _ObjectAuthorizer(Protocol):
    def require_child_run(
        self,
        principal: LocalPrincipal,
        *,
        parent_run_id: str,
        resource_run_id: str | None,
        capability: OperatorCapability,
    ) -> None: ...


class ActionApplicationService:
    """Compose Action views without exposing raw persistence objects or output."""

    def __init__(
        self,
        repository: ActionReadRepository,
        *,
        authorizer: _ObjectAuthorizer,
    ) -> None:
        self._repository = repository
        self._authorizer = authorizer

    async def list(
        self,
        run_id: str,
        *,
        principal: LocalPrincipal,
        limit: int = 50,
        cursor: str | None = None,
        sort: str = _DEFAULT_SORT,
    ) -> RunActionListView:
        if type(limit) is not int or not 1 <= limit <= _MAX_LIMIT:
            raise ValueError(f"Action list limit must be between 1 and {_MAX_LIMIT}")

        resolved_run_id = await self._repository.resolve_run(run_id)
        self._authorize(principal, run_id, resolved_run_id)

        after: ActionPageKey | None = None
        snapshot: ActionPageKey | None = None
        if cursor is not None:
            after, snapshot = _decode_cursor(
                cursor,
                run_id=run_id,
                sort=sort,
                limit=limit,
            )
        elif sort != _DEFAULT_SORT:
            raise InvalidActionCursorError()

        page = await self._repository.list_page(
            run_id,
            limit=limit,
            after=after,
            snapshot=snapshot,
        )
        seen_action_ids: set[str] = set()
        try:
            for aggregate in page.items:
                if (
                    aggregate.intent.run_id != run_id
                    or aggregate.intent.action_id in seen_action_ids
                ):
                    self._authorize(principal, run_id, None)
                    raise AssertionError("object authorizer returned for a cross-Run Action")
                seen_action_ids.add(aggregate.intent.action_id)
                _validate_list_aggregate(aggregate)
        except (AttributeError, TypeError):
            raise _ActionPageContractError("Repository returned an invalid Action page") from None

        _validate_page_contract(
            page,
            limit=limit,
            after=after,
            requested_snapshot=snapshot,
        )
        aggregates = page.items[:limit]
        has_more = page.has_more or len(page.items) > limit
        views = tuple(self._project_list(item, principal) for item in aggregates)
        next_cursor: str | None = None
        if has_more and aggregates and page.snapshot is not None:
            last = aggregates[-1].intent
            next_cursor = _encode_cursor(
                run_id=run_id,
                sort=sort,
                limit=limit,
                snapshot=page.snapshot,
                after=ActionPageKey(last.created_at, last.action_id),
            )
        return RunActionListView(
            items=views,
            limit=limit,
            sort=sort,
            has_more=has_more,
            next_cursor=next_cursor,
        )

    async def get(
        self,
        run_id: str,
        action_id: str,
        *,
        principal: LocalPrincipal,
    ) -> RunActionView:
        resource_run_id = await self._repository.resolve_action_run(run_id, action_id)
        self._authorize(principal, run_id, resource_run_id)
        aggregate = await self._repository.get(run_id, action_id)
        if (
            aggregate is None
            or aggregate.intent.run_id != run_id
            or aggregate.intent.action_id != action_id
        ):
            self._authorize(principal, run_id, None)
            raise AssertionError("object authorizer returned for an inaccessible Action")
        return self._project(aggregate, principal)

    def _authorize(
        self,
        principal: LocalPrincipal,
        parent_run_id: str,
        resource_run_id: str | None,
    ) -> None:
        self._authorizer.require_child_run(
            principal,
            parent_run_id=parent_run_id,
            resource_run_id=resource_run_id,
            capability=OperatorCapability.READ,
        )

    def _project_list(
        self,
        aggregate: ActionListAggregateRead,
        principal: LocalPrincipal,
    ) -> RunActionListItemView:
        _validate_list_aggregate(aggregate)
        intent = aggregate.intent
        approval = aggregate.approval
        detail = self._project(
            ActionAggregateRead(
                intent=ActionIntentRead(
                    action_id=intent.action_id,
                    run_id=intent.run_id,
                    session_id=intent.session_id,
                    cycle_id=intent.cycle_id,
                    step_id=intent.step_id,
                    engine_call_id=intent.engine_call_id,
                    tool_id=intent.tool_id,
                    skill_id=intent.skill_id,
                    reason=intent.reason,
                    target_summary=intent.target_summary,
                    approval_level=intent.approval_level,
                    status=intent.status,
                    arguments={},
                    created_at=intent.created_at,
                ),
                approval=(
                    ActionApprovalRead(
                        approval_id=approval.approval_id,
                        runtime_status=approval.runtime_status,
                        public_status=approval.public_status,
                        runtime_decided_by=approval.runtime_decided_by,
                        public_decided_by=approval.public_decided_by,
                        runtime_decided_at=approval.runtime_decided_at,
                        public_decided_at=approval.public_decided_at,
                        feedback=None,
                        bridge_correlation_quality=approval.bridge_correlation_quality,
                        bridge_partial_reasons=approval.bridge_partial_reasons,
                    )
                    if approval is not None
                    else None
                ),
                executions=tuple(_list_execution_to_detail(item) for item in aggregate.executions),
                current_execution_id=aggregate.current_execution_id,
                execution_count=aggregate.execution_count,
                execution_coverage=aggregate.execution_coverage,
                result=ActionResultRead(
                    artifact_ids=aggregate.result.artifact_ids,
                    artifact_count=aggregate.result.artifact_count,
                    output_size=aggregate.result.output_size,
                    output_available=aggregate.result.output_available,
                    artifacts_truncated=aggregate.result.artifacts_truncated,
                ),
                finding_ids=(),
                finding_count=aggregate.finding_count,
                events=(),
                event_count=aggregate.event_count,
                finding_coverage=aggregate.finding_coverage,
                event_coverage=aggregate.event_coverage,
                correlation_quality=aggregate.correlation_quality,
                partial_reasons=aggregate.partial_reasons,
                updated_at=aggregate.updated_at,
            ),
            principal,
            validate_source=False,
        )
        execution_statuses = {item.execution_id: item.status for item in detail.executions}
        stop_confirmations = {
            item.execution_id: item.stop_confirmation for item in detail.executions
        }
        approval_view = detail.approval
        view = RunActionListItemView(
            action_id=detail.action_id,
            run_id=detail.run_id,
            session_id=detail.session_id,
            cycle_id=detail.cycle_id,
            step_id=detail.step_id,
            engine_call_id=detail.engine_call_id,
            tool_id=detail.tool_id,
            skill_id=detail.skill_id,
            reason=detail.reason,
            target_summary=detail.target_summary,
            approval_level=detail.approval_level,
            approval_id=approval_view.approval_id if approval_view is not None else None,
            approval_status=approval_view.status if approval_view is not None else None,
            approval_actor=approval_view.actor if approval_view is not None else None,
            approval_decided_at=approval_view.decided_at if approval_view is not None else None,
            approval_correlation_quality=(
                approval_view.correlation_quality if approval_view is not None else None
            ),
            execution_count=aggregate.execution_count,
            attempts=tuple(
                ActionListAttemptView(
                    execution_id=item.execution_id,
                    attempt_group=item.attempt_group,
                    node_id=item.node_id,
                    status=item.status,
                    created_at=item.created_at,
                    started_at=item.started_at,
                    finished_at=item.finished_at,
                    exit_code=item.exit_code,
                    correlation_quality=item.correlation_quality,
                    physical_stop_confirmed_at=item.physical_stop_confirmed_at,
                    stop_confirmation=item.stop_confirmation,
                )
                for item in detail.executions
            ),
            attempt_coverage=aggregate.execution_coverage,
            latest_execution_id=detail.latest_execution_id,
            latest_execution_status=execution_statuses.get(detail.latest_execution_id),
            current_execution_id=detail.current_execution_id,
            current_execution_status=execution_statuses.get(detail.current_execution_id),
            latest_stop_confirmation=stop_confirmations.get(detail.latest_execution_id),
            current_stop_confirmation=stop_confirmations.get(detail.current_execution_id),
            attempt_order_quality=detail.attempt_order_quality,
            artifact_ids=aggregate.result.artifact_ids,
            artifact_count=aggregate.result.artifact_count,
            artifacts_truncated=aggregate.result.artifacts_truncated,
            output_size=aggregate.result.output_size,
            output_available=aggregate.result.output_available,
            finding_count=aggregate.finding_count,
            event_count=aggregate.event_count,
            finding_coverage=aggregate.finding_coverage,
            event_coverage=aggregate.event_coverage,
            lifecycle=detail.lifecycle,
            lifecycle_sources=detail.lifecycle_sources,
            correlation_quality=detail.correlation_quality,
            partial_reasons=detail.partial_reasons,
            created_at=detail.created_at,
            updated_at=aggregate.updated_at,
            version="",
        )
        version = _action_metadata_version(view, detail.executions)
        return view.model_copy(update={"version": version})

    def _project(
        self,
        aggregate: ActionAggregateRead,
        principal: LocalPrincipal,
        *,
        validate_source: bool = True,
    ) -> RunActionView:
        if validate_source:
            _validate_detail_aggregate(aggregate)
        intent = aggregate.intent
        reasons = list(_normalise_reasons(aggregate.partial_reasons))
        quality = aggregate.correlation_quality
        if reasons:
            quality = ActionCorrelationQuality.PARTIAL

        approval_level = _safe_enum(intent.approval_level, ApprovalLevel)
        if approval_level is None:
            reasons.append(ActionPartialReason.INTENT_APPROVAL_LEVEL_UNKNOWN)
            quality = ActionCorrelationQuality.PARTIAL

        intent_status = _safe_enum(intent.status, ToolCallStatus)
        if intent_status is None:
            reasons.append(ActionPartialReason.INTENT_STATUS_UNKNOWN)
            quality = ActionCorrelationQuality.PARTIAL

        approval_view, approval_status, approval_quality, approval_reasons = _project_approval(
            aggregate.approval, principal
        )
        reasons.extend(approval_reasons)
        quality = _merge_quality(quality, approval_quality)

        execution_views: list[ActionExecutionView] = []
        execution_statuses: dict[str, ExecutionStatus | None] = {}
        ordered_executions = tuple(sorted(aggregate.executions, key=_execution_sort_key))
        for execution in ordered_executions:
            status = _safe_enum(execution.status, ExecutionStatus)
            execution_statuses[execution.execution_id] = status
            execution_quality = execution.correlation_quality
            stop_confirmation, stop_proof_invalid = _execution_stop_state(
                execution,
                status,
            )
            if status is None:
                reasons.append(ActionPartialReason.EXECUTION_STATUS_UNKNOWN)
                execution_quality = ActionCorrelationQuality.PARTIAL
            if stop_proof_invalid:
                reasons.append(ActionPartialReason.EXECUTION_STOP_PROOF_INVALID)
                execution_quality = ActionCorrelationQuality.PARTIAL
            quality = _merge_quality(quality, execution_quality)
            execution_views.append(
                ActionExecutionView(
                    execution_id=execution.execution_id,
                    attempt_group=_safe_text(execution.attempt_group),
                    node_id=_safe_text(execution.node_id) or "",
                    status=status,
                    created_at=execution.created_at,
                    started_at=execution.started_at,
                    finished_at=execution.finished_at,
                    exit_code=execution.exit_code,
                    error_summary=_safe_text(execution.error_summary),
                    correlation_quality=execution_quality,
                    physical_stop_confirmed_at=(
                        None if stop_proof_invalid else execution.physical_stop_confirmed_at
                    ),
                    stop_confirmation=stop_confirmation,
                )
            )

        latest, _, order_quality = _select_attempt(aggregate.executions)
        attempts_incomplete = (
            aggregate.execution_coverage.truncated
            or aggregate.execution_count != len(aggregate.executions)
        )
        if attempts_incomplete:
            reasons.append(ActionPartialReason.EXECUTION_ATTEMPTS_TRUNCATED)
            quality = ActionCorrelationQuality.PARTIAL
            latest = None
            order_quality = ActionAttemptOrderQuality.UNKNOWN
        selected_execution = (
            next(
                (item for item in aggregate.executions if item.execution_id == latest),
                None,
            )
            if latest is not None
            else None
        )
        selected_status = execution_statuses.get(latest) if latest is not None else None
        active_executions = [
            execution
            for execution in aggregate.executions
            if execution_statuses.get(execution.execution_id) in _ACTIVE_EXECUTION_STATUSES
        ]
        current = aggregate.current_execution_id
        if current is not None:
            current_execution = next(
                (
                    execution
                    for execution in aggregate.executions
                    if execution.execution_id == current
                ),
                None,
            )
            if (
                current_execution is None
                or current_execution.correlation_quality is not ActionCorrelationQuality.EXACT
            ):
                current = None
                reasons.append(ActionPartialReason.EXECUTION_CURRENT_CORRELATION_PARTIAL)
                quality = ActionCorrelationQuality.PARTIAL
        elif (
            len(active_executions) > 1
            and ActionPartialReason.EXECUTION_CURRENT_CORRELATION_PARTIAL not in reasons
        ):
            reasons.append(ActionPartialReason.EXECUTION_CURRENT_AMBIGUOUS)
            quality = ActionCorrelationQuality.PARTIAL
        if selected_execution is not None and (
            selected_execution.correlation_quality is ActionCorrelationQuality.PARTIAL
        ):
            latest = None
            selected_execution = None
            selected_status = None
            quality = ActionCorrelationQuality.PARTIAL
        if (
            not attempts_incomplete
            and len(aggregate.executions) > 1
            and order_quality is not ActionAttemptOrderQuality.EXACT
        ):
            reasons.append(
                ActionPartialReason.EXECUTION_ATTEMPT_ORDER_AMBIGUOUS
                if order_quality is ActionAttemptOrderQuality.AMBIGUOUS
                else ActionPartialReason.EXECUTION_ATTEMPT_ORDER_UNKNOWN
            )
            quality = ActionCorrelationQuality.PARTIAL
        if selected_status is ExecutionStatus.LOST:
            quality = ActionCorrelationQuality.PARTIAL
        stop_confirmations = {
            execution.execution_id: execution.stop_confirmation for execution in execution_views
        }
        latest_stop_confirmation = stop_confirmations.get(latest)
        if latest_stop_confirmation is ActionStopConfirmation.UNCONFIRMED:
            reasons.append(ActionPartialReason.EXECUTION_STOP_UNCONFIRMED)
        current_stop_confirmation = stop_confirmations.get(current)

        if intent_status is ToolCallStatus.WAITING_APPROVAL and aggregate.approval is None:
            reasons.extend(
                (
                    ActionPartialReason.APPROVAL_RUNTIME_MISSING,
                    ActionPartialReason.APPROVAL_PUBLIC_MISSING,
                )
            )
            quality = ActionCorrelationQuality.PARTIAL
        if intent_status is ToolCallStatus.REJECTED and aggregate.approval is None:
            reasons.extend(
                (
                    ActionPartialReason.APPROVAL_RUNTIME_MISSING,
                    ActionPartialReason.APPROVAL_PUBLIC_MISSING,
                )
            )
            quality = ActionCorrelationQuality.PARTIAL
        if (
            intent_status
            in {
                ToolCallStatus.COMPLETED,
                ToolCallStatus.FAILED,
                ToolCallStatus.CANCELLED,
                ToolCallStatus.REJECTED,
            }
            and aggregate.event_count == 0
        ):
            reasons.append(ActionPartialReason.EVENT_CORRELATION_PARTIAL)
            quality = ActionCorrelationQuality.PARTIAL
        if (
            intent_status
            in {
                ToolCallStatus.EXECUTING,
                ToolCallStatus.COMPLETED,
                ToolCallStatus.FAILED,
            }
            and aggregate.execution_count == 0
        ):
            reasons.append(ActionPartialReason.EXECUTION_MISSING_FOR_INTENT_STATUS)
            quality = ActionCorrelationQuality.PARTIAL
        if not attempts_incomplete and not _intent_execution_statuses_compatible(
            intent_status=intent_status,
            executions=aggregate.executions,
            latest_execution=selected_execution,
            latest_status=selected_status,
            current_execution_id=current,
            active_count=len(active_executions),
        ):
            reasons.append(ActionPartialReason.INTENT_EXECUTION_STATUS_MISMATCH)
            quality = ActionCorrelationQuality.PARTIAL
        approval_intent_mismatch, approval_execution_mismatch = _approval_action_statuses_mismatch(
            approval_status=approval_status,
            intent_status=intent_status,
            has_executions=aggregate.execution_count > 0,
        )
        if approval_intent_mismatch:
            reasons.append(ActionPartialReason.APPROVAL_INTENT_STATUS_MISMATCH)
            quality = ActionCorrelationQuality.PARTIAL
        if approval_execution_mismatch:
            reasons.append(ActionPartialReason.APPROVAL_EXECUTION_STATUS_MISMATCH)
            quality = ActionCorrelationQuality.PARTIAL

        reasons = list(_dedupe(reasons))
        lifecycle, sources = _derive_lifecycle(
            intent_status=intent_status,
            approval_status=approval_status,
            execution=selected_execution,
            execution_status=selected_status,
        )
        if quality is ActionCorrelationQuality.PARTIAL:
            lifecycle = ActionLifecycle.PARTIAL

        events = tuple(
            ActionEventView(
                event_id=event.event_id,
                sequence=event.sequence,
                event_type=_safe_text(event.event_type) or "",
                created_at=event.created_at,
            )
            for event in aggregate.events
        )
        view = RunActionView(
            action_id=intent.action_id,
            run_id=intent.run_id,
            session_id=intent.session_id,
            cycle_id=intent.cycle_id,
            step_id=intent.step_id,
            engine_call_id=_safe_text(intent.engine_call_id),
            tool_id=_safe_text(intent.tool_id),
            skill_id=_safe_text(intent.skill_id),
            reason=_safe_text(intent.reason) or "",
            target_summary=_safe_text(intent.target_summary),
            approval_level=approval_level,
            arguments_summary=_redact_arguments(intent.arguments),
            approval=approval_view,
            executions=tuple(execution_views),
            execution_count=aggregate.execution_count,
            attempt_coverage=aggregate.execution_coverage,
            latest_execution_id=latest,
            current_execution_id=current,
            latest_stop_confirmation=latest_stop_confirmation,
            current_stop_confirmation=current_stop_confirmation,
            attempt_order_quality=order_quality,
            result=ActionResultView(
                truncated=aggregate.result.artifacts_truncated,
                artifact_ids=aggregate.result.artifact_ids,
                artifact_count=aggregate.result.artifact_count,
                output_size=aggregate.result.output_size,
                output_available=aggregate.result.output_available,
            ),
            evidence=ActionEvidenceView(
                finding_ids=aggregate.finding_ids,
                artifact_ids=aggregate.result.artifact_ids,
                events=events,
                finding_count=aggregate.finding_count,
                event_count=aggregate.event_count,
                finding_coverage=aggregate.finding_coverage,
                event_coverage=aggregate.event_coverage,
            ),
            lifecycle=lifecycle,
            lifecycle_sources=sources,
            correlation_quality=quality,
            partial_reasons=tuple(reasons),
            created_at=intent.created_at,
            updated_at=aggregate.updated_at,
            version="",
        )
        version = _action_metadata_version(view, view.executions)
        return view.model_copy(update={"version": version})


def _action_metadata_version(
    view: RunActionView | RunActionListItemView,
    executions: Sequence[ActionExecutionView],
) -> str:
    """Hash the metadata shared by list/detail, never detail-only historical text."""

    dumped = view.model_dump(mode="json")
    identity_keys = (
        "action_id",
        "run_id",
        "session_id",
        "cycle_id",
        "step_id",
        "engine_call_id",
        "tool_id",
        "skill_id",
        "reason",
        "target_summary",
        "approval_level",
    )
    payload = {key: dumped[key] for key in identity_keys}
    if isinstance(view, RunActionView):
        approval = dumped["approval"]
        if approval is None:
            approval_metadata = None
        else:
            approval_metadata = {
                key: approval[key]
                for key in (
                    "approval_id",
                    "status",
                    "actor",
                    "decided_at",
                    "correlation_quality",
                )
            }
        execution_statuses = {item.execution_id: item.status for item in view.executions}
        result = dumped["result"]
        evidence = dumped["evidence"]
        representation = {
            "approval": approval_metadata,
            "execution_count": view.execution_count,
            "attempt_coverage": dumped["attempt_coverage"],
            "latest_execution_status": execution_statuses.get(view.latest_execution_id),
            "current_execution_status": execution_statuses.get(view.current_execution_id),
            "artifact_count": result["artifact_count"],
            "artifacts_truncated": result["truncated"],
            "output_size": result["output_size"],
            "output_available": result["output_available"],
            "finding_count": view.evidence.finding_count,
            "event_count": view.evidence.event_count,
            "finding_coverage": evidence["finding_coverage"],
            "event_coverage": evidence["event_coverage"],
        }
    else:
        representation = {
            "approval": (
                {
                    "approval_id": dumped["approval_id"],
                    "status": dumped["approval_status"],
                    "actor": dumped["approval_actor"],
                    "decided_at": dumped["approval_decided_at"],
                    "correlation_quality": dumped["approval_correlation_quality"],
                }
                if dumped["approval_id"] is not None
                else None
            ),
            "execution_count": dumped["execution_count"],
            "attempt_coverage": dumped["attempt_coverage"],
            "latest_execution_status": dumped["latest_execution_status"],
            "current_execution_status": dumped["current_execution_status"],
            "artifact_count": dumped["artifact_count"],
            "artifacts_truncated": dumped["artifacts_truncated"],
            "output_size": dumped["output_size"],
            "output_available": dumped["output_available"],
            "finding_count": dumped["finding_count"],
            "event_count": dumped["event_count"],
            "finding_coverage": dumped["finding_coverage"],
            "event_coverage": dumped["event_coverage"],
        }
    payload.update(representation)
    payload.update(
        {
            "latest_execution_id": dumped["latest_execution_id"],
            "current_execution_id": dumped["current_execution_id"],
            "latest_stop_confirmation": dumped["latest_stop_confirmation"],
            "current_stop_confirmation": dumped["current_stop_confirmation"],
            "attempt_order_quality": dumped["attempt_order_quality"],
            "lifecycle": dumped["lifecycle"],
            "lifecycle_sources": dumped["lifecycle_sources"],
            "correlation_quality": dumped["correlation_quality"],
            "partial_reasons": dumped["partial_reasons"],
            "created_at": dumped["created_at"],
            "updated_at": dumped["updated_at"],
        }
    )
    attempt_metadata: list[dict[str, object]] = []
    for execution in executions:
        if not isinstance(execution, ActionExecutionView):
            raise TypeError("Action version execution metadata is invalid")
        execution_dump = execution.model_dump(mode="json")
        attempt_metadata.append(
            {
                key: execution_dump[key]
                for key in (
                    "execution_id",
                    "attempt_group",
                    "node_id",
                    "status",
                    "created_at",
                    "started_at",
                    "finished_at",
                    "exit_code",
                    "correlation_quality",
                    "physical_stop_confirmed_at",
                    "stop_confirmation",
                )
            }
        )
    payload["attempts"] = sorted(
        attempt_metadata,
        key=lambda item: str(item["execution_id"]),
    )
    return hashlib.sha256(_canonical_json(payload)).hexdigest()


def _project_approval(
    approval: ActionApprovalRead | None,
    principal: LocalPrincipal,
) -> tuple[
    ActionApprovalView | None,
    ApprovalStatus | None,
    ActionCorrelationQuality,
    tuple[ActionPartialReason, ...],
]:
    if approval is None:
        return None, None, ActionCorrelationQuality.EXACT, ()

    reasons = list(_normalise_reasons(approval.bridge_partial_reasons))
    quality = approval.bridge_correlation_quality
    if reasons:
        quality = ActionCorrelationQuality.PARTIAL
    runtime_status = _safe_enum(approval.runtime_status, ApprovalStatus)
    public_status = _safe_enum(approval.public_status, ApprovalStatus)
    if approval.runtime_status is None:
        reasons.append(ActionPartialReason.APPROVAL_RUNTIME_MISSING)
    elif runtime_status is None:
        reasons.append(ActionPartialReason.APPROVAL_STATUS_UNKNOWN)
    if approval.public_status is None:
        reasons.append(ActionPartialReason.APPROVAL_PUBLIC_MISSING)
    elif public_status is None:
        reasons.append(ActionPartialReason.APPROVAL_STATUS_UNKNOWN)

    if runtime_status is None or public_status is None:
        status = None
        quality = ActionCorrelationQuality.PARTIAL
    elif runtime_status is not public_status:
        reasons.append(ActionPartialReason.APPROVAL_STATUS_MISMATCH)
        status = None
        quality = ActionCorrelationQuality.PARTIAL
    else:
        status = runtime_status

    terminal_status = status in {
        ApprovalStatus.APPROVED,
        ApprovalStatus.REJECTED,
        ApprovalStatus.CANCELLED,
    }
    actors_match = (
        approval.runtime_decided_by is not None
        and approval.runtime_decided_by == approval.public_decided_by
        and approval.runtime_decided_by == principal.id
    )
    if approval.runtime_decided_by is None and approval.public_decided_by is None:
        actor = None
        if terminal_status:
            reasons.append(ActionPartialReason.APPROVAL_ACTOR_UNTRUSTED)
            quality = ActionCorrelationQuality.PARTIAL
    elif terminal_status and actors_match:
        actor = principal.id
    else:
        actor = None
        reasons.append(ActionPartialReason.APPROVAL_ACTOR_UNTRUSTED)
        quality = ActionCorrelationQuality.PARTIAL

    if terminal_status:
        if (
            approval.public_decided_at is not None
            and approval.runtime_decided_at is not None
            and approval.runtime_decided_at >= approval.public_decided_at
        ):
            decided_at = approval.public_decided_at
        else:
            decided_at = None
            reasons.append(ActionPartialReason.APPROVAL_DECISION_TIME_MISMATCH)
            quality = ActionCorrelationQuality.PARTIAL
    elif approval.runtime_decided_at is None and approval.public_decided_at is None:
        decided_at = None
    else:
        decided_at = None
        reasons.append(ActionPartialReason.APPROVAL_DECISION_TIME_MISMATCH)
        quality = ActionCorrelationQuality.PARTIAL

    quality = _merge_quality(quality, approval.bridge_correlation_quality)
    return (
        ActionApprovalView(
            approval_id=approval.approval_id,
            status=status,
            actor=actor,
            decided_at=decided_at,
            feedback_summary=_safe_text(approval.feedback),
            correlation_quality=quality,
        ),
        status,
        quality,
        _dedupe(reasons),
    )


def _select_attempt(
    executions: Sequence[ActionExecutionRead],
) -> tuple[str | None, str | None, ActionAttemptOrderQuality]:
    if not executions:
        return None, None, ActionAttemptOrderQuality.UNKNOWN
    if len(executions) == 1:
        execution = executions[0]
        quality = (
            ActionAttemptOrderQuality.EXACT
            if _execution_created_at_utc(execution) is not None
            else ActionAttemptOrderQuality.UNKNOWN
        )
        return execution.execution_id, execution.execution_id, quality
    if any(execution.created_at is None for execution in executions):
        return None, None, ActionAttemptOrderQuality.UNKNOWN
    timestamps = [_execution_created_at_utc(execution) for execution in executions]
    if any(timestamp is None for timestamp in timestamps):
        return None, None, ActionAttemptOrderQuality.UNKNOWN
    known_timestamps = [timestamp for timestamp in timestamps if timestamp is not None]
    maximum = max(known_timestamps)
    latest = [
        execution for execution in executions if _execution_created_at_utc(execution) == maximum
    ]
    if len(latest) != 1:
        return None, None, ActionAttemptOrderQuality.AMBIGUOUS
    quality = (
        ActionAttemptOrderQuality.AMBIGUOUS
        if len(set(known_timestamps)) != len(known_timestamps)
        else ActionAttemptOrderQuality.EXACT
    )
    return latest[0].execution_id, latest[0].execution_id, quality


def _execution_sort_key(
    execution: ActionExecutionRead,
) -> tuple[bool, datetime, str]:
    created_at = _execution_created_at_utc(execution)
    return (
        created_at is None,
        created_at or datetime.max.replace(tzinfo=UTC),
        execution.execution_id,
    )


def _execution_created_at_utc(execution: ActionExecutionRead) -> datetime | None:
    created_at = execution.created_at
    if created_at is None:
        return None
    try:
        if created_at.tzinfo is None or created_at.utcoffset() is None:
            return None
        return created_at.astimezone(UTC)
    except (OverflowError, ValueError):
        return None


def _is_aware_datetime(value: datetime) -> bool:
    if not isinstance(value, datetime):
        return False
    try:
        return value.tzinfo is not None and value.utcoffset() is not None
    except (OverflowError, ValueError):
        return False


def _list_execution_to_detail(execution: ActionListExecutionRead) -> ActionExecutionRead:
    return ActionExecutionRead(
        execution_id=execution.execution_id,
        attempt_group=execution.attempt_group,
        node_id=execution.node_id,
        status=execution.status,
        created_at=execution.created_at,
        started_at=execution.started_at,
        finished_at=execution.finished_at,
        exit_code=execution.exit_code,
        correlation_quality=execution.correlation_quality,
        error_summary=None,
        physical_stop_confirmed_at=execution.physical_stop_confirmed_at,
    )


def _execution_stop_state(
    execution: ActionExecutionRead,
    status: ExecutionStatus | None,
) -> tuple[ActionStopConfirmation | None, bool]:
    physical_stop_confirmed_at = execution.physical_stop_confirmed_at
    if status is None:
        return None, physical_stop_confirmed_at is not None
    if status in _ACTIVE_EXECUTION_STATUSES:
        return ActionStopConfirmation.NOT_APPLICABLE, physical_stop_confirmed_at is not None
    if physical_stop_confirmed_at is None:
        return ActionStopConfirmation.UNCONFIRMED, False
    if status in {ExecutionStatus.FAILED, ExecutionStatus.LOST}:
        return ActionStopConfirmation.UNCONFIRMED, True
    if status not in {
        ExecutionStatus.COMPLETED,
        ExecutionStatus.EXITED,
        ExecutionStatus.CANCELLED,
        ExecutionStatus.HARD_TIMEOUT,
    }:
        return ActionStopConfirmation.UNCONFIRMED, True
    timestamps = [
        timestamp
        for timestamp in (
            execution.created_at,
            execution.started_at,
            execution.finished_at,
        )
        if timestamp is not None
    ]
    if not _is_aware_datetime(physical_stop_confirmed_at) or any(
        not _is_aware_datetime(timestamp) or physical_stop_confirmed_at < timestamp
        for timestamp in timestamps
    ):
        return ActionStopConfirmation.UNCONFIRMED, True
    return ActionStopConfirmation.CONFIRMED, False


def _derive_lifecycle(
    *,
    intent_status: ToolCallStatus | None,
    approval_status: ApprovalStatus | None,
    execution: ActionExecutionRead | None,
    execution_status: ExecutionStatus | None,
) -> tuple[ActionLifecycle, tuple[str, ...]]:
    if execution_status in {ExecutionStatus.COMPLETED}:
        return ActionLifecycle.SUCCEEDED, ("execution.status",)
    if execution_status is ExecutionStatus.EXITED:
        lifecycle = (
            ActionLifecycle.SUCCEEDED
            if execution and execution.exit_code == 0
            else ActionLifecycle.FAILED
        )
        return (
            lifecycle,
            ("execution.status", "execution.exit_code"),
        )
    if execution_status in {ExecutionStatus.FAILED, ExecutionStatus.HARD_TIMEOUT}:
        return ActionLifecycle.FAILED, ("execution.status",)
    if execution_status is ExecutionStatus.CANCELLED:
        return ActionLifecycle.CANCELLED, ("execution.status",)
    if execution_status in {
        ExecutionStatus.CREATED,
        ExecutionStatus.QUEUED,
        ExecutionStatus.STARTING,
        ExecutionStatus.RUNNING,
    }:
        return ActionLifecycle.EXECUTING, ("execution.status",)
    if execution_status is ExecutionStatus.LOST:
        return ActionLifecycle.PARTIAL, ("execution.status",)

    if approval_status is ApprovalStatus.REJECTED or intent_status is ToolCallStatus.REJECTED:
        return ActionLifecycle.CANCELLED, ("approval.status", "intent.status")
    if approval_status is ApprovalStatus.CANCELLED:
        return ActionLifecycle.CANCELLED, ("approval.status",)
    if (
        approval_status is ApprovalStatus.APPROVED
        and intent_status is ToolCallStatus.WAITING_APPROVAL
    ):
        return ActionLifecycle.READY, ("approval.status", "intent.status")
    if approval_status is ApprovalStatus.PENDING:
        return ActionLifecycle.AWAITING_APPROVAL, ("approval.status",)

    mapping = {
        ToolCallStatus.PROPOSED: ActionLifecycle.PROPOSED,
        ToolCallStatus.WAITING_APPROVAL: ActionLifecycle.PARTIAL,
        ToolCallStatus.READY: ActionLifecycle.READY,
        ToolCallStatus.EXECUTING: ActionLifecycle.EXECUTING,
        ToolCallStatus.COMPLETED: ActionLifecycle.SUCCEEDED,
        ToolCallStatus.FAILED: ActionLifecycle.FAILED,
        ToolCallStatus.CANCELLED: ActionLifecycle.CANCELLED,
    }
    return mapping.get(intent_status, ActionLifecycle.PARTIAL), ("intent.status",)


def _intent_execution_statuses_compatible(
    *,
    intent_status: ToolCallStatus | None,
    executions: Sequence[ActionExecutionRead],
    latest_execution: ActionExecutionRead | None,
    latest_status: ExecutionStatus | None,
    current_execution_id: str | None,
    active_count: int,
) -> bool:
    if intent_status is None:
        return True
    if not executions:
        return intent_status not in {
            ToolCallStatus.EXECUTING,
            ToolCallStatus.COMPLETED,
            ToolCallStatus.FAILED,
        }
    if latest_execution is None or latest_status is None:
        return True
    if intent_status in {
        ToolCallStatus.PROPOSED,
        ToolCallStatus.WAITING_APPROVAL,
        ToolCallStatus.READY,
    }:
        return False
    if intent_status is ToolCallStatus.EXECUTING:
        return (
            active_count == 1
            and current_execution_id == latest_execution.execution_id
            and latest_status in _ACTIVE_EXECUTION_STATUSES
        )
    if intent_status is ToolCallStatus.COMPLETED:
        return active_count == 0 and (
            latest_status is ExecutionStatus.COMPLETED
            or (latest_status is ExecutionStatus.EXITED and latest_execution.exit_code == 0)
        )
    if intent_status is ToolCallStatus.FAILED:
        return active_count == 0 and (
            latest_status in {ExecutionStatus.FAILED, ExecutionStatus.HARD_TIMEOUT}
            or (
                latest_status is ExecutionStatus.EXITED
                and latest_execution.exit_code not in {None, 0}
            )
        )
    if intent_status in {ToolCallStatus.CANCELLED, ToolCallStatus.REJECTED}:
        return active_count == 0 and latest_status is ExecutionStatus.CANCELLED
    return True


def _approval_action_statuses_mismatch(
    *,
    approval_status: ApprovalStatus | None,
    intent_status: ToolCallStatus | None,
    has_executions: bool,
) -> tuple[bool, bool]:
    if approval_status is None or intent_status is None:
        return False, False
    if approval_status is ApprovalStatus.PENDING:
        return (
            intent_status
            not in {
                ToolCallStatus.PROPOSED,
                ToolCallStatus.WAITING_APPROVAL,
            },
            has_executions,
        )
    if approval_status is ApprovalStatus.APPROVED:
        return (
            intent_status
            not in {
                ToolCallStatus.WAITING_APPROVAL,
                ToolCallStatus.READY,
                ToolCallStatus.EXECUTING,
                ToolCallStatus.COMPLETED,
                ToolCallStatus.FAILED,
                ToolCallStatus.CANCELLED,
            },
            False,
        )
    if approval_status in {ApprovalStatus.REJECTED, ApprovalStatus.CANCELLED}:
        return (
            intent_status
            not in {
                ToolCallStatus.WAITING_APPROVAL,
                ToolCallStatus.REJECTED,
                ToolCallStatus.CANCELLED,
            },
            has_executions,
        )
    return False, False


def _safe_enum(value: object, enum_type: type):
    if isinstance(value, enum_type):
        return value
    if not isinstance(value, str):
        return None
    try:
        return enum_type(value)
    except ValueError:
        return None


def _merge_quality(
    left: ActionCorrelationQuality,
    right: ActionCorrelationQuality,
) -> ActionCorrelationQuality:
    if ActionCorrelationQuality.PARTIAL in {left, right}:
        return ActionCorrelationQuality.PARTIAL
    if ActionCorrelationQuality.LEGACY in {left, right}:
        return ActionCorrelationQuality.LEGACY
    return ActionCorrelationQuality.EXACT


def _normalise_reasons(
    values: Sequence[ActionPartialReason | str],
) -> tuple[ActionPartialReason, ...]:
    normalised: list[ActionPartialReason] = []
    for value in values:
        try:
            normalised.append(ActionPartialReason(value))
        except (TypeError, ValueError):
            normalised.append(ActionPartialReason.REPOSITORY_PARTIAL_REASON_INVALID)
    return _dedupe(normalised)


def _dedupe(values: Sequence[ActionPartialReason]) -> tuple[ActionPartialReason, ...]:
    return tuple(dict.fromkeys(values))


def _safe_text(value: str | None) -> str | None:
    if value is None:
        return None
    redacted = _redact_string(value)
    if len(redacted) <= _MAX_TEXT:
        return redacted
    return redacted[: _MAX_TEXT - len(_TRUNCATED)] + _TRUNCATED


@dataclass(slots=True)
class _RedactionBudget:
    nodes_remaining: int = _MAX_REDACTION_NODES
    bytes_remaining: int = _MAX_REDACTION_BYTES

    def enter(self) -> bool:
        if self.nodes_remaining <= 0 or self.bytes_remaining <= 0:
            return False
        self.nodes_remaining -= 1
        return True

    def text(self, value: str) -> str:
        redacted = _redact_string(value)
        encoded = redacted.encode("utf-8")
        allowance = min(self.bytes_remaining, _MAX_TEXT)
        marker = _TRUNCATED.encode("utf-8")
        if len(encoded) > allowance:
            if allowance <= len(marker):
                redacted = _TRUNCATED[:allowance]
                encoded = redacted.encode("utf-8")
                self.bytes_remaining = max(0, self.bytes_remaining - len(encoded))
                return redacted
            prefix_allowance = max(0, allowance - len(marker))
            prefix = encoded[:prefix_allowance].decode("utf-8", errors="ignore")
            redacted = prefix + _TRUNCATED
            encoded = redacted.encode("utf-8")
        self.bytes_remaining = max(0, self.bytes_remaining - len(encoded))
        return redacted


def _redact(
    value: object,
    *,
    _depth: int = 0,
    _budget: _RedactionBudget | None = None,
) -> object:
    budget = _budget or _RedactionBudget()
    if _depth >= _MAX_DEPTH or not budget.enter():
        return _TRUNCATED
    if isinstance(value, Mapping):
        result: dict[str, object] = {}
        tainted_keys: set[str] = set()
        items = list(islice(value.items(), _MAX_COLLECTION + 1))
        for raw_key, child in items[:_MAX_COLLECTION]:
            if budget.nodes_remaining <= 0 or budget.bytes_remaining <= 0:
                result["_truncated"] = _TRUNCATED
                break
            raw_key_text = str(raw_key)
            key_is_too_long = len(raw_key_text) > _MAX_TEXT
            key_has_unsafe_unicode = (
                False if key_is_too_long else _contains_unsafe_unicode(raw_key_text)
            )
            key_is_sensitive = (
                key_is_too_long or key_has_unsafe_unicode or _is_sensitive_key(raw_key_text)
            )
            if key_is_too_long:
                key = budget.text(_TRUNCATED)
            elif key_has_unsafe_unicode:
                key = budget.text(_REDACTED)
            else:
                key = budget.text(raw_key_text)
            projected_child = (
                _budgeted_redacted_value(budget)
                if key_is_sensitive
                else _redact(child, _depth=_depth + 1, _budget=budget)
            )
            if key_is_sensitive:
                tainted_keys.add(key)
            if key in tainted_keys or result.get(key) == _REDACTED:
                result[key] = _REDACTED
            else:
                result[key] = projected_child
        if len(items) > _MAX_COLLECTION:
            result["_truncated"] = _TRUNCATED
        return result
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        source = list(islice(iter(value), _MAX_COLLECTION + 1))
        children: list[object] = []
        for child in source[:_MAX_COLLECTION]:
            if budget.nodes_remaining <= 0 or budget.bytes_remaining <= 0:
                children.append(_TRUNCATED)
                break
            children.append(_redact(child, _depth=_depth + 1, _budget=budget))
        if len(source) > _MAX_COLLECTION:
            children.append(_TRUNCATED)
        return children
    if isinstance(value, str):
        return budget.text(value)
    if value is None:
        rendered = "null"
        return value if budget.text(rendered) == rendered else _TRUNCATED
    if isinstance(value, bool):
        rendered = "true" if value else "false"
        return value if budget.text(rendered) == rendered else _TRUNCATED
    if isinstance(value, int):
        try:
            rendered = str(value)
        except ValueError:
            return budget.text("[INVALID]")
        return value if budget.text(rendered) == rendered else _TRUNCATED
    if isinstance(value, float):
        if not math.isfinite(value):
            return budget.text("[INVALID]")
        rendered = repr(value)
        return value if budget.text(rendered) == rendered else _TRUNCATED
    return budget.text(str(value))


def _budgeted_redacted_value(budget: _RedactionBudget) -> str:
    if not budget.enter():
        return _TRUNCATED
    budget.bytes_remaining = max(
        0,
        budget.bytes_remaining - len(_REDACTED.encode("utf-8")),
    )
    return _REDACTED


def _redact_string(value: str) -> str:
    if len(value) > _MAX_TEXT_SCAN:
        return _TRUNCATED
    if _contains_unsafe_unicode(value):
        return _REDACTED
    if _HIGH_CONFIDENCE_SECRET.search(value) or _contains_private_key_marker(value):
        return _REDACTED
    value = _redact_quoted_absolute_paths(value)
    if value == _REDACTED:
        return value
    if _has_ambiguous_absolute_path(value):
        return _PATH
    value = _redact_uri_tokens(value)
    if value == _REDACTED:
        return value
    if _has_ambiguous_absolute_path(value):
        return _PATH
    value = _SECRET_HEADER.sub(lambda match: f"{match.group(1)}: {_REDACTED}", value)
    value = _AUTH_VALUE.sub(_REDACTED, value)
    if _SENSITIVE_ASSIGNMENT_PREFIX.search(value) or _EXACT_SIG_ASSIGNMENT_PREFIX.search(value):
        return _REDACTED
    value = _WINDOWS_UNC_PATH.sub(_PATH, value)
    value = _WINDOWS_PATH.sub(_PATH, value)
    return _POSIX_PATH.sub(_PATH, value)


def _contains_unsafe_unicode(value: str) -> bool:
    for character in value:
        category = unicodedata.category(character)
        if category.startswith("C") or category in {"Zl", "Zp"}:
            return True
    return False


def _contains_private_key_marker(value: str) -> bool:
    for line in value.splitlines():
        upper_line = line.upper()
        label_start: int | None = None
        cursor = 0
        while (delimiter := upper_line.find("-----", cursor)) >= 0:
            opener = next(
                (
                    candidate
                    for candidate in _PRIVATE_KEY_MARKER_OPENERS
                    if upper_line.startswith(candidate, delimiter)
                ),
                None,
            )
            if opener is not None:
                label_start = delimiter + len(opener)
                cursor = label_start
                continue
            if label_start is not None:
                label = upper_line[label_start:delimiter]
                if label.endswith(_PRIVATE_KEY_MARKER_SUFFIXES):
                    return True
                label_start = None
            cursor = delimiter + 5
    return False


def _redact_quoted_absolute_paths(value: str) -> str:
    rendered: list[str] = []
    cursor = 0
    index = 0
    while index < len(value):
        quote = value[index]
        if quote not in {"'", '"'} or not _starts_absolute_path(value, index + 1):
            index += 1
            continue
        closing_quote = _find_closing_text_quote(value, opening=index, quote=quote)
        if closing_quote is None:
            return _REDACTED
        rendered.extend((value[cursor : index + 1], _PATH, quote))
        cursor = closing_quote + 1
        index = cursor
    rendered.append(value[cursor:])
    return "".join(rendered)


def _starts_absolute_path(value: str, index: int) -> bool:
    if index >= len(value):
        return False
    if value[index] == "/" or value.startswith("\\\\", index):
        return True
    return (
        index + 2 < len(value)
        and value[index].isalpha()
        and value[index + 1] == ":"
        and value[index + 2] in {"\\", "/"}
    )


def _has_ambiguous_absolute_path(value: str) -> bool:
    for index in range(len(value)):
        if not _starts_absolute_path(value, index) or not _is_absolute_path_boundary(value, index):
            continue
        return any(character.isspace() for character in value[index:])
    return False


def _is_absolute_path_boundary(value: str, index: int) -> bool:
    if index == 0:
        return True
    previous = value[index - 1]
    return not (previous.isalnum() or previous in {"_", ":", "/", "\\"})


def _find_closing_text_quote(value: str, *, opening: int, quote: str) -> int | None:
    for index in range(opening + 1, len(value)):
        if value[index] in {"\r", "\n"}:
            return None
        if value[index] == quote:
            if value[index - 1] == "\\":
                return None
            return index
    return None


def _redact_uri_tokens(value: str) -> str:
    rendered: list[str] = []
    cursor = 0
    while match := _URI_START.search(value, cursor):
        start = match.start()
        end, safe = _uri_token_end(value, start=start, scheme_end=match.end())
        if not safe:
            return _REDACTED
        token = value[start:end]
        replacement = _PATH if token.casefold().startswith("file://") else _redact_url(token)
        rendered.extend((value[cursor:start], replacement))
        cursor = end
    rendered.append(value[cursor:])
    return "".join(rendered)


def _uri_token_end(value: str, *, start: int, scheme_end: int) -> tuple[int, bool]:
    outer_quote = value[start - 1] if start > 0 and value[start - 1] in {"'", '"'} else None
    outer_closed = outer_quote is None
    index = scheme_end
    while index < len(value):
        character = value[index]
        if character.isspace() or character in {"<", ">"}:
            break
        if character in {"'", '"'} and value[index - 1] == "\\":
            return index, False
        if character == outer_quote:
            if (
                not _is_outer_uri_quote(value, index)
                or value[index - 1] == "="
                or value[index + 1 :].strip()
            ):
                return index, False
            outer_closed = True
            break
        if character not in {"'", '"'}:
            index += 1
            continue

        closing_quote, ambiguous = _find_uri_query_quote(
            value,
            opening=index,
            quote=character,
        )
        if ambiguous:
            return index, False
        if closing_quote is None:
            if (
                character == "'"
                and outer_quote is not None
                and _is_unambiguous_rfc_apostrophe(value, index)
            ):
                index += 1
                continue
            return index, False
        index = closing_quote + 1
    return index, outer_closed


def _is_outer_uri_quote(value: str, index: int) -> bool:
    if index + 1 >= len(value):
        return True
    following = value[index + 1]
    return following.isspace() or following in {"<", ">"}


def _is_unambiguous_rfc_apostrophe(value: str, index: int) -> bool:
    return index + 1 < len(value) and value[index + 1] in _RFC_APOSTROPHE_FOLLOWERS


def _find_uri_query_quote(
    value: str,
    *,
    opening: int,
    quote: str,
) -> tuple[int | None, bool]:
    stop = min(len(value), opening + _MAX_URI_QUOTED_VALUE_CHARS + 2)
    for index in range(opening + 1, stop):
        if value[index] in {"\r", "\n", "<", ">"}:
            return None, True
        if value[index] == "\\":
            return None, True
        if value[index] == quote:
            return index, False
    return None, False


def _redact_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return _REDACTED
    if not parsed.scheme or not parsed.netloc:
        return _REDACTED
    if _has_raw_at_after_authority_delimiter(value, netloc=parsed.netloc):
        return _REDACTED
    netloc = parsed.netloc
    if "@" in netloc:
        netloc = f"{_REDACTED}@{netloc.rsplit('@', 1)[1]}"
    query = [
        (_safe_text(key) or _REDACTED, _REDACTED)
        for key, _ in parse_qsl(parsed.query, keep_blank_values=True)
    ]
    fragment = _REDACTED if parsed.fragment else ""
    path = _PATH if parsed.path else ""
    return urlunsplit((parsed.scheme, netloc, path, urlencode(query), fragment))


def _has_raw_at_after_authority_delimiter(value: str, *, netloc: str) -> bool:
    """Fail closed when a raw at-sign follows an authority-ending delimiter."""
    authority_start = value.find("//")
    if authority_start < 0:
        return False
    suffix = value[authority_start + 2 + len(netloc) :]
    return suffix.startswith(("?", "#")) and "@" in suffix


def _is_sensitive_key(value: str) -> bool:
    normalised = re.sub(r"[-_\s]", "", value).casefold()
    return normalised in _SENSITIVE_EXACT_KEYS or _SENSITIVE_KEYS.search(value) is not None


def _redact_arguments(arguments: Mapping[str, object]) -> dict[str, object]:
    redacted = _redact(arguments)
    if not isinstance(redacted, dict):
        return {"_truncated": _TRUNCATED}
    if len(_canonical_json(redacted)) > _MAX_ARGUMENTS_JSON_BYTES:
        return {"_truncated": _TRUNCATED}
    return redacted


class _ActionPageContractError(RuntimeError):
    pass


class _ActionAggregateContractError(RuntimeError):
    pass


def _validate_detail_aggregate(aggregate: ActionAggregateRead) -> None:
    try:
        _require_reference_ids(
            (
                aggregate.intent.action_id,
                aggregate.intent.run_id,
                aggregate.intent.session_id,
                aggregate.intent.cycle_id,
                aggregate.intent.step_id,
            )
        )
        if aggregate.approval is not None:
            _require_reference_ids((aggregate.approval.approval_id,))
        _validate_aggregate_clock(
            aggregate.intent.created_at,
            aggregate.updated_at,
            _visible_timestamps(
                approval=aggregate.approval,
                executions=aggregate.executions,
                events=aggregate.events,
            ),
        )
        _require_covered_collection(
            aggregate.executions,
            count=aggregate.execution_count,
            coverage=aggregate.execution_coverage,
        )
        _require_current_execution(
            aggregate.current_execution_id,
            aggregate.executions,
        )
        _require_bounded_collection(
            aggregate.result.artifact_ids,
            count=aggregate.result.artifact_count,
            truncated=aggregate.result.artifacts_truncated,
        )
        _require_covered_collection(
            aggregate.finding_ids,
            count=aggregate.finding_count,
            coverage=aggregate.finding_coverage,
        )
        _require_covered_collection(
            aggregate.events,
            count=aggregate.event_count,
            coverage=aggregate.event_coverage,
        )
        _validate_result_numbers(
            artifact_count=aggregate.result.artifact_count,
            output_size=aggregate.result.output_size,
            output_available=aggregate.result.output_available,
            artifacts_truncated=aggregate.result.artifacts_truncated,
        )
        _require_unique_ids(execution.execution_id for execution in aggregate.executions)
        _require_unique_ids(aggregate.result.artifact_ids)
        _require_unique_ids(aggregate.finding_ids)
        _require_unique_ids(event.event_id for event in aggregate.events)
        sequences = [event.sequence for event in aggregate.events]
        if any(type(sequence) is not int or sequence < 0 for sequence in sequences):
            raise ValueError
        if any(
            previous >= current for previous, current in zip(sequences, sequences[1:], strict=False)
        ):
            raise ValueError
    except (AttributeError, OverflowError, TypeError, ValueError):
        raise _ActionAggregateContractError(
            "Repository returned an invalid Action aggregate"
        ) from None


def _validate_list_aggregate(aggregate: ActionListAggregateRead) -> None:
    try:
        _require_reference_ids(
            (
                aggregate.intent.action_id,
                aggregate.intent.run_id,
                aggregate.intent.session_id,
                aggregate.intent.cycle_id,
                aggregate.intent.step_id,
            )
        )
        if aggregate.approval is not None:
            _require_reference_ids((aggregate.approval.approval_id,))
        _validate_aggregate_clock(
            aggregate.intent.created_at,
            aggregate.updated_at,
            _visible_timestamps(
                approval=aggregate.approval,
                executions=aggregate.executions,
                events=(),
            ),
        )
        _require_covered_collection(
            aggregate.executions,
            count=aggregate.execution_count,
            coverage=aggregate.execution_coverage,
        )
        _require_current_execution(
            aggregate.current_execution_id,
            aggregate.executions,
        )
        _require_bounded_collection(
            aggregate.result.artifact_ids,
            count=aggregate.result.artifact_count,
            truncated=aggregate.result.artifacts_truncated,
        )
        _require_summary_coverage(
            count=aggregate.finding_count,
            coverage=aggregate.finding_coverage,
        )
        _require_summary_coverage(
            count=aggregate.event_count,
            coverage=aggregate.event_coverage,
        )
        _validate_result_numbers(
            artifact_count=aggregate.result.artifact_count,
            output_size=aggregate.result.output_size,
            output_available=aggregate.result.output_available,
            artifacts_truncated=aggregate.result.artifacts_truncated,
        )
        _require_unique_ids(execution.execution_id for execution in aggregate.executions)
        _require_unique_ids(aggregate.result.artifact_ids)
    except (AttributeError, OverflowError, TypeError, ValueError):
        raise _ActionAggregateContractError(
            "Repository returned an invalid Action aggregate"
        ) from None


def _visible_timestamps(
    *,
    approval: ActionApprovalRead | ActionListApprovalRead | None,
    executions: Sequence[ActionExecutionRead | ActionListExecutionRead],
    events: Sequence[ActionEventRead],
) -> tuple[datetime | None, ...]:
    timestamps: list[datetime | None] = []
    if approval is not None:
        timestamps.extend((approval.runtime_decided_at, approval.public_decided_at))
    for execution in executions:
        timestamps.extend(
            (
                execution.created_at,
                execution.started_at,
                execution.finished_at,
                execution.physical_stop_confirmed_at,
            )
        )
    timestamps.extend(event.created_at for event in events)
    return tuple(timestamps)


def _validate_aggregate_clock(
    created_at: datetime,
    updated_at: datetime,
    visible_timestamps: Sequence[datetime | None],
) -> None:
    if (
        not _is_aware_datetime(created_at)
        or not _is_aware_datetime(updated_at)
        or updated_at < created_at
    ):
        raise ValueError
    if any(
        timestamp is not None and (not _is_aware_datetime(timestamp) or updated_at < timestamp)
        for timestamp in visible_timestamps
    ):
        raise ValueError


def _validate_coverage(coverage: ActionCoverage) -> None:
    _require_nonnegative_int(coverage.scanned)
    _require_nonnegative_int(coverage.limit)
    if type(coverage.truncated) is not bool or coverage.scanned > coverage.limit:
        raise ValueError


def _require_covered_collection(
    values: Sequence[object],
    *,
    count: int,
    coverage: ActionCoverage,
) -> None:
    _require_nonnegative_int(count)
    _validate_coverage(coverage)
    if (
        len(values) != coverage.scanned
        or coverage.scanned > count
        or coverage.truncated is not (coverage.scanned < count)
    ):
        raise ValueError


def _require_summary_coverage(*, count: int, coverage: ActionCoverage) -> None:
    _require_nonnegative_int(count)
    _validate_coverage(coverage)
    if coverage.scanned > count or coverage.truncated is not (coverage.scanned < count):
        raise ValueError


def _require_bounded_collection(
    values: Sequence[object],
    *,
    count: int,
    truncated: bool,
) -> None:
    _require_nonnegative_int(count)
    if type(truncated) is not bool or len(values) > count or truncated is not (len(values) < count):
        raise ValueError


def _validate_result_numbers(
    *,
    artifact_count: int,
    output_size: int,
    output_available: bool,
    artifacts_truncated: bool,
) -> None:
    _require_nonnegative_int(artifact_count)
    _require_nonnegative_int(output_size)
    if type(output_available) is not bool or type(artifacts_truncated) is not bool:
        raise ValueError


def _require_nonnegative_int(value: int) -> None:
    if type(value) is not int or value < 0:
        raise ValueError


def _require_unique_ids(values: Iterable[str]) -> None:
    materialised = list(values)
    _require_reference_ids(materialised)
    if len(materialised) != len(set(materialised)):
        raise ValueError


def _require_current_execution(
    current_execution_id: str | None,
    executions: Sequence[ActionExecutionRead | ActionListExecutionRead],
) -> None:
    if current_execution_id is None:
        return
    _require_reference_ids((current_execution_id,))
    matched = [
        execution for execution in executions if execution.execution_id == current_execution_id
    ]
    if len(matched) != 1 or matched[0].correlation_quality is not ActionCorrelationQuality.EXACT:
        raise ValueError


def _require_reference_ids(values: Iterable[str]) -> None:
    if any(
        not isinstance(value, str)
        or not value
        or len(value) > _MAX_REFERENCE_ID_CHARS
        or value != value.strip()
        or _contains_unsafe_unicode(value)
        or _safe_text(value) != value
        for value in values
    ):
        raise ValueError


def _validate_page_contract(
    page: ActionReadPage,
    *,
    limit: int,
    after: ActionPageKey | None,
    requested_snapshot: ActionPageKey | None,
) -> None:
    try:
        if type(page.has_more) is not bool or len(page.items) > limit + 1:
            raise ValueError
        if page.items and page.snapshot is None:
            raise ValueError
        if page.has_more and (not page.items or page.snapshot is None):
            raise ValueError
        if len(page.items) > limit and not page.has_more:
            raise ValueError
        if requested_snapshot is not None and page.snapshot != requested_snapshot:
            raise ValueError

        keys = [ActionPageKey(item.intent.created_at, item.intent.action_id) for item in page.items]
        normalised_keys = [_normalised_page_key(key) for key in keys]
        if requested_snapshot is None:
            if normalised_keys:
                if page.snapshot is None or (
                    _normalised_page_key(page.snapshot) != normalised_keys[0]
                ):
                    raise ValueError
            elif page.snapshot is not None:
                raise ValueError
        if any(
            previous <= current
            for previous, current in zip(normalised_keys, normalised_keys[1:], strict=False)
        ):
            raise ValueError
        if page.snapshot is not None:
            snapshot_key = _normalised_page_key(page.snapshot)
            if any(key > snapshot_key for key in normalised_keys):
                raise ValueError
        if after is not None:
            after_key = _normalised_page_key(after)
            if any(key >= after_key for key in normalised_keys):
                raise ValueError
    except (AttributeError, OverflowError, TypeError, ValueError):
        raise _ActionPageContractError("Repository returned an invalid Action page") from None


def _normalised_page_key(key: ActionPageKey) -> tuple[datetime, str]:
    created_at = key.created_at
    _require_reference_ids((key.action_id,))
    if len(key.action_id) > _MAX_CURSOR_ACTION_ID_CHARS or not _is_aware_datetime(created_at):
        raise ValueError
    return created_at.astimezone(UTC), key.action_id


def _encode_cursor(
    *,
    run_id: str,
    sort: str,
    limit: int,
    snapshot: ActionPageKey,
    after: ActionPageKey,
) -> str:
    body = {
        "after": _key_payload(after),
        "limit": limit,
        "run_id": run_id,
        "snapshot": _key_payload(snapshot),
        "sort": sort,
        "version": 1,
    }
    encoded_body = _canonical_json(body)
    envelope = {"body": body, "checksum": _cursor_corruption_checksum(encoded_body)}
    raw = _canonical_json(envelope)
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_cursor(
    cursor: str,
    *,
    run_id: str,
    sort: str,
    limit: int,
) -> tuple[ActionPageKey, ActionPageKey]:
    try:
        if not cursor or len(cursor) > _MAX_CURSOR_BYTES:
            raise ValueError
        padding = "=" * (-len(cursor) % 4)
        raw = base64.b64decode(cursor + padding, altchars=b"-_", validate=True)
        envelope = json.loads(raw, object_pairs_hook=_reject_duplicate_json_keys)
        if not isinstance(envelope, dict) or set(envelope) != {"body", "checksum"}:
            raise ValueError
        body = envelope["body"]
        if not isinstance(body, dict) or set(body) != {
            "after",
            "limit",
            "run_id",
            "snapshot",
            "sort",
            "version",
        }:
            raise ValueError
        checksum = envelope["checksum"]
        if (
            not isinstance(checksum, str)
            or len(checksum) != 64
            or not hmac.compare_digest(
                checksum,
                _cursor_corruption_checksum(_canonical_json(body)),
            )
        ):
            raise ValueError
        if (
            type(body["version"]) is not int
            or body["version"] != 1
            or type(body["limit"]) is not int
            or not isinstance(body["run_id"], str)
            or not isinstance(body["sort"], str)
            or not body["run_id"]
            or len(body["run_id"]) > _MAX_CURSOR_RUN_ID_CHARS
            or not body["sort"]
            or len(body["sort"]) > _MAX_CURSOR_SORT_CHARS
            or body["run_id"] != run_id
            or body["sort"] != sort
            or body["limit"] != limit
            or sort != _DEFAULT_SORT
        ):
            raise ValueError
        _require_reference_ids((body["run_id"],))
        after = _parse_key(body["after"])
        snapshot = _parse_key(body["snapshot"])
        if after.as_tuple() > snapshot.as_tuple():
            raise ValueError
        return after, snapshot
    except (OverflowError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
        raise InvalidActionCursorError() from None


def _key_payload(key: ActionPageKey) -> dict[str, str]:
    return {"action_id": key.action_id, "created_at": key.created_at.isoformat()}


def _parse_key(value: object) -> ActionPageKey:
    if not isinstance(value, dict) or set(value) != {"action_id", "created_at"}:
        raise ValueError
    action_id = value["action_id"]
    created_at_raw = value["created_at"]
    if (
        not isinstance(action_id, str)
        or not action_id
        or len(action_id) > _MAX_CURSOR_ACTION_ID_CHARS
        or not isinstance(created_at_raw, str)
        or not created_at_raw
        or len(created_at_raw) > _MAX_CURSOR_TIME_CHARS
    ):
        raise ValueError
    _require_reference_ids((action_id,))
    created_at = datetime.fromisoformat(created_at_raw)
    if not _is_aware_datetime(created_at):
        raise ValueError
    return ActionPageKey(created_at=created_at, action_id=action_id)


def _canonical_json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _cursor_corruption_checksum(value: bytes) -> str:
    """Detect cursor corruption; Run authorization remains the security boundary."""

    return hashlib.sha256(_CURSOR_DOMAIN + value).hexdigest()


def _reject_duplicate_json_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate cursor field")
        result[key] = value
    return result
