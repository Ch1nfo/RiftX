from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event, inspect
from sqlalchemy.engine import Engine
from tests.integration.persistence.test_audit_migration import (
    NOW,
    _insert_engagement,
    _insert_run,
)
from tests.integration.persistence.test_migrations import (
    _run_alembic_with_sqlite_foreign_keys,
    downgrade_alembic,
    offline_downgrade_alembic,
    run_alembic,
)
from tests.integration.persistence.test_mutation_clock_migration import _offline_sql

from riftx.persistence.orm import Base

BASE_REVISION = "7c4e1a9b2d06"
ARTIFACT_REVISION = "91e6f4a2c8b7"


def _insert_legacy_artifact_graph(
    connection: sqlite3.Connection,
    *,
    run_kind: str = "general",
    artifact_name: str = "result.json",
    mime_type: str = "application/json",
    execution_id: str | None = None,
) -> None:
    _insert_engagement(connection, "engagement-artifact")
    _insert_run(
        connection,
        run_id="run-artifact",
        engagement_id="engagement-artifact",
        kind=run_kind,
        workflow_id="workflow-artifact",
    )
    if execution_id is not None:
        connection.execute(
            "INSERT INTO executions "
            "(id, execution_key, run_id, node_id, executor_type, argv_json, cwd, "
            "env_diff_json, status, stdout_path, stderr_path, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                execution_id,
                f"{execution_id}-key",
                "run-artifact",
                "node-1",
                "process",
                "[]",
                "/tmp/run-artifact",
                "{}",
                "running",
                "/tmp/stdout.log",
                "/tmp/stderr.log",
                NOW,
                NOW,
            ),
        )
    connection.execute(
        "INSERT INTO artifacts "
        "(id, run_id, execution_id, name, path, mime_type, sha256, size, "
        "description, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "artifact-legacy",
            "run-artifact",
            execution_id,
            artifact_name,
            "/legacy/private/result.json",
            mime_type,
            "a" * 64,
            17,
            "Legacy result",
            NOW,
        ),
    )


def _columns(connection: sqlite3.Connection) -> dict[str, tuple[object, ...]]:
    return {row[1]: row for row in connection.execute("PRAGMA table_info(artifacts)").fetchall()}


