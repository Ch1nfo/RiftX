import sqlite3
from pathlib import Path
from runpy import run_path
from types import SimpleNamespace

import pytest
from sqlalchemy import (
    CheckConstraint,
    ForeignKeyConstraint,
    UniqueConstraint,
    create_engine,
    event,
    inspect,
)
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine import Engine
from tests.integration.persistence.test_migrations import (
    downgrade_alembic,
    run_alembic,
    sqlite_tables,
)
from tests.integration.persistence.test_mutation_clock_migration import _offline_sql

from riftx.persistence.orm import Base

BASE_REVISION = "0d3a8b7c4e21"
AUDIT_REVISION = "3b7f1d9e5a02"
AUDIT_TABLE_ORDER = (
    "audit_work_items",
    "audit_scope_units",
    "audit_phase_runs",
    "audit_start_intents",
    "audit_scans",
    "audit_contracts",
    "source_snapshots",
    "audit_projects",
)
AUDIT_TABLES = set(AUDIT_TABLE_ORDER)
AUDIT_MIGRATION = run_path(
    str(
        Path(__file__).parents[3]
        / "migrations/versions/3b7f1d9e5a02_add_code_audit_persistence.py"
    )
)
NOW = "2026-08-03 00:00:00+00:00"
BETWEEN = "2026-08-03 01:00:00+00:00"
UPDATED = "2026-08-03 02:00:00+00:00"
AFTER_UPDATED = "2026-08-03 03:00:00+00:00"


def _assert_check_rejected(
    connection: sqlite3.Connection,
    check_name: str,
    statement: str,
    parameters: tuple[object, ...],
) -> None:
    with pytest.raises(sqlite3.IntegrityError, match=check_name):
        connection.execute(statement, parameters)
    connection.rollback()


def _insert_engagement(connection: sqlite3.Connection, engagement_id: str) -> None:
    connection.execute(
        "INSERT INTO engagements "
        "(id, name, description, authorization_reference, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (engagement_id, engagement_id, "", "policy:code-audit", NOW, NOW),
    )


def _insert_run(
    connection: sqlite3.Connection,
    *,
    run_id: str,
    engagement_id: str,
    kind: str,
    workflow_id: str,
) -> None:
    connection.execute(
        "INSERT INTO runs "
        "(id, engagement_id, kind, node_id, objective, success_criteria_json, "
        "entry_points_json, scope_json, status, approval_mode, model_profile, "
        "workspace_path, temporal_workflow_id, created_at, started_at, finished_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            run_id,
            engagement_id,
            kind,
            "analysis-node",
            "Migration fixture",
            "[]",
            "[]",
            "{}",
            "created",
            "balanced",
            None,
            "/tmp/riftx-audit-migration-fixture",
            workflow_id,
            NOW,
            None,
            None,
        ),
    )


def _insert_project(
    connection: sqlite3.Connection,
    *,
    project_id: str,
    engagement_id: str,
    digest_character: str,
) -> None:
    connection.execute(
        "INSERT INTO audit_projects "
        "(id, engagement_id, display_name, vcs_kind, repository_identity_digest, "
        "default_branch, state_version, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            project_id,
            engagement_id,
            project_id,
            "git",
            digest_character * 64,
            "main",
            1,
            NOW,
            NOW,
        ),
    )


def _insert_contract(
    connection: sqlite3.Connection,
    *,
    contract_id: str,
    audit_id: str,
    digest_character: str,
) -> None:
    connection.execute(
        "INSERT INTO audit_contracts "
        "(contract_id, audit_id, schema_version, canonical_contract_json, "
        "contract_digest, source_target_digest, source_node_id, "
        "source_ingest_backend_digest, source_prepare_proof_digest, "
        "selected_node_id, required_backend_id, snapshot_hydration_policy_digest, "
        "state_version, created_at, sealed_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            contract_id,
            audit_id,
            "riftx.audit-contract/v1",
            "{}",
            digest_character * 64,
            "1" * 64,
            "source-node",
            "2" * 64,
            "3" * 64,
            "analysis-node",
            "audit-sandbox-v1",
            "4" * 64,
            1,
            NOW,
            None,
        ),
    )


