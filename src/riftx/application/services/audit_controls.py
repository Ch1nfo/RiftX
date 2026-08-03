"""Kind-aware mutation layer for RiftX Code Audit controls."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable
from datetime import datetime

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
    AuditCleanupConvergence,
    AuditControlTransition,
    AuditControlUnitOfWork,
    AuditObjectAuthorizer,
    RunEventRepository,
)
from riftx.domain import (
    AuditLifecycleStatus,
    LocalPrincipal,
    OperatorCapability,
    RunKind,
    RunStatus,
    WorkflowSignalKind,
)
from riftx.domain.base import utc_now

from .audits import (
    AuditApplicationService,
    AuditControlAction,
    AuditControlDisposition,
    AuditControlEffect,
    AuditControlPlan,
)
from .run_safety import RunSafetyStopService, SafetyStopResult, stop_resources_payload

type AuditControlClock = Callable[[], datetime]


class AuditRunStateProjector:
    """Translate validated control plans into one atomic persistence UoW."""

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
                "The Code Audit control plan has no lifecycle target",
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
        request = AuditControlTransition(
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
            workflow_signal_kind=_workflow_signal_kind(plan),
        )
        await self._invoke(lambda: self._unit_of_work.transition(request))

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
        request = AuditCleanupConvergence(
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
        await self._invoke(lambda: self._unit_of_work.converge_cleanup(request))

    async def _invoke(self, operation: Callable[[], Awaitable[object]]) -> None:
        try:
            await operation()
        except RepositoryConflictError:
            raise ApplicationConflictError(
                "audit_control_conflict",
                "The Code Audit changed while applying the control request",
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
    """Apply dedicated Audit pause/resume/cancel and owner-process cleanup."""

    def __init__(
        self,
        *,
        audits: AuditApplicationService,
        projector: AuditRunStateProjector,
        safety_stopper: RunSafetyStopService,
        events: RunEventRepository,
    ) -> None:
        self._audits = audits
        self._projector = projector
        self._safety_stopper = safety_stopper
        self._events = events

    async def pause(
        self,
        audit_id: str,
        *,
        principal: LocalPrincipal,
        authorizer: AuditObjectAuthorizer,
    ) -> AuditAggregate:
        authorized = await self._audits.get_authorized(
            audit_id,
            principal=principal,
            authorizer=authorizer,
            capability=OperatorCapability.CONTROL,
        )
        _require_audit_control_effect(
            authorized,
            operation="service.audit.pause",
            origin="application_service",
            effect="workflow_control",
            mode="normal",
        )
        aggregate, _ = await self._pause(audit_id, initial=authorized)
        return aggregate

    async def resume(
        self,
        audit_id: str,
        *,
        principal: LocalPrincipal,
        authorizer: AuditObjectAuthorizer,
    ) -> AuditAggregate:
        authorized = await self._audits.get_authorized(
            audit_id,
            principal=principal,
            authorizer=authorizer,
            capability=OperatorCapability.CONTROL,
        )
        _require_audit_control_effect(
            authorized,
            operation="service.audit.resume",
            origin="application_service",
            effect="workflow_control",
            mode="normal",
        )
        plan = self._audits.plan_resume(authorized)
        await self._projector.transition(plan)
        return await self._audits.get(audit_id)

    async def cancel(
        self,
        audit_id: str,
        *,
        principal: LocalPrincipal,
        authorizer: AuditObjectAuthorizer,
    ) -> AuditAggregate:
        authorized = await self._audits.get_authorized(
            audit_id,
            principal=principal,
            authorizer=authorizer,
            capability=OperatorCapability.HOST_CONTROL,
        )
        _require_audit_control_effect(
            authorized,
            operation="service.audit.cancel",
            origin="application_service",
            effect="host_control",
            mode="normal",
        )
        aggregate, _ = await self._cancel(audit_id, initial=authorized)
        return aggregate

    async def reconcile_run(self, run_id: str) -> SafetyStopResult:
        aggregates = list(await self._audits.list(run_id=run_id, limit=2, offset=0))
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
        _require_audit_control_effect(
            aggregate,
            operation="service.audit.reconcile",
            origin="safety_reconciler",
            effect="host_control",
            mode="reconcile",
        )
        if aggregate.run.status is RunStatus.PAUSING:
            _, result = await self._pause(aggregate.audit.value.id)
            return result
        if aggregate.run.status is RunStatus.CANCELLING:
            _, result = await self._cancel(aggregate.audit.value.id)
            return result
        if aggregate.run.status is RunStatus.COMPLETING:
            return await self._converge_non_cancel_cleanup(aggregate)
        raise ApplicationConflictError(
            "audit_cleanup_not_fenced",
            "The Code Audit Run is not in a cleanup fence",
        )

    async def _pause(
        self,
        audit_id: str,
        *,
        initial: AuditAggregate | None = None,
    ) -> tuple[AuditAggregate, SafetyStopResult]:
        plan = (
            self._audits.plan_pause(initial)
            if initial is not None
            else await self._audits.pause(audit_id)
        )
        if plan.disposition is AuditControlDisposition.ALREADY_SATISFIED:
            return await self._audits.get(audit_id), _empty_stop_result()

        if plan.current_audit_lifecycle is not AuditLifecycleStatus.PAUSING:
            await self._projector.transition(plan)
            plan = await self._audits.pause(audit_id)
        result = await self._safety_stopper.stop_run(plan.run_id, drain=True)
        await self._raise_if_stop_failed(plan, result)
        reconciled = await self._audits.pause(audit_id)
        if reconciled.disposition is not AuditControlDisposition.ALREADY_SATISFIED:
            await self._projector.transition(reconciled)
        return await self._audits.get(audit_id), result

    async def _cancel(
        self,
        audit_id: str,
        *,
        initial: AuditAggregate | None = None,
    ) -> tuple[AuditAggregate, SafetyStopResult]:
        plan = (
            self._audits.plan_cancel(initial)
            if initial is not None
            else await self._audits.cancel(audit_id)
        )
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
            return await self._audits.get(audit_id), result

        if (
            plan.target_audit_lifecycle is AuditLifecycleStatus.CANCELLING
            and plan.current_audit_lifecycle is not AuditLifecycleStatus.CANCELLING
        ):
            await self._projector.transition(plan)
            plan = await self._audits.cancel(audit_id)

        result = await self._safety_stopper.stop_run(plan.run_id, drain=True)
        await self._raise_if_stop_failed(plan, result)
        current = await self._audits.get(audit_id)
        if current.audit.value.cleanup_proof_digest is None:
            await self._projector.converge_cleanup(
                current,
                cleanup_proof_digest=_cleanup_proof_digest(current, result),
                operation=AuditControlAction.CANCEL.value,
                reason_code="audit_cancel_cleanup_converged",
            )
        return await self._audits.get(audit_id), result

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
        synthetic_plan = AuditControlPlan(
            operation=AuditControlAction.CANCEL,
            disposition=AuditControlDisposition.RECONCILE,
            required_effect=AuditControlEffect.RECONCILE_CANCEL_STOP,
            audit_id=scan.id,
            run_id=aggregate.run.id,
            expected_audit_state_version=aggregate.audit.state_version,
            current_audit_lifecycle=scan.lifecycle_status,
            current_run_status=aggregate.run.status,
            reason_code="audit_cleanup_reconciliation_required",
        )
        await self._raise_if_stop_failed(synthetic_plan, result)
        await self._projector.converge_cleanup(
            aggregate,
            cleanup_proof_digest=_cleanup_proof_digest(aggregate, result),
            operation="cleanup",
            reason_code="audit_cleanup_converged",
        )
        return result

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


def _require_audit_control_effect(
    aggregate: AuditAggregate,
    *,
    operation: str,
    origin: str,
    effect: str,
    mode: str,
) -> None:
    """Apply the owner-aware catalog before any Audit control side effect."""

    from riftx.application.run_kind_effects import (
        RunEffectOwnership,
        RunKindEffectPolicyDenied,
        require_run_kind_effect_policy,
    )

    scan = aggregate.audit.value
    try:
        ownership = RunEffectOwnership(
            run_id=aggregate.run.id,
            run_kind=aggregate.run.kind,
            audit_id=(scan.id if aggregate.run.kind is RunKind.CODE_AUDIT else None),
        )
        require_run_kind_effect_policy(
            operation,
            origin,
            ownership=ownership,
            effect=effect,
            mode=mode,
        )
    except (RunKindEffectPolicyDenied, TypeError, ValueError):
        raise ApplicationConflictError(
            "run_kind_effect_policy_denied",
            "The requested Code Audit control is not admitted for this owner",
        ) from None


def _workflow_signal_kind(plan: AuditControlPlan) -> WorkflowSignalKind | None:
    """Stage a signal only with the first CAS that fences its control action."""

    return {
        AuditControlEffect.PAUSE_WORKFLOW_THEN_PROJECT: WorkflowSignalKind.PAUSE,
        AuditControlEffect.RESUME_WORKFLOW_THEN_PROJECT: WorkflowSignalKind.RESUME,
        AuditControlEffect.FENCE_NEW_EFFECTS_AND_STOP: WorkflowSignalKind.CANCEL,
    }.get(plan.required_effect)


def _cleanup_proof_digest(
    aggregate: AuditAggregate,
    result: SafetyStopResult,
) -> str:
    payload = {
        "schema_version": "riftx.audit-cleanup-proof/v1",
        "audit_id": aggregate.audit.value.id,
        "run_id": aggregate.run.id,
        "stop_resources": stop_resources_payload(result),
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _event_id(domain: str, *parts: str) -> str:
    canonical = "\x00".join((f"riftx.{domain}/v1", *parts)).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _empty_stop_result() -> SafetyStopResult:
    return SafetyStopResult(resources={})


__all__ = ["AuditControlApplicationService", "AuditRunStateProjector"]
