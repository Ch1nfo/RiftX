"""Offline SQLite diagnosis, backup, Alembic migration, and rollback."""

from __future__ import annotations

import os
import shutil
import sqlite3
import stat
import sysconfig
import tempfile
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

from alembic import command
from alembic.config import Config
from sqlalchemy import Connection, create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

from riftx.diagnostics import ALEMBIC_HEAD_REVISION
from riftx.local_fs import OwnerDirectoryError, ensure_owner_directories


class SQLiteMigrationStatus(StrEnum):
    MISSING = "missing"
    EMPTY = "empty"
    READY = "ready"
    MISMATCH = "mismatch"
    UNMANAGED = "unmanaged"
    INVALID = "invalid"


@dataclass(frozen=True, slots=True)
class SQLiteMigrationState:
    path: Path
    status: SQLiteMigrationStatus
    revisions: tuple[str, ...] = ()
    detail: str = ""

    @property
    def fixable(self) -> bool:
        return self.status in {
            SQLiteMigrationStatus.MISSING,
            SQLiteMigrationStatus.EMPTY,
            SQLiteMigrationStatus.MISMATCH,
        }


@dataclass(frozen=True, slots=True)
class DatabaseRepairResult:
    path: Path
    backup_path: Path | None
    previous_revisions: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SQLiteBackupResult:
    path: Path
    backup_path: Path
    source_identity: tuple[int, int] = field(repr=False)
    backup_identity: tuple[int, int] = field(repr=False)


@dataclass(frozen=True, slots=True)
class SQLiteBackupReadiness:
    path: Path
    backup_directory: Path


class DatabaseRepairError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        backup_path: Path | None = None,
        rollback_complete: bool = False,
    ) -> None:
        super().__init__(message)
        self.backup_path = backup_path
        self.rollback_complete = rollback_complete


class SQLiteBackupError(RuntimeError):
    """Raised when a ready SQLite backup or restore cannot be proven safe."""

    def __init__(self, message: str, *, backup_path: Path | None = None) -> None:
        super().__init__(message)
        self.backup_path = backup_path


def inspect_sqlite_migration(
    database_url: str,
    *,
    cwd: Path,
) -> SQLiteMigrationState | None:
    """Read a file-backed SQLite revision without creating or mutating it."""

    path = _sqlite_path(database_url, cwd=cwd)
    if path is None:
        return None
    if path.is_symlink():
        return SQLiteMigrationState(
            path=path,
            status=SQLiteMigrationStatus.INVALID,
            detail="SQLite database path must not be a symbolic link.",
        )
    if not path.exists():
        return SQLiteMigrationState(
            path=path,
            status=SQLiteMigrationStatus.MISSING,
            detail="SQLite database is not initialized.",
        )
    try:
        metadata = path.stat()
    except OSError as exc:
        return SQLiteMigrationState(
            path=path,
            status=SQLiteMigrationStatus.INVALID,
            detail=f"SQLite database metadata is unavailable: {exc}",
        )
    if not stat.S_ISREG(metadata.st_mode):
        return SQLiteMigrationState(
            path=path,
            status=SQLiteMigrationStatus.INVALID,
            detail="SQLite database path is not a regular file.",
        )
    try:
        with sqlite3.connect(_readonly_sqlite_uri(path), uri=True, timeout=1.0) as connection:
            integrity = connection.execute("PRAGMA quick_check").fetchone()
            if integrity != ("ok",):
                return SQLiteMigrationState(
                    path=path,
                    status=SQLiteMigrationStatus.INVALID,
                    detail="SQLite quick_check did not return ok.",
                )
            tables = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' "
                    "AND name NOT LIKE 'sqlite_%'"
                )
            }
            if not tables:
                return SQLiteMigrationState(
                    path=path,
                    status=SQLiteMigrationStatus.EMPTY,
                    detail="SQLite database contains no managed schema.",
                )
            if "alembic_version" not in tables:
                return SQLiteMigrationState(
                    path=path,
                    status=SQLiteMigrationStatus.UNMANAGED,
                    detail="SQLite database has tables but no Alembic revision.",
                )
            revisions = tuple(
                sorted(
                    str(row[0])
                    for row in connection.execute(
                        "SELECT version_num FROM alembic_version"
                    )
                )
            )
    except sqlite3.Error as exc:
        return SQLiteMigrationState(
            path=path,
            status=SQLiteMigrationStatus.INVALID,
            detail=f"SQLite database could not be inspected: {exc}",
        )
    return SQLiteMigrationState(
        path=path,
        status=(
            SQLiteMigrationStatus.READY
            if revisions == (ALEMBIC_HEAD_REVISION,)
            else SQLiteMigrationStatus.MISMATCH
        ),
        revisions=revisions,
        detail=(
            f"SQLite revision matches Alembic head {ALEMBIC_HEAD_REVISION}."
            if revisions == (ALEMBIC_HEAD_REVISION,)
            else "SQLite revision does not match the packaged Alembic head."
        ),
    )