def test_artifact_access_migration_backfills_legacy_rows_and_restarts(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "artifact-backfill.db"
    run_alembic(database_path, BASE_REVISION)
    with sqlite3.connect(database_path) as connection:
        _insert_legacy_artifact_graph(connection)
        connection.commit()

    run_alembic(database_path, "head")

    with sqlite3.connect(database_path) as connection:
        row = connection.execute(
            "SELECT audit_id, access_class, content_trust, storage_key, "
            "ingest_provenance_json, path FROM artifacts "
            "WHERE id = 'artifact-legacy'"
        ).fetchone()
        assert row is not None
        audit_id, access_class, content_trust, storage_key, provenance_json, path = row
        assert audit_id is None
        assert access_class == "public_export"
        assert content_trust == "untrusted_tool_output"
        assert storage_key == ("runs/run-artifact/artifacts/artifact-legacy/result.json")
        assert json.loads(provenance_json) == {
            "schema_version": "riftx.artifact-ingest-provenance/v1",
            "method": "legacy_migrated",
            "producer_node_id": None,
            "producer_execution_id": None,
        }
        assert path == "/legacy/private/result.json"
        columns = _columns(connection)
        for column_name in (
            "access_class",
            "content_trust",
            "storage_key",
            "ingest_provenance_json",
        ):
            assert columns[column_name][3] == 1
            assert columns[column_name][4] is None
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []

    # A second process opening the migrated database observes the same durable facts.
    with sqlite3.connect(database_path) as restarted:
        assert restarted.execute(
            "SELECT storage_key FROM artifacts WHERE id = 'artifact-legacy'"
        ).fetchone() == ("runs/run-artifact/artifacts/artifact-legacy/result.json",)


def test_artifact_access_constraints_reject_unsafe_raw_metadata_writes(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "artifact-raw-metadata-constraints.db"
    run_alembic(database_path, BASE_REVISION)
    with sqlite3.connect(database_path) as connection:
        _insert_legacy_artifact_graph(connection)
        connection.commit()
    run_alembic(database_path, "head")

    provenance_json = json.dumps(
        {
            "schema_version": "riftx.artifact-ingest-provenance/v1",
            "method": "legacy_migrated",
            "producer_node_id": None,
            "producer_execution_id": None,
        }
    )
    with sqlite3.connect(database_path) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE artifacts SET mime_type = ? WHERE id = 'artifact-legacy'",
                (" text/plain",),
            )
        connection.rollback()

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO artifacts "
                "(id, run_id, execution_id, name, path, mime_type, sha256, size, "
                "description, created_at, audit_id, access_class, content_trust, "
                "storage_key, ingest_provenance_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "artifact-invalid-mime",
                    "run-artifact",
                    None,
                    "invalid-mime.json",
                    "/legacy/private/invalid-mime.json",
                    "application/\u2624",
                    "b" * 64,
                    18,
                    "Invalid MIME",
                    NOW,
                    None,
                    "public_export",
                    "untrusted_tool_output",
                    "runs/run-artifact/artifacts/artifact-invalid-mime/invalid-mime.json",
                    provenance_json,
                ),
            )
        connection.rollback()

        oversized_name = "n" * 256
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO artifacts "
                "(id, run_id, execution_id, name, path, mime_type, sha256, size, "
                "description, created_at, audit_id, access_class, content_trust, "
                "storage_key, ingest_provenance_json) "
                "SELECT ?, run_id, execution_id, ?, path, mime_type, sha256, size, "
                "description, created_at, audit_id, access_class, content_trust, ?, "
                "ingest_provenance_json FROM artifacts WHERE id = 'artifact-legacy'",
                (
                    "artifact-oversized-name",
                    oversized_name,
                    f"runs/run-artifact/artifacts/artifact-oversized-name/{oversized_name}",
                ),
            )
        connection.rollback()

        unsafe_name = "a\u202eb"
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE artifacts SET name = ?, storage_key = ? WHERE id = 'artifact-legacy'",
                (
                    unsafe_name,
                    f"runs/run-artifact/artifacts/artifact-legacy/{unsafe_name}",
                ),
            )
        connection.rollback()

        assert connection.execute(
            "SELECT name, mime_type FROM artifacts WHERE id = 'artifact-legacy'"
        ).fetchone() == ("result.json", "application/json")
        assert (
            connection.execute(
                "SELECT 1 FROM artifacts WHERE id = 'artifact-invalid-mime'"
            ).fetchone()
            is None
        )
        assert (
            connection.execute(
                "SELECT 1 FROM artifacts WHERE id = 'artifact-oversized-name'"
            ).fetchone()
            is None
        )


def test_artifact_access_migration_rejects_legacy_code_audit_before_ddl(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "artifact-code-audit-rejected.db"
    run_alembic(database_path, BASE_REVISION)
    with sqlite3.connect(database_path) as connection:
        _insert_legacy_artifact_graph(connection, run_kind="code_audit")
        connection.commit()

    with pytest.raises(RuntimeError, match="code_audit Runs"):
        run_alembic(database_path, "head")

    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
            BASE_REVISION,
        )
        assert "storage_key" not in _columns(connection)
        assert connection.execute(
            "SELECT id FROM artifacts WHERE id = 'artifact-legacy'"
        ).fetchone() == ("artifact-legacy",)


def test_artifact_access_migration_rejects_cross_run_execution_owner_before_ddl(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "artifact-cross-run-execution.db"
    run_alembic(database_path, BASE_REVISION)
    with sqlite3.connect(database_path) as connection:
        _insert_legacy_artifact_graph(connection)
        _insert_run(
            connection,
            run_id="run-other",
            engagement_id="engagement-artifact",
            kind="general",
            workflow_id="workflow-other",
        )
        connection.execute(
            "INSERT INTO executions "
            "(id, execution_key, run_id, node_id, executor_type, argv_json, cwd, "
            "env_diff_json, status, stdout_path, stderr_path, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "execution-other",
                "execution-other-key",
                "run-other",
                "node-1",
                "process",
                "[]",
                "/tmp/run-other",
                "{}",
                "running",
                "/tmp/other-stdout.log",
                "/tmp/other-stderr.log",
                NOW,
                NOW,
            ),
        )
        connection.execute(
            "UPDATE artifacts SET execution_id = ? WHERE id = 'artifact-legacy'",
            ("execution-other",),
        )
        connection.commit()

    with pytest.raises(RuntimeError, match="cross-Run Execution owner"):
        run_alembic(database_path, "head")

    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
            BASE_REVISION,
        )
        assert "storage_key" not in _columns(connection)


