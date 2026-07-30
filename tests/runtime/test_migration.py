import os
import sqlite3
from pathlib import Path

from alembic import command
from alembic.config import Config

RUNTIME_TABLES = {
    "agent_sessions",
    "agent_cycles",
    "agent_steps",
    "provider_states",
    "tool_call_intents",
    "run_leases",
    "context_compilations",
    "context_checkpoints",
    "memories",
    "working_memories",
    "runtime_approval_requests",
    "user_input_requests",
    "web_documents",
    "web_document_chunks",
    "web_search_queries",
    "web_search_results",
    "web_research_notes",
    "web_research_packets",
    "source_references",
    "target_http_requests",
}


def run_alembic(database_path: Path, revision: str) -> None:
    config = Config("alembic.ini")
    old_url = os.environ.get("RIFTX_DATABASE_URL")
    os.environ["RIFTX_DATABASE_URL"] = f"sqlite+aiosqlite:///{database_path}"
    try:
        command.upgrade(config, revision) if revision != "base" else command.downgrade(
            config, revision
        )
    finally:
        if old_url is None:
            os.environ.pop("RIFTX_DATABASE_URL", None)
        else:
            os.environ["RIFTX_DATABASE_URL"] = old_url


def sqlite_tables(database_path: Path) -> set[str]:
    with sqlite3.connect(database_path) as connection:
        rows = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    return {row[0] for row in rows}


def sqlite_columns(database_path: Path, table: str) -> set[str]:
    with sqlite3.connect(database_path) as connection:
        rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
    return {row[1] for row in rows}


def test_runtime_migration_upgrades_and_downgrades_existing_v2_database(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "existing-v2.db"
    run_alembic(database_path, "d4f26a8b7c10")
    now = "2026-07-30 00:00:00+00:00"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "INSERT INTO engagements "
            "(id, name, description, authorization_reference, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("engagement-1", "Existing", "", None, now, now),
        )
        connection.execute(
            "INSERT INTO runs "
            "(id, engagement_id, node_id, objective, success_criteria_json, "
            "entry_points_json, scope_json, status, approval_mode, model_profile, "
            "workspace_path, temporal_workflow_id, created_at, started_at, finished_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "run-1",
                "engagement-1",
                "local",
                "Existing V2 run",
                "[]",
                "[]",
                "{}",
                "completed",
                "balanced",
                "default",
                "/tmp/run-1",
                "workflow-1",
                now,
                now,
                now,
            ),
        )
        connection.execute(
            "INSERT INTO agent_messages "
            "(id, run_id, role, message_type, content, sequence, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "message-1",
                "run-1",
                "user",
                "message",
                '{"role":"user","content":"legacy"}',
                1,
                now,
            ),
        )
        connection.commit()

    run_alembic(database_path, "head")
    assert RUNTIME_TABLES <= sqlite_tables(database_path)
    assert {"session_id", "tool_call_id", "attempt_group"} <= sqlite_columns(
        database_path, "executions"
    )
    assert {
        "manifest_json",
        "estimated_tokens",
        "actual_input_tokens",
        "actual_output_tokens",
    } <= sqlite_columns(database_path, "context_compilations")
    assert {"version", "state_json", "updated_at"} <= sqlite_columns(
        database_path, "working_memories"
    )
    assert {"waiting_object_id", "checkpoint_id"} <= sqlite_columns(database_path, "agent_cycles")
    assert "execution_spec_json" in sqlite_columns(database_path, "tool_call_intents")
    assert {
        "tool_call_intent_id",
        "context_compilation_id",
        "working_memory_version",
        "provider_state_id",
        "decision",
    } <= sqlite_columns(database_path, "runtime_approval_requests")
    assert {
        "prompt",
        "context_compilation_id",
        "working_memory_version",
        "provider_state_id",
        "response_message_id",
    } <= sqlite_columns(database_path, "user_input_requests")
    assert {
        "runner_id",
        "shell",
        "cwd",
        "output_cursor",
        "takeover_cursor",
        "takeover_started_at",
        "transcript_artifact_id",
    } <= sqlite_columns(database_path, "terminal_sessions")
    assert {
        "memory_type",
        "scope_type",
        "scope_id",
        "content",
        "summary",
        "source_refs_json",
        "valid_until",
        "supersedes",
        "status",
        "pinned",
    } <= sqlite_columns(database_path, "memories")
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT objective FROM runs WHERE id = 'run-1'").fetchone() == (
            "Existing V2 run",
        )
        assert connection.execute(
            "SELECT id, run_id, status FROM agent_sessions WHERE id = 'run-1'"
        ).fetchone() == ("run-1", "run-1", "active")
        assert connection.execute(
            "SELECT session_id, agent_id, message_type, visibility "
            "FROM agent_messages WHERE id = 'message-1'"
        ).fetchone() == ("run-1", "primary", "user_message", "user_visible")

    config = Config("alembic.ini")
    old_url = os.environ.get("RIFTX_DATABASE_URL")
    os.environ["RIFTX_DATABASE_URL"] = f"sqlite+aiosqlite:///{database_path}"
    try:
        command.downgrade(config, "d4f26a8b7c10")
    finally:
        if old_url is None:
            os.environ.pop("RIFTX_DATABASE_URL", None)
        else:
            os.environ["RIFTX_DATABASE_URL"] = old_url

    assert not (RUNTIME_TABLES & sqlite_tables(database_path))
    assert not (
        {"session_id", "tool_call_id", "attempt_group"}
        & sqlite_columns(database_path, "executions")
    )
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM runs").fetchone() == (1,)
        assert connection.execute(
            "SELECT run_id, content FROM agent_messages WHERE id = 'message-1'"
        ).fetchone() == ("run-1", '{"role":"user","content":"legacy"}')
