"""Pure Audit-to-Run lifecycle mapping shared by every Code Audit boundary."""

from __future__ import annotations

from .audit import AuditLifecycleStatus, AuditScan, AuditTerminalOutcome
from .enums import RunStatus


class AuditRunStateMappingPolicy:
    """Project one valid Audit lifecycle fact onto its authoritative Run state.

    Cleanup is the only lifecycle whose Run projection changes without changing
    the Audit lifecycle name: before convergence the Run is still stopping, and
    after the durable cleanup proof it is terminal.  Keeping that rule here
    prevents create/read/control/projector paths from drifting apart.
    """

    @staticmethod
    def expected_run_status(scan: AuditScan) -> RunStatus:
        direct = {
            AuditLifecycleStatus.DRAFT: RunStatus.CREATED,
            AuditLifecycleStatus.QUEUED: RunStatus.PREPARING,
            AuditLifecycleStatus.PREFLIGHTING: RunStatus.PREPARING,
            AuditLifecycleStatus.SNAPSHOTTING: RunStatus.PREPARING,
            AuditLifecycleStatus.RUNNING: RunStatus.RUNNING,
            AuditLifecycleStatus.WAITING_APPROVAL: RunStatus.WAITING_APPROVAL,
            AuditLifecycleStatus.PAUSING: RunStatus.PAUSING,
            AuditLifecycleStatus.PAUSED: RunStatus.PAUSED,
            AuditLifecycleStatus.FINALIZING: RunStatus.COMPLETING,
            AuditLifecycleStatus.CANCELLING: RunStatus.CANCELLING,
            AuditLifecycleStatus.FAILING: RunStatus.COMPLETING,
            AuditLifecycleStatus.COMPLETED: RunStatus.COMPLETED,
            AuditLifecycleStatus.COMPLETED_PARTIAL: RunStatus.COMPLETED,
            AuditLifecycleStatus.FAILED: RunStatus.FAILED,
            AuditLifecycleStatus.CANCELLED: RunStatus.CANCELLED,
        }
        expected = direct.get(scan.lifecycle_status)
        if expected is not None:
            return expected

        if scan.lifecycle_status is AuditLifecycleStatus.CLEANING:
            if scan.cleanup_proof_digest is not None:
                if scan.run_terminal_status is None:
                    raise ValueError("converged Audit cleanup has no Run terminal status")
                return scan.run_terminal_status
            return (
                RunStatus.CANCELLING
                if scan.terminal_outcome is AuditTerminalOutcome.CANCELLED
                else RunStatus.COMPLETING
            )

        if scan.lifecycle_status in {
            AuditLifecycleStatus.SEALING_CORE,
            AuditLifecycleStatus.REPORTING,
            AuditLifecycleStatus.PACKAGING,
        }:
            terminal_by_outcome = {
                AuditTerminalOutcome.COMPLETE: RunStatus.COMPLETED,
                AuditTerminalOutcome.PARTIAL: RunStatus.COMPLETED,
                AuditTerminalOutcome.FAILED: RunStatus.FAILED,
                AuditTerminalOutcome.CANCELLED: RunStatus.CANCELLED,
            }
            if scan.terminal_outcome is not None:
                return terminal_by_outcome[scan.terminal_outcome]

        raise ValueError("Audit lifecycle has no valid Run status projection")


__all__ = ["AuditRunStateMappingPolicy"]