@pytest.mark.parametrize(
    "unsafe_name",
    [
        ".",
        "..",
        "a/b",
        "a\\b",
        "a\nb",
        "a\x00b",
        "a\u0080b",
        "a\u202eb",
        "a\ue000b",
        "n" * 256,
    ],
)
def test_artifact_access_migration_rejects_unsafe_legacy_components(
    tmp_path: Path,
    unsafe_name: str,
) -> None:
    database_path = tmp_path / "artifact-unsafe.db"
    run_alembic(database_path, BASE_REVISION)
    with sqlite3.connect(database_path) as connection:
        _insert_legacy_artifact_graph(connection, artifact_name=unsafe_name)
        connection.commit()

    with pytest.raises(RuntimeError, match="unsafe legacy Artifact"):
        run_alembic(database_path, "head")

    with sqlite3.connect(database_path) as connection:
        assert "storage_key" not in _columns(connection)


@pytest.mark.parametrize(
    "unsafe_mime_type",
    ["application/\u2624", " text/plain", "text/plain ", "", "x" * 256],
)
def test_artifact_access_migration_rejects_unsafe_legacy_mime_type_before_ddl(
    tmp_path: Path,
    unsafe_mime_type: str,
) -> None:
    database_path = tmp_path / "artifact-unsafe-mime.db"
    run_alembic(database_path, BASE_REVISION)
    with sqlite3.connect(database_path) as connection:
        _insert_legacy_artifact_graph(connection, mime_type=unsafe_mime_type)
        connection.commit()

    with pytest.raises(RuntimeError, match="unsafe legacy Artifact metadata"):
        run_alembic(database_path, "head")

    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
            BASE_REVISION,
        )
        assert "storage_key" not in _columns(connection)
        assert connection.execute(
            "SELECT mime_type FROM artifacts WHERE id = 'artifact-legacy'"
        ).fetchone() == (unsafe_mime_type,)


def test_artifact_access_migration_lossless_downgrade_and_reupgrade(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "artifact-downgrade.db"
    run_alembic(database_path, BASE_REVISION)
    with sqlite3.connect(database_path) as connection:
        _insert_legacy_artifact_graph(connection)
        connection.commit()
    run_alembic(database_path, "head")

    downgrade_alembic(database_path, BASE_REVISION)
    with sqlite3.connect(database_path) as connection:
        assert "storage_key" not in _columns(connection)
        assert connection.execute(
            "SELECT path FROM artifacts WHERE id = 'artifact-legacy'"
        ).fetchone() == ("/legacy/private/result.json",)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []

    run_alembic(database_path, "head")
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT access_class FROM artifacts WHERE id = 'artifact-legacy'"
        ).fetchone() == ("public_export",)


@pytest.mark.parametrize(
    ("assignment", "value"),
    [
        ("content_trust = ?", "generated"),
        (
            "ingest_provenance_json = ?",
            json.dumps(
                {
                    "schema_version": "riftx.artifact-ingest-provenance/v1",
                    "method": "control_plane_bytes",
                    "producer_node_id": None,
                    "producer_execution_id": None,
                }
            ),
        ),
    ],
)
def test_artifact_access_downgrade_refuses_new_security_facts_before_ddl(
    tmp_path: Path,
    assignment: str,
    value: str,
) -> None:
    database_path = tmp_path / "artifact-downgrade-rejected.db"
    run_alembic(database_path, BASE_REVISION)
    with sqlite3.connect(database_path) as connection:
        _insert_legacy_artifact_graph(connection)
        connection.commit()
    run_alembic(database_path, "head")
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            f"UPDATE artifacts SET {assignment} WHERE id = 'artifact-legacy'",
            (value,),
        )
        connection.commit()

    with pytest.raises(RuntimeError, match="cannot downgrade Artifact access"):
        downgrade_alembic(database_path, BASE_REVISION)

    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
            ARTIFACT_REVISION,
        )
        assert "storage_key" in _columns(connection)


