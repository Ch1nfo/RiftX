from datetime import UTC, datetime

import pytest

from riftx.domain import (
    ApprovalMode,
    EntryPoint,
    EntryPointKind,
    Objective,
    Run,
    RunKind,
    RunStatus,
    Scope,
    SuccessCriterion,
)
from riftx.persistence.mappers import apply_run_to_record, run_from_record, run_to_record


def test_run_mapper_round_trip_preserves_domain_data() -> None:
    created_at = datetime(2026, 7, 29, 5, 0, tzinfo=UTC)
    run = Run(
        kind="general",
        id="run-1",
        engagement_id="engagement-1",
        node_id="node-1",
        objective=Objective(description="Assess the target"),
        success_criteria=[SuccessCriterion(description="Collect evidence")],
        entry_points=[EntryPoint(kind=EntryPointKind.DOMAIN, value="example.test")],
        scope=Scope(domains=["example.test"], exclusions=["admin.example.test"]),
        status=RunStatus.CREATED,
        approval_mode=ApprovalMode.MANUAL,
        workspace_path="/tmp/runs/run-1",
        temporal_workflow_id="workflow-1",
        created_at=created_at,
    )

    restored = run_from_record(run_to_record(run))

    assert restored == run
    assert restored.created_at.tzinfo is UTC


def test_run_mapper_preserves_code_audit_and_rejects_kind_confusion() -> None:
    audit_run = Run(
        kind=RunKind.CODE_AUDIT,
        id="audit-run-1",
        engagement_id="engagement-1",
        node_id="node-1",
        objective=Objective(description="Audit the repository"),
        workspace_path="/tmp/runs/audit-run-1",
    )
    record = run_to_record(audit_run)

    assert record.kind == "code_audit"
    assert run_from_record(record).kind is RunKind.CODE_AUDIT

    record.kind = "unknown"
    with pytest.raises(ValueError, match="unknown"):
        run_from_record(record)

    record.kind = RunKind.CODE_AUDIT.value
    reclassified = audit_run.model_copy(update={"kind": RunKind.GENERAL})
    with pytest.raises(ValueError, match="immutable"):
        apply_run_to_record(reclassified, record)
