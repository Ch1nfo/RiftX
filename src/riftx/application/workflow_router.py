"""RunKind-aware Workflow protocol routing."""

from __future__ import annotations

from enum import StrEnum
from typing import Protocol

from riftx.application.errors import (
    ApplicationConflictError,
    EntityNotFoundError,
    ServiceUnavailableError,
)
from riftx.application.ports import AuditAggregate, AuditAggregateReadRepository, RunRepository
from riftx.domain import Execution, Run, RunKind


class GeneralRunWorkflowClient(Protocol):
    async def start_run(
        self,
        run_id: str,
        *,
        workflow_id: str | None = None,
    ) -> object: ...

    async def pause(self, run_id: str, *, workflow_id: str | None = None) -> None: ...

    async def resume(self, run_id: str, *, workflow_id: str | None = None) -> None: ...

    async def approve(
        self,
        run_id: str,
        approval_id: str,
        *,
        workflow_id: str | None = None,
    ) -> None: ...

    async def reject(
        self,
        run_id: str,
        approval_id: str,
        *,
        workflow_id: str | None = None,
    ) -> None: ...

    async def execution_completed(
        self,
        run_id: str,
        execution_id: str,
        *,
        workflow_id: str | None = None,
    ) -> None: ...

    async def cancel_current_execution(
        self,
        run_id: str,
        *,
        workflow_id: str | None = None,
    ) -> None: ...

    async def cancel(self, run_id: str, *, workflow_id: str | None = None) -> None: ...

    async def compact(
        self,
        run_id: str,
        max_history_items: int = 100,
        *,
        workflow_id: str | None = None,
    ) -> None: ...

    async def switch_model(
        self,
        run_id: str,
        model_profile: str,
        *,
        workflow_id: str | None = None,
    ) -> None: ...

    async def append_user_message(
        self,
        run_id: str,
        message_id: str,
        *,
        workflow_id: str | None = None,
    ) -> None: ...

    def workflow_id(self, run_id: str) -> str: ...


class AuditWorkflowClient(Protocol):
    """Dedicated Code Audit Workflow protocol, introduced before its runtime.

    Every operation receives the already-persisted Workflow ID.  Implementors
    must address that exact ID and must never construct a general
    ``riftx-run-{run_id}`` fallback.
    """

    async def pause(
        self,
        *,
        workflow_id: str,
        audit_id: str,
        signal_identity_digest: str,
    ) -> None: ...

    async def resume(
        self,
        *,
        workflow_id: str,
        audit_id: str,
        signal_identity_digest: str,
    ) -> None: ...

    async def cancel(
        self,
        *,
        workflow_id: str,
        audit_id: str,
        signal_identity_digest: str,
    ) -> None: ...

    async def execution_completed(
        self,
        *,
        workflow_id: str,
        audit_id: str,
        execution_id: str,
        plan_digest: str,
    ) -> None: ...


class AuditExecutionPlanVerifier(Protocol):
    async def require_execution_plan(
        self,
        *,
        audit_id: str,
        run_id: str,
        execution_id: str,
        plan_digest: str,
    ) -> None: ...


class WorkflowDispatchDisposition(StrEnum):
    DISPATCHED = "dispatched"
    NOT_STARTED = "not_started"


