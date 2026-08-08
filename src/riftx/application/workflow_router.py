"""RunKind-aware Workflow protocol routing."""

from __future__ import annotations

from typing import Protocol

from riftx.application.errors import (
    ApplicationConflictError,
    EntityNotFoundError,
)
from riftx.application.ports import RunRepository
from riftx.domain import Run, RunKind


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


class RunWorkflowControlRouter:
    """Route General and Pentest controls through their persisted Workflow ID."""

    def __init__(
        self,
        *,
        runs: RunRepository,
        general: GeneralRunWorkflowClient,
    ) -> None:
        self._runs = runs
        self._general = general

    async def start_run(
        self,
        run_id: str,
        *,
        workflow_id: str | None = None,
    ) -> object:
        run = await self._require_interactive(
            run_id,
            operation="workflow.start_run",
            effect="host_execution",
        )
        exact_workflow_id = _require_exact_interactive_workflow_id(run, workflow_id)
        return await self._general.start_run(
            run_id,
            workflow_id=exact_workflow_id,
        )

    async def pause(self, run_id: str, *, workflow_id: str | None = None) -> None:
        run = await self._require_interactive(
            run_id,
            operation="service.run.pause",
            effect="workflow_control",
        )
        await self._general.pause(
            run_id,
            workflow_id=_require_exact_interactive_workflow_id(run, workflow_id),
        )

    async def resume(self, run_id: str, *, workflow_id: str | None = None) -> None:
        run = await self._require_interactive(
            run_id,
            operation="service.run.resume",
            effect="workflow_control",
        )
        await self._general.resume(
            run_id,
            workflow_id=_require_exact_interactive_workflow_id(run, workflow_id),
        )

    async def approve(
        self,
        run_id: str,
        approval_id: str,
        *,
        workflow_id: str | None = None,
    ) -> None:
        run = await self._require_interactive(
            run_id,
            operation="service.approval.approve",
            effect="workflow_control",
            resource_kind="approval",
            resource_id=approval_id,
        )
        await self._general.approve(
            run_id,
            approval_id,
            workflow_id=_require_exact_interactive_workflow_id(run, workflow_id),
        )

    async def reject(
        self,
        run_id: str,
        approval_id: str,
        *,
        workflow_id: str | None = None,
    ) -> None:
        run = await self._require_interactive(
            run_id,
            operation="service.approval.reject",
            effect="workflow_control",
            resource_kind="approval",
            resource_id=approval_id,
        )
        await self._general.reject(
            run_id,
            approval_id,
            workflow_id=_require_exact_interactive_workflow_id(run, workflow_id),
        )

    async def execution_completed(
        self,
        run_id: str,
        execution_id: str,
        *,
        workflow_id: str | None = None,
    ) -> None:
        """Compatibility entrypoint for a proven interactive completion only."""

        run = await self._require_interactive(
            run_id,
            operation="workflow.execution_completion",
            effect="workflow_control",
            execution_id=execution_id,
        )
        await self._general.execution_completed(
            run_id,
            execution_id,
            workflow_id=_require_exact_interactive_workflow_id(run, workflow_id),
        )

    async def cancel_current_execution(
        self,
        run_id: str,
        *,
        workflow_id: str | None = None,
    ) -> None:
        run = await self._require_interactive(
            run_id,
            operation="service.run.cancel_current_execution",
            effect="workflow_control",
        )
        await self._general.cancel_current_execution(
            run_id,
            workflow_id=_require_exact_interactive_workflow_id(run, workflow_id),
        )

    async def cancel(self, run_id: str, *, workflow_id: str | None = None) -> None:
        run = await self._require_interactive(
            run_id,
            operation="service.run.cancel",
            effect="workflow_control",
        )
        await self._general.cancel(
            run_id,
            workflow_id=_require_exact_interactive_workflow_id(run, workflow_id),
        )

    async def compact(
        self,
        run_id: str,
        max_history_items: int = 100,
        *,
        workflow_id: str | None = None,
    ) -> None:
        run = await self._require_interactive(
            run_id,
            operation="service.run.compact",
            effect="workflow_control",
        )
        await self._general.compact(
            run_id,
            max_history_items,
            workflow_id=_require_exact_interactive_workflow_id(run, workflow_id),
        )

    async def switch_model(
        self,
        run_id: str,
        model_profile: str,
        *,
        workflow_id: str | None = None,
    ) -> None:
        run = await self._require_interactive(
            run_id,
            operation="service.run.switch_model",
            effect="workflow_control",
        )
        await self._general.switch_model(
            run_id,
            model_profile,
            workflow_id=_require_exact_interactive_workflow_id(run, workflow_id),
        )

    async def append_user_message(
        self,
        run_id: str,
        message_id: str,
        *,
        workflow_id: str | None = None,
    ) -> None:
        run = await self._require_interactive(
            run_id,
            operation="service.run.append_message",
            effect="workflow_control",
        )
        await self._general.append_user_message(
            run_id,
            message_id,
            workflow_id=_require_exact_interactive_workflow_id(run, workflow_id),
        )

    def workflow_id(self, run_id: str) -> str:
        """Preserve the existing General Workflow ID byte-for-byte.

        Run creation calls this before persistence, so no kind lookup is
        possible here. Audit IDs are created by the Audit aggregate factory and
        never pass through this compatibility method.
        """

        return self._general.workflow_id(run_id)

    async def _require_interactive(
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

def _require_exact_interactive_workflow_id(
    run: Run,
    requested_workflow_id: str | None,
) -> str:
    """Resolve only the Workflow identity persisted on an interactive Run.

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
    "GeneralRunWorkflowClient",
    "RunWorkflowControlRouter",
]
