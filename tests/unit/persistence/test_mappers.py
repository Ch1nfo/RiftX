from datetime import UTC, datetime

import pytest

from riftx.domain import (
    ApprovalMode,
    EntryPoint,
    EntryPointKind,
    Objective,
    PentestAdmission,
    PentestBudget,
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


def test_run_mapper_round_trip_preserves_pentest_admission() -> None:
    run = Run(
        kind=RunKind.PENTEST,
        id="pentest-run-1",
        engagement_id="engagement-1",
        node_id="node-1",
        objective=Objective(description="Assess the authorized target"),
        entry_points=[EntryPoint(kind=EntryPointKind.DOMAIN, value="example.test")],
        scope=Scope(domains=["example.test"]),
        pentest_admission=PentestAdmission(
            budget=PentestBudget(
                max_duration_seconds=3600,
                max_model_calls=100,
                max_tokens=100_000,
                max_tool_calls=200,
                max_target_interactions=50,
                max_concurrent_target_interactions=2,
            )
        ),
        workspace_path="/tmp/runs/pentest-run-1",
    )

    record = run_to_record(run)
    restored = run_from_record(record)

    assert record.kind == "pentest"
    assert record.pentest_admission_json == run.pentest_admission.model_dump(mode="json")
    assert restored == run
