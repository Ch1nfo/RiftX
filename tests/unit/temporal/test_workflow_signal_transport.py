from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from temporalio.api.enums.v1 import EventType

from riftx.application.errors import (
    ApplicationConflictError,
    RepositoryConflictError,
    ServiceUnavailableError,
)
from riftx.application.services.workflow_signals import (
    WorkflowSignalDefinitelyNotDelivered,
    WorkflowSignalObservationState,
    WorkflowSignalOutcomeUnknown,
    WorkflowSignalTerminallyRejected,
)
from riftx.application.workflow_router import WorkflowDispatchDisposition
from riftx.domain import RunKind, RunStatus
from riftx.domain.workflow_signal import (
    WorkflowSignalIntent,
    WorkflowSignalKind,
    WorkflowSignalSourceKind,
)
from riftx.temporal.workflow_signal_transport import (
    RoutedWorkflowSignalTransport,
    TemporalWorkflowSignalOutcomeProbe,
)

NOW = datetime(2026, 8, 3, tzinfo=UTC)


def _general_intent() -> WorkflowSignalIntent:
    return WorkflowSignalIntent.general_run(
        run_id="run-1",
        workflow_id="riftx-run-run-1",
        signal_kind=WorkflowSignalKind.APPROVE,
        source_event_kind=WorkflowSignalSourceKind.APPROVAL_DECISION,
        source_event_id="approval-event-1",
        source_state_version=1,
        payload={"approval_id": "approval-1"},
        created_at=NOW,
    )


def _audit_intent() -> WorkflowSignalIntent:
    return WorkflowSignalIntent.code_audit(
        audit_id="audit-1",
        run_id="run-audit-1",
        workflow_id="riftx-code-audit-audit-1",
        signal_kind=WorkflowSignalKind.CANCEL,
        source_event_kind=WorkflowSignalSourceKind.CONTROL_INTENT,
        source_event_id="audit-control-1",
        source_state_version=1,
        payload={"audit_id": "audit-1"},
        created_at=NOW,
    )


def _pentest_intent(
    *,
    signal_kind: WorkflowSignalKind = WorkflowSignalKind.APPROVE,
    source_event_kind: WorkflowSignalSourceKind = (
        WorkflowSignalSourceKind.APPROVAL_DECISION
    ),
    payload: dict[str, object] | None = None,
) -> WorkflowSignalIntent:
    return WorkflowSignalIntent.pentest_run(
        run_id="pentest-1",
        workflow_id="riftx-pentest-pentest-1",
        signal_kind=signal_kind,
        source_event_kind=source_event_kind,
        source_event_id="pentest-source-1",
        source_state_version=1,
        payload=payload or {"approval_id": "approval-pentest-1"},
        created_at=NOW,
    )


class _Router:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []
        self.exact_workflow_ids: list[str | None] = []
        self.general_workflow_id = "riftx-run-run-1"
        self.approve_error: Exception | None = None
        self.audit_disposition = WorkflowDispatchDisposition.DISPATCHED

    def workflow_id(self, run_id: str) -> str:
        assert run_id == "run-1"
        return self.general_workflow_id

    async def approve(
        self,
        run_id: str,
        approval_id: str,
        *,
        workflow_id: str | None = None,
    ) -> None:
        if self.approve_error is not None:
            raise self.approve_error
        self.exact_workflow_ids.append(workflow_id)
        self.calls.append(("approve", run_id, approval_id))

    async def reject(
        self,
        run_id: str,
        approval_id: str,
        *,
        workflow_id: str | None = None,
    ) -> None:
        self.exact_workflow_ids.append(workflow_id)
        self.calls.append(("reject", run_id, approval_id))

    async def execution_completed(
        self,
        run_id: str,
        execution_id: str,
        *,
        workflow_id: str | None = None,
    ) -> None:
        self.exact_workflow_ids.append(workflow_id)
        self.calls.append(("execution_completed", run_id, execution_id))

    async def pause(self, run_id: str, *, workflow_id: str | None = None) -> None:
        self.exact_workflow_ids.append(workflow_id)
        self.calls.append(("pause", run_id))

    async def resume(self, run_id: str, *, workflow_id: str | None = None) -> None:
        self.exact_workflow_ids.append(workflow_id)
        self.calls.append(("resume", run_id))

    async def cancel(self, run_id: str, *, workflow_id: str | None = None) -> None:
        self.exact_workflow_ids.append(workflow_id)
        self.calls.append(("cancel", run_id))

    async def pause_audit(self, **owner: str) -> WorkflowDispatchDisposition:
        self.calls.append(("pause_audit", owner))
        return WorkflowDispatchDisposition.DISPATCHED

    async def resume_audit(self, **owner: str) -> WorkflowDispatchDisposition:
        self.calls.append(("resume_audit", owner))
        return WorkflowDispatchDisposition.DISPATCHED

    async def cancel_audit(self, **owner: str) -> WorkflowDispatchDisposition:
        self.calls.append(("cancel_audit", owner))
        return self.audit_disposition


