from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

import riftx.application.services.actions as actions_service
from riftx.application.actions import (
    ActionAggregateRead,
    ActionApprovalRead,
    ActionAttemptOrderQuality,
    ActionCorrelationQuality,
    ActionCoverage,
    ActionEventRead,
    ActionExecutionRead,
    ActionIntentRead,
    ActionLifecycle,
    ActionListAggregateRead,
    ActionListApprovalRead,
    ActionListExecutionRead,
    ActionListIntentRead,
    ActionListResultRead,
    ActionPageKey,
    ActionPartialReason,
    ActionReadPage,
    ActionResultRead,
    ActionStopConfirmation,
    InvalidActionCursorError,
)
from riftx.application.errors import ResourceNotAccessibleError
from riftx.application.services.actions import ActionApplicationService
from riftx.domain import (
    ApprovalLevel,
    ApprovalStatus,
    ExecutionStatus,
    LocalPrincipal,
    OperatorCapability,
)
from riftx.runtime.types import ToolCallStatus

NOW = datetime(2026, 8, 1, tzinfo=UTC)
PRINCIPAL_ID = "local-principal:v1:operator"
PRINCIPAL = LocalPrincipal(
    id=PRINCIPAL_ID,
    capabilities=frozenset({OperatorCapability.READ}),
)
UNSAFE_UNICODE_CHARACTERS = (
    pytest.param("\u200b", id="zero-width-space"),
    pytest.param("\u202e", id="right-to-left-override"),
    pytest.param("\u2066", id="left-to-right-isolate"),
    pytest.param("\ufeff", id="byte-order-mark"),
    pytest.param("\u2028", id="line-separator"),
    pytest.param("\u2029", id="paragraph-separator"),
)


def _intent(
    *,
    action_id: str = "intent-1",
    status: ToolCallStatus | str | None = ToolCallStatus.PROPOSED,
    approval_level: ApprovalLevel | str | None = ApprovalLevel.SENSITIVE,
    created_at: datetime = NOW,
    arguments: dict[str, object] | None = None,
) -> ActionIntentRead:
    return ActionIntentRead(
        action_id=action_id,
        run_id="run-1",
        session_id="session-1",
        cycle_id="cycle-1",
        step_id="step-1",
        engine_call_id="engine-call-1",
        tool_id="python",
        skill_id=None,
        reason="Inspect the authorized target",
        target_summary="example.test",
        approval_level=approval_level,
        status=status,
        arguments=arguments or {"target": "example.test"},
        created_at=created_at,
    )


def _aggregate(
    *,
    intent: ActionIntentRead | None = None,
    approval: ActionApprovalRead | None = None,
    executions: tuple[ActionExecutionRead, ...] = (),
    events: tuple[ActionEventRead, ...] | None = None,
    current_execution_id: str | None = None,
) -> ActionAggregateRead:
    resolved_intent = intent or _intent()
    resolved_events = events
    if resolved_events is None:
        resolved_events = (
            (
                ActionEventRead(
                    event_id=f"event-{resolved_intent.action_id}",
                    sequence=1,
                    event_type=f"tool.{resolved_intent.status}",
                    created_at=resolved_intent.created_at,
                ),
            )
            if resolved_intent.status
            in {
                ToolCallStatus.COMPLETED,
                ToolCallStatus.FAILED,
                ToolCallStatus.CANCELLED,
                ToolCallStatus.REJECTED,
            }
            else ()
        )
    updated_candidates = [resolved_intent.created_at]
    updated_candidates.extend(event.created_at for event in resolved_events)
    for execution in executions:
        updated_candidates.extend(
            timestamp
            for timestamp in (
                execution.created_at,
                execution.started_at,
                execution.finished_at,
                execution.physical_stop_confirmed_at,
            )
            if timestamp is not None
        )
    if approval is not None:
        updated_candidates.extend(
            timestamp
            for timestamp in (
                approval.runtime_decided_at,
                approval.public_decided_at,
            )
            if timestamp is not None
        )
    return ActionAggregateRead(
        intent=resolved_intent,
        approval=approval,
        executions=executions,
        current_execution_id=current_execution_id,
        execution_count=len(executions),
        execution_coverage=ActionCoverage(
            scanned=len(executions),
            limit=100,
            truncated=False,
        ),
        result=ActionResultRead(
            artifact_ids=("artifact-1",),
            artifact_count=1,
            output_size=12,
            output_available=True,
            artifacts_truncated=False,
        ),
        finding_ids=("finding-1",),
        finding_count=1,
        events=resolved_events,
        event_count=len(resolved_events),
        finding_coverage=ActionCoverage(scanned=1, limit=100, truncated=False),
        event_coverage=ActionCoverage(
            scanned=len(resolved_events),
            limit=200,
            truncated=False,
        ),
        correlation_quality=ActionCorrelationQuality.EXACT,
        partial_reasons=(),
        updated_at=max(updated_candidates),
    )


def _approval_read(
    *,
    approval_id: str = "approval-1",
    feedback: str | None = None,
) -> ActionApprovalRead:
    return ActionApprovalRead(
        approval_id=approval_id,
        runtime_status=ApprovalStatus.APPROVED,
        public_status=ApprovalStatus.APPROVED,
        runtime_decided_by=PRINCIPAL_ID,
        public_decided_by=PRINCIPAL_ID,
        runtime_decided_at=NOW + timedelta(seconds=2),
        public_decided_at=NOW + timedelta(seconds=1),
        feedback=feedback,
        bridge_correlation_quality=ActionCorrelationQuality.EXACT,
        bridge_partial_reasons=(),
    )


def _execution_read(
    *,
    execution_id: str = "execution-1",
    attempt_group: str | None = "initial",
    node_id: str = "local",
    error_summary: str | None = None,
) -> ActionExecutionRead:
    return ActionExecutionRead(
        execution_id=execution_id,
        attempt_group=attempt_group,
        node_id=node_id,
        status=ExecutionStatus.RUNNING,
        created_at=NOW,
        started_at=NOW,
        finished_at=None,
        exit_code=None,
        correlation_quality=ActionCorrelationQuality.EXACT,
        error_summary=error_summary,
    )


def _aggregate_with_reference_id(reference_kind: str, value: str) -> ActionAggregateRead:
    aggregate = _aggregate(events=())
    if reference_kind in {"action_id", "run_id", "session_id", "cycle_id", "step_id"}:
        return replace(
            aggregate,
            intent=replace(aggregate.intent, **{reference_kind: value}),
        )
    if reference_kind == "approval_id":
        return _aggregate(approval=_approval_read(approval_id=value), events=())
    if reference_kind == "execution_id":
        return _aggregate(
            intent=_intent(status=ToolCallStatus.EXECUTING),
            executions=(_execution_read(execution_id=value),),
            events=(),
        )
    if reference_kind == "artifact_id":
        return replace(
            aggregate,
            result=replace(aggregate.result, artifact_ids=(value,)),
        )
    if reference_kind == "finding_id":
        return replace(aggregate, finding_ids=(value,))
    if reference_kind == "event_id":
        return _aggregate(
            events=(
                ActionEventRead(
                    event_id=value,
                    sequence=1,
                    event_type="tool.proposed",
                    created_at=NOW,
                ),
            )
        )
    raise AssertionError(f"unsupported reference kind: {reference_kind}")


def _list_aggregate(aggregate: ActionAggregateRead) -> ActionListAggregateRead:
    intent = aggregate.intent
    approval = aggregate.approval
    return ActionListAggregateRead(
        intent=ActionListIntentRead(
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
            created_at=intent.created_at,
        ),
        approval=(
            ActionListApprovalRead(
                approval_id=approval.approval_id,
                runtime_status=approval.runtime_status,
                public_status=approval.public_status,
                runtime_decided_by=approval.runtime_decided_by,
                public_decided_by=approval.public_decided_by,
                runtime_decided_at=approval.runtime_decided_at,
                public_decided_at=approval.public_decided_at,
                bridge_correlation_quality=approval.bridge_correlation_quality,
                bridge_partial_reasons=approval.bridge_partial_reasons,
            )
            if approval is not None
            else None
        ),
        executions=tuple(
            ActionListExecutionRead(
                execution_id=execution.execution_id,
                attempt_group=execution.attempt_group,
                status=execution.status,
                created_at=execution.created_at,
                started_at=execution.started_at,
                finished_at=execution.finished_at,
                exit_code=execution.exit_code,
                correlation_quality=execution.correlation_quality,
                physical_stop_confirmed_at=execution.physical_stop_confirmed_at,
            )
            for execution in aggregate.executions
        ),
        current_execution_id=aggregate.current_execution_id,
        execution_count=aggregate.execution_count,
        execution_coverage=aggregate.execution_coverage,
        result=ActionListResultRead(
            artifact_ids=aggregate.result.artifact_ids,
            artifact_count=aggregate.result.artifact_count,
            output_size=aggregate.result.output_size,
            output_available=aggregate.result.output_available,
            artifacts_truncated=aggregate.result.artifacts_truncated,
        ),
        finding_count=aggregate.finding_count,
        event_count=aggregate.event_count,
        finding_coverage=aggregate.finding_coverage,
        event_coverage=aggregate.event_coverage,
        updated_at=aggregate.updated_at,
        correlation_quality=aggregate.correlation_quality,
        partial_reasons=aggregate.partial_reasons,
    )


class FakeActionReadRepository:
    def __init__(self, items: tuple[ActionAggregateRead, ...]) -> None:
        self.items = tuple(
            sorted(
                items,
                key=lambda item: (item.intent.created_at, item.intent.action_id),
                reverse=True,
            )
        )
        self.list_calls: list[tuple[str, int, ActionPageKey | None, ActionPageKey | None]] = []

    async def resolve_run(self, run_id: str) -> str | None:
        return run_id if run_id in {"run-1", "run-2"} else None

    async def resolve_action_run(self, run_id: str, action_id: str) -> str | None:
        return next(
            (
                item.intent.run_id
                for item in self.items
                if item.intent.run_id == run_id and item.intent.action_id == action_id
            ),
            None,
        )

    async def list_page(
        self,
        run_id: str,
        *,
        limit: int,
        after: ActionPageKey | None,
        snapshot: ActionPageKey | None,
    ) -> ActionReadPage:
        self.list_calls.append((run_id, limit, after, snapshot))
        matches = [item for item in self.items if item.intent.run_id == run_id]
        effective_snapshot = snapshot
        if effective_snapshot is None and matches:
            first = matches[0].intent
            effective_snapshot = ActionPageKey(first.created_at, first.action_id)

        def key(item: ActionAggregateRead) -> tuple[datetime, str]:
            return item.intent.created_at, item.intent.action_id

        if effective_snapshot is not None:
            matches = [item for item in matches if key(item) <= effective_snapshot.as_tuple()]
        if after is not None:
            matches = [item for item in matches if key(item) < after.as_tuple()]
        return ActionReadPage(
            items=tuple(_list_aggregate(item) for item in matches[:limit]),
            has_more=len(matches) > limit,
            snapshot=effective_snapshot,
        )

    async def get(self, run_id: str, action_id: str) -> ActionAggregateRead | None:
        return next(
            (
                item
                for item in self.items
                if item.intent.run_id == run_id and item.intent.action_id == action_id
            ),
            None,
        )


class PermissiveRunRepository(FakeActionReadRepository):
    async def resolve_run(self, run_id: str) -> str | None:
        return run_id


class RecordingObjectAuthorizer:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str | None, OperatorCapability]] = []

    def require_child_run(
        self,
        principal: LocalPrincipal,
        *,
        parent_run_id: str,
        resource_run_id: str | None,
        capability: OperatorCapability,
    ) -> None:
        self.calls.append((principal.id, parent_run_id, resource_run_id, capability))
        if capability is not OperatorCapability.READ:
            raise AssertionError("Action reads must require the READ capability")
        if resource_run_id != parent_run_id:
            raise ResourceNotAccessibleError(
                "resource_not_accessible",
                "The requested resource was not found",
            )


def _service(
    repository: FakeActionReadRepository,
    *,
    authorizer: RecordingObjectAuthorizer | None = None,
) -> ActionApplicationService:
    return ActionApplicationService(
        repository,
        authorizer=authorizer or RecordingObjectAuthorizer(),  # type: ignore[arg-type]
    )


def _tamper_cursor_field(
    cursor: str,
    field: str,
    value: object,
    *,
    resign_corruption_checksum: bool = False,
) -> str:
    padding = "=" * (-len(cursor) % 4)
    envelope = json.loads(base64.urlsafe_b64decode(cursor + padding))
    envelope["body"][field] = value
    if resign_corruption_checksum:
        body = json.dumps(envelope["body"], sort_keys=True, separators=(",", ":")).encode()
        envelope["checksum"] = hashlib.sha256(b"riftx-action-cursor-v1\0" + body).hexdigest()
    return (
        base64.urlsafe_b64encode(
            json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode()
        )
        .decode()
        .rstrip("=")
    )


async def test_action_reads_authorize_the_parent_run_with_read_capability() -> None:
    repository = FakeActionReadRepository((_aggregate(),))
    authorizer = RecordingObjectAuthorizer()
    service = _service(repository, authorizer=authorizer)

    await service.list("run-1", principal=PRINCIPAL, limit=1)
    await service.get("run-1", "intent-1", principal=PRINCIPAL)

    assert authorizer.calls == [
        (PRINCIPAL_ID, "run-1", "run-1", OperatorCapability.READ),
        (PRINCIPAL_ID, "run-1", "run-1", OperatorCapability.READ),
    ]
    assert repository.list_calls[0][1] == 1


async def test_action_detail_wrong_run_and_missing_id_are_indistinguishable() -> None:
    service = _service(FakeActionReadRepository((_aggregate(),)))

    failures: list[ResourceNotAccessibleError] = []
    for run_id, action_id in (("run-2", "intent-1"), ("run-1", "missing")):
        with pytest.raises(ResourceNotAccessibleError) as captured:
            await service.get(run_id, action_id, principal=PRINCIPAL)
        failures.append(captured.value)

    assert [(error.code, error.message, error.details) for error in failures] == [
        ("resource_not_accessible", "The requested resource was not found", {}),
        ("resource_not_accessible", "The requested resource was not found", {}),
    ]


async def test_action_list_contract_cannot_carry_detail_text() -> None:
    canary = "LIST-DETAIL-ACTION-CANARY-SECRET"
    approval = ActionApprovalRead(
        approval_id="approval-1",
        runtime_status=ApprovalStatus.APPROVED,
        public_status=ApprovalStatus.APPROVED,
        runtime_decided_by=PRINCIPAL_ID,
        public_decided_by=PRINCIPAL_ID,
        runtime_decided_at=NOW + timedelta(seconds=2),
        public_decided_at=NOW + timedelta(seconds=1),
        feedback=canary,
        bridge_correlation_quality=ActionCorrelationQuality.EXACT,
        bridge_partial_reasons=(),
    )
    execution = ActionExecutionRead(
        execution_id="execution-1",
        attempt_group="initial",
        node_id="local",
        status=ExecutionStatus.RUNNING,
        created_at=NOW + timedelta(seconds=2),
        started_at=NOW + timedelta(seconds=2),
        finished_at=None,
        exit_code=None,
        correlation_quality=ActionCorrelationQuality.EXACT,
        error_summary=canary,
    )
    aggregate = _aggregate(
        intent=_intent(
            status=ToolCallStatus.EXECUTING,
            arguments={"secret": canary},
        ),
        approval=approval,
        executions=(execution,),
    )
    repository = FakeActionReadRepository((aggregate,))

    page = await repository.list_page("run-1", limit=10, after=None, snapshot=None)
    assert not hasattr(page.items[0].intent, "arguments")
    assert page.items[0].approval is not None
    assert not hasattr(page.items[0].approval, "feedback")
    assert not hasattr(page.items[0].executions[0], "error_summary")

    view = await _service(repository).list("run-1", principal=PRINCIPAL)
    serialized = view.model_dump_json()
    assert canary not in serialized
    assert "arguments_summary" not in serialized
    assert "feedback_summary" not in serialized
    assert "error_summary" not in serialized
    assert '"events"' not in serialized
    assert view.items[0].attempts[0].attempt_group == "initial"
    assert view.items[0].attempt_coverage.truncated is False
    assert view.items[0].artifact_ids == ("artifact-1",)


async def test_action_service_fails_closed_on_repository_identity_mismatch() -> None:
    expected = _aggregate(intent=_intent(action_id="intent-requested"))
    wrong_action = _aggregate(intent=_intent(action_id="intent-other"))

    class WrongDetailRepository(FakeActionReadRepository):
        async def get(self, run_id: str, action_id: str) -> ActionAggregateRead | None:
            return wrong_action

    detail_repository = WrongDetailRepository((expected,))
    with pytest.raises(ResourceNotAccessibleError):
        await _service(detail_repository).get("run-1", "intent-requested", principal=PRINCIPAL)

    duplicate = _list_aggregate(expected)

    class DuplicateListRepository(FakeActionReadRepository):
        async def list_page(
            self,
            run_id: str,
            *,
            limit: int,
            after: ActionPageKey | None,
            snapshot: ActionPageKey | None,
        ) -> ActionReadPage:
            return ActionReadPage(
                items=(duplicate, duplicate),
                has_more=False,
                snapshot=ActionPageKey(
                    duplicate.intent.created_at,
                    duplicate.intent.action_id,
                ),
            )

    with pytest.raises(ResourceNotAccessibleError):
        await _service(DuplicateListRepository((expected,))).list(
            "run-1", principal=PRINCIPAL, limit=2
        )

    cross_run_sentinel = replace(
        duplicate,
        intent=replace(
            duplicate.intent,
            action_id="intent-cross-run",
            run_id="run-2",
        ),
    )

    class CrossRunSentinelRepository(FakeActionReadRepository):
        async def list_page(
            self,
            run_id: str,
            *,
            limit: int,
            after: ActionPageKey | None,
            snapshot: ActionPageKey | None,
        ) -> ActionReadPage:
            return ActionReadPage(
                items=(duplicate, cross_run_sentinel),
                has_more=True,
                snapshot=ActionPageKey(
                    duplicate.intent.created_at,
                    duplicate.intent.action_id,
                ),
            )

    with pytest.raises(ResourceNotAccessibleError):
        await _service(CrossRunSentinelRepository((expected,))).list(
            "run-1", principal=PRINCIPAL, limit=1
        )