def test_artifact_access_downgrade_rejects_corrupt_mime_before_ddl(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "artifact-downgrade-corrupt-mime.db"
    run_alembic(database_path, BASE_REVISION)
    with sqlite3.connect(database_path) as connection:
        _insert_legacy_artifact_graph(connection)
        connection.commit()
    run_alembic(database_path, "head")
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA ignore_check_constraints=ON")
        connection.execute(
            "UPDATE artifacts SET mime_type = ? WHERE id = 'artifact-legacy'",
            ("application/\u2624",),
        )
        connection.commit()
        connection.execute("PRAGMA ignore_check_constraints=OFF")

    with pytest.raises(RuntimeError, match="cannot downgrade Artifact access"):
        downgrade_alembic(database_path, BASE_REVISION)

    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
            ARTIFACT_REVISION,
        )
        assert "storage_key" in _columns(connection)
        assert connection.execute(
            "SELECT mime_type FROM artifacts WHERE id = 'artifact-legacy'"
        ).fetchone() == ("application/\u2624",)


def test_artifact_access_downgrade_rejects_cross_run_execution_owner(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "artifact-downgrade-cross-run-execution.db"
    run_alembic(database_path, BASE_REVISION)
    with sqlite3.connect(database_path) as connection:
        _insert_legacy_artifact_graph(connection)
        _insert_run(
            connection,
            run_id="run-other",
            engagement_id="engagement-artifact",
            kind="general",
            workflow_id="workflow-other",
        )
        connection.execute(
            "INSERT INTO executions "
            "(id, execution_key, run_id, node_id, executor_type, argv_json, cwd, "
            "env_diff_json, status, stdout_path, stderr_path, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "execution-other",
                "execution-other-key",
                "run-other",
                "node-1",
                "process",
                "[]",
                "/tmp/run-other",
                "{}",
                "running",
                "/tmp/other-stdout.log",
                "/tmp/other-stderr.log",
                NOW,
                NOW,
            ),
        )
        connection.commit()
    run_alembic(database_path, "head")
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "UPDATE artifacts SET execution_id = ? WHERE id = 'artifact-legacy'",
            ("execution-other",),
        )
        connection.commit()

    with pytest.raises(RuntimeError, match="cross-Run Execution ownership"):
        downgrade_alembic(database_path, BASE_REVISION)

    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
            ARTIFACT_REVISION,
        )
        assert "storage_key" in _columns(connection)