def _insert_scan(
    connection: sqlite3.Connection,
    *,
    audit_id: str,
    run_id: str,
    engagement_id: str,
    project_id: str,
    contract_id: str,
    contract_digest_character: str,
    workflow_id: str,
    run_kind: str = "code_audit",
) -> None:
    connection.execute(
        "INSERT INTO audit_scans "
        "(id, run_id, engagement_id, run_kind, project_id, contract_id, "
        "snapshot_id, base_snapshot_id, baseline_audit_id, purpose, parent_audit_id, "
        "mode, analysis_profile, lifecycle_status, current_phase, terminal_outcome, "
        "cleanup_proof_digest, run_terminal_status, closure_status, "
        "publication_status, core_seal_root, initial_distribution_revision_id, "
        "latest_distribution_revision_id, model_profile, selected_node_id, "
        "required_backend_id, policy_digest, budget_digest, config_digest, "
        "contract_digest, temporal_workflow_id, state_version, created_at, started_at, "
        "analysis_finished_at, publication_finished_at, sealed_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
        "?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            audit_id,
            run_id,
            engagement_id,
            run_kind,
            project_id,
            contract_id,
            None,
            None,
            None,
            "primary",
            None,
            "standard",
            "deterministic",
            "draft",
            "authorize_and_freeze",
            None,
            None,
            None,
            None,
            "not_started",
            None,
            None,
            None,
            None,
            "analysis-node",
            "audit-sandbox-v1",
            "5" * 64,
            "6" * 64,
            "7" * 64,
            contract_digest_character * 64,
            workflow_id,
            1,
            NOW,
            None,
            None,
            None,
            None,
        ),
    )


def _insert_snapshot(
    connection: sqlite3.Connection,
    *,
    snapshot_id: str,
    project_id: str,
    digest_character: str,
) -> None:
    connection.execute(
        "INSERT INTO source_snapshots "
        "(id, project_id, source_kind, parent_snapshot_id, base_tree_digest, "
        "patch_digest, commit_sha, base_commit_sha, working_tree_digest, tree_digest, "
        "capture_policy_digest, materializer_schema_version, snapshot_digest, "
        "snapshot_store_version, content_storage_key, manifest_storage_key, "
        "manifest_digest, file_count, total_bytes, created_at, sealed_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            snapshot_id,
            project_id,
            "revision",
            None,
            None,
            None,
            "a" * 40,
            None,
            None,
            "8" * 64,
            "9" * 64,
            "riftx.snapshot-materializer/v1",
            digest_character * 64,
            "riftx.snapshot-store/v1",
            f"cas/{snapshot_id}",
            f"manifest/{snapshot_id}",
            "b" * 64,
            1,
            128,
            NOW,
            NOW,
        ),
    )