def _runs(
    *,
    kind: RunKind = RunKind.GENERAL,
    status: RunStatus = RunStatus.RUNNING,
    workflow_id: str = "riftx-run-run-1",
) -> SimpleNamespace:
    return SimpleNamespace(
        get=AsyncMock(
            return_value=SimpleNamespace(
                kind=kind,
                status=status,
                temporal_workflow_id=workflow_id,
            )
        )
    )


def _sources(*, error: Exception | None = None) -> SimpleNamespace:
    validate = AsyncMock(side_effect=error)

    @asynccontextmanager
    async def guard(intent: WorkflowSignalIntent) -> AsyncIterator[None]:
        await validate(intent)
        yield

    return SimpleNamespace(
        validate_for_delivery=validate,
        guard_for_delivery=guard,
    )


async def test_transport_dispatches_general_approval_to_exact_workflow() -> None:
    router = _Router()
    intent = _general_intent()

    receipt = await RoutedWorkflowSignalTransport(
        router,  # type: ignore[arg-type]
        runs=_runs(),  # type: ignore[arg-type]
        sources=_sources(),  # type: ignore[arg-type]
    ).send(intent)

    assert router.calls == [("approve", "run-1", "approval-1")]
    assert router.exact_workflow_ids == ["riftx-run-run-1"]
    assert receipt.workflow_id == intent.workflow_id
    assert receipt.identity_digest == intent.identity_digest
    assert receipt.payload_digest == intent.payload_digest


async def test_transport_dispatches_pentest_approval_without_general_owner_fallback() -> None:
    router = _Router()
    intent = _pentest_intent()

    receipt = await RoutedWorkflowSignalTransport(
        router,  # type: ignore[arg-type]
        runs=_runs(
            kind=RunKind.PENTEST,
            workflow_id="riftx-pentest-pentest-1",
        ),  # type: ignore[arg-type]
        sources=_sources(),  # type: ignore[arg-type]
    ).send(intent)

    assert router.calls == [("approve", "pentest-1", "approval-pentest-1")]
    assert router.exact_workflow_ids == ["riftx-pentest-pentest-1"]
    assert receipt.owner_kind is intent.owner_kind
    assert receipt.workflow_protocol_version == intent.workflow_protocol_version


async def test_transport_supersedes_general_workflow_identity_drift_before_signal() -> None:
    router = _Router()

    with pytest.raises(WorkflowSignalTerminallyRejected) as captured:
        await RoutedWorkflowSignalTransport(
            router,  # type: ignore[arg-type]
            runs=_runs(workflow_id="riftx-run-historical"),  # type: ignore[arg-type]
            sources=_sources(),  # type: ignore[arg-type]
        ).send(_general_intent())

    assert captured.value.error_code == "persisted_workflow_identity_mismatch"
    assert router.calls == []


async def test_transport_preconnect_unavailable_is_retryable() -> None:
    router = _Router()
    router.approve_error = ServiceUnavailableError(
        "temporal_unavailable",
        "Temporal connection unavailable",
    )

    with pytest.raises(WorkflowSignalDefinitelyNotDelivered) as captured:
        await RoutedWorkflowSignalTransport(
            router,  # type: ignore[arg-type]
            runs=_runs(),  # type: ignore[arg-type]
            sources=_sources(),  # type: ignore[arg-type]
        ).send(_general_intent())

    assert captured.value.error_code == "temporal_unavailable"