@pytest.mark.parametrize(
    ("phase", "pause_prefix"),
    [
        (
            "preflight",
            "select 1 from artifacts as a join runs as r on r.id = a.run_id",
        ),
        ("backfill", "update artifacts set audit_id = null"),
        ("batch_ddl", "create table _alembic_tmp_artifacts"),
    ],
)
def test_artifact_upgrade_holds_exclusive_lock_across_every_phase(
    tmp_path: Path,
    phase: str,
    pause_prefix: str,
) -> None:
    database_path = tmp_path / f"artifact-upgrade-{phase}-lock.db"
    run_alembic(database_path, BASE_REVISION)
    with sqlite3.connect(database_path) as connection:
        _insert_legacy_artifact_graph(connection)
        connection.commit()

    phase_reached = threading.Event()
    release_migration = threading.Event()
    migration_errors: list[Exception] = []

    def pause_in_phase(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        normalized = " ".join(statement.replace('"', "").split()).lower()
        if normalized.startswith(pause_prefix):
            phase_reached.set()
            if not release_migration.wait(timeout=15):
                raise RuntimeError(f"timed out waiting to release {phase}")

    def migrate() -> None:
        try:
            _run_alembic_with_sqlite_foreign_keys(database_path, "head")
        except Exception as error:  # pragma: no cover - surfaced below
            migration_errors.append(error)

    event.listen(Engine, "before_cursor_execute", pause_in_phase)
    migration_thread = threading.Thread(target=migrate, daemon=True)
    reached = False
    try:
        migration_thread.start()
        reached = phase_reached.wait(timeout=15)
        if reached:
            with sqlite3.connect(database_path, timeout=0.05) as competitor:
                with pytest.raises(sqlite3.OperationalError, match="database is locked"):
                    competitor.execute(
                        "UPDATE artifacts SET name = 'racing.json' WHERE id = 'artifact-legacy'"
                    )
                competitor.rollback()
    finally:
        release_migration.set()
        migration_thread.join(timeout=15)
        event.remove(Engine, "before_cursor_execute", pause_in_phase)

    assert not migration_thread.is_alive()
    if migration_errors:
        raise migration_errors[0]
    assert reached

    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
            ARTIFACT_REVISION,
        )
        assert connection.execute(
            "SELECT name, storage_key FROM artifacts WHERE id = 'artifact-legacy'"
        ).fetchone() == (
            "result.json",
            "runs/run-artifact/artifacts/artifact-legacy/result.json",
        )
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_artifact_downgrade_holds_exclusive_lock_through_batch_ddl(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "artifact-downgrade-lock.db"
    run_alembic(database_path, BASE_REVISION)
    with sqlite3.connect(database_path) as connection:
        _insert_legacy_artifact_graph(connection)
        connection.commit()
    _run_alembic_with_sqlite_foreign_keys(database_path, "head")

    batch_ddl_reached = threading.Event()
    release_migration = threading.Event()
    migration_errors: list[Exception] = []

    def pause_before_batch_ddl(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        normalized = " ".join(statement.replace('"', "").split()).lower()
        if normalized.startswith("create table _alembic_tmp_artifacts"):
            batch_ddl_reached.set()
            if not release_migration.wait(timeout=15):
                raise RuntimeError("timed out waiting to release Artifact migration")

    def migrate() -> None:
        try:
            _run_alembic_with_sqlite_foreign_keys(
                database_path,
                BASE_REVISION,
                downgrade=True,
            )
        except Exception as error:  # pragma: no cover - surfaced below
            migration_errors.append(error)

    event.listen(Engine, "before_cursor_execute", pause_before_batch_ddl)
    migration_thread = threading.Thread(target=migrate, daemon=True)
    reached = False
    try:
        migration_thread.start()
        reached = batch_ddl_reached.wait(timeout=15)
        if reached:
            with sqlite3.connect(database_path, timeout=0.05) as competitor:
                with pytest.raises(sqlite3.OperationalError, match="database is locked"):
                    competitor.execute(
                        "UPDATE artifacts SET content_trust = 'generated' "
                        "WHERE id = 'artifact-legacy'"
                    )
                competitor.rollback()
    finally:
        release_migration.set()
        migration_thread.join(timeout=15)
        event.remove(Engine, "before_cursor_execute", pause_before_batch_ddl)

    assert not migration_thread.is_alive()
    if migration_errors:
        raise migration_errors[0]
    assert reached

    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
            BASE_REVISION,
        )
        assert "content_trust" not in _columns(connection)
        assert connection.execute(
            "SELECT path FROM artifacts WHERE id = 'artifact-legacy'"
        ).fetchone() == ("/legacy/private/result.json",)

    run_alembic(database_path, "head")
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT content_trust FROM artifacts WHERE id = 'artifact-legacy'"
        ).fetchone() == ("untrusted_tool_output",)


def test_artifact_upgrade_fault_rolls_back_and_can_retry(tmp_path: Path) -> None:
    database_path = tmp_path / "artifact-upgrade-fault.db"
    run_alembic(database_path, BASE_REVISION)
    with sqlite3.connect(database_path) as connection:
        _insert_legacy_artifact_graph(connection)
        connection.commit()

    fault_injected = False

    def fail_after_partial_column_ddl(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        nonlocal fault_injected
        normalized = " ".join(statement.replace('"', "").split()).lower()
        if normalized.startswith("alter table artifacts add column content_trust"):
            fault_injected = True
            raise RuntimeError("injected Artifact upgrade fault")

    event.listen(Engine, "before_cursor_execute", fail_after_partial_column_ddl)
    try:
        with pytest.raises(RuntimeError, match="injected Artifact upgrade fault"):
            _run_alembic_with_sqlite_foreign_keys(database_path, "head")
    finally:
        event.remove(Engine, "before_cursor_execute", fail_after_partial_column_ddl)
    assert fault_injected

    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
            BASE_REVISION,
        )
        assert {
            "audit_id",
            "access_class",
            "content_trust",
            "storage_key",
            "ingest_provenance_json",
        }.isdisjoint(_columns(connection))
        assert connection.execute(
            "SELECT id, path FROM artifacts WHERE id = 'artifact-legacy'"
        ).fetchone() == ("artifact-legacy", "/legacy/private/result.json")
        assert (
            connection.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type = 'table' AND name = '_alembic_tmp_artifacts'"
            ).fetchone()
            is None
        )

    _run_alembic_with_sqlite_foreign_keys(database_path, "head")
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
            ARTIFACT_REVISION,
        )
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute(
            "SELECT content_trust FROM artifacts WHERE id = 'artifact-legacy'"
        ).fetchone() == ("untrusted_tool_output",)


