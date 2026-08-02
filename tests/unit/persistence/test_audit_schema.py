from sqlalchemy import CheckConstraint, ForeignKeyConstraint, Text, UniqueConstraint

from riftx.persistence.orm import Base

AUDIT_TABLES = {
    "audit_projects",
    "source_snapshots",
    "audit_contracts",
    "audit_scans",
    "audit_start_intents",
    "audit_phase_runs",
    "audit_scope_units",
    "audit_work_items",
}


def _unique_columns(table_name: str) -> set[tuple[str, ...]]:
    table = Base.metadata.tables[table_name]
    return {
        tuple(column.name for column in constraint.columns)
        for constraint in table.constraints
        if isinstance(constraint, UniqueConstraint)
    }


def _foreign_keys(table_name: str) -> dict[tuple[str, ...], tuple[str, ...]]:
    table = Base.metadata.tables[table_name]
    return {
        tuple(element.parent.name for element in constraint.elements): tuple(
            element.target_fullname for element in constraint.elements
        )
        for constraint in table.constraints
        if isinstance(constraint, ForeignKeyConstraint)
    }


def _check_names(table_name: str) -> set[str | None]:
    return {
        constraint.name
        for constraint in Base.metadata.tables[table_name].constraints
        if isinstance(constraint, CheckConstraint)
    }


def _check_sql(table_name: str) -> dict[str | None, str]:
    return {
        constraint.name: str(constraint.sqltext)
        for constraint in Base.metadata.tables[table_name].constraints
        if isinstance(constraint, CheckConstraint)
    }


def _indexes(table_name: str) -> dict[str | None, tuple[str, ...]]:
    return {
        index.name: tuple(column.name for column in index.columns)
        for index in Base.metadata.tables[table_name].indexes
    }


def test_metadata_contains_code_audit_foundation_tables() -> None:
    assert AUDIT_TABLES <= set(Base.metadata.tables)


def test_audit_project_and_snapshot_keys_preserve_authorization_domains() -> None:
    assert {
        ("repository_identity_digest",),
        ("id", "engagement_id"),
    } <= _unique_columns("audit_projects")
    assert _foreign_keys("audit_projects")[("engagement_id",)] == (
        "engagements.id",
    )

    snapshot_uniques = _unique_columns("source_snapshots")
    assert ("id", "project_id") in snapshot_uniques
    assert ("project_id", "snapshot_digest") in snapshot_uniques
    assert _foreign_keys("source_snapshots")[("parent_snapshot_id", "project_id")] == (
        "source_snapshots.id",
        "source_snapshots.project_id",
    )
    assert Base.metadata.tables["source_snapshots"].c.sealed_at.nullable is False


def test_contract_keeps_exact_canonical_text_and_noncyclic_binding() -> None:
    contracts = Base.metadata.tables["audit_contracts"]

    assert isinstance(contracts.c.canonical_contract_json.type, Text)
    assert contracts.c.state_version.nullable is False
    assert ("audit_id",) in _unique_columns("audit_contracts")
    assert (
        "contract_id",
        "audit_id",
        "contract_digest",
    ) in _unique_columns("audit_contracts")
    assert _foreign_keys("audit_contracts") == {}

    scan_contract_fk = _foreign_keys("audit_scans")[(
        "contract_id",
        "id",
        "contract_digest",
    )]
    assert scan_contract_fk == (
        "audit_contracts.contract_id",
        "audit_contracts.audit_id",
        "audit_contracts.contract_digest",
    )


def test_scan_composite_keys_bind_run_project_contract_and_history() -> None:
    run_indexes = _indexes("runs")
    assert run_indexes["uq_runs_id_engagement_kind"] == (
        "id",
        "engagement_id",
        "kind",
    )
    assert next(
        index.unique
        for index in Base.metadata.tables["runs"].indexes
        if index.name == "uq_runs_id_engagement_kind"
    )
    assert run_indexes["uq_runs_id_engagement_kind_node"] == (
        "id",
        "engagement_id",
        "kind",
        "node_id",
    )
    assert run_indexes["uq_runs_id_status"] == ("id", "status")

    foreign_keys = _foreign_keys("audit_scans")
    assert foreign_keys[(
        "run_id",
        "engagement_id",
        "run_kind",
        "selected_node_id",
    )] == (
        "runs.id",
        "runs.engagement_id",
        "runs.kind",
        "runs.node_id",
    )
    assert foreign_keys[("run_id", "run_terminal_status")] == (
        "runs.id",
        "runs.status",
    )
    assert foreign_keys[("project_id", "engagement_id")] == (
        "audit_projects.id",
        "audit_projects.engagement_id",
    )
    assert foreign_keys[("snapshot_id", "project_id")] == (
        "source_snapshots.id",
        "source_snapshots.project_id",
    )
    assert foreign_keys[("base_snapshot_id", "project_id")] == (
        "source_snapshots.id",
        "source_snapshots.project_id",
    )
    assert foreign_keys[("baseline_audit_id", "project_id")] == (
        "audit_scans.id",
        "audit_scans.project_id",
    )
    assert foreign_keys[("parent_audit_id", "project_id")] == (
        "audit_scans.id",
        "audit_scans.project_id",
    )

    scan_uniques = _unique_columns("audit_scans")
    assert ("run_id",) in scan_uniques
    assert ("temporal_workflow_id",) in scan_uniques
    assert ("id", "project_id") in scan_uniques
    assert (
        "id",
        "run_id",
        "contract_digest",
        "temporal_workflow_id",
    ) in scan_uniques