def repair_sqlite_database(
    database_url: str,
    *,
    cwd: Path,
    alembic_config_path: Path | None = None,
) -> DatabaseRepairResult:
    """Back up an existing managed SQLite DB, migrate it, and restore on failure."""

    state = inspect_sqlite_migration(database_url, cwd=cwd)
    if state is None:
        raise DatabaseRepairError("Doctor database repair only supports file-backed SQLite.")
    if not state.fixable:
        raise DatabaseRepairError(
            f"SQLite database is {state.status.value} and cannot be repaired automatically."
        )
    config_path = resolve_alembic_config_path(alembic_config_path)
    database_path = state.path
    existed = database_path.exists()
    try:
        ensure_owner_directories((database_path.parent,))
    except OwnerDirectoryError as exc:
        raise DatabaseRepairError(str(exc)) from exc

    original_identity = _regular_file_identity(database_path) if existed else None
    backup_path: Path | None = None
    engine = create_engine(
        f"sqlite:///{database_path}",
        connect_args={"timeout": 1.0},
        hide_parameters=True,
    )
    try:
        with engine.connect() as connection:
            raw = connection.connection.driver_connection
            assert isinstance(raw, sqlite3.Connection)
            raw.execute("PRAGMA busy_timeout=1000")
            raw.execute("PRAGMA locking_mode=EXCLUSIVE")
            raw.execute("BEGIN EXCLUSIVE")
            raw.commit()
            if existed:
                backup_path = _backup_locked_database(raw, database_path)
            if not existed:
                os.chmod(database_path, 0o600)
            migrated_identity = _regular_file_identity(database_path)
            _run_upgrade(connection, config_path)
    except Exception as exc:
        engine.dispose()
        try:
            if backup_path is not None:
                _restore_backup(
                    backup_path,
                    database_path,
                    expected_identity=original_identity,
                )
                message = f"Database migration failed and was restored from {backup_path}."
            else:
                _remove_created_database(
                    database_path,
                    expected_identity=locals().get("migrated_identity"),
                )
                message = "Database migration failed; the new database was rolled back."
        except Exception as rollback_error:
            raise DatabaseRepairError(
                "Database migration failed and rollback was incomplete.",
                backup_path=backup_path,
                rollback_complete=False,
            ) from rollback_error
        raise DatabaseRepairError(
            message,
            backup_path=backup_path,
            rollback_complete=True,
        ) from exc
    finally:
        engine.dispose()

    verified = inspect_sqlite_migration(database_url, cwd=cwd)
    if verified is None or verified.status is not SQLiteMigrationStatus.READY:
        try:
            if backup_path is not None:
                _restore_backup(
                    backup_path,
                    database_path,
                    expected_identity=original_identity,
                )
            else:
                _remove_created_database(
                    database_path,
                    expected_identity=migrated_identity,
                )
        except Exception as rollback_error:
            raise DatabaseRepairError(
                "Database migration verification failed and rollback was incomplete.",
                backup_path=backup_path,
                rollback_complete=False,
            ) from rollback_error
        raise DatabaseRepairError(
            "Database migration verification failed and the prior state was restored.",
            backup_path=backup_path,
            rollback_complete=True,
        )
    return DatabaseRepairResult(
        path=database_path,
        backup_path=backup_path,
        previous_revisions=state.revisions,
    )


def inspect_sqlite_backup_readiness(
    database_url: str,
    *,
    cwd: Path,
) -> SQLiteBackupReadiness:
    """Verify the read-only preconditions shared by SQLite backup and restore."""

    state = inspect_sqlite_migration(database_url, cwd=cwd)
    if state is None:
        raise SQLiteBackupError("SQLite backup requires file-backed SQLite.")
    if state.status is not SQLiteMigrationStatus.READY:
        raise SQLiteBackupError(
            f"SQLite database is {state.status.value}; migrate it before backup."
        )
    backup_directory = state.path.parent / "backups"
    try:
        _regular_file_identity(state.path)
        _verify_backup_directory_target(backup_directory)
    except (DatabaseRepairError, OSError) as exc:
        raise SQLiteBackupError(f"SQLite backup destination is unsafe: {exc}") from exc
    return SQLiteBackupReadiness(
        path=state.path,
        backup_directory=backup_directory,
    )