class RunWorkflowControlRouter:
    """Select one Workflow protocol from authoritative Run/Audit ownership.

    This class intentionally performs no authorization, state projection,
    resource stop, or owner inference. Generic methods accept only General Runs;
    dedicated Audit methods resolve the exact Audit aggregate and call only the
    Audit protocol.  M1 has no Audit Workflow client or execution-plan verifier,
    so those paths fail closed without touching the General client.
    """

    def __init__(
        self,
        *,
        runs: RunRepository,
        audits: AuditAggregateReadRepository,
        general: GeneralRunWorkflowClient,
        audit: AuditWorkflowClient | None = None,
        audit_execution_plans: AuditExecutionPlanVerifier | None = None,
    ) -> None:
        self._runs = runs
        self._audits = audits
        self._general = general
        self._audit = audit
        self._audit_execution_plans = audit_execution_plans

    async def start_run(
        self,
        run_id: str,
        *,
        workflow_id: str | None = None,
    ) -> object:
        run = await self._require_general(
            run_id,
            operation="workflow.start_run",
            effect="host_execution",
        )
        exact_workflow_id = _require_exact_general_workflow_id(run, workflow_id)
        return await self._general.start_run(
            run_id,
            workflow_id=exact_workflow_id,
        )

    async def pause(self, run_id: str, *, workflow_id: str | None = None) -> None:
        run = await self._require_general(
            run_id,
            operation="service.run.pause",
            effect="workflow_control",
        )
        await self._general.pause(
            run_id,
            workflow_id=_require_exact_general_workflow_id(run, workflow_id),
        )

    async def resume(self, run_id: str, *, workflow_id: str | None = None) -> None:
        run = await self._require_general(
            run_id,
            operation="service.run.resume",
            effect="workflow_control",
        )
        await self._general.resume(
            run_id,
            workflow_id=_require_exact_general_workflow_id(run, workflow_id),
        )

    async def approve(
        self,
        run_id: str,
        approval_id: str,
        *,
        workflow_id: str | None = None,
    ) -> None:
        run = await self._require_general(
            run_id,
            operation="service.approval.approve",
            effect="workflow_control",
            resource_kind="approval",
            resource_id=approval_id,
        )
        await self._general.approve(
            run_id,
            approval_id,
            workflow_id=_require_exact_general_workflow_id(run, workflow_id),
        )

    async def reject(
        self,
        run_id: str,
        approval_id: str,
        *,
        workflow_id: str | None = None,
    ) -> None:
        run = await self._require_general(
            run_id,
            operation="service.approval.reject",
            effect="workflow_control",
            resource_kind="approval",
            resource_id=approval_id,
        )
        await self._general.reject(
            run_id,
            approval_id,
            workflow_id=_require_exact_general_workflow_id(run, workflow_id),
        )

    async def execution_completed(
        self,
        run_id: str,
        execution_id: str,
        *,
        workflow_id: str | None = None,
    ) -> None:
        """Compatibility entrypoint for a proven General completion only."""

        run = await self._require_general(
            run_id,
            operation="workflow.execution_completion",
            effect="workflow_control",
            execution_id=execution_id,
        )
        await self._general.execution_completed(
            run_id,
            execution_id,
            workflow_id=_require_exact_general_workflow_id(run, workflow_id),
        )

    async def cancel_current_execution(
        self,
        run_id: str,
        *,
        workflow_id: str | None = None,
    ) -> None:
        run = await self._require_general(
            run_id,
            operation="service.run.cancel_current_execution",
            effect="workflow_control",
        )
        await self._general.cancel_current_execution(
            run_id,
            workflow_id=_require_exact_general_workflow_id(run, workflow_id),
        )

    async def cancel(self, run_id: str, *, workflow_id: str | None = None) -> None:
        run = await self._require_general(
            run_id,
            operation="service.run.cancel",
            effect="workflow_control",
        )
        await self._general.cancel(
            run_id,
            workflow_id=_require_exact_general_workflow_id(run, workflow_id),
        )

    async def compact(
        self,
        run_id: str,
        max_history_items: int = 100,
        *,
        workflow_id: str | None = None,
    ) -> None:
        run = await self._require_general(
            run_id,
            operation="service.run.compact",
            effect="workflow_control",
        )
        await self._general.compact(
            run_id,
            max_history_items,
            workflow_id=_require_exact_general_workflow_id(run, workflow_id),
        )

    async def switch_model(
        self,
        run_id: str,
        model_profile: str,
        *,
        workflow_id: str | None = None,
    ) -> None:
        run = await self._require_general(
            run_id,
            operation="service.run.switch_model",
            effect="workflow_control",
        )
        await self._general.switch_model(
            run_id,
            model_profile,
            workflow_id=_require_exact_general_workflow_id(run, workflow_id),
        )

    async def append_user_message(
        self,
        run_id: str,
        message_id: str,
        *,
        workflow_id: str | None = None,
    ) -> None:
        run = await self._require_general(
            run_id,
            operation="service.run.append_message",
            effect="workflow_control",
        )
        await self._general.append_user_message(
            run_id,
            message_id,
            workflow_id=_require_exact_general_workflow_id(run, workflow_id),
        )

    def workflow_id(self, run_id: str) -> str:
        """Preserve the existing General Workflow ID byte-for-byte.

        Run creation calls this before persistence, so no kind lookup is
        possible here. Audit IDs are created by the Audit aggregate factory and
        never pass through this compatibility method.
        """

        return self._general.workflow_id(run_id)

    async def pause_audit(
        self,
        *,
        audit_id: str,
        run_id: str,
        signal_identity_digest: str,
    ) -> WorkflowDispatchDisposition:
        aggregate = await self._require_audit_owner(
            audit_id=audit_id,
            run_id=run_id,
            operation="service.audit.pause",
            effect="workflow_control",
        )
        if aggregate.audit.value.started_at is None:
            return WorkflowDispatchDisposition.NOT_STARTED
        client = self._require_audit_client()
        await client.pause(
            workflow_id=aggregate.audit.value.temporal_workflow_id,
            audit_id=audit_id,
            signal_identity_digest=_require_signal_identity_digest(
                signal_identity_digest
            ),
        )
        return WorkflowDispatchDisposition.DISPATCHED

    async def resume_audit(
        self,
        *,
        audit_id: str,
        run_id: str,
        signal_identity_digest: str,
    ) -> WorkflowDispatchDisposition:
        aggregate = await self._require_audit_owner(
            audit_id=audit_id,
            run_id=run_id,
            operation="service.audit.resume",
            effect="workflow_control",
        )
        client = self._require_audit_client()
        await client.resume(
            workflow_id=aggregate.audit.value.temporal_workflow_id,
            audit_id=audit_id,
            signal_identity_digest=_require_signal_identity_digest(
                signal_identity_digest
            ),
        )
        return WorkflowDispatchDisposition.DISPATCHED

    async def cancel_audit(
        self,
        *,
        audit_id: str,
        run_id: str,
        signal_identity_digest: str,
    ) -> WorkflowDispatchDisposition:
        aggregate = await self._require_audit_owner(
            audit_id=audit_id,
            run_id=run_id,
            operation="service.audit.cancel",
            effect="host_control",
        )
        if aggregate.audit.value.started_at is None:
            return WorkflowDispatchDisposition.NOT_STARTED
        client = self._require_audit_client()
        await client.cancel(
            workflow_id=aggregate.audit.value.temporal_workflow_id,
            audit_id=audit_id,
            signal_identity_digest=_require_signal_identity_digest(
                signal_identity_digest
            ),
        )
        return WorkflowDispatchDisposition.DISPATCHED

    async def execution_completed_owned(self, execution: Execution) -> None:
        """Route one immutable Execution completion without guessing ownership."""

        run = await self._runs.get(execution.run_id)
        if run is None:
            raise EntityNotFoundError("Run", execution.run_id)
        if run.kind is RunKind.GENERAL:
            _require_routed_effect(
                operation="workflow.execution_completion",
                run_id=run.id,
                run_kind=run.kind,
                effect="workflow_control",
                execution_id=execution.id,
            )
            if execution.audit_id is not None or execution.plan_digest is not None:
                raise ApplicationConflictError(
                    "execution_ownership_invalid",
                    "General execution completion carried Code Audit ownership",
                )
            await self._general.execution_completed(
                run.id,
                execution.id,
                workflow_id=_require_exact_general_workflow_id(run, None),
            )
            return

        if run.kind is not RunKind.CODE_AUDIT:
            raise ApplicationConflictError(
                "run_kind_operation_unsupported",
                "The requested operation is not supported for this Run kind",
            )
        if execution.audit_id is None or execution.plan_digest is None:
            raise ApplicationConflictError(
                "audit_execution_ownership_unverified",
                "Code Audit execution completion has no verified plan ownership",
            )
        aggregate = await self._require_audit_owner(
            audit_id=execution.audit_id,
            run_id=execution.run_id,
            operation="workflow.execution_completion",
            effect="workflow_control",
            execution_id=execution.id,
            plan_digest=execution.plan_digest,
        )
        if self._audit_execution_plans is None:
            # AUD-702 owns the immutable plan authority. A Contract or policy
            # digest is deliberately not accepted as a substitute in M1.
            raise ApplicationConflictError(
                "audit_execution_plan_unavailable",
                "Code Audit execution plans are not available in this release stage",
            )
        await self._audit_execution_plans.require_execution_plan(
            audit_id=execution.audit_id,
            run_id=execution.run_id,
            execution_id=execution.id,
            plan_digest=execution.plan_digest,
        )
        client = self._require_audit_client()
        await client.execution_completed(
            workflow_id=aggregate.audit.value.temporal_workflow_id,
            audit_id=execution.audit_id,
            execution_id=execution.id,
            plan_digest=execution.plan_digest,
        )

    async def _require_general(
        self,
        run_id: str,
        *,
        operation: str,
        effect: str,
        execution_id: str | None = None,
        resource_kind: str | None = None,
        resource_id: str | None = None,
    ) -> Run:
        run = await self._runs.get(run_id)
        if run is None:
            raise EntityNotFoundError("Run", run_id)
        _require_routed_effect(
            operation=operation,
            run_id=run_id,
            run_kind=run.kind,
            effect=effect,
            execution_id=execution_id,
            resource_kind=resource_kind,
            resource_id=resource_id,
        )
        return run

    async def _require_audit_owner(
        self,
        *,
        audit_id: str,
        run_id: str,
        operation: str,
        effect: str,
        execution_id: str | None = None,
        plan_digest: str | None = None,
    ) -> AuditAggregate:
        aggregate = await self._audits.get(audit_id)
        if aggregate is None:
            raise EntityNotFoundError("Audit", audit_id)
        scan = aggregate.audit.value
        expected_workflow_id = f"riftx-code-audit-{scan.id}"
        if (
            aggregate.run.kind is not RunKind.CODE_AUDIT
            or scan.run_id != run_id
            or aggregate.run.id != run_id
            or scan.temporal_workflow_id != expected_workflow_id
            or aggregate.run.temporal_workflow_id != expected_workflow_id
        ):
            raise ApplicationConflictError(
                "audit_workflow_owner_conflict",
                "The Code Audit Workflow owner binding is inconsistent",
            )
        _require_routed_effect(
            operation=operation,
            run_id=run_id,
            run_kind=aggregate.run.kind,
            effect=effect,
            audit_id=scan.id,
            plan_digest=plan_digest,
            execution_id=execution_id,
        )
        return aggregate

    def _require_audit_client(self) -> AuditWorkflowClient:
        if self._audit is None:
            raise ServiceUnavailableError(
                "audit_workflow_unavailable",
                "The dedicated Code Audit Workflow runtime is not available",
            )
        return self._audit


