import io
import os
import sqlite3
from pathlib import Path

from alembic import command
from alembic.config import Config
from tests.integration.persistence.test_migrations import (
    _run_alembic_with_sqlite_foreign_keys,
    run_alembic,
)

BASE_REVISION = "d5e7f9a1b304"
DECISION_REVISION = "e6f8a0b2c415"
NOW = "2026-08-01 18:00:00+00:00"
LATER = "2026-08-01 18:05:00+00:00"


def _offline_sql(url: str, revision_range: str, *, downgrade: bool = False) -> str:
    output = io.StringIO()
    config = Config("alembic.ini", output_buffer=output)
    old_url = os.environ.get("RIFTX_DATABASE_URL")
    os.environ["RIFTX_DATABASE_URL"] = url
    try:
        if downgrade:
            command.downgrade(config, revision_range, sql=True)
        else:
            command.upgrade(config, revision_range, sql=True)
    finally:
        if old_url is None:
            os.environ.pop("RIFTX_DATABASE_URL", None)
        else:
            os.environ["RIFTX_DATABASE_URL"] = old_url
    return output.getvalue()


def _seed_legacy_approval_rows(database_path: Path) -> None:
    approval_rows = (
        # A terminal Runtime row outranks even a matching Run-wide grant.
        (
            "approval-runtime-once",
            "approved",
            "request reason",
            "operator",
            NOW,
        ),
        (
            "approval-runtime-run",
            "approved",
            "request reason",
            "operator",
            NOW,
        ),
        # Runtime REJECT outranks a non-blank public reason.
        (
            "approval-runtime-reject",
            "rejected",
            "would otherwise look like feedback",
            "operator",
            NOW,
        ),
        (
            "approval-runtime-reject-feedback",
            "rejected",
            "stale public reason",
            "operator",
            NOW,
        ),
        ("approval-grant", "approved", "request reason", "operator", NOW),
        ("approval-once", "approved", "request reason", "operator", NOW),
        # A later decision for the same Run/Tool creates the only surviving
        # grant.  The grant cannot prove either public Approval's scope.
        ("approval-ambiguous-once", "approved", "request reason", "operator", NOW),
        (
            "approval-ambiguous-run",
            "approved",
            "request reason",
            "operator",
            LATER,
        ),
        (
            "approval-reject-feedback",
            "rejected",
            "  denied by operator  ",
            "operator",
            NOW,
        ),
        ("approval-reject", "rejected", "   ", "operator", NOW),
        ("approval-pending", "pending", "request reason", None, None),
        ("approval-cancelled", "cancelled", "request reason", "operator", NOW),
    )
    runtime_rows = (
        (
            "approval-runtime-once",
            "approved",
            "approve_once",
            "runtime once feedback",
        ),
        (
            "approval-runtime-run",
            "approved",
            "approve_tool_for_run",
            "runtime run feedback",
        ),
        ("approval-runtime-reject", "rejected", "reject", None),
        (
            "approval-runtime-reject-feedback",
            "rejected",
            "reject_with_feedback",
            "runtime rejection feedback",
        ),
    )

    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        assert connection.execute("PRAGMA foreign_keys").fetchone() == (1,)
        connection.execute(
            "INSERT INTO engagements "
            "(id, name, description, authorization_reference, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("engagement-approval", "Approval migration", "", None, NOW, NOW),
        )
        connection.execute(
            "INSERT INTO runs "
            "(id, engagement_id, node_id, objective, success_criteria_json, "
            "entry_points_json, scope_json, status, approval_mode, model_profile, "
            "workspace_path, temporal_workflow_id, created_at, started_at, finished_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "run-approval",
                "engagement-approval",
                "local",
                "Approval migration",
                "[]",
                "[]",
                "{}",
                "running",
                "balanced",
                None,
                "/tmp/run-approval",
                None,
                NOW,
                NOW,
                None,
            ),
        )
        connection.execute(
            "INSERT INTO agent_sessions "
            "(id, run_id, parent_session_id, agent_type, model_profile, status, "
            "latest_checkpoint_id, provider_state_id, turn_count, model_call_count, "
            "tool_call_count, created_at, closed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "session-approval",
                "run-approval",
                None,
                "primary",
                "default",
                "active",
                None,
                None,
                0,
                0,
                0,
                NOW,
                None,
            ),
        )
        connection.execute(
            "INSERT INTO agent_cycles "
            "(id, run_id, session_id, sequence, status, yield_reason, waiting_object_id, "
            "checkpoint_id, model_call_count, tool_call_count, started_at, finished_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "cycle-approval",
                "run-approval",
                "session-approval",
                1,
                "running",
                None,
                None,
                None,
                0,
                0,
                NOW,
                None,
            ),
        )
        connection.execute(
            "INSERT INTO agent_steps "
            "(id, cycle_id, sequence, step_type, status, input_refs_json, output_refs_json, "
            "started_at, finished_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "step-approval",
                "cycle-approval",
                1,
                "tool_proposal",
                "completed",
                "[]",
                "[]",
                NOW,
                NOW,
            ),
        )

        for approval_id, status, reason, decided_by, decided_at in approval_rows:
            tool_id = (
                "tool-ambiguous"
                if approval_id in {"approval-ambiguous-once", "approval-ambiguous-run"}
                else f"tool-{approval_id}"
            )
            tool_call_id = f"call-{approval_id}"
            connection.execute(
                "INSERT INTO tool_calls "
                "(id, sdk_call_id, run_id, agent_step_id, tool_id, skill_id, "
                "arguments_json, approval_status, execution_id, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    tool_call_id,
                    f"sdk-{approval_id}",
                    "run-approval",
                    "step-approval",
                    tool_id,
                    None,
                    "{}",
                    status,
                    None,
                    decided_at or NOW,
                ),
            )
            connection.execute(
                "INSERT INTO approvals "
                "(id, run_id, tool_call_id, status, tool_name, command_json, cwd, "
                "target_summary, env_diff_json, reason, decided_by, created_at, decided_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    approval_id,
                    "run-approval",
                    tool_call_id,
                    status,
                    tool_id,
                    "[]",
                    "/tmp/run-approval",
                    "local target",
                    "{}",
                    reason,
                    decided_by,
                    decided_at or NOW,
                    decided_at,
                ),
            )

        for approval_id, status, decision, feedback in runtime_rows:
            intent_id = f"intent-{approval_id}"
            connection.execute(
                "INSERT INTO tool_call_intents "
                "(id, run_id, session_id, cycle_id, step_id, tool_id, skill_id, "
                "arguments_json, command_preview, reason, target_summary, approval_level, "
                "status, engine_call_id, execution_spec_json, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    intent_id,
                    "run-approval",
                    "session-approval",
                    "cycle-approval",
                    "step-approval",
                    f"tool-{approval_id}",
                    None,
                    "{}",
                    "",
                    "runtime approval",
                    None,
                    "sensitive",
                    "ready" if status == "approved" else "rejected",
                    f"engine-{approval_id}",
                    None,
                    NOW,
                    NOW,
                ),
            )
            connection.execute(
                "INSERT INTO runtime_approval_requests "
                "(id, run_id, session_id, cycle_id, tool_call_intent_id, "
                "context_compilation_id, working_memory_version, provider_state_id, status, "
                "decision, feedback, decided_by, created_at, decided_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    approval_id,
                    "run-approval",
                    "session-approval",
                    "cycle-approval",
                    intent_id,
                    None,
                    None,
                    None,
                    status,
                    decision,
                    feedback,
                    "runtime-operator",
                    NOW,
                    NOW,
                ),
            )

        for approval_id, tool_id, created_at in (
            (
                "approval-runtime-once",
                "tool-approval-runtime-once",
                NOW,
            ),
            ("approval-grant", "tool-approval-grant", NOW),
            ("approval-ambiguous-run", "tool-ambiguous", LATER),
        ):
            connection.execute(
                "INSERT INTO approval_grants "
                "(id, run_id, tool_id, created_by, created_at) VALUES (?, ?, ?, ?, ?)",
                (
                    f"grant-{approval_id}",
                    "run-approval",
                    tool_id,
                    "grant-operator",
                    created_at,
                ),
            )
        connection.commit()