async def test_action_service_rejects_impossible_repository_aggregates() -> None:
    execution = ActionExecutionRead(
        execution_id="execution-1",
        attempt_group=None,
        node_id="local",
        status=ExecutionStatus.RUNNING,
        created_at=NOW,
        started_at=NOW,
        finished_at=None,
        exit_code=None,
        correlation_quality=ActionCorrelationQuality.EXACT,
        error_summary=None,
    )
    execution_base = _aggregate(
        intent=_intent(status=ToolCallStatus.EXECUTING),
        executions=(execution,),
    )
    evidence_base = _aggregate(
        events=(
            ActionEventRead(
                event_id="event-1",
                sequence=1,
                event_type="tool.proposed",
                created_at=NOW,
            ),
        )
    )
    malformed = (
        replace(
            execution_base,
            executions=(execution, replace(execution, status=ExecutionStatus.COMPLETED)),
            execution_count=2,
            execution_coverage=ActionCoverage(scanned=2, limit=100, truncated=False),
        ),
        replace(
            evidence_base,
            result=replace(
                evidence_base.result,
                artifact_ids=("artifact-1", "artifact-1"),
                artifact_count=2,
            ),
        ),
        replace(
            evidence_base,
            finding_ids=("finding-1", "finding-1"),
            finding_count=2,
            finding_coverage=ActionCoverage(scanned=2, limit=100, truncated=False),
        ),
        replace(
            evidence_base,
            events=(evidence_base.events[0], evidence_base.events[0]),
            event_count=2,
            event_coverage=ActionCoverage(scanned=2, limit=200, truncated=False),
        ),
        replace(
            evidence_base,
            result=replace(evidence_base.result, output_size=-1),
        ),
        replace(
            evidence_base,
            result=replace(evidence_base.result, artifact_count=0),
        ),
        replace(
            evidence_base,
            result=replace(evidence_base.result, artifacts_truncated=True),
        ),
        replace(
            evidence_base,
            finding_count=2,
            finding_coverage=ActionCoverage(scanned=2, limit=100, truncated=False),
        ),
        replace(
            evidence_base,
            event_coverage=ActionCoverage(scanned=0, limit=200, truncated=True),
        ),
        replace(
            evidence_base,
            event_coverage=ActionCoverage(scanned=2, limit=200, truncated=True),
        ),
        replace(
            evidence_base,
            event_coverage=ActionCoverage(scanned=1, limit=200, truncated=True),
        ),
        replace(
            evidence_base,
            event_count=2,
            event_coverage=ActionCoverage(scanned=1, limit=200, truncated=False),
        ),
        replace(
            execution_base,
            execution_coverage=ActionCoverage(scanned=0, limit=100, truncated=True),
        ),
        replace(
            evidence_base,
            updated_at=evidence_base.intent.created_at - timedelta(seconds=1),
        ),
    )

    for aggregate in malformed:
        with pytest.raises(RuntimeError, match="invalid Action aggregate"):
            await _service(FakeActionReadRepository((aggregate,))).get(
                "run-1", aggregate.intent.action_id, principal=PRINCIPAL
            )

    list_evidence_base = _list_aggregate(evidence_base)
    invalid_lists = (
        replace(
            list_evidence_base,
            result=replace(list_evidence_base.result, output_size=-1),
        ),
        replace(
            list_evidence_base,
            result=replace(list_evidence_base.result, artifacts_truncated=True),
        ),
        replace(
            _list_aggregate(execution_base),
            execution_coverage=ActionCoverage(scanned=0, limit=100, truncated=True),
        ),
        replace(
            list_evidence_base,
            finding_coverage=ActionCoverage(scanned=0, limit=100, truncated=False),
        ),
        replace(
            list_evidence_base,
            event_coverage=ActionCoverage(scanned=2, limit=200, truncated=True),
        ),
        replace(
            list_evidence_base,
            event_coverage=ActionCoverage(scanned=1, limit=200, truncated=True),
        ),
    )

    class InvalidListRepository(FakeActionReadRepository):
        invalid_list: ActionListAggregateRead

        async def list_page(
            self,
            run_id: str,
            *,
            limit: int,
            after: ActionPageKey | None,
            snapshot: ActionPageKey | None,
        ) -> ActionReadPage:
            return ActionReadPage(
                items=(self.invalid_list,),
                has_more=False,
                snapshot=ActionPageKey(
                    self.invalid_list.intent.created_at,
                    self.invalid_list.intent.action_id,
                ),
            )

    for invalid_list in invalid_lists:
        repository = InvalidListRepository((evidence_base,))
        repository.invalid_list = invalid_list
        with pytest.raises(RuntimeError, match="invalid Action aggregate"):
            await _service(repository).list("run-1", principal=PRINCIPAL)


async def test_foreign_coverage_and_page_objects_normalize_to_contract_errors() -> None:
    aggregate = _aggregate()
    invalid_coverage = replace(
        aggregate,
        execution_coverage=None,  # type: ignore[arg-type]
    )
    invalid_coverage_service = _service(FakeActionReadRepository((invalid_coverage,)))

    with pytest.raises(RuntimeError, match="invalid Action aggregate"):
        await invalid_coverage_service.get("run-1", "intent-1", principal=PRINCIPAL)
    with pytest.raises(RuntimeError, match="invalid Action aggregate"):
        await invalid_coverage_service.list("run-1", principal=PRINCIPAL)

    class ForeignPageRepository(FakeActionReadRepository):
        page: object

        async def list_page(
            self,
            run_id: str,
            *,
            limit: int,
            after: ActionPageKey | None,
            snapshot: ActionPageKey | None,
        ) -> ActionReadPage:
            return self.page  # type: ignore[return-value]

    foreign_pages = (
        None,
        object(),
        ActionReadPage(
            items=None,  # type: ignore[arg-type]
            has_more=False,
            snapshot=None,
        ),
    )
    for page in foreign_pages:
        repository = ForeignPageRepository((aggregate,))
        repository.page = page
        with pytest.raises(RuntimeError, match="invalid Action page"):
            await _service(repository).list("run-1", principal=PRINCIPAL)


async def test_action_service_rejects_invalid_or_stale_visible_child_clocks() -> None:
    approval = ActionApprovalRead(
        approval_id="approval-1",
        runtime_status=ApprovalStatus.APPROVED,
        public_status=ApprovalStatus.APPROVED,
        runtime_decided_by=PRINCIPAL_ID,
        public_decided_by=PRINCIPAL_ID,
        runtime_decided_at=NOW + timedelta(seconds=1),
        public_decided_at=NOW + timedelta(seconds=1),
        feedback=None,
        bridge_correlation_quality=ActionCorrelationQuality.EXACT,
        bridge_partial_reasons=(),
    )
    execution = ActionExecutionRead(
        execution_id="execution-1",
        attempt_group=None,
        node_id="local",
        status=ExecutionStatus.RUNNING,
        created_at=NOW + timedelta(seconds=1),
        started_at=NOW + timedelta(seconds=1),
        finished_at=None,
        exit_code=None,
        correlation_quality=ActionCorrelationQuality.EXACT,
        error_summary=None,
    )
    event = ActionEventRead(
        event_id="event-1",
        sequence=1,
        event_type="tool.executing",
        created_at=NOW + timedelta(seconds=1),
    )
    base = _aggregate(
        intent=_intent(status=ToolCallStatus.EXECUTING),
        approval=approval,
        executions=(execution,),
        events=(event,),
    )
    naive = datetime(2026, 8, 1, 0, 0)
    future = base.updated_at + timedelta(seconds=1)
    invalid_approval_clock = replace(
        base,
        approval=replace(approval, runtime_decided_at=naive),
    )
    invalid_execution_clock = replace(
        base,
        executions=(replace(execution, physical_stop_confirmed_at=future),),
    )
    invalid_event_clock = replace(
        base,
        events=(replace(event, created_at=future),),
    )
    invalid_event_type = replace(
        base,
        events=(replace(event, created_at="not-a-datetime"),),  # type: ignore[arg-type]
    )

    for aggregate in (
        invalid_approval_clock,
        invalid_execution_clock,
        invalid_event_clock,
        invalid_event_type,
    ):
        with pytest.raises(RuntimeError, match="invalid Action aggregate"):
            await _service(FakeActionReadRepository((aggregate,))).get(
                "run-1", "intent-1", principal=PRINCIPAL
            )

    for aggregate in (invalid_approval_clock, invalid_execution_clock):
        with pytest.raises(RuntimeError, match="invalid Action aggregate"):
            await _service(FakeActionReadRepository((aggregate,))).list(
                "run-1", principal=PRINCIPAL
            )


async def test_action_projection_redacts_and_bounds_every_historical_text_boundary() -> None:
    canary = "ACTION-CANARY-SECRET"
    basic_canary = base64.b64encode(f"operator:{canary}".encode()).decode()
    execution = ActionExecutionRead(
        execution_id="execution-1",
        attempt_group="initial",
        node_id="local",
        status=ExecutionStatus.FAILED,
        created_at=NOW + timedelta(seconds=1),
        started_at=NOW + timedelta(seconds=1),
        finished_at=NOW + timedelta(seconds=2),
        exit_code=1,
        correlation_quality=ActionCorrelationQuality.EXACT,
        error_summary=(
            f"Bearer {canary} failed at /Users/operator/private/output.log " + "x" * 2000
        ),
    )
    event = ActionEventRead(
        event_id="event-1",
        sequence=8,
        event_type="execution.failed",
        created_at=NOW + timedelta(seconds=2),
    )
    aggregate = _aggregate(
        intent=_intent(
            status=ToolCallStatus.FAILED,
            arguments={
                "authorization": f"Bearer {canary}",
                "signal_control": "signal=visible",
                "design_control": "design=blue",
                "design": "visible",
                "posix": "/workspace/private/output.log",
                "file_uri": "file:///workspace/private/output.log",
                "windows": r"C:\Users\operator\private\output.log",
                "unc": r"\\server\share\private\output.log",
                "nested": {"password": canary, "safe": "visible"},
                "items": list(range(100)),
                "header_one": f"Cookie: session={canary}",
                "header_two": f"Set-Cookie: session={canary}; HttpOnly",
                "header_three": f"Proxy-Authorization: Basic {basic_canary}",
                "basic_auth": f"Basic {basic_canary}",
                "assignment_one": f"authorization={canary}",
                "assignment_two": f"proxy_authorization={canary}",
                "assignment_three": f"set_cookie={canary}",
                "signed_url": (
                    f"https://operator:{canary}@example.test/download?X-Amz-Signature={canary}"
                ),
                "malformed_url": f"http://[invalid/u:{canary}@example.test/",
                "fragment_url": f"https://example.test/callback#access_token={canary}",
                "plain_fragment_url": f"https://example.test/callback#{canary}",
                "database_uri": f"postgresql://operator:{canary}@db.example.test/riftx",
                "azure_sas": f"https://example.test/blob?sig={canary}",
                "encoded_bearer_query": (f"https://example.test/callback?error=Bearer%20{canary}"),
                "encoded_assignment_query": (
                    f"https://example.test/callback?error=authorization%3D{canary}"
                ),
                "encoded_nested_uri": (
                    "https://example.test/callback?next="
                    f"https%3A%2F%2Foperator%3A{canary}%40inner.test"
                ),
                "encoded_semicolon_sig": (
                    f"https://example.test/callback?error=x%3Bsig%3D{canary}"
                ),
                "encoded_sensitive_path": (f"https://example.test/authorization%3D{canary}"),
                "sig_assignment": f"sig={canary}",
                "sig_json": f'{{"sig":"{canary}"}}',
                "quoted_assignment": f'password="prefix {canary} suffix"',
                "escaped_quoted_assignment": f'password="prefix\\"{canary} suffix"',
                "unterminated_quoted_assignment": f'password="prefix {canary}',
                "comma_quoted_assignment": f'password="prefix, {canary}, suffix"',
                "long_quoted_assignment": (f'password="prefix {canary} ' + "x" * 5000 + '"'),
                "/password": canary,
                ("x" * 600 + "password"): canary,
                "deep": {"a": {"b": {"c": {"d": {"e": {"secret": canary}}}}}},
            },
        ),
        executions=(execution,),
        events=(event,),
    )
    service = _service(FakeActionReadRepository((aggregate,)))

    view = await service.get("run-1", "intent-1", principal=PRINCIPAL)
    serialized = view.model_dump_json()

    assert canary not in serialized
    assert basic_canary not in serialized
    assert "/Users/operator" not in serialized
    assert "/workspace/private" not in serialized
    assert "[REDACTED]" in serialized
    assert "[PATH]" in serialized
    assert "[TRUNCATED]" in serialized
    assert view.arguments_summary["nested"]["safe"] == "visible"  # type: ignore[index]
    assert view.arguments_summary["design"] == "visible"
    assert view.arguments_summary["signal_control"] == "signal=visible"
    assert view.arguments_summary["design_control"] == "design=blue"
    assert view.arguments_summary["posix"] == "[PATH]"
    assert view.arguments_summary["file_uri"] == "[PATH]"
    assert view.arguments_summary["windows"] == "[PATH]"
    assert view.arguments_summary["unc"] == "[PATH]"
    assert view.executions[0].error_summary is not None
    assert len(view.executions[0].error_summary) <= 520
    assert len(serialized.encode()) < 32 * 1024


async def test_redactor_fails_closed_on_assignment_and_raw_key_edges() -> None:
    canary = "ACTION-CANARY-SECRET"
    arguments = {
        "quoted": f'password="prefix {canary} suffix"',
        "escaped": f'password="prefix\\"{canary} suffix"',
        "unterminated": f'password="prefix {canary}',
        "comma": f'password="prefix, {canary}, suffix"',
        "long": f'password="prefix {canary} ' + "x" * 5000 + '"',
        "/password": canary,
        ("x" * 600 + "password"): canary,
        ("y" * 600): canary,
        "signal": "signal=visible",
        "design": "design=blue",
    }

    view = await _service(
        FakeActionReadRepository((_aggregate(intent=_intent(arguments=arguments)),))
    ).get("run-1", "intent-1", principal=PRINCIPAL)
    serialized = view.model_dump_json()

    assert canary not in serialized
    assert view.arguments_summary["signal"] == "signal=visible"
    assert view.arguments_summary["design"] == "design=blue"


async def test_quoted_uri_query_values_are_bounded_and_do_not_leave_tail_canaries() -> None:
    canary = "ACTION-CANARY-SECRET"
    single_quoted = f"https://example.test/callback?code='{canary}' after=visible"
    double_quoted = f'https://example.test/callback?code="{canary}" after=visible'
    benign_single = "'https://example.test/callback' after=visible"
    benign_double = '"https://example.test/callback" after=visible'
    intent = replace(
        _intent(
            arguments={
                "single_quoted": single_quoted,
                "double_quoted": double_quoted,
                "benign_single": benign_single,
                "benign_double": benign_double,
            }
        ),
        reason=single_quoted,
        target_summary=double_quoted,
    )
    service = _service(FakeActionReadRepository((_aggregate(intent=intent),)))

    detail = await service.get("run-1", "intent-1", principal=PRINCIPAL)
    listed = await service.list("run-1", principal=PRINCIPAL)
    expected_redacted = "https://example.test/[PATH]?code=%5BREDACTED%5D after=visible"

    for serialized in (detail.model_dump_json(), listed.model_dump_json()):
        assert canary not in serialized
        assert "after=visible" in serialized
    assert detail.reason == expected_redacted
    assert detail.target_summary == expected_redacted
    assert listed.items[0].reason == expected_redacted
    assert listed.items[0].target_summary == expected_redacted
    assert detail.arguments_summary["single_quoted"] == expected_redacted
    assert detail.arguments_summary["double_quoted"] == expected_redacted
    assert detail.arguments_summary["benign_single"] == "[REDACTED]"
    assert detail.arguments_summary["benign_double"] == "[REDACTED]"


