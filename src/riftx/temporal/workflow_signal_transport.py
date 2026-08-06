"""Exact-identity Temporal transport and history probe for signal intents."""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable, Sequence

from temporalio.api.enums.v1 import EventType
from temporalio.client import Client

from riftx.application.errors import (
    ApplicationConflictError,
    EntityNotFoundError,
    RepositoryConflictError,
    RepositoryIntegrityError,
    ServiceUnavailableError,
)
from riftx.application.ports.repositories import RunRepository
from riftx.application.ports.workflow_signals import WorkflowSignalSourceValidator
from riftx.application.services.workflow_signals import (
    WorkflowSignalDefinitelyNotDelivered,
    WorkflowSignalObservation,
    WorkflowSignalObservationState,
    WorkflowSignalOutcomeUnknown,
    WorkflowSignalTerminallyRejected,
    WorkflowSignalTransportReceipt,
)
from riftx.application.workflow_router import (
    RunWorkflowControlRouter,
    WorkflowDispatchDisposition,
)
from riftx.domain import RunStatus
from riftx.domain.workflow_signal import (
    WorkflowSignalIntent,
    WorkflowSignalKind,
    WorkflowSignalOwnerKind,
)

type TemporalClientProvider = Callable[[], Awaitable[Client]]

_APPROVAL_TERMINAL_RUN_STATUSES = frozenset(
    {
        RunStatus.CANCELLING,
        RunStatus.COMPLETING,
        RunStatus.COMPLETED,
        RunStatus.FAILED,
        RunStatus.CANCELLED,
    }
)
_INTERACTIVE_WORKFLOW_OWNERS = frozenset(
    {
        WorkflowSignalOwnerKind.GENERAL_RUN,
        WorkflowSignalOwnerKind.PENTEST_RUN,
    }
)