def test_audit_migration_upgrades_sqlite_and_enforces_integrity(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "audit-upgrade.db"
    run_alembic(database_path, BASE_REVISION)

    with sqlite3.connect(database_path) as connection:
        _insert_engagement(connection, "engagement-audit")
        _insert_run(
            connection,
            run_id="legacy-general-run",
            engagement_id="engagement-audit",
            kind="general",
            workflow_id="legacy-general-workflow",
        )
        _insert_run(
            connection,
            run_id="audit-run-1",
            engagement_id="engagement-audit",
            kind="code_audit",
            workflow_id="audit-workflow-1",
        )
        connection.execute(
            "INSERT INTO run_events "
            "(id, run_id, sequence, event_type, payload_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                "legacy-event",
                "legacy-general-run",
                1,
                "run.created",
                "{}",
                NOW,
            ),
        )
        connection.commit()

    run_alembic(database_path, "head")

    assert AUDIT_TABLES <= sqlite_tables(database_path)
    assert "audit_client_requests" in sqlite_tables(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute(
            "SELECT kind FROM runs WHERE id = 'legacy-general-run'"
        ).fetchone() == ("general",)
        assert connection.execute(
            "SELECT count(*) FROM run_events WHERE id = 'legacy-event'"
        ).fetchone() == (1,)
        run_indexes = {
            row[1]: bool(row[2])
            for row in connection.execute("PRAGMA index_list(runs)").fetchall()
        }
        assert run_indexes["uq_runs_id_engagement_kind"] is True
        assert run_indexes["uq_runs_id_engagement_kind_node"] is True
        assert run_indexes["uq_runs_id_status"] is True

        _insert_project(
            connection,
            project_id="project-1",
            engagement_id="engagement-audit",
            digest_character="c",
        )
        _insert_contract(
            connection,
            contract_id="contract-1",
            audit_id="audit-1",
            digest_character="d",
        )
        _insert_scan(
            connection,
            audit_id="audit-1",
            run_id="audit-run-1",
            engagement_id="engagement-audit",
            project_id="project-1",
            contract_id="contract-1",
            contract_digest_character="d",
            workflow_id="audit-workflow-1",
        )
        _insert_snapshot(
            connection,
            snapshot_id="snapshot-1",
            project_id="project-1",
            digest_character="e",
        )
        connection.commit()

        with pytest.raises(sqlite3.IntegrityError):
            _insert_project(
                connection,
                project_id="uppercase-digest-project",
                engagement_id="engagement-audit",
                digest_character="A",
            )
        connection.rollback()

        _insert_contract(
            connection,
            contract_id="contract-general",
            audit_id="audit-general",
            digest_character="f",
        )
        connection.commit()
        with pytest.raises(sqlite3.IntegrityError):
            _insert_scan(
                connection,
                audit_id="audit-general",
                run_id="legacy-general-run",
                engagement_id="engagement-audit",
                project_id="project-1",
                contract_id="contract-general",
                contract_digest_character="f",
                workflow_id="legacy-general-workflow",
            )
        connection.rollback()

        _insert_project(
            connection,
            project_id="project-2",
            engagement_id="engagement-audit",
            digest_character="a",
        )
        _insert_snapshot(
            connection,
            snapshot_id="snapshot-2",
            project_id="project-2",
            digest_character="0",
        )
        connection.commit()
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO audit_scope_units "
                "(id, audit_id, project_id, snapshot_id, stable_key, kind, "
                "relative_path, blob_digest, symbol_anchor, risk_tier, "
                "required_analyses_json, status, closure_code, closure_reason, "
                "receipt_count, state_version, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "cross-project-scope",
                    "audit-1",
                    "project-1",
                    "snapshot-2",
                    "1" * 64,
                    "file",
                    "src/main.py",
                    "2" * 64,
                    None,
                    "high",
                    "[]",
                    "included",
                    None,
                    None,
                    0,
                    1,
                    NOW,
                    NOW,
                ),
            )
        connection.rollback()

        connection.execute(
            "INSERT INTO audit_scope_units "
            "(id, audit_id, project_id, snapshot_id, stable_key, kind, "
            "relative_path, blob_digest, symbol_anchor, risk_tier, "
            "required_analyses_json, status, closure_code, closure_reason, "
            "receipt_count, state_version, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "scope-1",
                "audit-1",
                "project-1",
                "snapshot-1",
                "3" * 64,
                "file",
                "src/main.py",
                "4" * 64,
                None,
                "high",
                "[]",
                "included",
                None,
                None,
                0,
                1,
                NOW,
                NOW,
            ),
        )
        connection.commit()
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO audit_work_items "
                "(id, audit_id, phase, epoch, primary_scope_unit_id, strategy, "
                "stable_key, risk_tier, status, lease_owner, lease_expires_at, "
                "attempt, input_digest, required_coverage_plan_artifact_id, "
                "required_coverage_plan_digest, receipt_id, state_version, "
                "created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "cross-audit-work",
                    "audit-other",
                    "agent_hunt",
                    0,
                    "scope-1",
                    "entry-first",
                    "5" * 64,
                    "high",
                    "queued",
                    None,
                    None,
                    0,
                    "6" * 64,
                    "plan-artifact",
                    "7" * 64,
                    None,
                    1,
                    NOW,
                    NOW,
                ),
            )
        connection.rollback()
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_audit_migration_enforces_temporal_and_text_bounds(tmp_path: Path) -> None:
    database_path = tmp_path / "audit-domain-bounds.db"
    run_alembic(database_path, "head")

    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        _insert_engagement(connection, "engagement-domain-bounds")
        _insert_run(
            connection,
            run_id="audit-run-domain-bounds",
            engagement_id="engagement-domain-bounds",
            kind="code_audit",
            workflow_id="audit-workflow-domain-bounds",
        )
        _insert_project(
            connection,
            project_id="project-domain-bounds",
            engagement_id="engagement-domain-bounds",
            digest_character="c",
        )
        _insert_contract(
            connection,
            contract_id="contract-domain-bounds",
            audit_id="audit-domain-bounds",
            digest_character="d",
        )
        _insert_scan(
            connection,
            audit_id="audit-domain-bounds",
            run_id="audit-run-domain-bounds",
            engagement_id="engagement-domain-bounds",
            project_id="project-domain-bounds",
            contract_id="contract-domain-bounds",
            contract_digest_character="d",
            workflow_id="audit-workflow-domain-bounds",
        )
        _insert_snapshot(
            connection,
            snapshot_id="snapshot-domain-bounds",
            project_id="project-domain-bounds",
            digest_character="e",
        )
        connection.commit()

        start_intent_insert = (
            "INSERT INTO audit_start_intents "
            "(intent_id, audit_id, run_id, start_request_id, contract_digest, "
            "workflow_id, task_queue, status, attempt, lease_owner, "
            "lease_expires_at, next_attempt_at, last_error_code, state_version, "
            "created_at, updated_at, started_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
        )
        common_start_values = (
            "audit-domain-bounds",
            "audit-run-domain-bounds",
            "d" * 64,
            "audit-workflow-domain-bounds",
            "audit-queue",
        )
        _assert_check_rejected(
            connection,
            "ck_audit_start_intents_lease_order",
            start_intent_insert,
            (
                "intent-expired-lease",
                common_start_values[0],
                common_start_values[1],
                "start-expired-lease",
                common_start_values[2],
                common_start_values[3],
                common_start_values[4],
                "claimed",
                1,
                "worker-1",
                UPDATED,
                None,
                None,
                1,
                NOW,
                UPDATED,
                None,
            ),
        )
        _assert_check_rejected(
            connection,
            "ck_audit_start_intents_retry_order",
            start_intent_insert,
            (
                "intent-past-retry",
                common_start_values[0],
                common_start_values[1],
                "start-past-retry",
                common_start_values[2],
                common_start_values[3],
                common_start_values[4],
                "retryable",
                1,
                None,
                None,
                BETWEEN,
                "transient-error",
                1,
                NOW,
                UPDATED,
                None,
            ),
        )
        _assert_check_rejected(
            connection,
            "ck_audit_start_intents_started_order",
            start_intent_insert,
            (
                "intent-future-start",
                common_start_values[0],
                common_start_values[1],
                "start-future-start",
                common_start_values[2],
                common_start_values[3],
                common_start_values[4],
                "started",
                1,
                None,
                None,
                None,
                None,
                1,
                NOW,
                UPDATED,
                AFTER_UPDATED,
            ),
        )

        phase_run_insert = (
            "INSERT INTO audit_phase_runs "
            "(id, audit_id, phase, attempt, idempotency_key, input_digest, "
            "config_digest, status, output_artifact_ids_json, summary_counts_json, "
            "error_code, error_summary, state_version, created_at, updated_at, "
            "started_at, finished_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
        )
        common_phase_values = (
            "audit-domain-bounds",
            "agent_hunt",
            1,
            "1" * 64,
            "2" * 64,
            "[]",
            "[]",
        )
        _assert_check_rejected(
            connection,
            "ck_audit_phase_runs_started_order",
            phase_run_insert,
            (
                "phase-future-start",
                common_phase_values[0],
                common_phase_values[1],
                common_phase_values[2],
                "phase-future-start",
                common_phase_values[3],
                common_phase_values[4],
                "running",
                common_phase_values[5],
                common_phase_values[6],
                None,
                None,
                1,
                NOW,
                UPDATED,
                AFTER_UPDATED,
                None,
            ),
        )
        _assert_check_rejected(
            connection,
            "ck_audit_phase_runs_finished_order",
            phase_run_insert,
            (
                "phase-future-finish",
                common_phase_values[0],
                common_phase_values[1],
                common_phase_values[2],
                "phase-future-finish",
                common_phase_values[3],
                common_phase_values[4],
                "failed",
                common_phase_values[5],
                common_phase_values[6],
                "probe-failed",
                "bounded failure",
                1,
                NOW,
                UPDATED,
                None,
                AFTER_UPDATED,
            ),
        )
        _assert_check_rejected(
            connection,
            "ck_audit_phase_runs_runtime_order",
            phase_run_insert,
            (
                "phase-reversed-runtime",
                common_phase_values[0],
                common_phase_values[1],
                common_phase_values[2],
                "phase-reversed-runtime",
                common_phase_values[3],
                common_phase_values[4],
                "completed",
                common_phase_values[5],
                common_phase_values[6],
                None,
                None,
                1,
                NOW,
                AFTER_UPDATED,
                UPDATED,
                BETWEEN,
            ),
        )
        _assert_check_rejected(
            connection,
            "ck_audit_phase_runs_error_summary_size",
            phase_run_insert,
            (
                "phase-oversized-error",
                common_phase_values[0],
                common_phase_values[1],
                common_phase_values[2],
                "phase-oversized-error",
                common_phase_values[3],
                common_phase_values[4],
                "failed",
                common_phase_values[5],
                common_phase_values[6],
                "probe-failed",
                "x" * 4097,
                1,
                NOW,
                NOW,
                None,
                NOW,
            ),
        )
        _assert_check_rejected(
            connection,
            "ck_audit_phase_runs_active_outputs",
            phase_run_insert,
            (
                "phase-queued-output",
                common_phase_values[0],
                common_phase_values[1],
                common_phase_values[2],
                "phase-queued-output",
                common_phase_values[3],
                common_phase_values[4],
                "queued",
                '["artifact-active"]',
                common_phase_values[6],
                None,
                None,
                1,
                NOW,
                NOW,
                None,
                None,
            ),
        )
        _assert_check_rejected(
            connection,
            "ck_audit_phase_runs_active_outputs",
            phase_run_insert,
            (
                "phase-running-summary",
                common_phase_values[0],
                common_phase_values[1],
                common_phase_values[2],
                "phase-running-summary",
                common_phase_values[3],
                common_phase_values[4],
                "running",
                common_phase_values[5],
                '[{"key":"active","count":1}]',
                None,
                None,
                1,
                NOW,
                NOW,
                NOW,
                None,
            ),
        )

        _assert_check_rejected(
            connection,
            "ck_source_snapshots_storage_keys",
            "UPDATE source_snapshots SET content_storage_key = ? WHERE id = ?",
            ("x" * 4097, "snapshot-domain-bounds"),
        )

        scope_unit_insert = (
            "INSERT INTO audit_scope_units "
            "(id, audit_id, project_id, snapshot_id, stable_key, kind, "
            "relative_path, blob_digest, symbol_anchor, risk_tier, "
            "required_analyses_json, status, closure_code, closure_reason, "
            "receipt_count, state_version, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
        )
        _assert_check_rejected(
            connection,
            "ck_audit_scope_units_relative_path_size",
            scope_unit_insert,
            (
                "scope-oversized-path",
                "audit-domain-bounds",
                "project-domain-bounds",
                "snapshot-domain-bounds",
                "3" * 64,
                "file",
                "x" * 4097,
                "4" * 64,
                None,
                "high",
                "[]",
                "included",
                None,
                None,
                0,
                1,
                NOW,
                NOW,
            ),
        )
        _assert_check_rejected(
            connection,
            "ck_audit_scope_units_closure_reason_size",
            scope_unit_insert,
            (
                "scope-oversized-closure",
                "audit-domain-bounds",
                "project-domain-bounds",
                "snapshot-domain-bounds",
                "5" * 64,
                "file",
                "src/main.py",
                "6" * 64,
                None,
                "high",
                "[]",
                "failed",
                "analysis-failed",
                "x" * 4097,
                0,
                1,
                NOW,
                NOW,
            ),
        )