def test_artifact_downgrade_fault_rolls_back_and_can_retry(tmp_path: Path) -> None:
    database_path = tmp_path / "artifact-downgrade-fault.db"
    run_alembic(database_path, BASE_REVISION)
    with sqlite3.connect(database_path) as connection:
        _insert_legacy_artifact_graph(connection)
        connection.commit()
    _run_alembic_with_sqlite_foreign_keys(database_path, "head")

    with sqlite3.connect(database_path) as connection:
        expected_row = connection.execute(
            "SELECT audit_id, access_class, content_trust, storage_key, "
            "ingest_provenance_json, path FROM artifacts "
            "WHERE id = 'artifact-legacy'"
        ).fetchone()
    assert expected_row is not None

    fault_injected = False

    def fail_after_batch_copy(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        nonlocal fault_injected
        normalized = " ".join(statement.replace('"', "").split()).lower()
        if normalized == "drop table artifacts":
            fault_injected = True
            raise RuntimeError("injected Artifact downgrade fault")

    event.listen(Engine, "before_cursor_execute", fail_after_batch_copy)
    try:
        with pytest.raises(RuntimeError, match="injected Artifact downgrade fault"):
            _run_alembic_with_sqlite_foreign_keys(
                database_path,
                BASE_REVISION,
                downgrade=True,
            )
    finally:
        event.remove(Engine, "before_cursor_execute", fail_after_batch_copy)
    assert fault_injected

    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
            ARTIFACT_REVISION,
        )
        assert {
            "audit_id",
            "access_class",
            "content_trust",
            "storage_key",
            "ingest_provenance_json",
        } <= _columns(connection).keys()
        assert (
            connection.execute(
                "SELECT audit_id, access_class, content_trust, storage_key, "
                "ingest_provenance_json, path FROM artifacts "
                "WHERE id = 'artifact-legacy'"
            ).fetchone()
            == expected_row
        )
        table_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'artifacts'"
        ).fetchone()
        assert table_sql is not None
        expected_checks = {
            constraint.name
            for constraint in Base.metadata.tables["artifacts"].constraints
            if constraint.__class__.__name__ == "CheckConstraint"
        }
        assert all(check_name in table_sql[0] for check_name in expected_checks)
        assert (
            connection.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type = 'table' AND name = '_alembic_tmp_artifacts'"
            ).fetchone()
            is None
        )
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []

    _run_alembic_with_sqlite_foreign_keys(
        database_path,
        BASE_REVISION,
        downgrade=True,
    )
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
            BASE_REVISION,
        )
        assert "storage_key" not in _columns(connection)
        assert connection.execute(
            "SELECT path FROM artifacts WHERE id = 'artifact-legacy'"
        ).fetchone() == ("/legacy/private/result.json",)

    _run_alembic_with_sqlite_foreign_keys(database_path, "head")
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
            ARTIFACT_REVISION,
        )


def test_artifact_access_downgrade_fails_closed_offline() -> None:
    with pytest.raises(RuntimeError, match="requires an online database"):
        offline_downgrade_alembic(f"{ARTIFACT_REVISION}:{BASE_REVISION}")


def test_artifact_access_upgrade_compiles_postgresql_contract_offline() -> None:
    sql = _offline_sql(
        "postgresql+asyncpg://riftx@localhost/riftx",
        f"{BASE_REVISION}:{ARTIFACT_REVISION}",
    )

    lock = 'LOCK TABLE "artifacts" IN ACCESS EXCLUSIVE MODE'
    assert (
        sql.index(lock)
        < sql.index("DO $riftx$")
        < sql.index("ALTER TABLE artifacts ADD COLUMN audit_id")
    )
    assert "length(run_id) BETWEEN 1 AND 64" in sql
    assert "length(id) BETWEEN 1 AND 64" in sql
    assert "length(name) BETWEEN 1 AND 255" in sql
    assert "mime_type = btrim(mime_type)" in sql
    assert "mime_type !~ '[^ -~]'" in sql
    assert "DROP CONSTRAINT artifacts_execution_id_fkey" in sql
    assert (
        "ADD CONSTRAINT fk_artifacts_execution FOREIGN KEY(execution_id) "
        "REFERENCES executions (id) ON DELETE RESTRICT"
    ) in sql