class RoutedWorkflowSignalTransport:
    """Dispatch through one RunKind router without deriving a new owner."""

    def __init__(
        self,
        router: RunWorkflowControlRouter,
        *,
        runs: RunRepository,
        sources: WorkflowSignalSourceValidator,
    ) -> None:
        self._router = router
        self._runs = runs
        self._sources = sources

    async def send(self, intent: WorkflowSignalIntent) -> WorkflowSignalTransportReceipt:
        try:
            async with self._sources.guard_for_delivery(intent):
                await self._validate_pre_dispatch(intent)
                await self._dispatch(intent)
        except (
            EntityNotFoundError,
            RepositoryConflictError,
            RepositoryIntegrityError,
            ApplicationConflictError,
        ) as exc:
            raise WorkflowSignalTerminallyRejected(
                _safe_error_code(exc, "workflow_signal_rejected")
            ) from exc
        except ServiceUnavailableError as exc:
            error_code = _safe_error_code(exc, "workflow_transport_unavailable")
            if _failure_may_follow_send(exc):
                raise WorkflowSignalOutcomeUnknown(error_code) from exc
            raise WorkflowSignalDefinitelyNotDelivered(error_code) from exc
        except WorkflowSignalDefinitelyNotDelivered:
            raise
        except WorkflowSignalTerminallyRejected:
            raise
        except Exception as exc:
            raise WorkflowSignalOutcomeUnknown("workflow_transport_outcome_unknown") from exc
        return WorkflowSignalTransportReceipt(
            owner_kind=intent.owner_kind,
            workflow_protocol_version=intent.workflow_protocol_version,
            workflow_id=intent.workflow_id,
            signal_kind=intent.signal_kind,
            identity_digest=intent.identity_digest,
            payload_digest=intent.payload_digest,
            transport_receipt=_receipt(
                "accepted",
                intent.identity_digest,
                intent.payload_digest,
            ),
        )

    async def _validate_pre_dispatch(self, intent: WorkflowSignalIntent) -> None:
        run = await self._runs.get(intent.run_id)
        if run is None:
            raise WorkflowSignalTerminallyRejected("run_not_found")
        if run.kind is not intent.run_kind:
            raise WorkflowSignalTerminallyRejected("run_owner_mismatch")
        if run.temporal_workflow_id != intent.workflow_id:
            raise WorkflowSignalTerminallyRejected(
                "persisted_workflow_identity_mismatch"
            )
        if intent.owner_kind not in _INTERACTIVE_WORKFLOW_OWNERS:
            return
        if intent.signal_kind in {
            WorkflowSignalKind.APPROVE,
            WorkflowSignalKind.REJECT,
        }:
            _payload_id(intent, "approval_id")
            if run.status is RunStatus.PAUSING:
                raise WorkflowSignalDefinitelyNotDelivered(
                    "approval_signal_deferred_by_run_state"
                )
            if run.status in _APPROVAL_TERMINAL_RUN_STATUSES:
                raise WorkflowSignalTerminallyRejected(
                    "approval_signal_superseded_by_run_state"
                )
        elif intent.signal_kind is WorkflowSignalKind.EXECUTION_COMPLETED:
            _payload_id(intent, "execution_id")

    async def _dispatch(self, intent: WorkflowSignalIntent) -> None:
        if intent.owner_kind in _INTERACTIVE_WORKFLOW_OWNERS:
            if intent.signal_kind is WorkflowSignalKind.APPROVE:
                await self._router.approve(
                    intent.run_id,
                    _payload_id(intent, "approval_id"),
                    workflow_id=intent.workflow_id,
                )
                return
            if intent.signal_kind is WorkflowSignalKind.REJECT:
                await self._router.reject(
                    intent.run_id,
                    _payload_id(intent, "approval_id"),
                    workflow_id=intent.workflow_id,
                )
                return
            if intent.signal_kind is WorkflowSignalKind.EXECUTION_COMPLETED:
                await self._router.execution_completed(
                    intent.run_id,
                    _payload_id(intent, "execution_id"),
                    workflow_id=intent.workflow_id,
                )
                return
            if intent.signal_kind is WorkflowSignalKind.PAUSE:
                await self._router.pause(
                    intent.run_id,
                    workflow_id=intent.workflow_id,
                )
                return
            if intent.signal_kind is WorkflowSignalKind.RESUME:
                await self._router.resume(
                    intent.run_id,
                    workflow_id=intent.workflow_id,
                )
                return
            if intent.signal_kind is WorkflowSignalKind.CANCEL:
                await self._router.cancel(
                    intent.run_id,
                    workflow_id=intent.workflow_id,
                )
                return
            raise WorkflowSignalTerminallyRejected("unsupported_interactive_signal_kind")

        if intent.owner_kind is not WorkflowSignalOwnerKind.CODE_AUDIT:
            raise WorkflowSignalTerminallyRejected("unsupported_workflow_signal_owner")
        audit_id = intent.audit_id
        if audit_id is None or intent.workflow_id != f"riftx-code-audit-{audit_id}":
            raise WorkflowSignalTerminallyRejected("audit_workflow_identity_mismatch")
        if intent.signal_kind is WorkflowSignalKind.PAUSE:
            disposition = await self._router.pause_audit(
                audit_id=audit_id,
                run_id=intent.run_id,
                signal_identity_digest=intent.identity_digest,
            )
        elif intent.signal_kind is WorkflowSignalKind.RESUME:
            disposition = await self._router.resume_audit(
                audit_id=audit_id,
                run_id=intent.run_id,
                signal_identity_digest=intent.identity_digest,
            )
        elif intent.signal_kind is WorkflowSignalKind.CANCEL:
            disposition = await self._router.cancel_audit(
                audit_id=audit_id,
                run_id=intent.run_id,
                signal_identity_digest=intent.identity_digest,
            )
        else:
            raise WorkflowSignalTerminallyRejected("unsupported_audit_signal_kind")
        if disposition is WorkflowDispatchDisposition.NOT_STARTED:
            raise WorkflowSignalTerminallyRejected("audit_workflow_not_started")


