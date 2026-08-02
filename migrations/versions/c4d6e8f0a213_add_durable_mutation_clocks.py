"""add store-owned durable mutation clocks

Revision ID: c4d6e8f0a213
Revises: b2c4d6e8f001
Create Date: 2026-08-01 13:00:00
"""

from collections.abc import Iterator, Sequence
from contextlib import contextmanager

import sqlalchemy as sa
from alembic import op

revision: str = "c4d6e8f0a213"
down_revision: str | None = "b2c4d6e8f001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CLOCK_TYPE = sa.DateTime(timezone=True)
_EXECUTION_FALLBACK_UTC_SQL = "'1970-01-01 00:00:00.000000+00:00'"
_EXECUTION_LIFECYCLE_COLUMNS = (
    "created_at",
    "process_created_at",
    "started_at",
    "finished_at",
    "physical_stop_confirmed_at",
)


def upgrade() -> None:
    with _sqlite_batch_foreign_keys_suspended():
        # Phase 1: legacy rows must remain writable while the clock is absent.
        op.add_column(
            "tool_call_intents",
            sa.Column("updated_at", _CLOCK_TYPE, nullable=True),
        )
        op.add_column(
            "executions",
            sa.Column("updated_at", _CLOCK_TYPE, nullable=True),
        )

        # Phase 2: use only durable row data and one fixed UTC fallback literal.
        _backfill_tool_call_intent_clocks()
        _backfill_execution_clocks()

        # Phase 3: the runtime owns every subsequent non-null stamp. There is
        # deliberately no server default or on-update expression.
        _set_clock_not_null("tool_call_intents")
        _set_clock_not_null("executions")


def downgrade() -> None:
    with _sqlite_batch_foreign_keys_suspended():
        _drop_clock("executions")
        _drop_clock("tool_call_intents")


def _backfill_tool_call_intent_clocks() -> None:
    intents = sa.table(
        "tool_call_intents",
        sa.column("created_at", _CLOCK_TYPE),
        sa.column("updated_at", _CLOCK_TYPE),
    )
    op.execute(intents.update().values(updated_at=intents.c.created_at))


def _backfill_execution_clocks() -> None:
    executions = sa.table(
        "executions",
        *(sa.column(column_name, _CLOCK_TYPE) for column_name in _EXECUTION_LIFECYCLE_COLUMNS),
        sa.column("updated_at", _CLOCK_TYPE),
    )
    lifecycle_columns = [executions.c[column_name] for column_name in _EXECUTION_LIFECYCLE_COLUMNS]
    latest = lifecycle_columns[0]
    for candidate in lifecycle_columns[1:]:
        # Pairwise nullable maximum: unlike GREATEST, this does not let one
        # NULL erase a real timestamp on PostgreSQL or SQLite.
        latest = sa.case(
            (latest.is_(None), candidate),
            (candidate.is_(None), latest),
            (candidate > latest, candidate),
            else_=latest,
        )
    fixed_fallback = sa.literal_column(
        _EXECUTION_FALLBACK_UTC_SQL,
        type_=_CLOCK_TYPE,
    )
    op.execute(executions.update().values(updated_at=sa.func.coalesce(latest, fixed_fallback)))


def _set_clock_not_null(table_name: str) -> None:
    if op.get_context().dialect.name == "sqlite":
        with op.batch_alter_table(table_name) as batch_op:
            batch_op.alter_column(
                "updated_at",
                existing_type=_CLOCK_TYPE,
                nullable=False,
            )
        return
    op.alter_column(
        table_name,
        "updated_at",
        existing_type=_CLOCK_TYPE,
        nullable=False,
    )


def _drop_clock(table_name: str) -> None:
    if op.get_context().dialect.name == "sqlite":
        with op.batch_alter_table(table_name) as batch_op:
            batch_op.drop_column("updated_at")
        return
    op.drop_column(table_name, "updated_at")


@contextmanager
def _sqlite_batch_foreign_keys_suspended() -> Iterator[None]:
    context = op.get_context()
    if context.dialect.name != "sqlite":
        # PostgreSQL online and offline migrations use native ALTER TABLE.
        yield
        return
    if context.as_sql:
        raise RuntimeError(
            "SQLite durable mutation clock migration requires an online database "
            "for batch table reflection and foreign-key verification"
        )

    connection = op.get_bind()
    foreign_keys_enabled = bool(connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one())
    if not foreign_keys_enabled:
        yield
        return

    # SQLite cannot change PRAGMA foreign_keys inside an active transaction.
    # Keep it disabled across every batch copy, verify the copied graph, and
    # restore the caller's original enforcement state even on failure.
    with context.autocommit_block():
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        if connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() != 0:
            raise RuntimeError("could not suspend SQLite foreign key enforcement")
        try:
            yield
            violations = connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise RuntimeError(
                    "durable mutation clock migration produced SQLite foreign key violations"
                )
        finally:
            connection.exec_driver_sql("PRAGMA foreign_keys=ON")
            if connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() != 1:
                raise RuntimeError("could not restore SQLite foreign key enforcement")