@pytest.mark.parametrize(
    ("query_quote", "canary"),
    [("'", "QUERY_SINGLE_CANARY"), ('"', "QUERY_DOUBLE_CANARY")],
)
async def test_quoted_uri_query_values_with_spaces_preserve_only_trailing_text(
    query_quote: str,
    canary: str,
) -> None:
    raw_url = f"https://example.test/cb?q={query_quote}prefix {canary} suffix{query_quote}"
    value = f"{raw_url} trailing=visible"
    opposite_outer = '"' if query_quote == "'" else "'"
    opposite_outer_value = f"{opposite_outer}{raw_url}{opposite_outer} trailing=visible"
    same_outer_value = f"{query_quote}{raw_url}{query_quote} trailing=visible"
    unterminated_opposite_outer = f"{opposite_outer}{raw_url} trailing=visible"
    unterminated_same_outer = f"{query_quote}{raw_url} trailing=visible"
    empty_outer_value = f"{query_quote}https://example.test/cb?q={query_quote} trailing=visible"
    empty_quoted_value = f"https://example.test/cb?q={query_quote}{query_quote} trailing=visible"
    unterminated_value = (
        f"https://example.test/cb?q={query_quote}prefix {canary} suffix trailing=visible"
    )
    escaped_value = (
        f"https://example.test/cb?q={query_quote}prefix\\{query_quote} "
        f"{canary} suffix{query_quote} trailing=visible"
    )
    intent = replace(
        _intent(
            arguments={
                "value": value,
                "opposite_outer": opposite_outer_value,
                "same_outer": same_outer_value,
                "empty_outer": empty_outer_value,
                "empty_quoted": empty_quoted_value,
                "unterminated_opposite_outer": unterminated_opposite_outer,
                "unterminated_same_outer": unterminated_same_outer,
                "unterminated": unterminated_value,
                "escaped": escaped_value,
            }
        ),
        reason=value,
        target_summary=opposite_outer_value,
    )
    service = _service(FakeActionReadRepository((_aggregate(intent=intent),)))

    detail = await service.get("run-1", "intent-1", principal=PRINCIPAL)
    listed = await service.list("run-1", principal=PRINCIPAL)
    redacted_url = "https://example.test/[PATH]?q=%5BREDACTED%5D"
    expected = f"{redacted_url} trailing=visible"

    for serialized in (detail.model_dump_json(), listed.model_dump_json()):
        assert canary not in serialized
        assert "trailing=visible" in serialized
    assert detail.reason == expected
    assert detail.target_summary == "[REDACTED]"
    assert listed.items[0].reason == expected
    assert listed.items[0].target_summary == detail.target_summary
    assert detail.arguments_summary["value"] == expected
    assert detail.arguments_summary["opposite_outer"] == "[REDACTED]"
    assert detail.arguments_summary["same_outer"] == "[REDACTED]"
    # The quote after '=' could be either an empty-value outer close or a
    # leading-space quoted-value opener, so the redactor must not guess.
    assert detail.arguments_summary["empty_outer"] == "[REDACTED]"
    assert detail.arguments_summary["empty_quoted"] == expected
    assert detail.arguments_summary["unterminated_opposite_outer"] == "[REDACTED]"
    assert detail.arguments_summary["unterminated_same_outer"] == "[REDACTED]"
    assert detail.arguments_summary["unterminated"] == "[REDACTED]"
    assert detail.arguments_summary["escaped"] == "[REDACTED]"


@pytest.mark.parametrize("query_quote", ["'", '"'], ids=("single", "double"))
@pytest.mark.parametrize(
    "leading_boundary",
    [" ", "\t", "   ", "<", ">"],
    ids=("space", "tab", "multi-space", "less-than", "greater-than"),
)
@pytest.mark.parametrize("outer_mode", ["same", "opposite", "none"])
@pytest.mark.parametrize("closure", ["matching", "unmatched", "escaped"])
@pytest.mark.parametrize("position", ["immediate", "mid"])
async def test_leading_boundary_query_quote_never_leaks(
    query_quote: str,
    leading_boundary: str,
    outer_mode: str,
    closure: str,
    position: str,
) -> None:
    canary = "URI_LEADING_BOUNDARY_CANARY"
    outer_quote = {
        "same": query_quote,
        "opposite": '"' if query_quote == "'" else "'",
        "none": "",
    }[outer_mode]
    query_prefix = "" if position == "immediate" else "prefix"
    query_closer = {
        "matching": query_quote,
        "unmatched": "",
        "escaped": f"\\{query_quote}",
    }[closure]
    raw_url = (
        f"https://example.test/cb?q={query_prefix}{query_quote}"
        f"{leading_boundary}{canary}{query_closer}"
    )
    value = f"{outer_quote}{raw_url}{outer_quote} trailing=visible"
    intent = replace(
        _intent(arguments={"value": value}),
        reason=value,
        target_summary=value,
    )
    service = _service(FakeActionReadRepository((_aggregate(intent=intent),)))

    detail = await service.get("run-1", "intent-1", principal=PRINCIPAL)
    listed = await service.list("run-1", principal=PRINCIPAL)

    for serialized in (detail.model_dump_json(), listed.model_dump_json()):
        assert canary not in serialized
    safely_bounded = (
        closure == "matching" and outer_mode == "none" and leading_boundary in {" ", "   "}
    )
    if safely_bounded:
        redacted_url = "https://example.test/[PATH]?q=%5BREDACTED%5D"
        expected = f"{redacted_url} trailing=visible"
        assert "trailing=visible" in detail.reason
    else:
        expected = "[REDACTED]"
    assert detail.reason == expected
    assert detail.target_summary == expected
    assert listed.items[0].reason == expected
    assert listed.items[0].target_summary == expected
    assert detail.arguments_summary["value"] == expected


@pytest.mark.parametrize("outer_quote", ["'", '"'], ids=("single", "double"))
@pytest.mark.parametrize("position", ["immediate", "mid"])
async def test_same_outer_leading_space_without_later_quote_fails_closed(
    outer_quote: str,
    position: str,
) -> None:
    canary = "URI_NO_LATER_QUOTE_CANARY"
    query_prefix = "" if position == "immediate" else "prefix"
    value = (
        f"{outer_quote}https://example.test/cb?q={query_prefix}{outer_quote} "
        f"{canary} trailing=visible"
    )
    intent = replace(
        _intent(arguments={"value": value}),
        reason=value,
        target_summary=value,
    )
    service = _service(FakeActionReadRepository((_aggregate(intent=intent),)))

    detail = await service.get("run-1", "intent-1", principal=PRINCIPAL)
    listed = await service.list("run-1", principal=PRINCIPAL)

    for serialized in (detail.model_dump_json(), listed.model_dump_json()):
        assert canary not in serialized
    assert detail.reason == "[REDACTED]"
    assert detail.target_summary == "[REDACTED]"
    assert listed.items[0].reason == "[REDACTED]"
    assert listed.items[0].target_summary == "[REDACTED]"
    assert detail.arguments_summary["value"] == "[REDACTED]"


@pytest.mark.parametrize("outer_quote", ["'", '"'], ids=("single", "double"))
@pytest.mark.parametrize("suffix", ["", " ", "\t  "], ids=("end", "space", "whitespace"))
@pytest.mark.parametrize(
    ("raw_url", "redacted_url"),
    [
        ("https://example.test/path", "https://example.test/[PATH]"),
        (
            "https://example.test/cb?q=visible",
            "https://example.test/[PATH]?q=%5BREDACTED%5D",
        ),
    ],
    ids=("path", "query"),
)
async def test_outer_uri_quote_without_trailing_text_preserves_safe_projection(
    outer_quote: str,
    suffix: str,
    raw_url: str,
    redacted_url: str,
) -> None:
    value = f"{outer_quote}{raw_url}{outer_quote}{suffix}"
    intent = replace(
        _intent(arguments={"value": value}),
        reason=value,
        target_summary=value,
    )
    service = _service(FakeActionReadRepository((_aggregate(intent=intent),)))

    detail = await service.get("run-1", "intent-1", principal=PRINCIPAL)
    listed = await service.list("run-1", principal=PRINCIPAL)

    expected = (
        "[REDACTED]" if "\t" in suffix else f"{outer_quote}{redacted_url}{outer_quote}{suffix}"
    )
    assert detail.reason == expected
    assert detail.target_summary == expected
    assert listed.items[0].reason == expected
    assert listed.items[0].target_summary == expected
    assert detail.arguments_summary["value"] == expected


@pytest.mark.parametrize("position", ["immediate", "mid"])
@pytest.mark.parametrize("query_quote", ["'", '"'])
@pytest.mark.parametrize("space_count", [0, 1, 4])
@pytest.mark.parametrize("outer_mode", ["none", "same", "opposite"])
@pytest.mark.parametrize("closure", ["closed", "unterminated", "escaped"])
async def test_uri_quote_state_cross_matrix_never_serializes_canaries(
    position: str,
    query_quote: str,
    space_count: int,
    outer_mode: str,
    closure: str,
) -> None:
    canary = "URI_QUOTE_MATRIX_CANARY"
    if space_count == 0:
        payload = f"before{canary}suffix"
    elif space_count == 1:
        payload = f"before{canary} suffix"
    else:
        payload = f"before  {canary}  suffix"
    if closure == "escaped":
        quoted = f"{query_quote}before\\{query_quote}{payload}{query_quote}"
    elif closure == "unterminated":
        quoted = f"{query_quote}{payload}"
    else:
        quoted = f"{query_quote}{payload}{query_quote}"
    query_value = quoted if position == "immediate" else f"prefix{quoted}"
    raw_url = f"https://example.test/cb?q={query_value}"
    outer_quote = {
        "none": "",
        "same": query_quote,
        "opposite": '"' if query_quote == "'" else "'",
    }[outer_mode]
    value = f"{outer_quote}{raw_url}{outer_quote} trailing=visible"
    intent = replace(
        _intent(arguments={"value": value}),
        reason=value,
        target_summary=value,
    )
    service = _service(FakeActionReadRepository((_aggregate(intent=intent),)))

    detail = await service.get("run-1", "intent-1", principal=PRINCIPAL)
    listed = await service.list("run-1", principal=PRINCIPAL)

    for serialized in (detail.model_dump_json(), listed.model_dump_json()):
        assert canary not in serialized
    if closure == "closed" and outer_mode == "none":
        redacted_url = "https://example.test/[PATH]?q=%5BREDACTED%5D"
        expected = f"{outer_quote}{redacted_url}{outer_quote} trailing=visible"
        assert detail.reason == expected
        assert detail.target_summary == expected
        assert listed.items[0].reason == expected
        assert listed.items[0].target_summary == expected
        assert detail.arguments_summary["value"] == expected
    else:
        assert detail.reason == "[REDACTED]"
        assert detail.target_summary == "[REDACTED]"
        assert listed.items[0].reason == "[REDACTED]"
        assert listed.items[0].target_summary == "[REDACTED]"
        assert detail.arguments_summary["value"] == "[REDACTED]"


@pytest.mark.parametrize("query_quote", ["'", '"'])
@pytest.mark.parametrize("ambiguous_character", ["\r", "\n", "<", ">"])
async def test_uri_quote_state_fails_closed_at_multiline_or_angle_boundaries(
    query_quote: str,
    ambiguous_character: str,
) -> None:
    canary = "URI_BOUNDARY_CANARY"
    value = (
        f"https://example.test/cb?q=prefix{query_quote}before"
        f"{ambiguous_character}{canary}{query_quote} trailing=visible"
    )
    intent = replace(
        _intent(arguments={"value": value}),
        reason=value,
        target_summary=value,
    )
    service = _service(FakeActionReadRepository((_aggregate(intent=intent),)))

    detail = await service.get("run-1", "intent-1", principal=PRINCIPAL)
    listed = await service.list("run-1", principal=PRINCIPAL)

    for serialized in (detail.model_dump_json(), listed.model_dump_json()):
        assert canary not in serialized
    assert detail.reason == "[REDACTED]"
    assert detail.target_summary == "[REDACTED]"
    assert listed.items[0].reason == "[REDACTED]"
    assert listed.items[0].target_summary == "[REDACTED]"
    assert detail.arguments_summary["value"] == "[REDACTED]"


async def test_uri_apostrophes_stay_inside_tokens_until_a_clear_text_boundary() -> None:
    canary = "URI_APOSTROPHE_CANARY"
    path_uri = f"https://example.test/reset/'{canary}' after=visible"
    userinfo_uri = f"https://user:'{canary}'@example.test/path after=visible"
    query_uri = f"https://example.test/callback?code=prefix'{canary} after=visible"
    outer_single = "'https://example.test/reset/safe' after=visible"
    outer_double = '"https://example.test/reset/safe" after=visible'
    outer_with_apostrophe = f"\"https://example.test/reset/'{canary}'\" after=visible"
    intent = replace(
        _intent(
            arguments={
                "path_uri": path_uri,
                "userinfo_uri": userinfo_uri,
                "query_uri": query_uri,
                "outer_single": outer_single,
                "outer_double": outer_double,
                "outer_with_apostrophe": outer_with_apostrophe,
            }
        ),
        reason=path_uri,
        target_summary=userinfo_uri,
    )
    service = _service(FakeActionReadRepository((_aggregate(intent=intent),)))

    detail = await service.get("run-1", "intent-1", principal=PRINCIPAL)
    listed = await service.list("run-1", principal=PRINCIPAL)

    for serialized in (detail.model_dump_json(), listed.model_dump_json()):
        assert canary not in serialized
        assert "after=visible" in serialized
    assert detail.reason == "https://example.test/[PATH] after=visible"
    assert detail.target_summary == "https://[REDACTED]@example.test/[PATH] after=visible"
    assert detail.arguments_summary["query_uri"] == "[REDACTED]"
    assert detail.arguments_summary["outer_single"] == "[REDACTED]"
    assert detail.arguments_summary["outer_double"] == "[REDACTED]"
    assert detail.arguments_summary["outer_with_apostrophe"] == "[REDACTED]"


async def test_outer_single_quote_with_internal_apostrophe_fails_closed() -> None:
    path_value = "'https://example.test/path/O'.segment/PATH_PUNC_CANARY' trailing=visible"
    userinfo_value = "'https://user'.name:USERINFO_PUNC_CANARY@example.test/path' trailing=visible"
    query_value = "'https://example.test/cb?q=O'.value=QUERY_PUNC_CANARY' trailing=visible"
    intent = replace(
        _intent(arguments={"query_value": query_value}),
        reason=path_value,
        target_summary=userinfo_value,
    )
    service = _service(FakeActionReadRepository((_aggregate(intent=intent),)))

    detail = await service.get("run-1", "intent-1", principal=PRINCIPAL)
    listed = await service.list("run-1", principal=PRINCIPAL)

    for serialized in (detail.model_dump_json(), listed.model_dump_json()):
        assert "PUNC_CANARY" not in serialized
    assert detail.reason == "[REDACTED]"
    assert detail.target_summary == "[REDACTED]"
    assert listed.items[0].reason == detail.reason
    assert listed.items[0].target_summary == detail.target_summary
    assert detail.arguments_summary["query_value"] == "[REDACTED]"


@pytest.mark.parametrize(
    ("component", "punctuation"),
    [
        (component, punctuation)
        for component in ("path", "userinfo", "query")
        for punctuation in (
            ".",
            ",",
            "!",
            "$",
            "&",
            "'",
            "(",
            ")",
            "*",
            "+",
            ";",
            "=",
            ":",
            "?",
            "@",
        )
        if component != "userinfo" or punctuation != "?"
    ],
)
@pytest.mark.parametrize("outer_quote", ["'", '"'])
@pytest.mark.parametrize("trailing_mode", ["whitespace", "punctuation"])
async def test_outer_uri_quote_rfc_punctuation_matrix_never_leaks(
    punctuation: str,
    component: str,
    outer_quote: str,
    trailing_mode: str,
) -> None:
    if component == "path":
        raw_url = f"https://example.test/PATH_PUNC_CANARY/O'{punctuation}segment"
    elif component == "userinfo":
        raw_url = f"https://user-USERINFO_PUNC_CANARY'{punctuation}name:pw@example.test/path"
    else:
        raw_url = f"https://example.test/cb?q=QUERY_PUNC_CANARY-O'{punctuation}value"
    trailing = " trailing=visible" if trailing_mode == "whitespace" else ",trailing=visible"
    value = f"{outer_quote}{raw_url}{outer_quote}{trailing}"
    intent = replace(
        _intent(arguments={"value": value}),
        reason=value,
        target_summary=value,
    )
    service = _service(FakeActionReadRepository((_aggregate(intent=intent),)))

    detail = await service.get("run-1", "intent-1", principal=PRINCIPAL)
    listed = await service.list("run-1", principal=PRINCIPAL)

    for serialized in (detail.model_dump_json(), listed.model_dump_json()):
        assert "PUNC_CANARY" not in serialized
    assert detail.reason == "[REDACTED]"
    assert detail.target_summary == "[REDACTED]"
    assert listed.items[0].reason == "[REDACTED]"
    assert listed.items[0].target_summary == "[REDACTED]"
    assert detail.arguments_summary["value"] == "[REDACTED]"


@pytest.mark.parametrize("outer_quote", ["'", '"'])
async def test_outer_uri_quote_with_question_mark_in_userinfo_fails_closed(
    outer_quote: str,
) -> None:
    canary = "MALFORMED_USERINFO_CANARY"
    value = (
        f"{outer_quote}https://user-{canary}'?name:pw@example.test/path"
        f"{outer_quote} trailing=visible"
    )
    intent = replace(
        _intent(arguments={"value": value}),
        reason=value,
        target_summary=value,
    )
    service = _service(FakeActionReadRepository((_aggregate(intent=intent),)))

    detail = await service.get("run-1", "intent-1", principal=PRINCIPAL)
    listed = await service.list("run-1", principal=PRINCIPAL)

    expected = "[REDACTED]"
    for serialized in (detail.model_dump_json(), listed.model_dump_json()):
        assert canary not in serialized
    assert detail.reason == expected
    assert detail.target_summary == expected
    assert listed.items[0].reason == expected
    assert listed.items[0].target_summary == expected
    assert detail.arguments_summary["value"] == expected