class TemporalWorkflowSignalOutcomeProbe:
    """Resolve ambiguous sends by inspecting the exact Workflow history."""

    def __init__(self, client_provider: TemporalClientProvider) -> None:
        self._client_provider = client_provider

    async def observe(self, intent: WorkflowSignalIntent) -> WorkflowSignalObservation:
        correlatable = intent.signal_kind in {
            WorkflowSignalKind.APPROVE,
            WorkflowSignalKind.REJECT,
            WorkflowSignalKind.EXECUTION_COMPLETED,
        } or (
            intent.owner_kind is WorkflowSignalOwnerKind.CODE_AUDIT
            and intent.signal_kind
            in {
                WorkflowSignalKind.PAUSE,
                WorkflowSignalKind.RESUME,
                WorkflowSignalKind.CANCEL,
            }
        )
        if not correlatable:
            # General/Pentest controls retain the zero-argument wire contract,
            # and undefined signal kinds have no correlation
            # contract. A same-name history event cannot prove that this exact
            # durable intent was accepted.
            return _observation(
                intent,
                WorkflowSignalObservationState.UNKNOWN,
                _receipt(
                    "history-uncorrelatable",
                    intent.signal_kind.value,
                    intent.identity_digest,
                ),
            )
        try:
            client = await self._client_provider()
            handle = client.get_workflow_handle(intent.workflow_id)
            expected_name = intent.signal_kind.value
            expected_args = _expected_signal_args(intent)
            async for event in handle.fetch_history_events():
                if event.event_type != EventType.EVENT_TYPE_WORKFLOW_EXECUTION_SIGNALED:
                    continue
                attributes = event.workflow_execution_signaled_event_attributes
                if attributes.signal_name != expected_name:
                    continue
                payloads = getattr(attributes.input, "payloads", ())
                decoded = (
                    await client.data_converter.decode(payloads)
                    if payloads
                    else []
                )
                if list(decoded) != list(expected_args):
                    continue
                return _observation(
                    intent,
                    WorkflowSignalObservationState.DELIVERED,
                    _receipt("history-event", str(event.event_id), intent.identity_digest),
                )
        except Exception as exc:
            return _observation(
                intent,
                WorkflowSignalObservationState.UNKNOWN,
                _receipt("history-unavailable", type(exc).__name__, intent.identity_digest),
            )
        return _observation(
            intent,
            WorkflowSignalObservationState.NOT_DELIVERED,
            _receipt("history-absent", intent.identity_digest, intent.payload_digest),
        )


def _expected_signal_args(intent: WorkflowSignalIntent) -> Sequence[object]:
    if intent.signal_kind in {WorkflowSignalKind.APPROVE, WorkflowSignalKind.REJECT}:
        return (_payload_id(intent, "approval_id"),)
    if intent.signal_kind is WorkflowSignalKind.EXECUTION_COMPLETED:
        return (_payload_id(intent, "execution_id"),)
    if (
        intent.owner_kind is WorkflowSignalOwnerKind.CODE_AUDIT
        and intent.signal_kind
        in {
            WorkflowSignalKind.PAUSE,
            WorkflowSignalKind.RESUME,
            WorkflowSignalKind.CANCEL,
        }
    ):
        return (_payload_id(intent, "audit_id"), intent.identity_digest)
    return ()


def _payload_id(intent: WorkflowSignalIntent, key: str) -> str:
    value = intent.payload.get(key)
    if not isinstance(value, str) or not value:
        raise WorkflowSignalTerminallyRejected("workflow_signal_payload_invalid")
    return value


def _observation(
    intent: WorkflowSignalIntent,
    state: WorkflowSignalObservationState,
    receipt: str,
) -> WorkflowSignalObservation:
    return WorkflowSignalObservation(
        state=state,
        owner_kind=intent.owner_kind,
        workflow_protocol_version=intent.workflow_protocol_version,
        workflow_id=intent.workflow_id,
        signal_kind=intent.signal_kind,
        identity_digest=intent.identity_digest,
        payload_digest=intent.payload_digest,
        observation_receipt=receipt,
    )


def _receipt(domain: str, *parts: str) -> str:
    encoded = "\x00".join((f"riftx.workflow-signal-{domain}/v1", *parts)).encode(
        "utf-8"
    )
    return hashlib.sha256(encoded).hexdigest()


def _safe_error_code(exc: object, fallback: str) -> str:
    code = getattr(exc, "code", None)
    if not isinstance(code, str):
        return fallback
    normalized = code.strip().lower()
    if (
        not normalized
        or len(normalized) > 128
        or not normalized[0].isalpha()
        or any(
            character not in "abcdefghijklmnopqrstuvwxyz0123456789._-"
            for character in normalized
        )
    ):
        return fallback
    return normalized


def _failure_may_follow_send(exc: ServiceUnavailableError) -> bool:
    """Differentiate connector failure from an ambiguous Temporal RPC result."""

    rpc_status = exc.details.get("rpc_status")
    return isinstance(rpc_status, str) and bool(rpc_status)


__all__ = [
    "RoutedWorkflowSignalTransport",
    "TemporalWorkflowSignalOutcomeProbe",
]