def _approval_foreign_keys(connection: sqlite3.Connection) -> set[tuple[str, str, str, str]]:
    return {
        (row[2], row[3], row[4], row[6])
        for row in connection.execute("PRAGMA foreign_key_list(approvals)").fetchall()
    }


def test_approval_decision_migration_backfills_precedence_and_preserves_foreign_keys(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "approval-decisions.db"
    run_alembic(database_path, BASE_REVISION)
    _seed_legacy_approval_rows(database_path)

    _run_alembic_with_sqlite_foreign_keys(database_path, DECISION_REVISION)

    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        decisions = {
            row[0]: row[1:]
            for row in connection.execute(
                "SELECT id, decision, decision_feedback FROM approvals ORDER BY id"
            ).fetchall()
        }
        columns = {
            row[1]: (row[2], row[3], row[4])
            for row in connection.execute("PRAGMA table_info(approvals)").fetchall()
        }
        foreign_keys = _approval_foreign_keys(connection)
        violations = connection.execute("PRAGMA foreign_key_check").fetchall()

    assert decisions == {
        "approval-cancelled": (None, None),
        "approval-ambiguous-once": ("approve_once", None),
        "approval-ambiguous-run": ("approve_once", None),
        "approval-grant": ("approve_once", None),
        "approval-once": ("approve_once", None),
        "approval-pending": (None, None),
        "approval-reject": ("reject", None),
        "approval-reject-feedback": (
            "reject_with_feedback",
            "  denied by operator  ",
        ),
        "approval-runtime-once": ("approve_once", "runtime once feedback"),
        "approval-runtime-reject": ("reject", None),
        "approval-runtime-reject-feedback": (
            "reject_with_feedback",
            "runtime rejection feedback",
        ),
        "approval-runtime-run": (
            "approve_tool_for_run",
            "runtime run feedback",
        ),
    }
    assert columns["decision"] == ("VARCHAR(32)", 0, None)
    assert columns["decision_feedback"] == ("TEXT", 0, None)
    assert foreign_keys == {
        ("runs", "run_id", "id", "CASCADE"),
        ("tool_calls", "tool_call_id", "id", "CASCADE"),
    }
    assert violations == []

    _run_alembic_with_sqlite_foreign_keys(
        database_path,
        BASE_REVISION,
        downgrade=True,
    )

    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        downgraded_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(approvals)").fetchall()
        }
        preserved_counts = (
            connection.execute("SELECT count(*) FROM approvals").fetchone()[0],
            connection.execute("SELECT count(*) FROM runtime_approval_requests").fetchone()[0],
            connection.execute("SELECT count(*) FROM approval_grants").fetchone()[0],
        )
        downgraded_foreign_keys = _approval_foreign_keys(connection)
        downgrade_violations = connection.execute("PRAGMA foreign_key_check").fetchall()

    assert "decision" not in downgraded_columns
    assert "decision_feedback" not in downgraded_columns
    assert preserved_counts == (12, 4, 3)
    assert downgraded_foreign_keys == foreign_keys
    assert downgrade_violations == []