def test_distribution_revision_pointers_are_explicitly_unbound_until_revision_schema() -> None:
    scans = Base.metadata.tables["audit_scans"]
    constrained_columns = {
        element.parent.name
        for constraint in scans.constraints
        if isinstance(constraint, ForeignKeyConstraint)
        for element in constraint.elements
    }

    assert "initial_distribution_revision_id" in scans.c
    assert "latest_distribution_revision_id" in scans.c
    assert "initial_distribution_revision_id" not in constrained_columns
    assert "latest_distribution_revision_id" not in constrained_columns
    assert "ck_audit_scans_published_revision" in _check_names("audit_scans")


def test_start_scope_and_work_relations_cannot_cross_audit_ownership() -> None:
    assert _foreign_keys("audit_start_intents")[(
        "audit_id",
        "run_id",
        "contract_digest",
        "workflow_id",
    )] == (
        "audit_scans.id",
        "audit_scans.run_id",
        "audit_scans.contract_digest",
        "audit_scans.temporal_workflow_id",
    )

    scope_fks = _foreign_keys("audit_scope_units")
    assert scope_fks[("audit_id", "project_id")] == (
        "audit_scans.id",
        "audit_scans.project_id",
    )
    assert scope_fks[("snapshot_id", "project_id")] == (
        "source_snapshots.id",
        "source_snapshots.project_id",
    )
    assert (
        "audit_id",
        "snapshot_id",
        "kind",
        "stable_key",
    ) in _unique_columns("audit_scope_units")
    assert _foreign_keys("audit_work_items")[(
        "primary_scope_unit_id",
        "audit_id",
    )] == (
        "audit_scope_units.id",
        "audit_scope_units.audit_id",
    )


def test_workflow_identifiers_use_audit_token_capacity() -> None:
    assert Base.metadata.tables["audit_scans"].c.temporal_workflow_id.type.length == 256
    assert Base.metadata.tables["audit_start_intents"].c.workflow_id.type.length == 256
    assert Base.metadata.tables["audit_start_intents"].c.task_queue.type.length == 256


def test_audit_model_profile_capacity_matches_the_authoritative_run() -> None:
    assert Base.metadata.tables["runs"].c.model_profile.type.length == 255
    assert Base.metadata.tables["audit_scans"].c.model_profile.type.length == 255


def test_mutable_audit_tables_have_positive_state_versions() -> None:
    mutable_tables = {
        "audit_projects",
        "audit_contracts",
        "audit_scans",
        "audit_start_intents",
        "audit_phase_runs",
        "audit_scope_units",
        "audit_work_items",
    }
    for table_name in mutable_tables:
        table = Base.metadata.tables[table_name]
        assert table.c.state_version.nullable is False
        assert any(
            name is not None and name.endswith("state_version")
            for name in _check_names(table_name)
        ) or any(
            name is not None and name.endswith("counters")
            for name in _check_names(table_name)
        )


def test_audit_query_indexes_include_stable_tie_breakers() -> None:
    assert _indexes("audit_projects")["ix_audit_projects_engagement_created_id"] == (
        "engagement_id",
        "created_at",
        "id",
    )
    assert _indexes("source_snapshots")["ix_source_snapshots_project_created_id"] == (
        "project_id",
        "created_at",
        "id",
    )
    assert _indexes("audit_scans")[
        "ix_audit_scans_project_lifecycle_created_id"
    ] == ("project_id", "lifecycle_status", "created_at", "id")
    assert _indexes("audit_start_intents")["ix_audit_start_intents_dispatch"][-1] == (
        "intent_id"
    )
    assert _indexes("audit_phase_runs")[
        "ix_audit_phase_runs_audit_phase_status_created_id"
    ][-1] == "id"
    assert _indexes("audit_scope_units")[
        "ix_audit_scope_units_audit_kind_status_risk_id"
    ][-1] == "id"
    assert _indexes("audit_work_items")[
        "ix_audit_work_items_audit_phase_status_lease_epoch_id"
    ][-1] == "id"