def test_artifact_access_migration_preserves_sqlite_foreign_keys(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "artifact-foreign-keys.db"
    run_alembic(database_path, BASE_REVISION)
    with sqlite3.connect(database_path) as connection:
        _insert_legacy_artifact_graph(connection)
        connection.commit()

    _run_alembic_with_sqlite_foreign_keys(database_path, "head")

    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        foreign_keys = connection.execute("PRAGMA foreign_key_list(artifacts)").fetchall()
        assert any(
            row[2] == "audit_scans"
            and row[3] == "audit_id"
            and row[4] == "id"
            and row[6] == "RESTRICT"
            for row in foreign_keys
        )


def test_artifact_execution_fk_restricts_provenance_deletion(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "artifact-execution-restrict.db"
    run_alembic(database_path, BASE_REVISION)
    with sqlite3.connect(database_path) as connection:
        _insert_legacy_artifact_graph(
            connection,
            execution_id="execution-artifact",
        )
        connection.commit()

    _run_alembic_with_sqlite_foreign_keys(database_path, "head")

    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        foreign_keys = connection.execute("PRAGMA foreign_key_list(artifacts)").fetchall()
        assert any(
            row[2] == "executions"
            and row[3] == "execution_id"
            and row[4] == "id"
            and row[6] == "RESTRICT"
            for row in foreign_keys
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "DELETE FROM executions WHERE id = ?",
                ("execution-artifact",),
            )
        connection.rollback()
        assert connection.execute(
            "SELECT execution_id FROM artifacts WHERE id = 'artifact-legacy'"
        ).fetchone() == ("execution-artifact",)


def test_artifact_downgrade_restores_legacy_execution_set_null(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "artifact-execution-set-null.db"
    run_alembic(database_path, BASE_REVISION)
    with sqlite3.connect(database_path) as connection:
        _insert_legacy_artifact_graph(
            connection,
            execution_id="execution-artifact",
        )
        connection.commit()
    _run_alembic_with_sqlite_foreign_keys(database_path, "head")

    _run_alembic_with_sqlite_foreign_keys(
        database_path,
        BASE_REVISION,
        downgrade=True,
    )

    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        foreign_keys = connection.execute("PRAGMA foreign_key_list(artifacts)").fetchall()
        assert any(
            row[2] == "executions"
            and row[3] == "execution_id"
            and row[4] == "id"
            and row[6] == "SET NULL"
            for row in foreign_keys
        )
        connection.execute(
            "DELETE FROM executions WHERE id = ?",
            ("execution-artifact",),
        )
        connection.commit()
        assert connection.execute(
            "SELECT execution_id FROM artifacts WHERE id = 'artifact-legacy'"
        ).fetchone() == (None,)


def test_artifact_access_migration_matches_orm_metadata(tmp_path: Path) -> None:
    database_path = tmp_path / "artifact-schema-parity.db"
    run_alembic(database_path, "head")
    engine = create_engine(f"sqlite:///{database_path}")
    try:
        inspector = inspect(engine)
        metadata_table = Base.metadata.tables["artifacts"]
        actual_columns = {
            column["name"]: (column["nullable"], getattr(column["type"], "length", None))
            for column in inspector.get_columns("artifacts")
        }
        expected_columns = {
            column.name: (column.nullable, getattr(column.type, "length", None))
            for column in metadata_table.columns
        }
        assert actual_columns == expected_columns
        actual_checks = {
            constraint["name"] for constraint in inspector.get_check_constraints("artifacts")
        }
        expected_checks = {
            constraint.name
            for constraint in metadata_table.constraints
            if constraint.__class__.__name__ == "CheckConstraint"
        }
        assert actual_checks == expected_checks
        assert {index["name"] for index in inspector.get_indexes("artifacts")} == {
            index.name for index in metadata_table.indexes
        }
        assert any(
            constraint["name"] == "fk_artifacts_audit"
            and constraint["constrained_columns"] == ["audit_id"]
            and constraint["referred_table"] == "audit_scans"
            and constraint["referred_columns"] == ["id"]
            and constraint["options"].get("ondelete") == "RESTRICT"
            for constraint in inspector.get_foreign_keys("artifacts")
        )
        assert any(
            constraint["name"] == "fk_artifacts_execution"
            and constraint["constrained_columns"] == ["execution_id"]
            and constraint["referred_table"] == "executions"
            and constraint["referred_columns"] == ["id"]
            and constraint["options"].get("ondelete") == "RESTRICT"
            for constraint in inspector.get_foreign_keys("artifacts")
        )
    finally:
        engine.dispose()