@pytest.mark.parametrize("delimiter", ["?", "#"], ids=("query", "fragment"))
@pytest.mark.parametrize(
    "displaced_host",
    [
        "localhost",
        "localhost:8080",
        "127.0.0.1",
        "[::1]",
        "xn--bcher-kva.example",
        "example.test",
        "example.test/path",
    ],
    ids=("localhost", "port", "ipv4", "ipv6", "punycode", "domain", "path"),
)
async def test_raw_at_sign_after_authority_delimiter_fails_closed(
    displaced_host: str,
    delimiter: str,
) -> None:
    canary = "V7-USERINFO-SPILL-CANARY"
    value = f"https://user:{delimiter}{canary}@{displaced_host}"
    intent = replace(
        _intent(arguments={"value": value}),
        reason=value,
        target_summary=value,
    )
    service = _service(FakeActionReadRepository((_aggregate(intent=intent),)))

    detail = await service.get("run-1", "intent-1", principal=PRINCIPAL)
    listed = await service.list("run-1", principal=PRINCIPAL)

    for serialized in (detail.model_dump_json(), listed.model_dump_json()):
        assert canary not in serialized
    assert detail.reason == "[REDACTED]"
    assert detail.target_summary == "[REDACTED]"
    assert listed.items[0].reason == "[REDACTED]"
    assert listed.items[0].target_summary == "[REDACTED]"
    assert detail.arguments_summary["value"] == "[REDACTED]"


@pytest.mark.parametrize(
    ("value", "canary"),
    [
        (
            "https://example.test?email=RAW_AT_QUERY_CANARY@example.test",
            "RAW_AT_QUERY_CANARY",
        ),
        (
            "https://example.test#RAW_AT_FRAGMENT_CANARY@example.test",
            "RAW_AT_FRAGMENT_CANARY",
        ),
    ],
    ids=("query-email", "fragment-email"),
)
async def test_raw_at_sign_in_authority_direct_query_or_fragment_fails_closed(
    value: str,
    canary: str,
) -> None:
    intent = replace(
        _intent(arguments={"value": value}),
        reason=value,
        target_summary=value,
    )
    service = _service(FakeActionReadRepository((_aggregate(intent=intent),)))

    detail = await service.get("run-1", "intent-1", principal=PRINCIPAL)
    listed = await service.list("run-1", principal=PRINCIPAL)

    for serialized in (detail.model_dump_json(), listed.model_dump_json()):
        assert canary not in serialized
    assert detail.reason == "[REDACTED]"
    assert detail.target_summary == "[REDACTED]"
    assert listed.items[0].reason == "[REDACTED]"
    assert listed.items[0].target_summary == "[REDACTED]"
    assert detail.arguments_summary["value"] == "[REDACTED]"


@pytest.mark.parametrize(
    ("value", "canary", "expected"),
    [
        (
            "https://example.test/users/LEGAL_PATH_AT_CANARY@operator/profile",
            "LEGAL_PATH_AT_CANARY",
            "https://example.test/[PATH]",
        ),
        (
            "https://user:STANDARD_USERINFO_CANARY@example.test/path",
            "STANDARD_USERINFO_CANARY",
            "https://[REDACTED]@example.test/[PATH]",
        ),
        (
            "https://example.test?email=ENCODED_QUERY_AT_CANARY%40example.test",
            "ENCODED_QUERY_AT_CANARY",
            "https://example.test?email=%5BREDACTED%5D",
        ),
        (
            "https://example.test#ENCODED_FRAGMENT_AT_CANARY%40example.test",
            "ENCODED_FRAGMENT_AT_CANARY",
            "https://example.test#[REDACTED]",
        ),
    ],
    ids=("path-raw-at", "standard-userinfo", "query-encoded-at", "fragment-encoded-at"),
)
async def test_uri_at_sign_controls_preserve_safe_projection(
    value: str,
    canary: str,
    expected: str,
) -> None:
    intent = replace(
        _intent(arguments={"value": value}),
        reason=value,
        target_summary=value,
    )
    service = _service(FakeActionReadRepository((_aggregate(intent=intent),)))

    detail = await service.get("run-1", "intent-1", principal=PRINCIPAL)
    listed = await service.list("run-1", principal=PRINCIPAL)

    for serialized in (detail.model_dump_json(), listed.model_dump_json()):
        assert canary not in serialized
    assert detail.reason == expected
    assert detail.target_summary == expected
    assert listed.items[0].reason == expected
    assert listed.items[0].target_summary == expected
    assert detail.arguments_summary["value"] == expected


@pytest.mark.parametrize("outer_quote", ["'", '"'])
async def test_escaped_outer_uri_quote_fails_closed_before_boundary_detection(
    outer_quote: str,
) -> None:
    canary = "OUTER_QUOTE_ESCAPE_CANARY"
    value = (
        f"{outer_quote}https://example.test/cb?q=prefix\\{outer_quote} "
        f"{canary} suffix{outer_quote} trailing=visible"
    )
    intent = replace(
        _intent(arguments={"value": value}),
        reason=value,
        target_summary=value,
    )
    service = _service(FakeActionReadRepository((_aggregate(intent=intent),)))

    detail = await service.get("run-1", "intent-1", principal=PRINCIPAL)
    listed = await service.list("run-1", principal=PRINCIPAL)

    for serialized in (detail.model_dump_json(), listed.model_dump_json()):
        assert canary not in serialized
    assert detail.reason == "[REDACTED]"
    assert detail.target_summary == "[REDACTED]"
    assert listed.items[0].reason == "[REDACTED]"
    assert listed.items[0].target_summary == "[REDACTED]"
    assert detail.arguments_summary["value"] == "[REDACTED]"


@pytest.mark.parametrize("quote", ["'", '"'])
@pytest.mark.parametrize(
    "absolute_path",
    [
        "/Users/operator/Private Folder/PATH_SPACE_CANARY.txt",
        r"C:\Users\operator\Private Folder\PATH_SPACE_CANARY.txt",
        r"\\server\share\Private Folder\PATH_SPACE_CANARY.txt",
    ],
    ids=("posix", "windows", "unc"),
)
async def test_quoted_absolute_paths_with_spaces_redact_through_the_closing_quote(
    absolute_path: str,
    quote: str,
) -> None:
    canary = "PATH_SPACE_CANARY.txt"
    quoted_path = f"{quote}{absolute_path}{quote} after=visible"
    unterminated_path = f"{quote}{absolute_path}"
    escaped_absolute_path = absolute_path.replace(canary, f"before\\{quote}{canary}")
    escaped_quoted_path = f"{quote}{escaped_absolute_path}{quote} after=visible"
    benign_posix_relative = f"{quote}Private Folder/report.txt{quote} after=visible"
    benign_windows_relative = rf"{quote}Private Folder\report.txt{quote} after=visible"
    intent = replace(
        _intent(
            arguments={
                "quoted_path": quoted_path,
                "unterminated_path": unterminated_path,
                "escaped_quoted_path": escaped_quoted_path,
                "benign_posix_relative": benign_posix_relative,
                "benign_windows_relative": benign_windows_relative,
            }
        ),
        reason=escaped_quoted_path,
        target_summary=unterminated_path,
    )
    service = _service(FakeActionReadRepository((_aggregate(intent=intent),)))

    detail = await service.get("run-1", "intent-1", principal=PRINCIPAL)
    listed = await service.list("run-1", principal=PRINCIPAL)
    expected = f"{quote}[PATH]{quote} after=visible"

    for serialized in (detail.model_dump_json(), listed.model_dump_json()):
        assert canary not in serialized
    assert detail.reason == "[REDACTED]"
    assert detail.target_summary == "[REDACTED]"
    assert listed.items[0].reason == "[REDACTED]"
    assert listed.items[0].target_summary == "[REDACTED]"
    assert detail.arguments_summary["quoted_path"] == expected
    assert detail.arguments_summary["unterminated_path"] == "[REDACTED]"
    assert detail.arguments_summary["escaped_quoted_path"] == "[REDACTED]"
    assert detail.arguments_summary["benign_posix_relative"] == benign_posix_relative
    assert detail.arguments_summary["benign_windows_relative"] == benign_windows_relative


@pytest.mark.parametrize(
    "absolute_path",
    [
        "/Users/operator/Private Folder/POSIX_PATH_CANARY.txt",
        r"C:\Users\operator\Private Folder\WINDOWS_PATH_CANARY.txt",
        r"\\server\share\Private Folder\UNC_PATH_CANARY.txt",
    ],
    ids=("posix", "windows", "unc"),
)
async def test_unquoted_absolute_paths_with_spaces_fail_closed_without_tail_text(
    absolute_path: str,
) -> None:
    text_value = f"inspect {absolute_path} trailing=visible"
    relative_control = "Private Folder/report.txt"
    uri_control = "https://example.test/Private%20Folder/report.txt?q=visible"
    intent = replace(
        _intent(
            arguments={
                "raw_path": absolute_path,
                "text_value": text_value,
                "relative_control": relative_control,
                "uri_control": uri_control,
            }
        ),
        reason=absolute_path,
        target_summary=text_value,
    )
    service = _service(FakeActionReadRepository((_aggregate(intent=intent),)))

    detail = await service.get("run-1", "intent-1", principal=PRINCIPAL)
    listed = await service.list("run-1", principal=PRINCIPAL)

    for serialized in (detail.model_dump_json(), listed.model_dump_json()):
        assert "PATH_CANARY" not in serialized
    assert detail.reason == "[PATH]"
    assert detail.target_summary == "[PATH]"
    assert listed.items[0].reason == "[PATH]"
    assert listed.items[0].target_summary == "[PATH]"
    assert detail.arguments_summary["raw_path"] == "[PATH]"
    assert detail.arguments_summary["text_value"] == "[PATH]"
    assert detail.arguments_summary["relative_control"] == relative_control
    assert detail.arguments_summary["uri_control"] == (
        "https://example.test/[PATH]?q=%5BREDACTED%5D"
    )


@pytest.mark.parametrize(
    "absolute_path",
    [
        "/Users/operator/Private Folder/URI_POSIX_PATH_CANARY.txt",
        r"C:\Users\operator\Private Folder\URI_WINDOWS_PATH_CANARY.txt",
        r"\\server\share\Private Folder\URI_UNC_PATH_CANARY.txt",
    ],
    ids=("posix", "windows", "unc"),
)
async def test_uri_query_with_ambiguous_absolute_path_is_checked_before_uri_projection(
    absolute_path: str,
) -> None:
    value = f"https://example.test/cb?next={absolute_path} trailing=visible"
    normal_uri = "https://example.test/cb?next=relative%2Freport.txt"
    intent = replace(
        _intent(arguments={"value": value, "normal_uri": normal_uri}),
        reason=value,
        target_summary=value,
    )
    service = _service(FakeActionReadRepository((_aggregate(intent=intent),)))

    detail = await service.get("run-1", "intent-1", principal=PRINCIPAL)
    listed = await service.list("run-1", principal=PRINCIPAL)

    for serialized in (detail.model_dump_json(), listed.model_dump_json()):
        assert "PATH_CANARY" not in serialized
    assert detail.reason == "[PATH]"
    assert detail.target_summary == "[PATH]"
    assert listed.items[0].reason == "[PATH]"
    assert listed.items[0].target_summary == "[PATH]"
    assert detail.arguments_summary["value"] == "[PATH]"
    assert detail.arguments_summary["normal_uri"] == (
        "https://example.test/[PATH]?next=%5BREDACTED%5D"
    )


async def test_display_identifiers_are_redacted_in_list_and_detail_serialization() -> None:
    canary = "RIFTX_TEST_SECRET_DO_NOT_LEAK_ID_123"
    intent = replace(
        _intent(status=ToolCallStatus.EXECUTING),
        engine_call_id=canary,
        tool_id=canary,
        skill_id=canary,
    )
    execution = ActionExecutionRead(
        execution_id="execution-1",
        attempt_group=canary,
        node_id=canary,
        status=ExecutionStatus.RUNNING,
        created_at=NOW,
        started_at=NOW,
        finished_at=None,
        exit_code=None,
        correlation_quality=ActionCorrelationQuality.EXACT,
        error_summary=None,
    )
    service = _service(
        FakeActionReadRepository((_aggregate(intent=intent, executions=(execution,)),))
    )

    detail = await service.get("run-1", "intent-1", principal=PRINCIPAL)
    listed = await service.list("run-1", principal=PRINCIPAL)

    for serialized in (detail.model_dump_json(), listed.model_dump_json()):
        assert canary not in serialized
        assert "[REDACTED]" in serialized
    assert detail.engine_call_id == "[REDACTED]"
    assert detail.tool_id == "[REDACTED]"
    assert detail.skill_id == "[REDACTED]"
    assert detail.executions[0].node_id == "[REDACTED]"


@pytest.mark.parametrize("unsafe_character", UNSAFE_UNICODE_CHARACTERS)
@pytest.mark.parametrize(
    "display_field",
    ["engine_call_id", "tool_id", "skill_id", "attempt_group", "node_id"],
)
async def test_display_identifiers_with_unsafe_unicode_redact_the_whole_field(
    unsafe_character: str,
    display_field: str,
) -> None:
    canary = f"DISPLAY_UNICODE_CANARY{unsafe_character}TAIL"
    intent = _intent(status=ToolCallStatus.EXECUTING)
    execution = _execution_read()
    if display_field in {"engine_call_id", "tool_id", "skill_id"}:
        intent = replace(intent, **{display_field: canary})
    else:
        execution = replace(execution, **{display_field: canary})
    service = _service(
        FakeActionReadRepository((_aggregate(intent=intent, executions=(execution,)),))
    )

    detail = await service.get("run-1", "intent-1", principal=PRINCIPAL)
    listed = await service.list("run-1", principal=PRINCIPAL)

    for serialized in (detail.model_dump_json(), listed.model_dump_json()):
        assert "DISPLAY_UNICODE_CANARY" not in serialized
    if display_field in {"engine_call_id", "tool_id", "skill_id"}:
        assert getattr(detail, display_field) == "[REDACTED]"
        assert getattr(listed.items[0], display_field) == "[REDACTED]"
    elif display_field == "attempt_group":
        assert detail.executions[0].attempt_group == "[REDACTED]"
        assert listed.items[0].attempts[0].attempt_group == "[REDACTED]"
    else:
        assert detail.executions[0].node_id == "[REDACTED]"


@pytest.mark.parametrize("unsafe_character", UNSAFE_UNICODE_CHARACTERS)
async def test_ordinary_text_with_unsafe_unicode_redacts_whole_fields(
    unsafe_character: str,
) -> None:
    canary = f"TEXT_UNICODE_CANARY{unsafe_character}TAIL"
    intent = replace(
        _intent(
            status=ToolCallStatus.EXECUTING,
            arguments={"unsafe": canary, "safe_after": "visible"},
        ),
        reason=canary,
        target_summary=canary,
    )
    aggregate = _aggregate(
        intent=intent,
        approval=_approval_read(feedback=canary),
        executions=(_execution_read(error_summary=canary),),
    )
    service = _service(FakeActionReadRepository((aggregate,)))

    detail = await service.get("run-1", "intent-1", principal=PRINCIPAL)
    listed = await service.list("run-1", principal=PRINCIPAL)

    for serialized in (detail.model_dump_json(), listed.model_dump_json()):
        assert "TEXT_UNICODE_CANARY" not in serialized
    assert detail.reason == "[REDACTED]"
    assert detail.target_summary == "[REDACTED]"
    assert detail.arguments_summary["unsafe"] == "[REDACTED]"
    assert detail.arguments_summary["safe_after"] == "visible"
    assert detail.approval is not None
    assert detail.approval.feedback_summary == "[REDACTED]"
    assert detail.executions[0].error_summary == "[REDACTED]"
    assert listed.items[0].reason == "[REDACTED]"
    assert listed.items[0].target_summary == "[REDACTED]"


@pytest.mark.parametrize("unsafe_character", UNSAFE_UNICODE_CHARACTERS)
def test_unsafe_unicode_replacement_consumes_shared_budget(unsafe_character: str) -> None:
    replacement_size = len(b"[REDACTED]")
    budget = actions_service._RedactionBudget(bytes_remaining=replacement_size)

    assert budget.text(f"BUDGET_CANARY{unsafe_character}TAIL") == "[REDACTED]"
    assert budget.bytes_remaining == 0


@pytest.mark.parametrize(
    "unsafe_key",
    ["tok\u200ben", "api\u200b_key", "authoriza\u202etion", "pass\u2066word"],
)
@pytest.mark.parametrize("placement", ["root", "nested", "list-in-map"])
async def test_unsafe_unicode_mapping_keys_redact_values_without_recursing(
    unsafe_key: str,
    placement: str,
) -> None:
    canary = "GENERIC_SECRET_VALUE_CANARY_7H3K9"
    unsafe_mapping: object = {unsafe_key: canary}
    if placement == "nested":
        arguments = {"container": unsafe_mapping}
    elif placement == "list-in-map":
        arguments = {"container": [unsafe_mapping]}
    else:
        arguments = unsafe_mapping
    service = _service(
        FakeActionReadRepository((_aggregate(intent=_intent(arguments=arguments)),))  # type: ignore[arg-type]
    )

    detail = await service.get("run-1", "intent-1", principal=PRINCIPAL)
    listed = await service.list("run-1", principal=PRINCIPAL)

    for serialized in (detail.model_dump_json(), listed.model_dump_json()):
        assert canary not in serialized
    if placement == "root":
        projected = detail.arguments_summary
    elif placement == "nested":
        projected = detail.arguments_summary["container"]
    else:
        projected = detail.arguments_summary["container"][0]  # type: ignore[index]
    assert projected == {"[REDACTED]": "[REDACTED]"}


@pytest.mark.parametrize("unsafe_character", UNSAFE_UNICODE_CHARACTERS)
async def test_all_unsafe_unicode_categories_in_mapping_keys_redact_values(
    unsafe_character: str,
) -> None:
    canary = "UNSAFE_KEY_CATEGORY_VALUE_CANARY"
    arguments = {f"prefix{unsafe_character}suffix": canary}
    detail = await _service(
        FakeActionReadRepository((_aggregate(intent=_intent(arguments=arguments)),))
    ).get("run-1", "intent-1", principal=PRINCIPAL)

    assert canary not in detail.model_dump_json()
    assert detail.arguments_summary == {"[REDACTED]": "[REDACTED]"}


