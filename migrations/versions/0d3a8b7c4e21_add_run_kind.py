"""add immutable Run kind

Revision ID: 0d3a8b7c4e21
Revises: f7a9c1d3e526
Create Date: 2026-08-02
"""

from collections.abc import Iterator, Sequence
from contextlib import contextmanager

import sqlalchemy as sa
from alembic import op

revision: str = "0d3a8b7c4e21"
down_revision: str | Sequence[str] | None = "f7a9c1d3e526"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "runs",
        sa.Column(
            "kind",
            sa.String(length=32),
            nullable=False,
            server_default=sa.text("'general'"),
        ),
    )
    with _sqlite_batch_foreign_keys_suspended():
        with op.batch_alter_table("runs") as batch_op:
            batch_op.alter_column(
                "kind",
                existing_type=sa.String(length=32),
                nullable=False,
                server_default=None,
            )
            batch_op.create_check_constraint(
                "ck_runs_kind",
                "kind IN ('general', 'code_audit')",
            )
    op.create_index(op.f("ix_runs_kind"), "runs", ["kind"], unique=False)


def downgrade() -> None:
    _require_only_general_runs_before_downgrade()
    op.drop_index(op.f("ix_runs_kind"), table_name="runs")
    with _sqlite_batch_foreign_keys_suspended():
        with op.batch_alter_table("runs") as batch_op:
            batch_op.drop_constraint("ck_runs_kind", type_="check")
            batch_op.drop_column("kind")


@contextmanager
def _sqlite_batch_foreign_keys_suspended() -> Iterator[None]:
    context = op.get_context()
    if context.dialect.name != "sqlite":
        yield
        return
    if context.as_sql:
        raise RuntimeError(
            "SQLite Run kind migration requires an online database for batch "
            "table reflection and foreign-key verification"
        )

    connection = op.get_bind()
    foreign_keys_enabled = bool(
        connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one()
    )
    if not foreign_keys_enabled:
        yield
        return

    with context.autocommit_block():
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        if connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() != 0:
            raise RuntimeError("could not suspend SQLite foreign key enforcement")
        try:
            yield
            violations = connection.exec_driver_sql(
                "PRAGMA foreign_key_check"
            ).fetchall()
            if violations:
                raise RuntimeError(
                    "Run kind migration produced SQLite foreign key violations"
                )
        finally:
            connection.exec_driver_sql("PRAGMA foreign_keys=ON")
            if connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() != 1:
                raise RuntimeError("could not restore SQLite foreign key enforcement")


def _require_only_general_runs_before_downgrade() -> None:
    context = op.get_context()
    if context.as_sql:
        raise RuntimeError(
            "Run kind downgrade requires an online database to prove that no "
            "code_audit Runs exist"
        )
    connection = op.get_bind()
    non_general = connection.execute(
        sa.text("SELECT 1 FROM runs WHERE kind <> 'general' LIMIT 1")
    ).first()
    if non_general is not None:
        raise RuntimeError(
            "cannot downgrade Run kind while code_audit or unknown Run kinds exist"
        )
