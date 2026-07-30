from riftx.persistence.orm import Base

EXPECTED_TABLES = {
    "agent_checkpoints",
    "agent_cycles",
    "agent_sessions",
    "agent_steps",
    "agent_messages",
    "alembic_version",
    "approvals",
    "approval_grants",
    "artifacts",
    "context_compilations",
    "engagements",
    "executions",
    "findings",
    "nodes",
    "provider_states",
    "reports",
    "runtime_approval_requests",
    "runner_commands",
    "runner_credentials",
    "run_events",
    "run_leases",
    "runs",
    "terminal_sessions",
    "tool_call_intents",
    "tool_calls",
    "tool_states",
    "user_input_requests",
    "working_memories",
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
        "model_profile",
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


def test_context_compilation_table_records_manifest_and_actual_usage() -> None:
    assert set(Base.metadata.tables["context_compilations"].columns.keys()) == {
        "id",
        "run_id",
        "session_id",
        "agent_id",
        "model_profile",
        "purpose",
        "manifest_json",
        "estimated_tokens",
        "actual_input_tokens",
        "actual_output_tokens",
        "loaded_memory_ids_json",
        "checkpoint_id",
        "created_at",
    }


def test_working_memory_table_is_versioned_structured_state() -> None:
    assert set(Base.metadata.tables["working_memories"].columns.keys()) == {
        "id",
        "run_id",
        "version",
        "state_json",
        "created_at",
        "updated_at",
    }


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


def test_runner_control_tables_match_durable_channel_contract() -> None:
    credentials = Base.metadata.tables["runner_credentials"]
    assert set(credentials.columns.keys()) == {
        "node_id",
        "token_hash",
        "token_prefix",
        "created_at",
        "rotated_at",
        "revoked_at",
    }

    commands = Base.metadata.tables["runner_commands"]
    assert set(commands.columns.keys()) == {
        "id",
        "node_id",
        "kind",
        "idempotency_key",
        "payload_json",
        "status",
        "attempts",
        "lease_id",
        "lease_expires_at",
        "result_json",
        "error",
        "created_at",
        "updated_at",
        "completed_at",
    }
    unique_columns = {
        tuple(column.name for column in constraint.columns)
        for constraint in commands.constraints
        if constraint.__class__.__name__ == "UniqueConstraint"
    }
    assert ("node_id", "idempotency_key") in unique_columns