@pytest.mark.parametrize("value_kind", ["string", "mapping", "sequence", "object"])
async def test_unsafe_unicode_mapping_key_never_visits_child(value_kind: str) -> None:
    canary = "UNSAFE_KEY_CHILD_CANARY"

    class ExplodingValue:
        def __str__(self) -> str:
            raise AssertionError("unsafe-key child must not be visited")

    child: object = {
        "string": canary,
        "mapping": {"nested": canary},
        "sequence": [canary, {"nested": canary}],
        "object": ExplodingValue(),
    }[value_kind]
    detail = await _service(
        FakeActionReadRepository((_aggregate(intent=_intent(arguments={"tok\u200ben": child})),))
    ).get("run-1", "intent-1", principal=PRINCIPAL)

    assert canary not in detail.model_dump_json()
    assert detail.arguments_summary == {"[REDACTED]": "[REDACTED]"}


@pytest.mark.parametrize("safe_marker_first", [True, False], ids=("safe-first", "unsafe-first"))
async def test_redacted_mapping_key_collisions_are_sticky_and_deterministic(
    safe_marker_first: bool,
) -> None:
    entries = [
        ("[REDACTED]", "SAFE_MARKER_VALUE_CANARY"),
        ("tok\u200ben", "UNSAFE_VALUE_CANARY_ONE"),
        ("authoriza\u202etion", {"nested": "UNSAFE_VALUE_CANARY_TWO"}),
        ("token", "SENSITIVE_VALUE_CANARY"),
    ]
    if not safe_marker_first:
        entries = entries[1:3] + entries[:1] + entries[3:]
    detail = await _service(
        FakeActionReadRepository((_aggregate(intent=_intent(arguments=dict(entries))),))
    ).get("run-1", "intent-1", principal=PRINCIPAL)
    serialized = detail.model_dump_json()

    assert "VALUE_CANARY" not in serialized
    assert detail.arguments_summary == {
        "[REDACTED]": "[REDACTED]",
        "token": "[REDACTED]",
    }


async def test_overlong_mapping_key_uses_safe_marker_and_redacts_value() -> None:
    canary = "OVERLONG_KEY_VALUE_CANARY"
    detail = await _service(
        FakeActionReadRepository((_aggregate(intent=_intent(arguments={"k" * 513: canary})),))
    ).get("run-1", "intent-1", principal=PRINCIPAL)

    assert canary not in detail.model_dump_json()
    assert detail.arguments_summary == {"[TRUNCATED]": "[REDACTED]"}


def test_unsafe_mapping_key_and_redacted_value_consume_shared_budget() -> None:
    marker_bytes = len(b"[REDACTED]")
    budget = actions_service._RedactionBudget(
        nodes_remaining=3,
        bytes_remaining=marker_bytes * 2,
    )

    projected = actions_service._redact(
        {"tok\u200ben": "BUDGET_VALUE_CANARY"},
        _budget=budget,
    )

    assert projected == {"[REDACTED]": "[REDACTED]"}
    assert budget.nodes_remaining == 1
    assert budget.bytes_remaining == 0


async def test_reference_identifiers_with_secret_markers_fail_closed() -> None:
    canary = "RIFTX_TEST_SECRET_DO_NOT_LEAK_ID_123"
    event = ActionEventRead(
        event_id="event-1",
        sequence=1,
        event_type="tool.proposed",
        created_at=NOW,
    )
    base = _aggregate(events=(event,))
    execution = ActionExecutionRead(
        execution_id=canary,
        attempt_group=None,
        node_id="local",
        status=ExecutionStatus.RUNNING,
        created_at=NOW,
        started_at=NOW,
        finished_at=None,
        exit_code=None,
        correlation_quality=ActionCorrelationQuality.EXACT,
        error_summary=None,
    )
    invalid_detail_aggregates = (
        replace(
            base,
            intent=replace(base.intent, session_id=canary),
        ),
        replace(
            base,
            result=replace(base.result, artifact_ids=(canary,)),
        ),
        replace(base, finding_ids=(canary,)),
        replace(base, events=(replace(event, event_id=canary),)),
        _aggregate(
            intent=_intent(status=ToolCallStatus.EXECUTING),
            executions=(execution,),
        ),
    )

    for aggregate in invalid_detail_aggregates:
        with pytest.raises(RuntimeError, match="invalid Action aggregate"):
            await _service(FakeActionReadRepository((aggregate,))).get(
                "run-1", aggregate.intent.action_id, principal=PRINCIPAL
            )

    for aggregate in (
        invalid_detail_aggregates[0],
        invalid_detail_aggregates[1],
        invalid_detail_aggregates[4],
    ):
        with pytest.raises(RuntimeError, match="invalid Action aggregate"):
            await _service(FakeActionReadRepository((aggregate,))).list(
                "run-1", principal=PRINCIPAL
            )


@pytest.mark.parametrize(
    "invalid_id",
    [" artifact-1", "artifact-1 ", "   ", "artifact\x00id", "artifact\x7fid", "artifact\x85id"],
)
async def test_reference_identifiers_reject_trim_instability_and_control_characters(
    invalid_id: str,
) -> None:
    aggregate = _aggregate()
    aggregate = replace(
        aggregate,
        result=replace(aggregate.result, artifact_ids=(invalid_id,)),
    )
    service = _service(FakeActionReadRepository((aggregate,)))

    with pytest.raises(RuntimeError, match="invalid Action aggregate"):
        await service.get("run-1", "intent-1", principal=PRINCIPAL)
    with pytest.raises(RuntimeError, match="invalid Action aggregate"):
        await service.list("run-1", principal=PRINCIPAL)


async def test_reference_identifiers_keep_uuid_colon_dot_and_hyphen_forms() -> None:
    reference_id = "550e8400-e29b-41d4-a716-446655440000:part.name-1"
    aggregate = _aggregate()
    aggregate = replace(
        aggregate,
        result=replace(aggregate.result, artifact_ids=(reference_id,)),
    )
    service = _service(FakeActionReadRepository((aggregate,)))

    detail = await service.get("run-1", "intent-1", principal=PRINCIPAL)
    listed = await service.list("run-1", principal=PRINCIPAL)

    assert detail.result.artifact_ids == (reference_id,)
    assert listed.items[0].artifact_ids == (reference_id,)