def backup_sqlite_database(
    database_url: str,
    *,
    cwd: Path,
) -> SQLiteBackupResult:
    """Create an owner-only consistent backup of one ready local SQLite database."""

    readiness = inspect_sqlite_backup_readiness(database_url, cwd=cwd)
    database_path = readiness.path
    try:
        source_identity = _regular_file_identity(database_path)
        source = sqlite3.connect(database_path, timeout=1.0)
        try:
            source.execute("PRAGMA busy_timeout=1000")
            source.execute("PRAGMA locking_mode=EXCLUSIVE")
            source.execute("BEGIN EXCLUSIVE")
            source.commit()
            backup_path = _backup_locked_database(source, database_path)
        finally:
            source.close()
        if _regular_file_identity(database_path) != source_identity:
            raise SQLiteBackupError(
                "SQLite database identity changed during backup.",
                backup_path=backup_path,
            )
        return SQLiteBackupResult(
            path=database_path,
            backup_path=backup_path,
            source_identity=source_identity,
            backup_identity=_regular_file_identity(backup_path),
        )
    except SQLiteBackupError:
        raise
    except (DatabaseRepairError, OSError, sqlite3.Error) as exc:
        raise SQLiteBackupError(f"SQLite backup failed: {exc}") from exc


def restore_sqlite_database_backup(backup: SQLiteBackupResult) -> Path:
    """Restore a backup only while both source and backup identities still match."""

    if not isinstance(backup, SQLiteBackupResult):
        raise SQLiteBackupError("SQLite backup receipt is invalid.")
    try:
        if _regular_file_identity(backup.path) != backup.source_identity:
            raise SQLiteBackupError("SQLite database identity changed before restore.")
        if _regular_file_identity(backup.backup_path) != backup.backup_identity:
            raise SQLiteBackupError("SQLite backup identity changed before restore.")
        _verify_sqlite_file(backup.backup_path)
        _restore_backup(
            backup.backup_path,
            backup.path,
            expected_identity=backup.source_identity,
            expected_backup_identity=backup.backup_identity,
        )
        _verify_sqlite_file(backup.path)
        return backup.path
    except SQLiteBackupError:
        raise
    except (DatabaseRepairError, OSError, sqlite3.Error) as exc:
        raise SQLiteBackupError(
            f"SQLite backup restore failed: {exc}",
            backup_path=backup.backup_path,
        ) from exc


def resolve_alembic_config_path(explicit_path: Path | None = None) -> Path:
    candidates = (
        (explicit_path,) if explicit_path is not None else (
            Path(__file__).resolve().parents[2] / "alembic.ini",
            Path(sysconfig.get_path("data")) / "share" / "riftx" / "alembic.ini",
        )
    )
    for candidate in candidates:
        if candidate.is_file() and (candidate.parent / "migrations" / "env.py").is_file():
            return candidate
    raise DatabaseRepairError("Packaged Alembic migration assets are unavailable.")


def _sqlite_path(database_url: str, *, cwd: Path) -> Path | None:
    try:
        url = make_url(database_url)
    except ArgumentError:
        return None
    if url.get_backend_name() != "sqlite" or url.database in {None, "", ":memory:"}:
        return None
    path = Path(str(url.database)).expanduser()
    if not path.is_absolute():
        path = cwd / path
    return Path(os.path.abspath(os.fspath(path)))


def _readonly_sqlite_uri(path: Path) -> str:
    return f"file:{quote(str(path), safe='/')}?mode=ro"


def _backup_locked_database(
    source: sqlite3.Connection,
    database_path: Path,
) -> Path:
    backup_root = database_path.parent / "backups"
    ensure_owner_directories((backup_root,))
    backup_path = backup_root / f"{database_path.name}.{uuid4().hex}.bak"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    descriptor = os.open(backup_path, flags, 0o600)
    os.close(descriptor)
    try:
        with sqlite3.connect(backup_path) as destination:
            source.backup(destination)
            integrity = destination.execute("PRAGMA integrity_check").fetchone()
            if integrity != ("ok",):
                raise DatabaseRepairError("SQLite backup integrity_check did not return ok.")
        descriptor = os.open(backup_path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.chmod(backup_path, 0o600)
        _fsync_directory(backup_root)
        return backup_path
    except Exception:
        backup_path.unlink(missing_ok=True)
        raise


def _verify_backup_directory_target(path: Path) -> None:
    normalized = Path(os.path.abspath(os.fspath(path)))
    nearest_existing: Path | None = None
    missing_parent = False
    for candidate in reversed((normalized, *normalized.parents)):
        try:
            metadata = candidate.lstat()
        except FileNotFoundError:
            missing_parent = True
            continue
        if missing_parent:
            raise DatabaseRepairError(
                f"Backup path changed while it was being inspected: {normalized}"
            )
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise DatabaseRepairError(
                f"Backup path contains a non-directory or symbolic link: {normalized}"
            )
        mode = stat.S_IMODE(metadata.st_mode)
        trusted_sticky_root = metadata.st_uid == 0 and bool(mode & stat.S_ISVTX)
        if metadata.st_uid not in {0, os.geteuid()} or (
            mode & 0o022 and not trusted_sticky_root
        ):
            raise DatabaseRepairError(
                f"Backup path has an unsafe writable ancestor: {normalized}"
            )
        nearest_existing = candidate
    if nearest_existing is None or not os.access(
        nearest_existing,
        os.W_OK | os.X_OK,
    ):
        raise DatabaseRepairError(
            f"Backup directory cannot be created or written: {normalized}"
        )
    if normalized.exists():
        metadata = normalized.lstat()
        if metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) != 0o700:
            raise DatabaseRepairError(
                f"Existing backup directory must be owner-only (0700): {normalized}"
            )


