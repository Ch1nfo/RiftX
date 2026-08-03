"""Atomic Audit control and cleanup state projection."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from riftx.application.errors import (
    EntityNotFoundError,
    RepositoryConflictError,
    RepositoryError,
    RepositoryIntegrityError,
    RepositoryUnavailableError,
)
from riftx.application.ports import (
    AuditCleanupConvergence,
    AuditControlProjection,
    AuditControlTransition,
    StoredAuditEntity,
)
from riftx.domain import (
    AuditLifecycleStatus,
    AuditRunStateMappingPolicy,
    AuditScan,
    AuditTerminalOutcome,
    InvalidStateTransitionError,
    RunEvent,
    RunKind,
    RunStatus,
    WorkflowSignalIntent,
    WorkflowSignalSourceKind,
)

from .audit_repositories import compare_and_set_audit_scan, load_validated_audit_scan
from .mappers import apply_run_to_record, event_from_record, event_to_record, run_from_record
from .orm import (
    AuditContractRecord as AuditContractORMRecord,
)
from .orm import (
    AuditProjectRecord,
    AuditScanRecord,
    RunEventRecord,
    RunRecord,
)
from .transactions import SessionFactory, serialized_write


class SQLAlchemyAuditControlUnitOfWork:
    """Project an Audit Scan, its Run, and control events in one transaction."""

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    async def transition(
        self,
        request: AuditControlTransition,
    ) -> AuditControlProjection:
        try:
            async with serialized_write(self._session_factory) as session:
                bundle = await _load_locked_owner(session, request.audit_id, request.run_id)
                scan_record, _, _, run_record, scan = bundle
                run = run_from_record(run_record)
                stored = StoredAuditEntity(scan, scan_record.state_version)

                if (
                    scan.lifecycle_status is request.target_audit_lifecycle
                    and run.status is request.target_run_status
                ):
                    return AuditControlProjection(audit=stored, run=run, changed=False)

                if (
                    stored.state_version != request.expected_audit_state_version
                    or scan.lifecycle_status is not request.expected_audit_lifecycle
                    or run.status is not request.expected_run_status
                ):
                    raise RepositoryConflictError(
                        "Code Audit control state changed before projection"
                    )

                try:
                    replacement_scan = scan.transition_to(
                        request.target_audit_lifecycle,
                        at=request.occurred_at,
                    )
                    previous_run_status = run.status
                    run.transition_to(request.target_run_status, at=request.occurred_at)
                except (InvalidStateTransitionError, TypeError, ValueError):
                    raise RepositoryConflictError(
                        "Code Audit control transition is no longer admissible"
                    ) from None

                if (
                    AuditRunStateMappingPolicy.expected_run_status(replacement_scan)
                    is not run.status
                ):
                    raise RepositoryConflictError(
                        "Code Audit control target does not match its Run projection"
                    )

                projected, changed = await compare_and_set_audit_scan(
                    session,
                    stored,
                    replacement_scan,
                )
                if not changed:
                    raise RepositoryConflictError(
                        "Code Audit control projection did not acquire its CAS token"
                    )
                apply_run_to_record(run, run_record)
                await _append_event(
                    session,
                    RunEvent(
                        id=request.run_event_id,
                        run_id=run.id,
                        sequence=await _next_event_sequence(session, run.id),
                        event_type="run.status_changed",
                        payload={
                            "from": previous_run_status.value,
                            "to": run.status.value,
                        },
                        created_at=request.occurred_at,
                    ),
                )
                await _append_event(
                    session,
                    RunEvent(
                        id=request.audit_event_id,
                        run_id=run.id,
                        sequence=await _next_event_sequence(session, run.id),
                        event_type="audit.control_projected",
                        payload={
                            "audit_id": scan.id,
                            "operation": request.operation,
                            "reason_code": request.reason_code,
                            "from_audit_lifecycle": scan.lifecycle_status.value,
                            "to_audit_lifecycle": replacement_scan.lifecycle_status.value,
                            "from_run_status": previous_run_status.value,
                            "to_run_status": run.status.value,
                            "audit_state_version": projected.state_version,
                        },
                        created_at=request.occurred_at,
                    ),
                )
                if (
                    request.workflow_signal_kind is not None
                    and replacement_scan.started_at is not None
                ):
                    await _stage_control_signal(
                        session,
                        session_factory=self._session_factory,
                        request=request,
                        scan=replacement_scan,
                        projected_state_version=projected.state_version,
                    )
                return AuditControlProjection(audit=projected, run=run, changed=True)
        except RepositoryError:
            raise
        except SQLAlchemyError:
            raise RepositoryUnavailableError(
                "Code Audit control projection failed"
            ) from None

    async def converge_cleanup(
        self,
        request: AuditCleanupConvergence,
    ) -> AuditControlProjection:
        try:
            async with serialized_write(self._session_factory) as session:
                bundle = await _load_locked_owner(session, request.audit_id, request.run_id)
                scan_record, _, _, run_record, scan = bundle
                run = run_from_record(run_record)
                stored = StoredAuditEntity(scan, scan_record.state_version)

                if scan.cleanup_proof_digest is not None:
                    expected_terminal = _terminal_run_status(scan.terminal_outcome)
                    if (
                        scan.cleanup_proof_digest != request.cleanup_proof_digest
                        or scan.run_terminal_status is not expected_terminal
                        or run.status is not expected_terminal
                    ):
                        raise RepositoryConflictError(
                            "Code Audit cleanup proof is already bound to different facts"
                        )
                    return AuditControlProjection(audit=stored, run=run, changed=False)

                if (
                    stored.state_version != request.expected_audit_state_version
                    or scan.lifecycle_status is not request.expected_audit_lifecycle
                    or run.status is not request.expected_run_status
                ):
                    raise RepositoryConflictError(
                        "Code Audit cleanup state changed before convergence"
                    )

                if scan.lifecycle_status is not AuditLifecycleStatus.CLEANING:
                    if scan.lifecycle_status not in {
                        AuditLifecycleStatus.FINALIZING,
                        AuditLifecycleStatus.CANCELLING,
                        AuditLifecycleStatus.FAILING,
                    }:
                        raise RepositoryConflictError(
                            "Code Audit cleanup convergence has no lifecycle fence"
                        )
                    try:
                        cleaning = scan.transition_to(
                            AuditLifecycleStatus.CLEANING,
                            at=request.occurred_at,
                        )
                    except (InvalidStateTransitionError, TypeError, ValueError):
                        raise RepositoryConflictError(
                            "Code Audit cannot enter cleanup from its current lifecycle"
                        ) from None
                    stored, changed = await compare_and_set_audit_scan(
                        session,
                        stored,
                        cleaning,
                    )
                    if not changed:
                        raise RepositoryConflictError(
                            "Code Audit cleanup transition lost its CAS token"
                        )
                    scan = cleaning

                terminal_status = _terminal_run_status(scan.terminal_outcome)
                previous_run_status = run.status
                try:
                    run.transition_to(terminal_status, at=request.occurred_at)
                    converged = scan.record_cleanup_convergence(
                        cleanup_proof_digest=request.cleanup_proof_digest,
                        run_terminal_status=terminal_status,
                    )
                except (InvalidStateTransitionError, TypeError, ValueError):
                    raise RepositoryConflictError(
                        "Code Audit cleanup facts cannot converge from their current state"
                    ) from None
                if AuditRunStateMappingPolicy.expected_run_status(converged) is not terminal_status:
                    raise RepositoryConflictError(
                        "Code Audit cleanup terminal target is inconsistent"
                    )

                # The restricted Scan CAS explicitly requires the caller-owned
                # transaction to lock and terminalize the Run before recording
                # the immutable convergence proof.
                apply_run_to_record(run, run_record)
                projected, changed = await compare_and_set_audit_scan(
                    session,
                    stored,
                    converged,
                    allow_run_convergence=True,
                )
                if not changed:
                    raise RepositoryConflictError(
                        "Code Audit cleanup proof did not acquire its CAS token"
                    )
                await _append_event(
                    session,
                    RunEvent(
                        id=request.run_event_id,
                        run_id=run.id,
                        sequence=await _next_event_sequence(session, run.id),
                        event_type="run.status_changed",
                        payload={
                            "from": previous_run_status.value,
                            "to": run.status.value,
                        },
                        created_at=request.occurred_at,
                    ),
                )
                await _append_event(
                    session,
                    RunEvent(
                        id=request.audit_event_id,
                        run_id=run.id,
                        sequence=await _next_event_sequence(session, run.id),
                        event_type="audit.cleanup_converged",
                        payload={
                            "audit_id": scan.id,
                            "operation": request.operation,
                            "reason_code": request.reason_code,
                            "cleanup_proof_digest": request.cleanup_proof_digest,
                            "run_terminal_status": terminal_status.value,
                            "audit_state_version": projected.state_version,
                        },
                        created_at=request.occurred_at,
                    ),
                )
                return AuditControlProjection(audit=projected, run=run, changed=True)
        except RepositoryError:
            raise
        except SQLAlchemyError:
            raise RepositoryUnavailableError(
                "Code Audit cleanup projection failed"
            ) from None


async def _load_locked_owner(
    session: AsyncSession,
    audit_id: str,
    run_id: str,
) -> tuple[
    AuditScanRecord,
    AuditContractORMRecord,
    AuditProjectRecord,
    RunRecord,
    AuditScan,
]:
    bundle = await load_validated_audit_scan(session, audit_id, for_update=True)
    if bundle is None:
        raise EntityNotFoundError("AuditScan", audit_id)
    scan_record, contract_record, project_record, run_record, scan = bundle
    run = run_from_record(run_record)
    if (
        scan.run_id != run_id
        or run.id != run_id
        or run.kind is not RunKind.CODE_AUDIT
        or run.temporal_workflow_id != scan.temporal_workflow_id
    ):
        raise RepositoryIntegrityError(
            "AuditScan",
            audit_id,
            reason_code="owner_binding_mismatch",
        )
    return scan_record, contract_record, project_record, run_record, scan


def _terminal_run_status(outcome: AuditTerminalOutcome | None) -> RunStatus:
    if outcome is None:
        raise RepositoryConflictError(
            "Code Audit cleanup convergence requires a terminal outcome"
        )
    return {
        AuditTerminalOutcome.COMPLETE: RunStatus.COMPLETED,
        AuditTerminalOutcome.PARTIAL: RunStatus.COMPLETED,
        AuditTerminalOutcome.FAILED: RunStatus.FAILED,
        AuditTerminalOutcome.CANCELLED: RunStatus.CANCELLED,
    }[outcome]


async def _next_event_sequence(session: AsyncSession, run_id: str) -> int:
    maximum = await session.scalar(
        select(func.max(RunEventRecord.sequence)).where(RunEventRecord.run_id == run_id)
    )
    return int(maximum or 0) + 1


async def _append_event(session: AsyncSession, event: RunEvent) -> None:
    existing = await session.get(RunEventRecord, event.id)
    if existing is not None:
        persisted = event_from_record(existing)
        if (
            persisted.run_id == event.run_id
            and persisted.event_type == event.event_type
            and persisted.payload == event.payload
        ):
            return
        raise RepositoryConflictError(
            "Code Audit control event identity is already bound to different facts"
        )
    session.add(event_to_record(event))
    await session.flush()


async def _stage_control_signal(
    session: AsyncSession,
    *,
    session_factory: SessionFactory,
    request: AuditControlTransition,
    scan: AuditScan,
    projected_state_version: int,
) -> None:
    signal_kind = request.workflow_signal_kind
    if signal_kind is None or signal_kind.value != request.operation:
        raise RepositoryConflictError(
            "Code Audit control signal does not match its lifecycle operation"
        )
    intent = WorkflowSignalIntent.code_audit(
        audit_id=scan.id,
        run_id=scan.run_id,
        workflow_id=scan.temporal_workflow_id,
        signal_kind=signal_kind,
        source_event_kind=WorkflowSignalSourceKind.CONTROL_INTENT,
        source_event_id=request.audit_event_id,
        source_state_version=projected_state_version,
        payload={"audit_id": scan.id},
        created_at=request.occurred_at,
    )

    from .workflow_signals import (  # noqa: PLC0415
        SQLAlchemyWorkflowSignalIntentRepository,
    )

    await SQLAlchemyWorkflowSignalIntentRepository(
        session_factory
    ).create_in_session(session, intent)


__all__ = ["SQLAlchemyAuditControlUnitOfWork"]