@pytest.mark.parametrize("unsafe_character", UNSAFE_UNICODE_CHARACTERS)
@pytest.mark.parametrize(
    "reference_kind",
    [
        "action_id",
        "run_id",
        "session_id",
        "cycle_id",
        "step_id",
        "approval_id",
        "execution_id",
        "artifact_id",
        "finding_id",
        "event_id",
    ],
)
async def test_reference_identifiers_reject_unsafe_unicode_before_versioning(
    unsafe_character: str,
    reference_kind: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    canary = f"REFERENCE_UNICODE_CANARY{unsafe_character}TAIL"
    aggregate = _aggregate_with_reference_id(reference_kind, canary)
    repository_type = (
        PermissiveRunRepository if reference_kind == "run_id" else FakeActionReadRepository
    )
    service = _service(repository_type((aggregate,)))
    version_calls: list[object] = []
    original_versioner = actions_service._action_metadata_version

    def record_version(*args: object, **kwargs: object) -> str:
        version_calls.append((args, kwargs))
        return "0" * 64

    monkeypatch.setattr(actions_service, "_action_metadata_version", record_version)
    with pytest.raises(RuntimeError, match="invalid Action aggregate") as detail_error:
        await service.get(
            aggregate.intent.run_id,
            aggregate.intent.action_id,
            principal=PRINCIPAL,
        )
    assert "REFERENCE_UNICODE_CANARY" not in str(detail_error.value)
    assert version_calls == []

    if reference_kind not in {"finding_id", "event_id"}:
        with pytest.raises(RuntimeError, match="invalid Action aggregate") as list_error:
            await service.list(aggregate.intent.run_id, principal=PRINCIPAL)
        assert "REFERENCE_UNICODE_CANARY" not in str(list_error.value)
        assert version_calls == []
    else:
        # Finding and Event IDs are intentionally absent from the list repository DTO.
        monkeypatch.setattr(actions_service, "_action_metadata_version", original_versioner)
        listed = await service.list(aggregate.intent.run_id, principal=PRINCIPAL)
        assert "REFERENCE_UNICODE_CANARY" not in listed.model_dump_json()


async def test_reference_identifiers_preserve_safe_unicode_without_nfc_rewriting() -> None:
    safe_ids = {
        "action": "动作-550e8400-e29b-41d4-a716-446655440000:part.name",
        "run": "运行-1",
        "session": "cafe\u0301:会话-1",
        "cycle": "周期.1",
        "step": "步骤-1",
        "approval": "批准:1",
        "execution": "执行.1",
        "artifact": "制品-1",
        "finding": "发现:1",
        "event": "事件.1",
    }
    intent = replace(
        _intent(action_id=safe_ids["action"], status=ToolCallStatus.EXECUTING),
        run_id=safe_ids["run"],
        session_id=safe_ids["session"],
        cycle_id=safe_ids["cycle"],
        step_id=safe_ids["step"],
    )
    aggregate = _aggregate(
        intent=intent,
        approval=_approval_read(approval_id=safe_ids["approval"]),
        executions=(_execution_read(execution_id=safe_ids["execution"]),),
        events=(
            ActionEventRead(
                event_id=safe_ids["event"],
                sequence=1,
                event_type="tool.proposed",
                created_at=NOW,
            ),
        ),
    )
    aggregate = replace(
        aggregate,
        result=replace(aggregate.result, artifact_ids=(safe_ids["artifact"],)),
        finding_ids=(safe_ids["finding"],),
    )
    service = _service(PermissiveRunRepository((aggregate,)))

    detail = await service.get(safe_ids["run"], safe_ids["action"], principal=PRINCIPAL)
    listed = await service.list(safe_ids["run"], principal=PRINCIPAL)

    assert detail.action_id == safe_ids["action"]
    assert detail.run_id == safe_ids["run"]
    assert detail.session_id == safe_ids["session"]
    assert detail.cycle_id == safe_ids["cycle"]
    assert detail.step_id == safe_ids["step"]
    assert detail.approval is not None
    assert detail.approval.approval_id == safe_ids["approval"]
    assert detail.executions[0].execution_id == safe_ids["execution"]
    assert detail.result.artifact_ids == (safe_ids["artifact"],)
    assert detail.evidence.finding_ids == (safe_ids["finding"],)
    assert detail.evidence.events[0].event_id == safe_ids["event"]
    assert listed.items[0].action_id == safe_ids["action"]
    assert listed.items[0].run_id == safe_ids["run"]
    assert len(detail.version) == 64
    assert listed.items[0].version == detail.version


async def test_action_projection_has_a_shared_recursive_collection_budget() -> None:
    value: object = "leaf"
    for _ in range(8):
        value = {f"branch-{index}": value for index in range(40)}
    aggregate = _aggregate(
        intent=_intent(
            arguments={
                "non_finite": [float("nan"), float("inf"), float("-inf")],
                "wide_numbers": [10**4000 for _ in range(32)],
                "tree": value,
            }
        )
    )

    view = await _service(FakeActionReadRepository((aggregate,))).get(
        "run-1",
        "intent-1",
        principal=PRINCIPAL,
    )
    serialized = view.model_dump_json()

    assert "[TRUNCATED]" in serialized
    assert "[INVALID]" in serialized
    assert len(serialized.encode()) < 64 * 1024


@pytest.mark.parametrize(
    "private_marker",
    [
        "-----BEGIN PRIVATE KEY-----",
        "-----BEGIN RSA PRIVATE KEY-----",
        "-----BEGIN OPENSSH PRIVATE KEY-----",
        "-----BEGIN ENCRYPTED PRIVATE KEY-----",
        "-----BEGIN ED25519 PRIVATE KEY-----",
        "-----BEGIN PGP PRIVATE KEY BLOCK-----",
        "-----BEGIN FOO-BAR PRIVATE KEY-----",
        "-----END PRIVATE KEY-----",
        "-----END ENCRYPTED PRIVATE KEY-----",
        "-----END ED25519 PRIVATE KEY-----",
        "-----END FOO-BAR PRIVATE KEY BLOCK-----",
        f"-----BEGIN {'A' * 65} PRIVATE KEY-----",
        f"-----END {'B-' * 256} PRIVATE KEY BLOCK-----",
    ],
)
async def test_high_confidence_secret_markers_are_redacted_from_list_and_detail(
    private_marker: str,
) -> None:
    canary = "RIFTX_TEST_SECRET_DO_NOT_LEAK_ACTION_123"
    private_material_canary = f"{private_marker}\nACTION-PRIVATE-MATERIAL-CANARY"
    approval = ActionApprovalRead(
        approval_id="approval-1",
        runtime_status=ApprovalStatus.APPROVED,
        public_status=ApprovalStatus.APPROVED,
        runtime_decided_by=PRINCIPAL_ID,
        public_decided_by=PRINCIPAL_ID,
        runtime_decided_at=NOW + timedelta(seconds=1),
        public_decided_at=NOW,
        feedback=f"feedback {canary}",
        bridge_correlation_quality=ActionCorrelationQuality.EXACT,
        bridge_partial_reasons=(),
    )
    execution = ActionExecutionRead(
        execution_id="execution-1",
        attempt_group=None,
        node_id="local",
        status=ExecutionStatus.FAILED,
        created_at=NOW,
        started_at=NOW,
        finished_at=NOW + timedelta(seconds=1),
        exit_code=None,
        correlation_quality=ActionCorrelationQuality.EXACT,
        error_summary=f"error {canary}",
    )
    aggregate = _aggregate(
        intent=replace(
            _intent(status=ToolCallStatus.FAILED),
            reason=private_material_canary,
            target_summary=private_material_canary,
            arguments={
                "canary": canary,
                "marker_canary": private_material_canary,
                "public_key_begin_control": "-----BEGIN PUBLIC KEY-----",
                "public_key_end_control": "-----END PUBLIC KEY-----",
            },
        ),
        approval=approval,
        executions=(execution,),
    )
    service = _service(FakeActionReadRepository((aggregate,)))

    detail = await service.get("run-1", "intent-1", principal=PRINCIPAL)
    listed = await service.list("run-1", principal=PRINCIPAL)
    detail_serialized = detail.model_dump_json()
    list_serialized = listed.model_dump_json()

    for serialized in (detail_serialized, list_serialized):
        assert canary not in serialized
        assert private_marker not in serialized
        assert "ACTION-PRIVATE-MATERIAL-CANARY" not in serialized
        assert "[REDACTED]" in serialized
    assert detail.arguments_summary["public_key_begin_control"] == "-----BEGIN PUBLIC KEY-----"
    assert detail.arguments_summary["public_key_end_control"] == "-----END PUBLIC KEY-----"


async def test_private_key_marker_at_text_scan_cap_redacts_and_invalidates_reference_ids() -> None:
    prefix = "-----BEGIN "
    suffix = " PRIVATE KEY-----"
    private_marker = prefix + "X" * (4096 - len(prefix) - len(suffix)) + suffix
    assert len(private_marker) == 4096
    intent = replace(
        _intent(arguments={"marker": private_marker}),
        reason=private_marker,
        target_summary=private_marker,
    )
    service = _service(FakeActionReadRepository((_aggregate(intent=intent),)))

    detail = await service.get("run-1", "intent-1", principal=PRINCIPAL)
    listed = await service.list("run-1", principal=PRINCIPAL)

    for serialized in (detail.model_dump_json(), listed.model_dump_json()):
        assert private_marker not in serialized
        assert "[REDACTED]" in serialized
    assert detail.arguments_summary["marker"] == "[REDACTED]"

    invalid_reference = _aggregate()
    invalid_reference = replace(
        invalid_reference,
        result=replace(invalid_reference.result, artifact_ids=(private_marker,)),
    )
    invalid_service = _service(FakeActionReadRepository((invalid_reference,)))
    with pytest.raises(RuntimeError, match="invalid Action aggregate"):
        await invalid_service.get("run-1", "intent-1", principal=PRINCIPAL)
    with pytest.raises(RuntimeError, match="invalid Action aggregate"):
        await invalid_service.list("run-1", principal=PRINCIPAL)


@pytest.mark.parametrize("text_length", [4097, 1_000_000], ids=("cap-plus-one", "very-large"))
def test_text_over_scan_cap_skips_unicode_and_secret_scanners(
    text_length: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scanner_calls: list[str] = []

    class ForbiddenPattern:
        def search(self, value: str) -> None:
            scanner_calls.append(f"secret:{len(value)}")
            raise AssertionError("secret scanner must not run over the text cap")

    def forbidden_category(character: str) -> str:
        scanner_calls.append(f"unicode:{character}")
        raise AssertionError("Unicode scanner must not run over the text cap")

    def forbidden_private_marker(value: str) -> bool:
        scanner_calls.append(f"private:{len(value)}")
        raise AssertionError("private-key scanner must not run over the text cap")

    monkeypatch.setattr(actions_service.unicodedata, "category", forbidden_category)
    monkeypatch.setattr(actions_service, "_HIGH_CONFIDENCE_SECRET", ForbiddenPattern())
    monkeypatch.setattr(actions_service, "_contains_private_key_marker", forbidden_private_marker)

    assert actions_service._redact_string("x" * text_length) == "[TRUNCATED]"
    assert scanner_calls == []


def test_text_at_scan_cap_still_checks_unsafe_unicode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_category = actions_service.unicodedata.category
    category_calls = 0

    def tracked_category(character: str) -> str:
        nonlocal category_calls
        category_calls += 1
        return original_category(character)

    monkeypatch.setattr(actions_service.unicodedata, "category", tracked_category)

    assert actions_service._redact_string("x" * 4095 + "\u200b") == "[REDACTED]"
    assert category_calls == 4096


def test_reference_id_length_guard_precedes_unicode_and_text_scanners(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def forbidden_unicode(value: str) -> bool:
        calls.append(f"unicode:{len(value)}")
        raise AssertionError("Unicode scanner must not run over the reference-ID cap")

    def forbidden_safe_text(value: str | None) -> str | None:
        calls.append(f"text:{len(value or '')}")
        raise AssertionError("text scanner must not run over the reference-ID cap")

    monkeypatch.setattr(actions_service, "_contains_unsafe_unicode", forbidden_unicode)
    monkeypatch.setattr(actions_service, "_safe_text", forbidden_safe_text)

    with pytest.raises(ValueError):
        actions_service._require_reference_ids(("x" * 513,))
    assert calls == []


def test_reference_id_at_length_cap_still_checks_unsafe_unicode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_guard = actions_service._contains_unsafe_unicode
    guard_calls = 0

    def tracked_guard(value: str) -> bool:
        nonlocal guard_calls
        guard_calls += 1
        return original_guard(value)

    monkeypatch.setattr(actions_service, "_contains_unsafe_unicode", tracked_guard)

    with pytest.raises(ValueError):
        actions_service._require_reference_ids(("x" * 511 + "\u200b",))
    assert guard_calls == 1


async def test_arguments_summary_has_a_root_json_size_cap() -> None:
    arguments = {f"key-{index}-" + "x" * 500: "safe-value" for index in range(32)}
    aggregate = _aggregate(intent=_intent(arguments=arguments))

    view = await _service(FakeActionReadRepository((aggregate,))).get(
        "run-1", "intent-1", principal=PRINCIPAL
    )

    assert view.arguments_summary == {"_truncated": "[TRUNCATED]"}
    encoded = json.dumps(
        view.arguments_summary,
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode()
    assert len(encoded) <= 16 * 1024


@pytest.mark.parametrize(
    ("runtime_actor", "public_actor", "principal_id", "expected_actor", "quality"),
    [
        (
            PRINCIPAL_ID,
            PRINCIPAL_ID,
            PRINCIPAL_ID,
            PRINCIPAL_ID,
            ActionCorrelationQuality.EXACT,
        ),
        ("legacy-user", "legacy-user", PRINCIPAL_ID, None, ActionCorrelationQuality.PARTIAL),
        (PRINCIPAL_ID, "split-user", PRINCIPAL_ID, None, ActionCorrelationQuality.PARTIAL),
        (None, PRINCIPAL_ID, PRINCIPAL_ID, None, ActionCorrelationQuality.PARTIAL),
    ],
)
async def test_historical_approval_actor_requires_matching_runtime_public_and_principal(
    runtime_actor: str | None,
    public_actor: str | None,
    principal_id: str,
    expected_actor: str | None,
    quality: ActionCorrelationQuality,
) -> None:
    approval = ActionApprovalRead(
        approval_id="approval-1",
        runtime_status=ApprovalStatus.APPROVED,
        public_status=ApprovalStatus.APPROVED,
        runtime_decided_by=runtime_actor,
        public_decided_by=public_actor,
        runtime_decided_at=NOW,
        public_decided_at=NOW,
        feedback="approved safely",
        bridge_correlation_quality=ActionCorrelationQuality.EXACT,
        bridge_partial_reasons=(),
    )
    aggregate = _aggregate(
        intent=_intent(status=ToolCallStatus.READY),
        approval=approval,
    )
    service = _service(FakeActionReadRepository((aggregate,)))

    principal = LocalPrincipal(
        id=principal_id,
        capabilities=frozenset({OperatorCapability.READ}),
    )
    view = await service.get("run-1", "intent-1", principal=principal)

    assert view.approval is not None
    assert view.approval.actor == expected_actor
    assert view.approval.correlation_quality is quality
    assert view.correlation_quality is quality
    expected_lifecycle = (
        ActionLifecycle.READY
        if quality is ActionCorrelationQuality.EXACT
        else ActionLifecycle.PARTIAL
    )
    assert view.lifecycle is expected_lifecycle


async def test_split_approval_status_is_partial_and_not_presented_as_authoritative() -> None:
    approval = ActionApprovalRead(
        approval_id="approval-1",
        runtime_status=ApprovalStatus.APPROVED,
        public_status=ApprovalStatus.REJECTED,
        runtime_decided_by=PRINCIPAL_ID,
        public_decided_by=PRINCIPAL_ID,
        runtime_decided_at=NOW,
        public_decided_at=NOW,
        feedback=None,
        bridge_correlation_quality=ActionCorrelationQuality.EXACT,
        bridge_partial_reasons=(),
    )
    service = _service(
        FakeActionReadRepository(
            (_aggregate(intent=_intent(status=ToolCallStatus.READY), approval=approval),)
        )
    )

    view = await service.get("run-1", "intent-1", principal=PRINCIPAL)

    assert view.approval is not None and view.approval.status is None
    assert view.lifecycle is ActionLifecycle.PARTIAL
    assert "approval_status_mismatch" in view.partial_reasons


@pytest.mark.parametrize(
    ("status", "intent_status", "actor", "decided_at", "expected_lifecycle", "expected_quality"),
    [
        (
            ApprovalStatus.PENDING,
            ToolCallStatus.WAITING_APPROVAL,
            None,
            None,
            ActionLifecycle.AWAITING_APPROVAL,
            ActionCorrelationQuality.EXACT,
        ),
        (
            ApprovalStatus.APPROVED,
            ToolCallStatus.WAITING_APPROVAL,
            PRINCIPAL_ID,
            NOW,
            ActionLifecycle.READY,
            ActionCorrelationQuality.EXACT,
        ),
        (
            ApprovalStatus.REJECTED,
            ToolCallStatus.WAITING_APPROVAL,
            PRINCIPAL_ID,
            NOW,
            ActionLifecycle.CANCELLED,
            ActionCorrelationQuality.EXACT,
        ),
        (
            ApprovalStatus.CANCELLED,
            ToolCallStatus.WAITING_APPROVAL,
            PRINCIPAL_ID,
            NOW,
            ActionLifecycle.CANCELLED,
            ActionCorrelationQuality.EXACT,
        ),
        (
            ApprovalStatus.APPROVED,
            ToolCallStatus.WAITING_APPROVAL,
            None,
            None,
            ActionLifecycle.PARTIAL,
            ActionCorrelationQuality.PARTIAL,
        ),
    ],
)
async def test_approval_bridge_requires_complete_terminal_decision_metadata(
    status: ApprovalStatus,
    intent_status: ToolCallStatus,
    actor: str | None,
    decided_at: datetime | None,
    expected_lifecycle: ActionLifecycle,
    expected_quality: ActionCorrelationQuality,
) -> None:
    approval = ActionApprovalRead(
        approval_id="approval-1",
        runtime_status=status,
        public_status=status,
        runtime_decided_by=actor,
        public_decided_by=actor,
        runtime_decided_at=decided_at,
        public_decided_at=decided_at,
        feedback=None,
        bridge_correlation_quality=ActionCorrelationQuality.EXACT,
        bridge_partial_reasons=(),
    )
    aggregate = _aggregate(intent=_intent(status=intent_status), approval=approval)

    view = await _service(FakeActionReadRepository((aggregate,))).get(
        "run-1", "intent-1", principal=PRINCIPAL
    )

    assert view.lifecycle is expected_lifecycle
    assert view.correlation_quality is expected_quality


@pytest.mark.parametrize("runtime_after_public", [True, False])
async def test_approval_bridge_uses_public_decision_time_and_monotonic_runtime_write(
    runtime_after_public: bool,
) -> None:
    public_decided_at = NOW
    runtime_decided_at = NOW + (
        timedelta(seconds=1) if runtime_after_public else timedelta(seconds=-1)
    )
    approval = ActionApprovalRead(
        approval_id="approval-1",
        runtime_status=ApprovalStatus.APPROVED,
        public_status=ApprovalStatus.APPROVED,
        runtime_decided_by=PRINCIPAL_ID,
        public_decided_by=PRINCIPAL_ID,
        runtime_decided_at=runtime_decided_at,
        public_decided_at=public_decided_at,
        feedback=None,
        bridge_correlation_quality=ActionCorrelationQuality.EXACT,
        bridge_partial_reasons=(),
    )
    aggregate = _aggregate(
        intent=_intent(status=ToolCallStatus.READY),
        approval=approval,
    )

    view = await _service(FakeActionReadRepository((aggregate,))).get(
        "run-1", "intent-1", principal=PRINCIPAL
    )

    assert view.approval is not None
    if runtime_after_public:
        assert view.approval.decided_at == public_decided_at
        assert view.approval.correlation_quality is ActionCorrelationQuality.EXACT
    else:
        assert view.approval.decided_at is None
        assert view.approval.correlation_quality is ActionCorrelationQuality.PARTIAL
        assert ActionPartialReason.APPROVAL_DECISION_TIME_MISMATCH in view.partial_reasons


@pytest.mark.parametrize(
    ("runtime_status", "public_status"),
    [("future", "future"), (None, "pending")],
)
async def test_unknown_or_one_sided_approval_status_is_nullable_partial(
    runtime_status: str | None,
    public_status: str | None,
) -> None:
    raw_canary = "future-approval-ACTION-CANARY-SECRET"
    runtime_value = raw_canary if runtime_status == "future" else runtime_status
    public_value = raw_canary if public_status == "future" else public_status
    approval = ActionApprovalRead(
        approval_id="approval-1",
        runtime_status=runtime_value,
        public_status=public_value,
        runtime_decided_by=None,
        public_decided_by=None,
        runtime_decided_at=None,
        public_decided_at=None,
        feedback=None,
        bridge_correlation_quality=ActionCorrelationQuality.EXACT,
        bridge_partial_reasons=(),
    )
    aggregate = _aggregate(
        intent=_intent(status=ToolCallStatus.WAITING_APPROVAL),
        approval=approval,
    )

    view = await _service(FakeActionReadRepository((aggregate,))).get(
        "run-1", "intent-1", principal=PRINCIPAL
    )

    assert view.approval is not None and view.approval.status is None
    assert view.lifecycle is ActionLifecycle.PARTIAL
    assert raw_canary not in view.model_dump_json()


@pytest.mark.parametrize(
    ("approval_status", "intent_status", "with_execution", "expected_reasons"),
    [
        (
            ApprovalStatus.PENDING,
            ToolCallStatus.COMPLETED,
            True,
            {
                ActionPartialReason.APPROVAL_INTENT_STATUS_MISMATCH,
                ActionPartialReason.APPROVAL_EXECUTION_STATUS_MISMATCH,
            },
        ),
        (
            ApprovalStatus.REJECTED,
            ToolCallStatus.COMPLETED,
            True,
            {
                ActionPartialReason.APPROVAL_INTENT_STATUS_MISMATCH,
                ActionPartialReason.APPROVAL_EXECUTION_STATUS_MISMATCH,
            },
        ),
        (
            ApprovalStatus.APPROVED,
            ToolCallStatus.REJECTED,
            False,
            {ActionPartialReason.APPROVAL_INTENT_STATUS_MISMATCH},
        ),
    ],
)
async def test_approval_cannot_conflict_with_intent_or_execution_as_exact(
    approval_status: ApprovalStatus,
    intent_status: ToolCallStatus,
    with_execution: bool,
    expected_reasons: set[ActionPartialReason],
) -> None:
    terminal = approval_status is not ApprovalStatus.PENDING
    approval = ActionApprovalRead(
        approval_id="approval-1",
        runtime_status=approval_status,
        public_status=approval_status,
        runtime_decided_by=PRINCIPAL_ID if terminal else None,
        public_decided_by=PRINCIPAL_ID if terminal else None,
        runtime_decided_at=NOW + timedelta(seconds=1) if terminal else None,
        public_decided_at=NOW if terminal else None,
        feedback=None,
        bridge_correlation_quality=ActionCorrelationQuality.EXACT,
        bridge_partial_reasons=(),
    )
    executions = (
        (
            ActionExecutionRead(
                execution_id="execution-1",
                attempt_group=None,
                node_id="local",
                status=ExecutionStatus.COMPLETED,
                created_at=NOW,
                started_at=NOW,
                finished_at=NOW + timedelta(seconds=1),
                exit_code=0,
                correlation_quality=ActionCorrelationQuality.EXACT,
                error_summary=None,
            ),
        )
        if with_execution
        else ()
    )
    aggregate = _aggregate(
        intent=_intent(status=intent_status),
        approval=approval,
        executions=executions,
    )

    view = await _service(FakeActionReadRepository((aggregate,))).get(
        "run-1", "intent-1", principal=PRINCIPAL
    )

    assert expected_reasons.issubset(view.partial_reasons)
    assert view.lifecycle is ActionLifecycle.PARTIAL
    assert view.correlation_quality is ActionCorrelationQuality.PARTIAL


async def test_unknown_persisted_intent_execution_and_reason_values_fail_partial() -> None:
    raw_canary = "future-ACTION-CANARY-SECRET"
    execution = ActionExecutionRead(
        execution_id="execution-1",
        attempt_group=None,
        node_id="local",
        status=raw_canary,
        created_at=NOW + timedelta(seconds=1),
        started_at=None,
        finished_at=None,
        exit_code=None,
        correlation_quality=ActionCorrelationQuality.EXACT,
        error_summary=None,
    )
    aggregate = replace(
        _aggregate(
            intent=_intent(status=raw_canary, approval_level=raw_canary),
            executions=(execution,),
        ),
        partial_reasons=(
            ActionPartialReason.EVENT_CORRELATION_PARTIAL,
            f"bad-reason-{raw_canary}",
        ),
    )

    view = await _service(FakeActionReadRepository((aggregate,))).get(
        "run-1", "intent-1", principal=PRINCIPAL
    )

    assert view.approval_level is None
    assert view.executions[0].status is None
    assert view.lifecycle is ActionLifecycle.PARTIAL
    assert ActionPartialReason.INTENT_STATUS_UNKNOWN in view.partial_reasons
    assert ActionPartialReason.INTENT_APPROVAL_LEVEL_UNKNOWN in view.partial_reasons
    assert ActionPartialReason.EXECUTION_STATUS_UNKNOWN in view.partial_reasons
    assert ActionPartialReason.REPOSITORY_PARTIAL_REASON_INVALID in view.partial_reasons
    assert raw_canary not in view.model_dump_json()


@pytest.mark.parametrize(
    ("intent_status", "execution_status", "exit_code", "expected"),
    [
        (ToolCallStatus.PROPOSED, None, None, ActionLifecycle.PROPOSED),
        (ToolCallStatus.WAITING_APPROVAL, None, None, ActionLifecycle.PARTIAL),
        (ToolCallStatus.READY, None, None, ActionLifecycle.READY),
        (ToolCallStatus.EXECUTING, ExecutionStatus.RUNNING, None, ActionLifecycle.EXECUTING),
        (ToolCallStatus.COMPLETED, ExecutionStatus.EXITED, 0, ActionLifecycle.SUCCEEDED),
        (ToolCallStatus.COMPLETED, ExecutionStatus.EXITED, 7, ActionLifecycle.PARTIAL),
        (ToolCallStatus.FAILED, ExecutionStatus.FAILED, None, ActionLifecycle.FAILED),
        (ToolCallStatus.CANCELLED, ExecutionStatus.CANCELLED, None, ActionLifecycle.CANCELLED),
        (ToolCallStatus.EXECUTING, ExecutionStatus.LOST, None, ActionLifecycle.PARTIAL),
    ],
)
async def test_action_lifecycle_uses_durable_intent_and_execution_state(
    intent_status: ToolCallStatus,
    execution_status: ExecutionStatus | None,
    exit_code: int | None,
    expected: ActionLifecycle,
) -> None:
    executions = (
        (
            ActionExecutionRead(
                execution_id="execution-1",
                attempt_group="initial",
                node_id="local",
                status=execution_status,
                created_at=NOW + timedelta(seconds=1),
                started_at=NOW + timedelta(seconds=1),
                finished_at=(
                    NOW + timedelta(seconds=2)
                    if execution_status
                    in {
                        ExecutionStatus.EXITED,
                        ExecutionStatus.COMPLETED,
                        ExecutionStatus.FAILED,
                        ExecutionStatus.CANCELLED,
                        ExecutionStatus.LOST,
                    }
                    else None
                ),
                exit_code=exit_code,
                correlation_quality=ActionCorrelationQuality.EXACT,
                error_summary=None,
            ),
        )
        if execution_status is not None
        else ()
    )
    aggregate = _aggregate(
        intent=_intent(status=intent_status),
        executions=executions,
        current_execution_id=(
            executions[0].execution_id if execution_status is ExecutionStatus.RUNNING else None
        ),
    )
    service = _service(FakeActionReadRepository((aggregate,)))

    view = await service.get("run-1", "intent-1", principal=PRINCIPAL)

    assert view.lifecycle is expected
    assert view.lifecycle_sources
    if execution_status is ExecutionStatus.LOST:
        assert "execution_stop_unconfirmed" in view.partial_reasons


@pytest.mark.parametrize(
    "intent_status",
    [ToolCallStatus.EXECUTING, ToolCallStatus.COMPLETED, ToolCallStatus.FAILED],
)
async def test_execution_requiring_intent_without_execution_is_partial(
    intent_status: ToolCallStatus,
) -> None:
    aggregate = _aggregate(intent=_intent(status=intent_status))

    view = await _service(FakeActionReadRepository((aggregate,))).get(
        "run-1", "intent-1", principal=PRINCIPAL
    )

    assert view.lifecycle is ActionLifecycle.PARTIAL
    assert ActionPartialReason.EXECUTION_MISSING_FOR_INTENT_STATUS in view.partial_reasons


async def test_waiting_approval_without_either_bridge_side_is_partial() -> None:
    aggregate = _aggregate(intent=_intent(status=ToolCallStatus.WAITING_APPROVAL))

    view = await _service(FakeActionReadRepository((aggregate,))).get(
        "run-1", "intent-1", principal=PRINCIPAL
    )

    assert view.lifecycle is ActionLifecycle.PARTIAL
    assert ActionPartialReason.APPROVAL_RUNTIME_MISSING in view.partial_reasons
    assert ActionPartialReason.APPROVAL_PUBLIC_MISSING in view.partial_reasons


async def test_terminal_audit_sources_cannot_be_missing_as_exact() -> None:
    rejected = _aggregate(intent=_intent(status=ToolCallStatus.REJECTED))
    execution = ActionExecutionRead(
        execution_id="execution-1",
        attempt_group=None,
        node_id="local",
        status=ExecutionStatus.COMPLETED,
        created_at=NOW,
        started_at=NOW,
        finished_at=NOW + timedelta(seconds=1),
        exit_code=0,
        correlation_quality=ActionCorrelationQuality.EXACT,
        error_summary=None,
    )
    completed_without_event = _aggregate(
        intent=_intent(action_id="intent-2", status=ToolCallStatus.COMPLETED),
        executions=(execution,),
        events=(),
    )

    rejected_view = await _service(FakeActionReadRepository((rejected,))).get(
        "run-1", "intent-1", principal=PRINCIPAL
    )
    completed_view = await _service(FakeActionReadRepository((completed_without_event,))).get(
        "run-1", "intent-2", principal=PRINCIPAL
    )

    assert rejected_view.lifecycle is ActionLifecycle.PARTIAL
    assert ActionPartialReason.APPROVAL_RUNTIME_MISSING in rejected_view.partial_reasons
    assert ActionPartialReason.APPROVAL_PUBLIC_MISSING in rejected_view.partial_reasons
    assert completed_view.lifecycle is ActionLifecycle.PARTIAL
    assert ActionPartialReason.EVENT_CORRELATION_PARTIAL in completed_view.partial_reasons


async def test_strict_attempt_order_uses_created_at_not_late_old_status() -> None:
    old = ActionExecutionRead(
        execution_id="execution-old",
        attempt_group="initial",
        node_id="local",
        status=ExecutionStatus.COMPLETED,
        created_at=NOW + timedelta(seconds=1),
        started_at=NOW + timedelta(seconds=1),
        finished_at=NOW + timedelta(minutes=10),
        exit_code=0,
        correlation_quality=ActionCorrelationQuality.EXACT,
        error_summary=None,
    )
    latest = ActionExecutionRead(
        execution_id="execution-latest",
        attempt_group="retry",
        node_id="local",
        status=ExecutionStatus.RUNNING,
        created_at=NOW + timedelta(seconds=2),
        started_at=NOW + timedelta(seconds=2),
        finished_at=None,
        exit_code=None,
        correlation_quality=ActionCorrelationQuality.EXACT,
        error_summary=None,
    )
    aggregate = _aggregate(
        intent=_intent(status=ToolCallStatus.EXECUTING),
        executions=(latest, old),
        current_execution_id=latest.execution_id,
    )

    view = await _service(FakeActionReadRepository((aggregate,))).get(
        "run-1", "intent-1", principal=PRINCIPAL
    )

    assert view.attempt_order_quality is ActionAttemptOrderQuality.EXACT
    assert view.latest_execution_id == "execution-latest"
    assert view.current_execution_id == "execution-latest"
    assert [item.execution_id for item in view.executions] == [
        "execution-old",
        "execution-latest",
    ]
    assert view.lifecycle is ActionLifecycle.EXECUTING


async def test_attempt_order_compares_absolute_time_across_timezone_offsets() -> None:
    older = ActionExecutionRead(
        execution_id="execution-older",
        attempt_group=None,
        node_id="local",
        status=ExecutionStatus.COMPLETED,
        created_at=datetime.fromisoformat("2026-08-01T08:00:00+08:00"),
        started_at=None,
        finished_at=None,
        exit_code=0,
        correlation_quality=ActionCorrelationQuality.EXACT,
        error_summary=None,
    )
    latest = ActionExecutionRead(
        execution_id="execution-latest",
        attempt_group=None,
        node_id="local",
        status=ExecutionStatus.RUNNING,
        created_at=datetime.fromisoformat("2026-08-01T01:00:00+00:00"),
        started_at=None,
        finished_at=None,
        exit_code=None,
        correlation_quality=ActionCorrelationQuality.EXACT,
        error_summary=None,
    )
    aggregate = _aggregate(
        intent=_intent(status=ToolCallStatus.EXECUTING),
        executions=(older, latest),
        current_execution_id=latest.execution_id,
    )

    view = await _service(FakeActionReadRepository((aggregate,))).get(
        "run-1", "intent-1", principal=PRINCIPAL
    )

    assert view.latest_execution_id == "execution-latest"
    assert view.current_execution_id == "execution-latest"
    assert [item.execution_id for item in view.executions] == [
        "execution-older",
        "execution-latest",
    ]


async def test_duplicate_older_timestamp_keeps_latest_but_marks_global_order_ambiguous() -> None:
    executions = tuple(
        ActionExecutionRead(
            execution_id=execution_id,
            attempt_group=None,
            node_id="local",
            status=(
                ExecutionStatus.RUNNING
                if execution_id == "execution-c"
                else ExecutionStatus.COMPLETED
            ),
            created_at=created_at,
            started_at=None,
            finished_at=None,
            exit_code=None,
            correlation_quality=ActionCorrelationQuality.EXACT,
            error_summary=None,
        )
        for execution_id, created_at in (
            ("execution-a", NOW + timedelta(seconds=1)),
            ("execution-b", NOW + timedelta(seconds=1)),
            ("execution-c", NOW + timedelta(seconds=2)),
        )
    )
    aggregate = _aggregate(
        intent=_intent(status=ToolCallStatus.EXECUTING),
        executions=executions,
        current_execution_id="execution-c",
    )

    view = await _service(FakeActionReadRepository((aggregate,))).get(
        "run-1", "intent-1", principal=PRINCIPAL
    )

    assert view.attempt_order_quality is ActionAttemptOrderQuality.AMBIGUOUS
    assert view.latest_execution_id == "execution-c"
    assert view.current_execution_id == "execution-c"
    assert view.lifecycle is ActionLifecycle.PARTIAL


async def test_multiple_active_attempts_have_no_authoritative_current() -> None:
    executions = tuple(
        ActionExecutionRead(
            execution_id=f"execution-{index}",
            attempt_group=None,
            node_id="local",
            status=ExecutionStatus.RUNNING,
            created_at=NOW + timedelta(seconds=index),
            started_at=NOW + timedelta(seconds=index),
            finished_at=None,
            exit_code=None,
            correlation_quality=ActionCorrelationQuality.EXACT,
            error_summary=None,
        )
        for index in (1, 2)
    )
    aggregate = _aggregate(
        intent=_intent(status=ToolCallStatus.EXECUTING),
        executions=executions,
    )

    view = await _service(FakeActionReadRepository((aggregate,))).get(
        "run-1", "intent-1", principal=PRINCIPAL
    )

    assert view.latest_execution_id == "execution-2"
    assert view.current_execution_id is None
    assert ActionPartialReason.EXECUTION_CURRENT_AMBIGUOUS in view.partial_reasons
    assert ActionPartialReason.INTENT_EXECUTION_STATUS_MISMATCH in view.partial_reasons
    assert view.lifecycle is ActionLifecycle.PARTIAL


async def test_partial_execution_cannot_be_authoritative_latest_or_current() -> None:
    execution = ActionExecutionRead(
        execution_id="execution-partial",
        attempt_group=None,
        node_id="local",
        status=ExecutionStatus.RUNNING,
        created_at=NOW,
        started_at=NOW,
        finished_at=None,
        exit_code=None,
        correlation_quality=ActionCorrelationQuality.PARTIAL,
        error_summary=None,
    )
    aggregate = _aggregate(
        intent=_intent(status=ToolCallStatus.EXECUTING),
        executions=(execution,),
    )

    view = await _service(FakeActionReadRepository((aggregate,))).get(
        "run-1", "intent-1", principal=PRINCIPAL
    )

    assert view.latest_execution_id is None
    assert view.current_execution_id is None
    assert view.lifecycle is ActionLifecycle.PARTIAL


async def test_truncated_attempts_hide_latest_but_preserve_exact_current() -> None:
    visible = ActionExecutionRead(
        execution_id="execution-visible",
        attempt_group="visible",
        node_id="local",
        status=ExecutionStatus.RUNNING,
        created_at=NOW,
        started_at=NOW,
        finished_at=None,
        exit_code=None,
        correlation_quality=ActionCorrelationQuality.EXACT,
        error_summary=None,
    )
    aggregate = replace(
        _aggregate(
            intent=_intent(status=ToolCallStatus.EXECUTING),
            executions=(visible,),
            current_execution_id=visible.execution_id,
        ),
        execution_count=2,
        execution_coverage=ActionCoverage(scanned=1, limit=1, truncated=True),
    )
    service = _service(FakeActionReadRepository((aggregate,)))

    detail = await service.get("run-1", "intent-1", principal=PRINCIPAL)
    listed = await service.list("run-1", principal=PRINCIPAL)

    for view in (detail, listed.items[0]):
        assert view.latest_execution_id is None
        assert view.current_execution_id == visible.execution_id
        assert view.attempt_order_quality is ActionAttemptOrderQuality.UNKNOWN
        assert ActionPartialReason.EXECUTION_ATTEMPTS_TRUNCATED in view.partial_reasons
        assert view.lifecycle is ActionLifecycle.PARTIAL


@pytest.mark.parametrize(
    ("status", "proof", "expected_confirmation", "expected_lifecycle"),
    [
        (
            ExecutionStatus.RUNNING,
            None,
            ActionStopConfirmation.NOT_APPLICABLE,
            ActionLifecycle.EXECUTING,
        ),
        (
            ExecutionStatus.FAILED,
            None,
            ActionStopConfirmation.UNCONFIRMED,
            ActionLifecycle.FAILED,
        ),
        (
            ExecutionStatus.CANCELLED,
            NOW + timedelta(seconds=2),
            ActionStopConfirmation.CONFIRMED,
            ActionLifecycle.CANCELLED,
        ),
        (
            ExecutionStatus.LOST,
            None,
            ActionStopConfirmation.UNCONFIRMED,
            ActionLifecycle.PARTIAL,
        ),
    ],
)
async def test_execution_result_and_physical_stop_confirmation_are_separate_axes(
    status: ExecutionStatus,
    proof: datetime | None,
    expected_confirmation: ActionStopConfirmation,
    expected_lifecycle: ActionLifecycle,
) -> None:
    intent_status = {
        ExecutionStatus.RUNNING: ToolCallStatus.EXECUTING,
        ExecutionStatus.FAILED: ToolCallStatus.FAILED,
        ExecutionStatus.CANCELLED: ToolCallStatus.CANCELLED,
        ExecutionStatus.LOST: ToolCallStatus.EXECUTING,
    }[status]
    execution = ActionExecutionRead(
        execution_id="execution-1",
        attempt_group=None,
        node_id="local",
        status=status,
        created_at=NOW,
        started_at=NOW,
        finished_at=None if status is ExecutionStatus.RUNNING else NOW + timedelta(seconds=1),
        exit_code=None,
        correlation_quality=ActionCorrelationQuality.EXACT,
        error_summary=None,
        physical_stop_confirmed_at=proof,
    )
    aggregate = _aggregate(
        intent=_intent(status=intent_status),
        executions=(execution,),
        current_execution_id=(
            execution.execution_id if status is ExecutionStatus.RUNNING else None
        ),
    )

    view = await _service(FakeActionReadRepository((aggregate,))).get(
        "run-1", "intent-1", principal=PRINCIPAL
    )

    assert view.executions[0].stop_confirmation is expected_confirmation
    assert view.latest_stop_confirmation is expected_confirmation
    assert view.lifecycle is expected_lifecycle
    if expected_confirmation is ActionStopConfirmation.UNCONFIRMED:
        assert ActionPartialReason.EXECUTION_STOP_UNCONFIRMED in view.partial_reasons


@pytest.mark.parametrize(
    ("status", "proof", "expected_confirmation"),
    [
        (
            ExecutionStatus.FAILED,
            NOW + timedelta(seconds=3),
            ActionStopConfirmation.UNCONFIRMED,
        ),
        (
            ExecutionStatus.LOST,
            NOW + timedelta(seconds=3),
            ActionStopConfirmation.UNCONFIRMED,
        ),
        (
            ExecutionStatus.RUNNING,
            NOW + timedelta(seconds=3),
            ActionStopConfirmation.NOT_APPLICABLE,
        ),
        (
            ExecutionStatus.CANCELLED,
            NOW,
            ActionStopConfirmation.UNCONFIRMED,
        ),
    ],
)
async def test_invalid_physical_stop_proof_fails_partial(
    status: ExecutionStatus,
    proof: datetime,
    expected_confirmation: ActionStopConfirmation,
) -> None:
    intent_status = {
        ExecutionStatus.FAILED: ToolCallStatus.FAILED,
        ExecutionStatus.LOST: ToolCallStatus.EXECUTING,
        ExecutionStatus.RUNNING: ToolCallStatus.EXECUTING,
        ExecutionStatus.CANCELLED: ToolCallStatus.CANCELLED,
    }[status]
    execution = ActionExecutionRead(
        execution_id="execution-1",
        attempt_group=None,
        node_id="local",
        status=status,
        created_at=NOW,
        started_at=NOW,
        finished_at=(None if status is ExecutionStatus.RUNNING else NOW + timedelta(seconds=1)),
        exit_code=None,
        correlation_quality=ActionCorrelationQuality.EXACT,
        error_summary=None,
    )
    aggregate = _aggregate(
        intent=_intent(status=intent_status),
        executions=(execution,),
    )
    aggregate = replace(
        aggregate,
        executions=(replace(execution, physical_stop_confirmed_at=proof),),
        updated_at=max(aggregate.updated_at, proof),
    )

    view = await _service(FakeActionReadRepository((aggregate,))).get(
        "run-1", "intent-1", principal=PRINCIPAL
    )

    assert view.executions[0].physical_stop_confirmed_at is None
    assert view.executions[0].stop_confirmation is expected_confirmation
    assert ActionPartialReason.EXECUTION_STOP_PROOF_INVALID in view.partial_reasons
    assert view.lifecycle is ActionLifecycle.PARTIAL


@pytest.mark.parametrize(
    ("second_created_at", "expected_quality"),
    [
        (None, ActionAttemptOrderQuality.UNKNOWN),
        (NOW + timedelta(seconds=1), ActionAttemptOrderQuality.AMBIGUOUS),
    ],
)
async def test_multiple_attempts_with_null_or_tied_latest_never_claim_chronology(
    second_created_at: datetime | None,
    expected_quality: ActionAttemptOrderQuality,
) -> None:
    executions = (
        ActionExecutionRead(
            execution_id="execution-a",
            attempt_group="initial",
            node_id="local",
            status=ExecutionStatus.FAILED,
            created_at=NOW + timedelta(seconds=1),
            started_at=None,
            finished_at=None,
            exit_code=None,
            correlation_quality=ActionCorrelationQuality.EXACT,
            error_summary=None,
        ),
        ActionExecutionRead(
            execution_id="execution-b",
            attempt_group="retry",
            node_id="local",
            status=ExecutionStatus.COMPLETED,
            created_at=second_created_at,
            started_at=None,
            finished_at=None,
            exit_code=0,
            correlation_quality=ActionCorrelationQuality.EXACT,
            error_summary=None,
        ),
    )
    service = _service(
        FakeActionReadRepository(
            (_aggregate(intent=_intent(status=ToolCallStatus.COMPLETED), executions=executions),)
        )
    )

    view = await service.get("run-1", "intent-1", principal=PRINCIPAL)

    assert view.attempt_order_quality is expected_quality
    assert view.latest_execution_id is None
    assert view.current_execution_id is None
    assert view.lifecycle is ActionLifecycle.PARTIAL


async def test_single_legacy_attempt_is_identifiable_without_claiming_order_quality() -> None:
    execution = ActionExecutionRead(
        execution_id="execution-legacy",
        attempt_group="initial",
        node_id="local",
        status=ExecutionStatus.COMPLETED,
        created_at=None,
        started_at=NOW,
        finished_at=NOW + timedelta(seconds=1),
        exit_code=0,
        correlation_quality=ActionCorrelationQuality.LEGACY,
        error_summary=None,
    )
    service = _service(
        FakeActionReadRepository(
            (
                _aggregate(
                    intent=_intent(status=ToolCallStatus.COMPLETED),
                    executions=(execution,),
                ),
            )
        )
    )

    view = await service.get("run-1", "intent-1", principal=PRINCIPAL)

    assert view.latest_execution_id == "execution-legacy"
    assert view.current_execution_id is None
    assert view.latest_stop_confirmation is ActionStopConfirmation.UNCONFIRMED
    assert view.attempt_order_quality is ActionAttemptOrderQuality.UNKNOWN
    assert view.correlation_quality is ActionCorrelationQuality.LEGACY
    assert view.lifecycle is ActionLifecycle.SUCCEEDED


async def test_result_output_availability_is_not_inferred_from_artifact_size() -> None:
    aggregate = replace(
        _aggregate(),
        result=ActionResultRead(
            artifact_ids=("artifact-non-output",),
            artifact_count=1,
            output_size=4096,
            output_available=False,
            artifacts_truncated=False,
        ),
    )

    view = await _service(FakeActionReadRepository((aggregate,))).get(
        "run-1", "intent-1", principal=PRINCIPAL
    )

    assert view.result.output_size == 4096
    assert view.result.output_available is False


async def test_projection_version_is_stable_and_changes_without_an_event_write() -> None:
    running = ActionExecutionRead(
        execution_id="execution-1",
        attempt_group=None,
        node_id="local",
        status=ExecutionStatus.STARTING,
        created_at=NOW,
        started_at=NOW,
        finished_at=None,
        exit_code=None,
        correlation_quality=ActionCorrelationQuality.EXACT,
        error_summary=None,
    )
    before = _aggregate(
        intent=_intent(status=ToolCallStatus.EXECUTING),
        executions=(running,),
    )
    after = replace(before, executions=(replace(running, status=ExecutionStatus.RUNNING),))

    before_service = _service(FakeActionReadRepository((before,)))
    first = await before_service.get("run-1", "intent-1", principal=PRINCIPAL)
    repeated = await before_service.get("run-1", "intent-1", principal=PRINCIPAL)
    changed = await _service(FakeActionReadRepository((after,))).get(
        "run-1", "intent-1", principal=PRINCIPAL
    )
    listed = await before_service.list("run-1", principal=PRINCIPAL)

    assert first.version == repeated.version
    assert listed.items[0].version == first.version
    assert len(first.version) == 64
    assert first.version != changed.version


async def test_projection_version_hashes_older_attempts_and_shared_updated_at() -> None:
    older = ActionExecutionRead(
        execution_id="execution-older",
        attempt_group="initial",
        node_id="local",
        status=ExecutionStatus.FAILED,
        created_at=NOW,
        started_at=NOW,
        finished_at=NOW + timedelta(seconds=1),
        exit_code=None,
        correlation_quality=ActionCorrelationQuality.EXACT,
        error_summary=None,
    )
    current = ActionExecutionRead(
        execution_id="execution-current",
        attempt_group="retry",
        node_id="local",
        status=ExecutionStatus.RUNNING,
        created_at=NOW + timedelta(seconds=2),
        started_at=NOW + timedelta(seconds=2),
        finished_at=None,
        exit_code=None,
        correlation_quality=ActionCorrelationQuality.EXACT,
        error_summary=None,
    )
    base = _aggregate(
        intent=_intent(status=ToolCallStatus.EXECUTING),
        executions=(older, current),
    )
    older_changed = replace(
        base,
        executions=(replace(older, status=ExecutionStatus.COMPLETED), current),
    )
    hidden_child_changed = replace(
        base,
        updated_at=base.updated_at + timedelta(seconds=10),
    )

    async def versions(aggregate: ActionAggregateRead) -> tuple[str, str]:
        service = _service(FakeActionReadRepository((aggregate,)))
        detail = await service.get("run-1", "intent-1", principal=PRINCIPAL)
        listed = await service.list("run-1", principal=PRINCIPAL)
        return detail.version, listed.items[0].version

    base_versions = await versions(base)
    older_versions = await versions(older_changed)
    hidden_versions = await versions(hidden_child_changed)

    assert base_versions[0] == base_versions[1]
    assert older_versions[0] == older_versions[1]
    assert hidden_versions[0] == hidden_versions[1]
    assert older_versions[0] != base_versions[0]
    assert hidden_versions[0] != base_versions[0]


async def test_event_metadata_changes_require_and_use_the_shared_updated_high_water() -> None:
    event = ActionEventRead(
        event_id="event-1",
        sequence=1,
        event_type="tool.proposed",
        created_at=NOW,
    )
    base = _aggregate(events=(event,))
    changed_at = NOW + timedelta(seconds=1)
    changed_event = replace(
        base,
        events=(
            replace(
                event,
                event_type="tool.ready",
                created_at=changed_at,
            ),
        ),
        updated_at=changed_at,
    )
    stale_high_water = replace(changed_event, updated_at=base.updated_at)

    with pytest.raises(RuntimeError, match="invalid Action aggregate"):
        await _service(FakeActionReadRepository((stale_high_water,))).get(
            "run-1", "intent-1", principal=PRINCIPAL
        )

    async def versions(aggregate: ActionAggregateRead) -> tuple[str, str]:
        service = _service(FakeActionReadRepository((aggregate,)))
        detail = await service.get("run-1", "intent-1", principal=PRINCIPAL)
        listed = await service.list("run-1", principal=PRINCIPAL)
        return detail.version, listed.items[0].version

    base_versions = await versions(base)
    changed_versions = await versions(changed_event)

    assert base_versions[0] == base_versions[1]
    assert changed_versions[0] == changed_versions[1]
    assert changed_versions[0] != base_versions[0]


async def test_list_cursor_is_snapshot_bound_and_rejects_cross_run_or_tampering() -> None:
    items = tuple(
        _aggregate(
            intent=_intent(
                action_id=f"intent-{index:04d}",
                created_at=NOW + timedelta(seconds=index),
            )
        )
        for index in range(5)
    )
    repository = FakeActionReadRepository(items)
    service = _service(repository)

    first = await service.list("run-1", principal=PRINCIPAL, limit=2)
    second = await service.list(
        "run-1",
        principal=PRINCIPAL,
        limit=2,
        cursor=first.next_cursor,
    )

    assert first.has_more is True and first.next_cursor is not None
    assert second.has_more is True and second.next_cursor is not None
    assert {item.action_id for item in first.items}.isdisjoint(
        item.action_id for item in second.items
    )
    assert repository.list_calls[1][3] == repository.list_calls[0][3] or (
        repository.list_calls[0][3] is None and repository.list_calls[1][3] is not None
    )

    with pytest.raises(InvalidActionCursorError) as wrong_run:
        await service.list(
            "run-2",
            principal=PRINCIPAL,
            limit=2,
            cursor=first.next_cursor,
        )
    with pytest.raises(InvalidActionCursorError) as tampered:
        await service.list(
            "run-1",
            principal=PRINCIPAL,
            limit=2,
            cursor=first.next_cursor[:-1] + "!",
        )
    with pytest.raises(InvalidActionCursorError) as valid_json_tamper:
        await service.list(
            "run-1",
            principal=PRINCIPAL,
            limit=2,
            cursor=_tamper_cursor_field(first.next_cursor, "run_id", "run-2"),
        )
    with pytest.raises(InvalidActionCursorError) as limit_mismatch:
        await service.list(
            "run-1",
            principal=PRINCIPAL,
            limit=3,
            cursor=first.next_cursor,
        )
    with pytest.raises(InvalidActionCursorError) as sort_mismatch:
        await service.list(
            "run-1",
            principal=PRINCIPAL,
            limit=2,
            sort="action_id_desc",
            cursor=first.next_cursor,
        )
    with pytest.raises(InvalidActionCursorError) as oversized:
        await service.list(
            "run-1",
            principal=PRINCIPAL,
            limit=2,
            cursor="A" * 5000,
        )
    with pytest.raises(InvalidActionCursorError) as bool_version:
        await service.list(
            "run-1",
            principal=PRINCIPAL,
            limit=2,
            cursor=_tamper_cursor_field(
                first.next_cursor,
                "version",
                True,
                resign_corruption_checksum=True,
            ),
        )
    with pytest.raises(InvalidActionCursorError) as after_snapshot:
        await service.list(
            "run-1",
            principal=PRINCIPAL,
            limit=2,
            cursor=_tamper_cursor_field(
                first.next_cursor,
                "after",
                {
                    "action_id": "intent-after-snapshot",
                    "created_at": (NOW + timedelta(days=1)).isoformat(),
                },
                resign_corruption_checksum=True,
            ),
        )
    with pytest.raises(InvalidActionCursorError) as oversized_id:
        await service.list(
            "run-1",
            principal=PRINCIPAL,
            limit=2,
            cursor=_tamper_cursor_field(
                first.next_cursor,
                "after",
                {
                    "action_id": "x" * 129,
                    "created_at": NOW.isoformat(),
                },
                resign_corruption_checksum=True,
            ),
        )
    with pytest.raises(InvalidActionCursorError) as oversized_run_id:
        await service.list(
            "run-1",
            principal=PRINCIPAL,
            limit=2,
            cursor=_tamper_cursor_field(
                first.next_cursor,
                "run_id",
                "r" * 65,
                resign_corruption_checksum=True,
            ),
        )
    with pytest.raises(InvalidActionCursorError) as trimmed_action_id:
        await service.list(
            "run-1",
            principal=PRINCIPAL,
            limit=2,
            cursor=_tamper_cursor_field(
                first.next_cursor,
                "after",
                {
                    "action_id": " intent-1",
                    "created_at": NOW.isoformat(),
                },
                resign_corruption_checksum=True,
            ),
        )
    with pytest.raises(InvalidActionCursorError) as controlled_action_id:
        await service.list(
            "run-1",
            principal=PRINCIPAL,
            limit=2,
            cursor=_tamper_cursor_field(
                first.next_cursor,
                "after",
                {
                    "action_id": "intent\x00id",
                    "created_at": NOW.isoformat(),
                },
                resign_corruption_checksum=True,
            ),
        )
    assert {
        wrong_run.value.code,
        tampered.value.code,
        valid_json_tamper.value.code,
        limit_mismatch.value.code,
        sort_mismatch.value.code,
        oversized.value.code,
        bool_version.value.code,
        after_snapshot.value.code,
        oversized_id.value.code,
        oversized_run_id.value.code,
        trimmed_action_id.value.code,
        controlled_action_id.value.code,
    } == {"invalid_action_cursor"}


async def test_resigned_cursor_run_id_must_remain_canonical() -> None:
    items = tuple(
        _aggregate(
            intent=_intent(
                action_id=f"intent-{index}",
                created_at=NOW + timedelta(seconds=index),
            )
        )
        for index in range(2)
    )
    service = _service(FakeActionReadRepository(items))
    first = await service.list("run-1", principal=PRINCIPAL, limit=1)
    assert first.next_cursor is not None
    invalid_run_id = "run-1 "
    cursor = _tamper_cursor_field(
        first.next_cursor,
        "run_id",
        invalid_run_id,
        resign_corruption_checksum=True,
    )

    class AliasRunRepository(FakeActionReadRepository):
        async def resolve_run(self, run_id: str) -> str | None:
            return run_id

    with pytest.raises(InvalidActionCursorError):
        await _service(AliasRunRepository(items)).list(
            invalid_run_id,
            principal=PRINCIPAL,
            limit=1,
            cursor=cursor,
        )


@pytest.mark.parametrize("unsafe_character", UNSAFE_UNICODE_CHARACTERS)
@pytest.mark.parametrize("cursor_field", ["run_id", "after", "snapshot"])
async def test_resigned_cursor_reference_ids_reject_unsafe_unicode(
    unsafe_character: str,
    cursor_field: str,
) -> None:
    items = tuple(
        _aggregate(
            intent=_intent(
                action_id=f"intent-{index}",
                created_at=NOW + timedelta(seconds=index),
            )
        )
        for index in range(2)
    )
    first = await _service(FakeActionReadRepository(items)).list(
        "run-1",
        principal=PRINCIPAL,
        limit=1,
    )
    assert first.next_cursor is not None
    canary = f"CURSOR_UNICODE_CANARY{unsafe_character}TAIL"
    request_run_id = "run-1"
    repository: FakeActionReadRepository = FakeActionReadRepository(items)
    if cursor_field == "run_id":
        replacement: object = canary
        request_run_id = canary
        repository = PermissiveRunRepository(items)
    else:
        replacement = {
            "action_id": canary,
            "created_at": (NOW + timedelta(seconds=1)).isoformat(),
        }
    cursor = _tamper_cursor_field(
        first.next_cursor,
        cursor_field,
        replacement,
        resign_corruption_checksum=True,
    )

    with pytest.raises(InvalidActionCursorError) as captured:
        await _service(repository).list(
            request_run_id,
            principal=PRINCIPAL,
            limit=1,
            cursor=cursor,
        )

    assert captured.value.code == "invalid_action_cursor"
    assert "CURSOR_UNICODE_CANARY" not in str(captured.value)


async def test_safe_unicode_reference_ids_round_trip_through_cursor() -> None:
    run_id = "运行:run-1"
    action_ids = ("动作.alpha-1", "动作.beta-2")
    items = tuple(
        _aggregate(
            intent=replace(
                _intent(
                    action_id=action_id,
                    created_at=NOW + timedelta(seconds=index),
                ),
                run_id=run_id,
            )
        )
        for index, action_id in enumerate(action_ids)
    )
    service = _service(PermissiveRunRepository(items))

    first = await service.list(run_id, principal=PRINCIPAL, limit=1)
    assert first.next_cursor is not None
    second = await service.list(
        run_id,
        principal=PRINCIPAL,
        limit=1,
        cursor=first.next_cursor,
    )

    assert first.items[0].run_id == run_id
    assert second.items[0].run_id == run_id
    assert {first.items[0].action_id, second.items[0].action_id} == set(action_ids)


async def test_list_rejects_malformed_repository_page_invariants() -> None:
    aggregates = tuple(
        _aggregate(
            intent=_intent(
                action_id=f"intent-{index}",
                created_at=NOW + timedelta(seconds=index),
            )
        )
        for index in range(3)
    )

    class MalformedPageRepository(FakeActionReadRepository):
        mode = "normal"

        async def list_page(
            self,
            run_id: str,
            *,
            limit: int,
            after: ActionPageKey | None,
            snapshot: ActionPageKey | None,
        ) -> ActionReadPage:
            if self.mode == "normal":
                return await super().list_page(
                    run_id,
                    limit=limit,
                    after=after,
                    snapshot=snapshot,
                )
            if self.mode == "unsorted":
                low, high = self.items[-1], self.items[0]
                return ActionReadPage(
                    items=(_list_aggregate(low), _list_aggregate(high)),
                    has_more=False,
                    snapshot=ActionPageKey(
                        high.intent.created_at,
                        high.intent.action_id,
                    ),
                )
            if self.mode == "missing_snapshot":
                return ActionReadPage(
                    items=(_list_aggregate(self.items[0]),),
                    has_more=True,
                    snapshot=None,
                )
            if self.mode == "virtual_snapshot":
                high = self.items[0]
                return ActionReadPage(
                    items=(_list_aggregate(high),),
                    has_more=False,
                    snapshot=ActionPageKey(
                        high.intent.created_at + timedelta(days=1),
                        "intent-virtual",
                    ),
                )
            if self.mode == "empty_snapshot":
                return ActionReadPage(
                    items=(),
                    has_more=False,
                    snapshot=ActionPageKey(NOW, "intent-virtual"),
                )
            if self.mode == "invalid_snapshot_type":
                high = self.items[0]
                return ActionReadPage(
                    items=(_list_aggregate(high),),
                    has_more=False,
                    snapshot=ActionPageKey(
                        "not-a-datetime",  # type: ignore[arg-type]
                        high.intent.action_id,
                    ),
                )
            if self.mode == "equal_after":
                assert after is not None
                equal = next(
                    item
                    for item in self.items
                    if (item.intent.created_at, item.intent.action_id) == after.as_tuple()
                )
                return ActionReadPage(
                    items=(_list_aggregate(equal),),
                    has_more=False,
                    snapshot=snapshot,
                )
            raise AssertionError(self.mode)

    for mode in (
        "unsorted",
        "missing_snapshot",
        "virtual_snapshot",
        "empty_snapshot",
        "invalid_snapshot_type",
    ):
        repository = MalformedPageRepository(aggregates)
        repository.mode = mode
        with pytest.raises(RuntimeError, match="invalid Action page"):
            await _service(repository).list("run-1", principal=PRINCIPAL, limit=2)

    repository = MalformedPageRepository(aggregates)
    service = _service(repository)
    first = await service.list("run-1", principal=PRINCIPAL, limit=1)
    assert first.next_cursor is not None
    repository.mode = "equal_after"
    with pytest.raises(RuntimeError, match="invalid Action page"):
        await service.list(
            "run-1",
            principal=PRINCIPAL,
            limit=1,
            cursor=first.next_cursor,
        )


async def test_list_reports_bounded_finding_and_event_coverage() -> None:
    aggregate = _aggregate(
        events=(
            ActionEventRead(
                event_id="event-1",
                sequence=1,
                event_type="tool.proposed",
                created_at=NOW,
            ),
        )
    )
    aggregate = ActionAggregateRead(
        intent=aggregate.intent,
        approval=aggregate.approval,
        executions=aggregate.executions,
        current_execution_id=aggregate.current_execution_id,
        execution_count=aggregate.execution_count,
        execution_coverage=aggregate.execution_coverage,
        result=aggregate.result,
        finding_ids=(),
        finding_count=101,
        events=aggregate.events,
        event_count=201,
        finding_coverage=ActionCoverage(scanned=0, limit=100, truncated=True),
        event_coverage=ActionCoverage(scanned=1, limit=200, truncated=True),
        correlation_quality=aggregate.correlation_quality,
        partial_reasons=aggregate.partial_reasons,
        updated_at=aggregate.updated_at,
    )
    service = _service(FakeActionReadRepository((aggregate,)))

    view = await service.get("run-1", "intent-1", principal=PRINCIPAL)
    listed = await service.list("run-1", principal=PRINCIPAL)

    assert view.evidence.finding_coverage.truncated is True
    assert view.evidence.event_coverage.truncated is True
    assert view.evidence.finding_coverage.limit == 100
    assert view.evidence.event_coverage.limit == 200
    assert view.evidence.finding_count == 101
    assert view.evidence.event_count == 201
    assert listed.items[0].version == view.version