def _run_upgrade(connection: Connection, config_path: Path) -> None:
    config = Config(str(config_path))
    config.set_main_option("script_location", str(config_path.parent / "migrations"))
    config.attributes["connection"] = connection
    config.attributes["riftx_database_url"] = str(connection.engine.url)
    command.upgrade(config, "head")


def _restore_backup(
    backup_path: Path,
    database_path: Path,
    *,
    expected_identity: tuple[int, int] | None,
    expected_backup_identity: tuple[int, int] | None = None,
) -> None:
    if expected_identity is None or _regular_file_identity(database_path) != expected_identity:
        raise DatabaseRepairError("Database identity changed before rollback.")
    temporary_name: str | None = None
    try:
        source_descriptor = os.open(backup_path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            source_metadata = os.fstat(source_descriptor)
        except Exception:
            os.close(source_descriptor)
            raise
        if (
            not stat.S_ISREG(source_metadata.st_mode)
            or source_metadata.st_uid != os.geteuid()
            or (
                expected_backup_identity is not None
                and (source_metadata.st_dev, source_metadata.st_ino)
                != expected_backup_identity
            )
        ):
            os.close(source_descriptor)
            raise DatabaseRepairError("SQLite backup identity changed before restore.")
        with os.fdopen(source_descriptor, "rb") as source, tempfile.NamedTemporaryFile(
            mode="wb",
            dir=database_path.parent,
            prefix=f".{database_path.name}.",
            suffix=".restore",
            delete=False,
        ) as temporary:
            shutil.copyfileobj(source, temporary)
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_name = temporary.name
        os.chmod(temporary_name, 0o600)
        _remove_sqlite_sidecars(database_path)
        os.replace(temporary_name, database_path)
        temporary_name = None
        _fsync_directory(database_path.parent)
    finally:
        if temporary_name is not None:
            Path(temporary_name).unlink(missing_ok=True)


def _verify_sqlite_file(path: Path) -> None:
    connection = sqlite3.connect(_readonly_sqlite_uri(path), uri=True, timeout=1.0)
    try:
        if connection.execute("PRAGMA integrity_check").fetchone() != ("ok",):
            raise SQLiteBackupError(f"SQLite integrity_check failed for {path}.")
    finally:
        connection.close()


def _remove_created_database(
    database_path: Path,
    *,
    expected_identity: object,
) -> None:
    if not isinstance(expected_identity, tuple) or len(expected_identity) != 2:
        raise DatabaseRepairError("New database identity is unavailable for rollback.")
    if _regular_file_identity(database_path) != expected_identity:
        raise DatabaseRepairError("New database identity changed before rollback.")
    _remove_sqlite_sidecars(database_path)
    database_path.unlink()
    _fsync_directory(database_path.parent)


def _remove_sqlite_sidecars(database_path: Path) -> None:
    for suffix in ("-wal", "-shm"):
        path = Path(f"{database_path}{suffix}")
        if not path.exists() and not path.is_symlink():
            continue
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid():
            raise DatabaseRepairError(f"Unsafe SQLite sidecar blocks rollback: {path}")
        path.unlink()


def _regular_file_identity(path: Path) -> tuple[int, int]:
    metadata = path.lstat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
    ):
        raise DatabaseRepairError(f"SQLite database is not a trusted local file: {path}")
    return metadata.st_dev, metadata.st_ino


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


__all__ = [
    "DatabaseRepairError",
    "DatabaseRepairResult",
    "SQLiteBackupError",
    "SQLiteBackupReadiness",
    "SQLiteBackupResult",
    "SQLiteMigrationState",
    "SQLiteMigrationStatus",
    "backup_sqlite_database",
    "inspect_sqlite_backup_readiness",
    "inspect_sqlite_migration",
    "repair_sqlite_database",
    "resolve_alembic_config_path",
    "restore_sqlite_database_backup",
]
