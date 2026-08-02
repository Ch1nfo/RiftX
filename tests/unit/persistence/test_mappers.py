from datetime import UTC, datetime

from riftx.domain import (
    ApprovalMode,
    EntryPoint,
    EntryPointKind,
    Objective,
    Run,
    RunStatus,
    Scope,
    SuccessCriterion,
)
from riftx.persistence.mappers import run_from_record, run_to_record


def test_run_mapper_round_trip_preserves_domain_data() -> None:
    created_at = datetime(2026, 7, 29, 5, 0, tzinfo=UTC)
    run = Run(
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