def test_high_value_enum_digest_and_atomic_pair_checks_are_present() -> None:
    expected = {
        "source_snapshots": {
            "ck_source_snapshots_source_kind",
            "ck_source_snapshots_snapshot_digest",
            "ck_source_snapshots_retest_fields",
        },
        "audit_contracts": {
            "ck_audit_contracts_schema_version",
            "ck_audit_contracts_canonical_size",
            "ck_audit_contracts_contract_digest",
        },
        "audit_scans": {
            "ck_audit_scans_run_kind",
            "ck_audit_scans_snapshot_mode",
            "ck_audit_scans_cleanup_pair",
            "ck_audit_scans_cleanup_outcome",
            "ck_audit_scans_core_seal_pair",
            "ck_audit_scans_distribution_pair",
        },
        "audit_start_intents": {
            "ck_audit_start_intents_status",
            "ck_audit_start_intents_lease_pair",
            "ck_audit_start_intents_lease_order",
            "ck_audit_start_intents_retry_order",
            "ck_audit_start_intents_started_order",
        },
        "audit_phase_runs": {
            "ck_audit_phase_runs_phase",
            "ck_audit_phase_runs_status",
            "ck_audit_phase_runs_error_pair",
            "ck_audit_phase_runs_started_order",
            "ck_audit_phase_runs_finished_order",
            "ck_audit_phase_runs_runtime_order",
            "ck_audit_phase_runs_error_summary_size",
            "ck_audit_phase_runs_active_outputs",
        },
        "audit_scope_units": {
            "ck_audit_scope_units_kind",
            "ck_audit_scope_units_risk_tier",
            "ck_audit_scope_units_closure_pair",
            "ck_audit_scope_units_relative_path_size",
            "ck_audit_scope_units_closure_reason_size",
        },
        "audit_work_items": {
            "ck_audit_work_items_phase",
            "ck_audit_work_items_status",
            "ck_audit_work_items_lease_pair",
            "ck_audit_work_items_receipt_status",
        },
    }
    for table_name, required_names in expected.items():
        assert required_names <= _check_names(table_name)


def test_audit_time_and_bounded_text_checks_match_domain_contract() -> None:
    snapshot_checks = _check_sql("source_snapshots")
    assert snapshot_checks["ck_source_snapshots_storage_keys"] == (
        "length(content_storage_key) BETWEEN 1 AND 4096 AND "
        "length(manifest_storage_key) BETWEEN 1 AND 4096"
    )

    intent_checks = _check_sql("audit_start_intents")
    assert intent_checks["ck_audit_start_intents_lease_order"] == (
        "lease_expires_at IS NULL OR lease_expires_at > updated_at"
    )
    assert intent_checks["ck_audit_start_intents_retry_order"] == (
        "next_attempt_at IS NULL OR next_attempt_at >= updated_at"
    )
    assert intent_checks["ck_audit_start_intents_started_order"] == (
        "started_at IS NULL OR "
        "(started_at >= created_at AND started_at <= updated_at)"
    )

    phase_checks = _check_sql("audit_phase_runs")
    assert phase_checks["ck_audit_phase_runs_started_order"] == (
        "started_at IS NULL OR "
        "(started_at >= created_at AND started_at <= updated_at)"
    )
    assert phase_checks["ck_audit_phase_runs_finished_order"] == (
        "finished_at IS NULL OR "
        "(finished_at >= created_at AND finished_at <= updated_at)"
    )
    assert phase_checks["ck_audit_phase_runs_runtime_order"] == (
        "finished_at IS NULL OR started_at IS NULL OR finished_at >= started_at"
    )
    assert phase_checks["ck_audit_phase_runs_error_summary_size"] == (
        "error_summary IS NULL OR length(error_summary) BETWEEN 1 AND 4096"
    )
    assert phase_checks["ck_audit_phase_runs_active_outputs"] == (
        "status NOT IN ('queued', 'running') OR "
        "(json_array_length(output_artifact_ids_json) = 0 AND "
        "json_array_length(summary_counts_json) = 0)"
    )

    scope_checks = _check_sql("audit_scope_units")
    assert scope_checks["ck_audit_scope_units_relative_path_size"] == (
        "relative_path IS NULL OR length(relative_path) BETWEEN 1 AND 4096"
    )
    assert scope_checks["ck_audit_scope_units_closure_reason_size"] == (
        "closure_reason IS NULL OR length(closure_reason) BETWEEN 1 AND 4096"
    )