def test_approval_decision_migration_compiles_for_postgresql_offline() -> None:
    url = "postgresql+asyncpg://riftx@localhost/riftx"
    upgrade_sql = _offline_sql(url, f"{BASE_REVISION}:{DECISION_REVISION}")
    downgrade_sql = _offline_sql(
        url,
        f"{DECISION_REVISION}:{BASE_REVISION}",
        downgrade=True,
    )

    assert "ALTER TABLE approvals ADD COLUMN decision VARCHAR(32);" in upgrade_sql
    assert "ALTER TABLE approvals ADD COLUMN decision_feedback TEXT;" in upgrade_sql
    assert "SELECT runtime_approval_requests.decision" in upgrade_sql
    assert "SELECT runtime_approval_requests.feedback" in upgrade_sql
    assert "TRIM(COALESCE(approvals.reason, ''))" in upgrade_sql

    runtime_position = upgrade_sql.index("SET decision = (")
    once_position = upgrade_sql.index("SET decision = 'approve_once'")
    reject_position = upgrade_sql.index("SET decision = CASE")
    assert runtime_position < once_position < reject_position
    assert "JOIN approval_grants" not in upgrade_sql

    feedback_drop = "ALTER TABLE approvals DROP COLUMN decision_feedback;"
    decision_drop = "ALTER TABLE approvals DROP COLUMN decision;"
    assert feedback_drop in downgrade_sql
    assert decision_drop in downgrade_sql
    assert downgrade_sql.index(feedback_drop) < downgrade_sql.index(decision_drop)
