"""add Run Action read-model persistence foundations

Revision ID: b2c4d6e8f001
Revises: fa72b4c8d901
Create Date: 2026-08-01 12:00:00
"""

from collections.abc import Iterator, Sequence
from contextlib import contextmanager

import sqlalchemy as sa
from alembic import op

revision: str = "b2c4d6e8f001"
down_revision: str | None = "fa72b4c8d901"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_LEGACY_ID_LENGTH = 64
_INTENT_ID_LENGTH = 128
_WIDENED_COLUMNS = (
    ("tool_call_intents", "id"),
    ("runtime_approval_requests", "tool_call_intent_id"),
    ("executions", "tool_call_id"),
    ("target_http_requests", "tool_call_id"),
)


def upgrade() -> None:
    # SQLite implements a type change by copying and dropping the old table.
    # With foreign keys enabled, dropping the ToolCallIntent parent would
    # otherwise cascade-delete durable RuntimeApprovalRequest rows.
    with _sqlite_batch_foreign_keys_suspended():
        _alter_intent_reference(
            "tool_call_intents",
            "id",
            old_length=_LEGACY_ID_LENGTH,
            new_length=_INTENT_ID_LENGTH,
            nullable=False,
        )
        _alter_intent_reference(
            "runtime_approval_requests",
            "tool_call_intent_id",
            old_length=_LEGACY_ID_LENGTH,
            new_length=_INTENT_ID_LENGTH,
            nullable=False,
        )
        _alter_intent_reference(
            "executions",
            "tool_call_id",
            old_length=_LEGACY_ID_LENGTH,
            new_length=_INTENT_ID_LENGTH,
            nullable=True,
        )
        _alter_intent_reference(
            "target_http_requests",
            "tool_call_id",
            old_length=_LEGACY_ID_LENGTH,
            new_length=_INTENT_ID_LENGTH,
            nullable=False,
        )

        op.add_column(
            "executions",
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        )
        op.create_index(
            "ix_tool_call_intents_run_created_id",
            "tool_call_intents",
            ["run_id", "created_at", "id"],
        )
        op.create_index(
            "ix_executions_run_tool_created_id",
            "executions",
            ["run_id", "tool_call_id", "created_at", "id"],
        )


def downgrade() -> None:
    if op.get_context().as_sql:
        raise RuntimeError(
            "Action read foundation downgrade requires an online database width preflight"
        )
    _require_legacy_width_before_downgrade()

    with _sqlite_batch_foreign_keys_suspended():
        op.drop_index("ix_executions_run_tool_created_id", table_name="executions")
        op.drop_index("ix_tool_call_intents_run_created_id", table_name="tool_call_intents")
        op.drop_column("executions", "created_at")

        # Reverse referencing columns before their parent primary key. The
        # length preflight above happens before every DDL statement so a
        # downgrade never partially mutates a database containing durable
        # 77-character IDs.
        _alter_intent_reference(
            "target_http_requests",
            "tool_call_id",
            old_length=_INTENT_ID_LENGTH,
            new_length=_LEGACY_ID_LENGTH,
            nullable=False,
        )
        _alter_intent_reference(
            "executions",
            "tool_call_id",
            old_length=_INTENT_ID_LENGTH,
            new_length=_LEGACY_ID_LENGTH,
            nullable=True,
        )
        _alter_intent_reference(
            "runtime_approval_requests",
            "tool_call_intent_id",
            old_length=_INTENT_ID_LENGTH,
            new_length=_LEGACY_ID_LENGTH,
            nullable=False,
        )
        _alter_intent_reference(
            "tool_call_intents",
            "id",
            old_length=_INTENT_ID_LENGTH,
            new_length=_LEGACY_ID_LENGTH,
            nullable=False,
        )


def _alter_intent_reference(
    table_name: str,
    column_name: str,
    *,
    old_length: int,
    new_length: int,
    nullable: bool,
) -> None:
    with op.batch_alter_table(table_name) as batch_op:
        batch_op.alter_column(
            column_name,
            existing_type=sa.String(length=old_length),
            type_=sa.String(length=new_length),
            existing_nullable=nullable,
        )


@contextmanager
def _sqlite_batch_foreign_keys_suspended() -> Iterator[None]:
    connection = op.get_bind()
    if connection.dialect.name != "sqlite":
        yield
        return

    foreign_keys_enabled = bool(connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one())
    if not foreign_keys_enabled:
        yield
        return

    # PRAGMA foreign_keys is a no-op inside an active SQLite transaction.
    # Alembic's autocommit block establishes the required boundary and returns
    # to the surrounding migration transaction after the batch is complete.
    with op.get_context().autocommit_block():
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        if connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() != 0:
            raise RuntimeError("could not suspend SQLite foreign key enforcement")
        try:
            yield
            violations = connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise RuntimeError(
                    "Action read foundation migration produced SQLite foreign key violations"
                )
        finally:
            connection.exec_driver_sql("PRAGMA foreign_keys=ON")
            if connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() != 1:
                raise RuntimeError("could not restore SQLite foreign key enforcement")


def _require_legacy_width_before_downgrade() -> None:
    connection = op.get_bind()
    violations: list[str] = []
    for table_name, column_name in _WIDENED_COLUMNS:
        statement = sa.text(
            f'SELECT 1 FROM "{table_name}" '
            f'WHERE length("{column_name}") > {_LEGACY_ID_LENGTH} LIMIT 1'
        )
        if connection.execute(statement).first() is not None:
            violations.append(f"{table_name}.{column_name}")
    if violations:
        joined = ", ".join(violations)
        raise RuntimeError(
            "cannot downgrade Action read foundations: values longer than 64 "
            f"characters exist in {joined}"
        )
