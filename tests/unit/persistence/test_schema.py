from riftx.persistence.orm import Base

EXPECTED_TABLES = {
    "agent_checkpoints",
    "agent_messages",
    "alembic_version",
    "approvals",
    "approval_grants",
    "artifacts",
    "engagements",
    "executions",
    "findings",
    "nodes",
    "reports",
    "run_events",
    "runs",
    "terminal_sessions",
    "tool_calls",
    "tool_states",
}


def test_metadata_contains_v2_business_tables() -> None:
    assert set(Base.metadata.tables) == EXPECTED_TABLES - {"alembic_version"}


def test_run_table_matches_design_contract() -> None:
    assert set(Base.metadata.tables["runs"].columns.keys()) == {
        "id",
        "engagement_id",
        "node_id",
        "objective",
        "success_criteria_json",
        "entry_points_json",
        "scope_json",
        "status",
        "approval_mode",
        "workspace_path",
        "temporal_workflow_id",
        "created_at",
        "started_at",
        "finished_at",
    }


def test_event_sequence_is_unique_per_run() -> None:
    constraints = Base.metadata.tables["run_events"].constraints
    unique_columns = {
        tuple(column.name for column in constraint.columns)
        for constraint in constraints
        if constraint.__class__.__name__ == "UniqueConstraint"
    }
    assert ("run_id", "sequence") in unique_columns


def test_node_table_matches_runner_lifecycle_contract() -> None:
    assert set(Base.metadata.tables["nodes"].columns.keys()) == {
        "id",
        "name",
        "platform",
        "architecture",
        "runner_version",
        "status",
        "capabilities_json",
        "labels_json",
        "last_seen_at",
        "created_at",
        "updated_at",
    }