async def test_transport_post_send_rpc_failure_is_outcome_unknown() -> None:
    router = _Router()
    router.approve_error = ServiceUnavailableError(
        "temporal_unavailable",
        "Temporal response unavailable",
        details={"rpc_status": "unavailable"},
    )

    with pytest.raises(WorkflowSignalOutcomeUnknown) as captured:
        await RoutedWorkflowSignalTransport(
            router,  # type: ignore[arg-type]
            runs=_runs(),  # type: ignore[arg-type]
            sources=_sources(),  # type: ignore[arg-type]
        ).send(_general_intent())

    assert captured.value.error_code == "temporal_unavailable"


async def test_transport_defers_approval_while_run_is_pausing() -> None:
    router = _Router()
    runs = _runs(status=RunStatus.PAUSING)

    with pytest.raises(WorkflowSignalDefinitelyNotDelivered) as captured:
        await RoutedWorkflowSignalTransport(
            router,
            runs=runs,
            sources=_sources(),
        ).send(  # type: ignore[arg-type]
            _general_intent()
        )

    assert captured.value.error_code == "approval_signal_deferred_by_run_state"
    assert router.calls == []


async def test_transport_supersedes_approval_after_run_is_terminal() -> None:
    router = _Router()
    runs = _runs(status=RunStatus.COMPLETED)

    with pytest.raises(WorkflowSignalTerminallyRejected) as captured:
        await RoutedWorkflowSignalTransport(
            router,
            runs=runs,
            sources=_sources(),
        ).send(  # type: ignore[arg-type]
            _general_intent()
        )

    assert captured.value.error_code == "approval_signal_superseded_by_run_state"
    assert router.calls == []


async def test_transport_supersedes_invalid_payload_and_policy_rejection() -> None:
    router = _Router()
    invalid = WorkflowSignalIntent.general_run(
        run_id="run-1",
        workflow_id="riftx-run-run-1",
        signal_kind=WorkflowSignalKind.APPROVE,
        source_event_kind=WorkflowSignalSourceKind.APPROVAL_DECISION,
        source_event_id="approval-event-invalid",
        source_state_version=1,
        payload={},
        created_at=NOW,
    )
    with pytest.raises(WorkflowSignalTerminallyRejected) as captured:
        await RoutedWorkflowSignalTransport(
            router,  # type: ignore[arg-type]
            runs=_runs(),  # type: ignore[arg-type]
            sources=_sources(),  # type: ignore[arg-type]
        ).send(invalid)
    assert captured.value.error_code == "workflow_signal_payload_invalid"

    router.approve_error = ApplicationConflictError(
        "run_kind_effect_policy_denied",
        "policy denied",
    )
    with pytest.raises(WorkflowSignalTerminallyRejected) as captured:
        await RoutedWorkflowSignalTransport(
            router,  # type: ignore[arg-type]
            runs=_runs(),  # type: ignore[arg-type]
            sources=_sources(),  # type: ignore[arg-type]
        ).send(_general_intent())
    assert captured.value.error_code == "run_kind_effect_policy_denied"


async def test_transport_supersedes_audit_signal_when_workflow_never_started() -> None:
    router = _Router()
    router.audit_disposition = WorkflowDispatchDisposition.NOT_STARTED

    with pytest.raises(WorkflowSignalTerminallyRejected) as captured:
        await RoutedWorkflowSignalTransport(
            router,  # type: ignore[arg-type]
            runs=_runs(
                kind=RunKind.CODE_AUDIT,
                workflow_id="riftx-code-audit-audit-1",
            ),  # type: ignore[arg-type]
            sources=_sources(),  # type: ignore[arg-type]
        ).send(_audit_intent())

    assert captured.value.error_code == "audit_workflow_not_started"


