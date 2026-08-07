"""Historical Code Audit cleanup used only by Safety Stop reconciliation."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from riftx.application.errors import (
    ApplicationConflictError,
    EntityNotFoundError,
    RepositoryConflictError,
    RepositoryIntegrityError,
    RepositoryUnavailableError,
    ServiceUnavailableError,
)
from riftx.application.ports import (
    AuditAggregate,
    AuditAggregateReadRepository,
    AuditCleanupConvergence,
    AuditControlTransition,
    AuditControlUnitOfWork,
    RunEventRepository,
)
from riftx.domain import (
    AuditLifecycleStatus,
    AuditRunStateMappingPolicy,
    AuditTerminalOutcome,
    RunKind,
    RunStatus,
)
from riftx.domain.base import utc_now

from .run_safety import RunSafetyStopService, SafetyStopResult, stop_resources_payload

type AuditControlClock = Callable[[], datetime]


class AuditControlAction(StrEnum):
    PAUSE = "pause"
    CANCEL = "cancel"


class AuditControlDisposition(StrEnum):
    TRANSITION = "transition"
    RECONCILE = "reconcile"
    ALREADY_SATISFIED = "already_satisfied"
    SAFETY_ONLY = "safety_only"


@dataclass(frozen=True, slots=True)
class AuditControlPlan:
    operation: AuditControlAction
    disposition: AuditControlDisposition
    audit_id: str
    run_id: str
    expected_audit_state_version: int
    current_audit_lifecycle: AuditLifecycleStatus
    current_run_status: RunStatus
    reason_code: str
    target_audit_lifecycle: AuditLifecycleStatus | None = None
    target_run_status: RunStatus | None = None


class AuditRunStateProjector:
    """Translate a cleanup plan into one atomic persistence operation."""

    def __init__(
        self,
        unit_of_work: AuditControlUnitOfWork,
        *,
        clock: AuditControlClock = utc_now,
    ) -> None:
        self._unit_of_work = unit_of_work
        self._clock = clock

    async def transition(self, plan: AuditControlPlan) -> None:
        target_audit = plan.target_audit_lifecycle
        target_run = plan.target_run_status
        if target_audit is None or target_run is None:
            raise ApplicationConflictError(
                "audit_control_projection_invalid",
                "The Code Audit cleanup plan has no lifecycle target",
            )
        occurred_at = self._aware_now()
        identity = (
            plan.audit_id,
            plan.run_id,
            str(plan.expected_audit_state_version),
            plan.operation.value,
            target_audit.value,
            target_run.value,
        )
        await self._invoke(
            lambda: self._unit_of_work.transition(
                AuditControlTransition(
                    audit_id=plan.audit_id,
                    run_id=plan.run_id,
                    expected_audit_state_version=plan.expected_audit_state_version,
                    expected_audit_lifecycle=plan.current_audit_lifecycle,
                    expected_run_status=plan.current_run_status,
                    target_audit_lifecycle=target_audit,
                    target_run_status=target_run,
                    operation=plan.operation.value,
                    reason_code=plan.reason_code,
                    occurred_at=occurred_at,
                    audit_event_id=_event_id("audit-control", *identity),
                    run_event_id=_event_id("audit-run-status", *identity),
                    workflow_signal_kind=None,
                )
            )
        )

    async def converge_cleanup(
        self,
        aggregate: AuditAggregate,
        *,
        cleanup_proof_digest: str,
        operation: str,
        reason_code: str,
    ) -> None:
        scan = aggregate.audit.value
        occurred_at = self._aware_now()
        identity = (
            scan.id,
            aggregate.run.id,
            str(aggregate.audit.state_version),
            operation,
            cleanup_proof_digest,
        )
        await self._invoke(
            lambda: self._unit_of_work.converge_cleanup(
                AuditCleanupConvergence(
                    audit_id=scan.id,
                    run_id=aggregate.run.id,
                    expected_audit_state_version=aggregate.audit.state_version,
                    expected_audit_lifecycle=scan.lifecycle_status,
                    expected_run_status=aggregate.run.status,
                    cleanup_proof_digest=cleanup_proof_digest,
                    operation=operation,
                    reason_code=reason_code,
                    occurred_at=occurred_at,
                    audit_event_id=_event_id("audit-cleanup", *identity),
                    run_event_id=_event_id("audit-cleanup-run-status", *identity),
                )
            )
        )

    async def _invoke(self, operation: Callable[[], Awaitable[object]]) -> None:
        try:
            await operation()
        except RepositoryConflictError:
            raise ApplicationConflictError(
                "audit_control_conflict",
                "The Code Audit changed while applying cleanup",
            ) from None
        except (EntityNotFoundError, RepositoryIntegrityError, RepositoryUnavailableError):
            raise ServiceUnavailableError(
                "audit_persistence_unavailable",
                "RiftX Code Audit persistence is temporarily unavailable",
            ) from None

    def _aware_now(self) -> datetime:
        value = self._clock()
        if value.utcoffset() is None:
            raise ValueError("Audit control clock must return an aware datetime")
        return value


class AuditControlApplicationService:
    """Converge already-fenced historical Code Audit runs to a safe state."""

    def __init__(
        self,
        *,
        audits: AuditAggregateReadRepository,
        projector: AuditRunStateProjector,
        safety_stopper: RunSafetyStopService,
        events: RunEventRepository,
    ) -> None:
        self._audits = audits
        self._projector = projector
        self._safety_stopper = safety_stopper
        self._events = events

    async def reconcile_run(self, run_id: str) -> SafetyStopResult:
        aggregates = list(await self._list_for_run(run_id))
        if len(aggregates) != 1:
            raise ApplicationConflictError(
                "audit_cleanup_owner_conflict",
                "The fenced Code Audit Run has no unique Audit owner",
            )
        aggregate = aggregates[0]
        if aggregate.run.kind is not RunKind.CODE_AUDIT or aggregate.run.id != run_id:
            raise ApplicationConflictError(
                "audit_cleanup_owner_conflict",
                "The fenced Run is not owned by this Code Audit",
            )
        _require_audit_cleanup_effect(aggregate)
        if aggregate.run.status is RunStatus.PAUSING:
            return await self._pause(aggregate.audit.value.id)
        if aggregate.run.status is RunStatus.CANCELLING:
            return await self._cancel(aggregate.audit.value.id)
        if aggregate.run.status is RunStatus.COMPLETING:
            return await self._converge_non_cancel_cleanup(aggregate)
        raise ApplicationConflictError(
            "audit_cleanup_not_fenced",
            "The Code Audit Run is not in a cleanup fence",
        )

    async def _pause(self, audit_id: str) -> SafetyStopResult:
        plan = _plan_pause(await self._get(audit_id))
        if plan.disposition is AuditControlDisposition.ALREADY_SATISFIED:
            return _empty_stop_result()
        if plan.current_audit_lifecycle is not AuditLifecycleStatus.PAUSING:
            await self._projector.transition(plan)
            plan = _plan_pause(await self._get(audit_id))
        result = await self._safety_stopper.stop_run(plan.run_id, drain=True)
        await self._raise_if_stop_failed(plan, result)
        reconciled = _plan_pause(await self._get(audit_id))
        if reconciled.disposition is not AuditControlDisposition.ALREADY_SATISFIED:
            await self._projector.transition(reconciled)
        return result

    async def _cancel(self, audit_id: str) -> SafetyStopResult:
        plan = _plan_cancel(await self._get(audit_id))
        if plan.disposition is AuditControlDisposition.SAFETY_ONLY:
            result = await self._safety_stopper.stop_run(plan.run_id, drain=True)
            await self._raise_if_stop_failed(plan, result)
            await self._events.append(
                plan.run_id,
                "audit.cancel_safety_sweep",
                {
                    "audit_id": plan.audit_id,
                    "stop_resources": stop_resources_payload(result),
                },
                event_id=_event_id(
                    "audit-cancel-safety-sweep",
                    plan.audit_id,
                    str(plan.expected_audit_state_version),
                ),
            )
            return result
        if (
            plan.target_audit_lifecycle is AuditLifecycleStatus.CANCELLING
            and plan.current_audit_lifecycle is not AuditLifecycleStatus.CANCELLING
        ):
            await self._projector.transition(plan)
            plan = _plan_cancel(await self._get(audit_id))
        result = await self._safety_stopper.stop_run(plan.run_id, drain=True)
        await self._raise_if_stop_failed(plan, result)
        current = await self._get(audit_id)
        if current.audit.value.cleanup_proof_digest is None:
            await self._projector.converge_cleanup(
                current,
                cleanup_proof_digest=_cleanup_proof_digest(current, result),
                operation=AuditControlAction.CANCEL.value,
                reason_code="audit_cancel_cleanup_converged",
            )
        return result

    async def _converge_non_cancel_cleanup(
        self,
        aggregate: AuditAggregate,
    ) -> SafetyStopResult:
        scan = aggregate.audit.value
        if scan.lifecycle_status not in {
            AuditLifecycleStatus.FINALIZING,
            AuditLifecycleStatus.FAILING,
            AuditLifecycleStatus.CLEANING,
        }:
            raise ApplicationConflictError(
                "audit_cleanup_state_conflict",
                "The Code Audit lifecycle does not own this completion fence",
            )
        result = await self._safety_stopper.stop_run(aggregate.run.id, drain=True)
        plan = _control_plan(
            aggregate,
            operation=AuditControlAction.CANCEL,
            disposition=AuditControlDisposition.RECONCILE,
            reason_code="audit_cleanup_reconciliation_required",
            target_audit=None,
            target_run=None,
        )
        await self._raise_if_stop_failed(plan, result)
        await self._projector.converge_cleanup(
            aggregate,
            cleanup_proof_digest=_cleanup_proof_digest(aggregate, result),
            operation="cleanup",
            reason_code="audit_cleanup_converged",
        )
        return result

    async def _get(self, audit_id: str) -> AuditAggregate:
        try:
            aggregate = await self._audits.get(audit_id)
        except (RepositoryIntegrityError, RepositoryUnavailableError):
            raise _audit_persistence_unavailable() from None
        if aggregate is None:
            raise EntityNotFoundError("Audit", audit_id)
        _validate_run_projection(aggregate)
        return aggregate

    async def _list_for_run(self, run_id: str) -> Sequence[AuditAggregate]:
        try:
            aggregates = await self._audits.list(run_id=run_id, limit=2, offset=0)
        except (RepositoryIntegrityError, RepositoryUnavailableError):
            raise _audit_persistence_unavailable() from None
        for aggregate in aggregates:
            _validate_run_projection(aggregate)
        return aggregates

    async def _raise_if_stop_failed(
        self,
        plan: AuditControlPlan,
        result: SafetyStopResult,
    ) -> None:
        if result.succeeded:
            return
        payload: dict[str, object] = {
            "audit_id": plan.audit_id,
            "operation": plan.operation.value,
            "stop_resources": stop_resources_payload(result),
            "failed_resource_types": list(result.failed_resource_types),
        }
        await self._events.append(
            plan.run_id,
            "audit.safety_stop_unconfirmed",
            payload,
            event_id=_event_id(
                "audit-safety-stop-unconfirmed",
                plan.audit_id,
                plan.operation.value,
                str(plan.expected_audit_state_version),
            ),
        )
        raise ServiceUnavailableError(
            "audit_safety_stop_failed",
            "Could not confirm that every Code Audit effect was safely stopped",
            details={
                "audit_id": plan.audit_id,
                "run_id": plan.run_id,
                "failed_resource_types": list(result.failed_resource_types),
                "stop_resources": stop_resources_payload(result),
            },
        )


def _plan_pause(aggregate: AuditAggregate) -> AuditControlPlan:
    status = aggregate.audit.value.lifecycle_status
    if status is AuditLifecycleStatus.PAUSING:
        return _control_plan(
            aggregate,
            operation=AuditControlAction.PAUSE,
            disposition=AuditControlDisposition.RECONCILE,
            reason_code="audit_pause_reconciliation_required",
            target_audit=AuditLifecycleStatus.PAUSED,
            target_run=RunStatus.PAUSED,
        )
    if status is AuditLifecycleStatus.PAUSED:
        return _control_plan(
            aggregate,
            operation=AuditControlAction.PAUSE,
            disposition=AuditControlDisposition.ALREADY_SATISFIED,
            reason_code="audit_already_paused",
            target_audit=AuditLifecycleStatus.PAUSED,
            target_run=RunStatus.PAUSED,
        )
    raise ApplicationConflictError(
        "audit_not_pauseable",
        f"Cannot reconcile Audit {aggregate.audit.value.id!r} while it is {status.value}",
    )


def _plan_cancel(aggregate: AuditAggregate) -> AuditControlPlan:
    scan = aggregate.audit.value
    cleanup_converged = (
        scan.cleanup_proof_digest is not None and scan.run_terminal_status is not None
    )
    publication_or_terminal = scan.lifecycle_status in {
        AuditLifecycleStatus.SEALING_CORE,
        AuditLifecycleStatus.REPORTING,
        AuditLifecycleStatus.PACKAGING,
        AuditLifecycleStatus.COMPLETED,
        AuditLifecycleStatus.COMPLETED_PARTIAL,
        AuditLifecycleStatus.FAILED,
        AuditLifecycleStatus.CANCELLED,
    }
    if cleanup_converged or publication_or_terminal:
        disposition = AuditControlDisposition.SAFETY_ONLY
        target_audit = scan.lifecycle_status
        target_run = aggregate.run.status
        reason_code = "audit_cancel_safety_sweep"
    elif scan.lifecycle_status is AuditLifecycleStatus.CANCELLING:
        disposition = AuditControlDisposition.RECONCILE
        target_audit = AuditLifecycleStatus.CLEANING
        target_run = RunStatus.CANCELLING
        reason_code = "audit_cancel_reconciliation_required"
    elif scan.lifecycle_status is AuditLifecycleStatus.CLEANING:
        disposition = AuditControlDisposition.RECONCILE
        target_audit = (
            AuditLifecycleStatus.CLEANING
            if scan.terminal_outcome is AuditTerminalOutcome.CANCELLED
            else AuditLifecycleStatus.CANCELLING
        )
        target_run = RunStatus.CANCELLING
        reason_code = "audit_cancel_reconciliation_required"
    else:
        disposition = AuditControlDisposition.SAFETY_ONLY
        target_audit = scan.lifecycle_status
        target_run = aggregate.run.status
        reason_code = "audit_cancel_safety_sweep"
    return _control_plan(
        aggregate,
        operation=AuditControlAction.CANCEL,
        disposition=disposition,
        reason_code=reason_code,
        target_audit=target_audit,
        target_run=target_run,
    )


def _validate_run_projection(aggregate: AuditAggregate) -> None:
    scan = aggregate.audit.value
    try:
        expected = AuditRunStateMappingPolicy.expected_run_status(scan)
    except ValueError:
        expected = None
    if (
        aggregate.run.kind is not RunKind.CODE_AUDIT
        or expected is None
        or aggregate.run.status is not expected
        or aggregate.run.id != scan.run_id
        or aggregate.run.temporal_workflow_id != scan.temporal_workflow_id
    ):
        raise ApplicationConflictError(
            "audit_run_state_conflict",
            "The Code Audit and its Run have inconsistent durable state",
        )


def _require_audit_cleanup_effect(aggregate: AuditAggregate) -> None:
    from riftx.application.run_kind_effects import (
        RunEffectOwnership,
        RunKindEffectPolicyDenied,
        require_run_kind_effect_policy,
    )

    try:
        require_run_kind_effect_policy(
            "service.audit.reconcile",
            "safety_reconciler",
            ownership=RunEffectOwnership(
                run_id=aggregate.run.id,
                run_kind=aggregate.run.kind,
                audit_id=aggregate.audit.value.id,
            ),
            effect="host_control",
            mode="reconcile",
        )
    except (RunKindEffectPolicyDenied, TypeError, ValueError):
        raise ApplicationConflictError(
            "run_kind_effect_policy_denied",
            "The requested Code Audit cleanup is not admitted for this owner",
        ) from None


def _control_plan(
    aggregate: AuditAggregate,
    *,
    operation: AuditControlAction,
    disposition: AuditControlDisposition,
    reason_code: str,
    target_audit: AuditLifecycleStatus | None,
    target_run: RunStatus | None,
) -> AuditControlPlan:
    scan = aggregate.audit.value
    return AuditControlPlan(
        operation=operation,
        disposition=disposition,
        audit_id=scan.id,
        run_id=aggregate.run.id,
        expected_audit_state_version=aggregate.audit.state_version,
        current_audit_lifecycle=scan.lifecycle_status,
        current_run_status=aggregate.run.status,
        reason_code=reason_code,
        target_audit_lifecycle=target_audit,
        target_run_status=target_run,
    )


def _cleanup_proof_digest(
    aggregate: AuditAggregate,
    result: SafetyStopResult,
) -> str:
    canonical = json.dumps(
        {
            "schema_version": "riftx.audit-cleanup-proof/v1",
            "audit_id": aggregate.audit.value.id,
            "run_id": aggregate.run.id,
            "stop_resources": stop_resources_payload(result),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _event_id(domain: str, *parts: str) -> str:
    canonical = "\x00".join((f"riftx.{domain}/v1", *parts)).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _audit_persistence_unavailable() -> ServiceUnavailableError:
    return ServiceUnavailableError(
        "audit_persistence_unavailable",
        "RiftX Code Audit persistence is temporarily unavailable",
    )


def _empty_stop_result() -> SafetyStopResult:
    return SafetyStopResult(resources={})


__all__ = ["AuditControlApplicationService", "AuditRunStateProjector"]