def _require_exact_general_workflow_id(
    run: Run,
    requested_workflow_id: str | None,
) -> str:
    """Resolve only the Workflow identity already persisted on the General Run.

    Legacy rows with no identity remain readable and safety-stoppable, but a
    control signal must fail closed: deriving from today's prefix could target
    a different Workflow than the one that historically owned the Run.
    """

    persisted_workflow_id = run.temporal_workflow_id
    if not persisted_workflow_id:
        raise ApplicationConflictError(
            "workflow_identity_missing",
            "The Run has no authoritative Workflow identity",
            details={"run_id": run.id},
        )
    if (
        requested_workflow_id is not None
        and requested_workflow_id != persisted_workflow_id
    ):
        raise ApplicationConflictError(
            "workflow_identity_mismatch",
            "The requested Workflow identity does not match the Run owner",
            details={"run_id": run.id},
        )
    return persisted_workflow_id


def _require_signal_identity_digest(value: str) -> str:
    """Keep Audit control signals individually probeable after ambiguous sends."""

    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ApplicationConflictError(
            "workflow_signal_identity_invalid",
            "The Code Audit control signal has no valid durable identity",
        )
    return value


def _require_routed_effect(
    *,
    operation: str,
    run_id: str,
    run_kind: RunKind,
    effect: str,
    audit_id: str | None = None,
    plan_digest: str | None = None,
    execution_id: str | None = None,
    resource_kind: str | None = None,
    resource_id: str | None = None,
) -> None:
    """Apply the catalog after bounded owner resolution and before dispatch."""

    from riftx.application.run_kind_effects import (
        EffectMode,
        EffectOrigin,
        PolicyDenialReason,
        RunEffectOwnership,
        RunKindEffectPolicyDenied,
        require_run_kind_effect_policy,
    )

    try:
        ownership = RunEffectOwnership(
            run_id=run_id,
            run_kind=run_kind,
            audit_id=audit_id,
            plan_digest=plan_digest,
            execution_id=execution_id,
            resource_kind=resource_kind,
            resource_id=resource_id,
        )
        require_run_kind_effect_policy(
            operation,
            EffectOrigin.APPLICATION_SERVICE,
            ownership=ownership,
            effect=effect,
            mode=EffectMode.NORMAL,
        )
    except RunKindEffectPolicyDenied as exc:
        code = (
            "run_kind_operation_unsupported"
            if exc.reason is PolicyDenialReason.RUN_KIND_UNSUPPORTED
            else "run_kind_effect_policy_denied"
        )
        raise ApplicationConflictError(
            code,
            "The requested Workflow effect is not admitted for this owner",
        ) from None
    except (TypeError, ValueError):
        raise ApplicationConflictError(
            "run_kind_effect_policy_denied",
            "The requested Workflow effect is not admitted for this owner",
        ) from None


__all__ = [
    "AuditExecutionPlanVerifier",
    "AuditWorkflowClient",
    "GeneralRunWorkflowClient",
    "RunWorkflowControlRouter",
    "WorkflowDispatchDisposition",
]