def test_audit_migration_rejects_nonempty_downgrade_before_ddl(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "audit-nonempty-downgrade.db"
    run_alembic(database_path, AUDIT_REVISION)

    with sqlite3.connect(database_path) as connection:
        _insert_engagement(connection, "engagement-protected-audit")
        _insert_project(
            connection,
            project_id="protected-project",
            engagement_id="engagement-protected-audit",
            digest_character="b",
        )
        connection.commit()

    statements: list[str] = []

    def record_statement(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        statements.append(statement)

    event.listen(Engine, "before_cursor_execute", record_statement)
    try:
        with pytest.raises(RuntimeError, match="Audit facts exist"):
            downgrade_alembic(database_path, BASE_REVISION)
    finally:
        event.remove(Engine, "before_cursor_execute", record_statement)

    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
            AUDIT_REVISION,
        )
        assert connection.execute(
            "SELECT count(*) FROM audit_projects WHERE id = 'protected-project'"
        ).fetchone() == (1,)
    assert AUDIT_TABLES <= sqlite_tables(database_path)
    audit_statements = [" ".join(statement.split()) for statement in statements]
    serialization_index = audit_statements.index(
        'UPDATE "audit_projects" SET "state_version" = "state_version" WHERE 1 = 0'
    )
    first_check_index = audit_statements.index(
        'SELECT 1 FROM "audit_work_items" LIMIT 1'
    )
    assert serialization_index < first_check_index
    destructive_statements = [
        statement
        for statement in audit_statements
        if statement.upper().startswith(("DROP TABLE", "DROP INDEX"))
    ]
    assert destructive_statements == []


def test_sqlite_downgrade_serialization_blocks_a_competing_writer(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "audit-sqlite-downgrade-lock.db"
    run_alembic(database_path, "head")
    with sqlite3.connect(database_path) as connection:
        _insert_engagement(connection, "engagement-lock-test")
        connection.commit()

    engine = create_engine(
        f"sqlite:///{database_path}",
        connect_args={"timeout": 0.0},
    )
    serialize = AUDIT_MIGRATION["_serialize_audit_downgrade"]
    try:
        with engine.begin() as migration_connection:
            serialize(migration_connection)
            with sqlite3.connect(database_path, timeout=0.0) as competitor:
                with pytest.raises(sqlite3.OperationalError, match="database is locked"):
                    _insert_project(
                        competitor,
                        project_id="racing-project",
                        engagement_id="engagement-lock-test",
                        digest_character="d",
                    )
                competitor.rollback()

            assert migration_connection.exec_driver_sql(
                "SELECT count(*) FROM audit_projects"
            ).scalar_one() == 0
    finally:
        engine.dispose()

    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT count(*) FROM audit_projects").fetchone() == (0,)


def test_postgresql_downgrade_locks_all_tables_before_reads_or_ddl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[tuple[str, str]] = []

    class EmptyResult:
        @staticmethod
        def first() -> None:
            return None

    class RecordingConnection:
        dialect = postgresql.dialect()

        @staticmethod
        def in_transaction() -> bool:
            return True

        @staticmethod
        def exec_driver_sql(statement: str) -> None:
            events.append(("lock", statement))

        @staticmethod
        def execute(statement: object) -> EmptyResult:
            events.append(("read", str(statement)))
            return EmptyResult()

    migration_op = AUDIT_MIGRATION["op"]
    monkeypatch.setattr(
        migration_op,
        "get_context",
        lambda: SimpleNamespace(as_sql=False),
    )
    monkeypatch.setattr(migration_op, "get_bind", RecordingConnection)
    monkeypatch.setattr(
        migration_op,
        "drop_table",
        lambda table_name: events.append(("drop_table", table_name)),
    )
    monkeypatch.setattr(
        migration_op,
        "drop_index",
        lambda index_name, *, table_name: events.append(
            ("drop_index", f"{table_name}.{index_name}")
        ),
    )

    AUDIT_MIGRATION["downgrade"]()

    expected_locks = [
        ("lock", f'LOCK TABLE "{table_name}" IN ACCESS EXCLUSIVE MODE')
        for table_name in AUDIT_TABLE_ORDER
    ]
    expected_reads = [
        ("read", f'SELECT 1 FROM "{table_name}" LIMIT 1')
        for table_name in AUDIT_TABLE_ORDER
    ]
    expected_drops = [
        ("drop_table", table_name) for table_name in AUDIT_TABLE_ORDER
    ]
    assert events == [
        *expected_locks,
        *expected_reads,
        *expected_drops,
        ("drop_index", "runs.uq_runs_id_status"),
        ("drop_index", "runs.uq_runs_id_engagement_kind_node"),
        ("drop_index", "runs.uq_runs_id_engagement_kind"),
    ]


@pytest.mark.parametrize("nonempty_table", AUDIT_TABLE_ORDER)
def test_downgrade_refuses_every_nonempty_audit_table_before_ddl(
    monkeypatch: pytest.MonkeyPatch,
    nonempty_table: str,
) -> None:
    events: list[tuple[str, str]] = []

    class PresenceResult:
        def __init__(self, present: bool) -> None:
            self._present = present

        def first(self) -> tuple[int] | None:
            return (1,) if self._present else None

    class RecordingConnection:
        dialect = SimpleNamespace(name="sqlite")

        @staticmethod
        def in_transaction() -> bool:
            return True

        @staticmethod
        def exec_driver_sql(statement: str) -> None:
            events.append(("serialize", statement))

        @staticmethod
        def execute(statement: object) -> PresenceResult:
            statement_text = str(statement)
            events.append(("read", statement_text))
            return PresenceResult(
                statement_text == f'SELECT 1 FROM "{nonempty_table}" LIMIT 1'
            )

    migration_op = AUDIT_MIGRATION["op"]
    monkeypatch.setattr(
        migration_op,
        "get_context",
        lambda: SimpleNamespace(as_sql=False),
    )
    monkeypatch.setattr(migration_op, "get_bind", RecordingConnection)
    monkeypatch.setattr(
        migration_op,
        "drop_table",
        lambda table_name: events.append(("drop_table", table_name)),
    )
    monkeypatch.setattr(
        migration_op,
        "drop_index",
        lambda index_name, *, table_name: events.append(
            ("drop_index", f"{table_name}.{index_name}")
        ),
    )

    with pytest.raises(RuntimeError, match=nonempty_table):
        AUDIT_MIGRATION["downgrade"]()

    assert events[0] == (
        "serialize",
        'UPDATE "audit_projects" '
        'SET "state_version" = "state_version" WHERE 1 = 0',
    )
    assert [event for event in events if event[0] == "read"] == [
        ("read", f'SELECT 1 FROM "{table_name}" LIMIT 1')
        for table_name in AUDIT_TABLE_ORDER
    ]
    assert not any(event_name.startswith("drop") for event_name, _ in events)


def test_empty_audit_migration_downgrades_and_reupgrades(tmp_path: Path) -> None:
    database_path = tmp_path / "audit-empty-roundtrip.db"
    run_alembic(database_path, "head")

    downgrade_alembic(database_path, BASE_REVISION)
    assert AUDIT_TABLES.isdisjoint(sqlite_tables(database_path))
    with sqlite3.connect(database_path) as connection:
        remaining_run_indexes = {
            row[1] for row in connection.execute("PRAGMA index_list(runs)").fetchall()
        }
        assert "uq_runs_id_engagement_kind" not in remaining_run_indexes
        assert "uq_runs_id_engagement_kind_node" not in remaining_run_indexes
        assert "uq_runs_id_status" not in remaining_run_indexes
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []

    run_alembic(database_path, "head")
    assert AUDIT_TABLES <= sqlite_tables(database_path)
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_audit_migration_compiles_for_postgresql_offline() -> None:
    sql = _offline_sql(
        "postgresql+asyncpg://riftx@localhost/riftx",
        f"{BASE_REVISION}:{AUDIT_REVISION}",
    )

    assert "CREATE UNIQUE INDEX uq_runs_id_engagement_kind" in sql
    assert "CREATE UNIQUE INDEX uq_runs_id_engagement_kind_node" in sql
    assert "CREATE UNIQUE INDEX uq_runs_id_status" in sql
    for table_name in AUDIT_TABLES:
        assert f"CREATE TABLE {table_name}" in sql
    assert "fk_audit_scans_run_owner_kind_node" in sql
    assert "fk_audit_scans_run_terminal_status" in sql
    assert "fk_audit_scans_contract_binding" in sql
    assert "canonical_contract_json TEXT NOT NULL" in sql


def test_audit_migration_refuses_postgresql_offline_downgrade() -> None:
    with pytest.raises(RuntimeError, match="requires an online database"):
        _offline_sql(
            "postgresql+asyncpg://riftx@localhost/riftx",
            f"{AUDIT_REVISION}:{BASE_REVISION}",
            downgrade=True,
        )


def test_audit_migration_schema_matches_orm_metadata(tmp_path: Path) -> None:
    database_path = tmp_path / "audit-metadata-parity.db"
    run_alembic(database_path, "head")
    engine = create_engine(f"sqlite:///{database_path}")
    try:
        inspector = inspect(engine)
        for table_name in AUDIT_TABLES:
            metadata_table = Base.metadata.tables[table_name]
            actual_columns = {
                column["name"]: (
                    column["nullable"],
                    getattr(column["type"], "length", None),
                )
                for column in inspector.get_columns(table_name)
            }
            expected_columns = {
                column.name: (column.nullable, getattr(column.type, "length", None))
                for column in metadata_table.columns
            }
            assert actual_columns == expected_columns

            actual_checks = {
                constraint["name"] for constraint in inspector.get_check_constraints(table_name)
            }
            expected_checks = {
                constraint.name
                for constraint in metadata_table.constraints
                if isinstance(constraint, CheckConstraint)
            }
            assert actual_checks == expected_checks

            actual_uniques = {
                tuple(constraint["column_names"])
                for constraint in inspector.get_unique_constraints(table_name)
            }
            expected_uniques = {
                tuple(column.name for column in constraint.columns)
                for constraint in metadata_table.constraints
                if isinstance(constraint, UniqueConstraint)
            }
            assert actual_uniques == expected_uniques

            actual_indexes = {
                (index["name"], tuple(index["column_names"]), index["unique"])
                for index in inspector.get_indexes(table_name)
            }
            expected_indexes = {
                (
                    index.name,
                    tuple(column.name for column in index.columns),
                    index.unique,
                )
                for index in metadata_table.indexes
            }
            assert actual_indexes == expected_indexes

            actual_foreign_keys = {
                (
                    tuple(constraint["constrained_columns"]),
                    constraint["referred_table"],
                    tuple(constraint["referred_columns"]),
                    constraint["options"].get("ondelete"),
                )
                for constraint in inspector.get_foreign_keys(table_name)
            }
            expected_foreign_keys = {
                (
                    tuple(element.parent.name for element in constraint.elements),
                    constraint.referred_table.name,
                    tuple(element.column.name for element in constraint.elements),
                    constraint.ondelete,
                )
                for constraint in metadata_table.constraints
                if isinstance(constraint, ForeignKeyConstraint)
            }
            assert actual_foreign_keys == expected_foreign_keys
    finally:
        engine.dispose()
