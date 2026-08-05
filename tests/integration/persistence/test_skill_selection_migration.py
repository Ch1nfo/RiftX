import sqlite3
from pathlib import Path

import pytest
from tests.integration.persistence.test_migrations import (
    downgrade_alembic,
    offline_downgrade_alembic,
    run_alembic,
    sqlite_tables,
)

PREVIOUS_REVISION = "7f2c8a1d4e90"
SKILL_REVISION = "9a4d6e2b7c11"
SKILL_TABLES = {"agent_skill_scopes", "agent_skill_selections"}


def _seed_session(database_path: Path) -> None:
    now = "2026-08-05 18:20:00+00:00"
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(
            "INSERT INTO engagements "
            "(id, name, description, authorization_reference, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("engagement-skill", "Skill", "", None, now, now),
        )
        connection.execute(
            "INSERT INTO runs "
            "(id, engagement_id, node_id, kind, objective, success_criteria_json, "
            "entry_points_json, scope_json, status, approval_mode, model_profile, "
            "workspace_path, temporal_workflow_id, created_at, started_at, finished_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "run-skill",
                "engagement-skill",
                "local",
                "general",
                "Skill migration",
                "[]",
                "[]",
                "{}",
                "running",
                "balanced",
                None,
                "/tmp/run-skill",
                None,
                now,
                now,
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
                "session-skill",
                "run-skill",
                None,
                "primary",
                "default",
                "active",
                None,
                None,
                0,
                0,
                0,
                now,
                None,
            ),
        )
        connection.commit()


def test_skill_selection_migration_guards_durable_session_facts(tmp_path: Path) -> None:
    database_path = tmp_path / "skills.db"
    run_alembic(database_path, PREVIOUS_REVISION)
    assert not SKILL_TABLES & sqlite_tables(database_path)
    run_alembic(database_path, SKILL_REVISION)
    assert SKILL_TABLES <= sqlite_tables(database_path)
    _seed_session(database_path)
    now = "2026-08-05 18:20:00+00:00"
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(
            "INSERT INTO agent_skill_scopes "
            "(session_id, run_id, agent_id, allowed_skill_ids_json, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("session-skill", "run-skill", "primary", "[]", now),
        )
        connection.commit()

    with pytest.raises(RuntimeError, match="durable Agent Skill Session facts exist"):
        downgrade_alembic(database_path, PREVIOUS_REVISION)
    assert SKILL_TABLES <= sqlite_tables(database_path)

    with sqlite3.connect(database_path) as connection:
        connection.execute("DELETE FROM agent_skill_scopes")
        connection.commit()
    downgrade_alembic(database_path, PREVIOUS_REVISION)
    assert not SKILL_TABLES & sqlite_tables(database_path)


def test_skill_selection_offline_downgrade_fails_closed() -> None:
    with pytest.raises(RuntimeError, match="offline downgrade"):
        offline_downgrade_alembic(f"{SKILL_REVISION}:{PREVIOUS_REVISION}")