async def test_transport_routes_audit_cancel_without_general_fallback() -> None:
    router = _Router()

    await RoutedWorkflowSignalTransport(
        router,  # type: ignore[arg-type]
        runs=_runs(
            kind=RunKind.CODE_AUDIT,
            workflow_id="riftx-code-audit-audit-1",
        ),  # type: ignore[arg-type]
        sources=_sources(),  # type: ignore[arg-type]
    ).send(_audit_intent())

    assert router.calls == [
        (
            "cancel_audit",
            {
                "audit_id": "audit-1",
                "run_id": "run-audit-1",
                "signal_identity_digest": _audit_intent().identity_digest,
            },
        )
    ]


async def test_transport_rejects_foreign_child_source_before_router_call() -> None:
    router = _Router()

    with pytest.raises(WorkflowSignalTerminallyRejected) as captured:
        await RoutedWorkflowSignalTransport(
            router,  # type: ignore[arg-type]
            runs=_runs(),  # type: ignore[arg-type]
            sources=_sources(
                error=RepositoryConflictError(
                    "Workflow signal source belongs to a different Run"
                )
            ),  # type: ignore[arg-type]
        ).send(_general_intent())

    assert captured.value.error_code == "workflow_signal_rejected"
    assert router.calls == []


class _Converter:
    async def decode(self, payloads: object) -> list[object]:
        assert payloads
        return ["approval-1"]


class _Handle:
    async def fetch_history_events(self):  # type: ignore[no-untyped-def]
        yield SimpleNamespace(
            event_type=EventType.EVENT_TYPE_WORKFLOW_EXECUTION_SIGNALED,
            event_id=42,
            workflow_execution_signaled_event_attributes=SimpleNamespace(
                signal_name="approve",
                input=SimpleNamespace(payloads=(object(),)),
            ),
        )


class _Client:
    data_converter = _Converter()

    def get_workflow_handle(self, workflow_id: str) -> _Handle:
        assert workflow_id == "riftx-run-run-1"
        return _Handle()


async def test_history_probe_observes_exact_signal_payload() -> None:
    async def provider() -> _Client:
        return _Client()

    observation = await TemporalWorkflowSignalOutcomeProbe(provider).observe(  # type: ignore[arg-type]
        _general_intent()
    )

    assert observation.state is WorkflowSignalObservationState.DELIVERED
    assert observation.workflow_id == "riftx-run-run-1"


async def test_history_probe_correlates_audit_control_with_durable_identity() -> None:
    intent = _audit_intent()

    class AuditConverter:
        async def decode(self, payloads: object) -> list[object]:
            assert payloads
            return ["audit-1", intent.identity_digest]

    class AuditHandle:
        async def fetch_history_events(self):  # type: ignore[no-untyped-def]
            yield SimpleNamespace(
                event_type=EventType.EVENT_TYPE_WORKFLOW_EXECUTION_SIGNALED,
                event_id=84,
                workflow_execution_signaled_event_attributes=SimpleNamespace(
                    signal_name="cancel",
                    input=SimpleNamespace(payloads=(object(), object())),
                ),
            )

    class AuditClient:
        data_converter = AuditConverter()

        def get_workflow_handle(self, workflow_id: str) -> AuditHandle:
            assert workflow_id == "riftx-code-audit-audit-1"
            return AuditHandle()

    async def provider() -> AuditClient:
        return AuditClient()

    observation = await TemporalWorkflowSignalOutcomeProbe(provider).observe(  # type: ignore[arg-type]
        intent
    )

    assert observation.state is WorkflowSignalObservationState.DELIVERED
    assert observation.identity_digest == intent.identity_digest


async def test_history_probe_keeps_zero_argument_signal_outcome_unknown() -> None:
    pause_intent = WorkflowSignalIntent.general_run(
        run_id="run-1",
        workflow_id="riftx-run-run-1",
        signal_kind=WorkflowSignalKind.PAUSE,
        source_event_kind=WorkflowSignalSourceKind.CONTROL_INTENT,
        source_event_id="pause-event-1",
        source_state_version=1,
        payload={},
        created_at=NOW,
    )
    provider = AsyncMock(side_effect=AssertionError("history must not be queried"))

    observation = await TemporalWorkflowSignalOutcomeProbe(provider).observe(pause_intent)

    assert observation.state is WorkflowSignalObservationState.UNKNOWN
    provider.assert_not_awaited()
