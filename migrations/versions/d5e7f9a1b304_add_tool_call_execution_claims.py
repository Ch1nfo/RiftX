"""add durable Tool Call execution claims

Revision ID: d5e7f9a1b304
Revises: c4d6e8f0a213
Create Date: 2026-08-01 16:00:00
"""

from collections.abc import Iterator, Sequence
from contextlib import contextmanager

import sqlalchemy as sa
from alembic import op

revision: str = "d5e7f9a1b304"
down_revision: str | None = "c4d6e8f0a213"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CLAIM_KEY = "claimed_execution_key"
_CLAIM_GROUP = "claimed_attempt_group"
_CLAIM_INDEX = "ix_tool_call_intents_execution_claim"
_CLAIM_CHECK = "ck_tool_call_intents_execution_claim_pair"


def upgrade() -> None:
    with _sqlite_batch_foreign_keys_suspended():
        with op.batch_alter_table("tool_call_intents") as batch_op:
            batch_op.add_column(sa.Column(_CLAIM_KEY, sa.String(255), nullable=True))
            batch_op.add_column(sa.Column(_CLAIM_GROUP, sa.String(64), nullable=True))
            batch_op.create_check_constraint(
                _CLAIM_CHECK,
                f"({_CLAIM_KEY} IS NULL) = ({_CLAIM_GROUP} IS NULL)",
            )
            batch_op.create_index(
                _CLAIM_INDEX,
                [_CLAIM_KEY, _CLAIM_GROUP],
                unique=False,
            )


def downgrade() -> None:
    with _sqlite_batch_foreign_keys_suspended():
        with op.batch_alter_table("tool_call_intents") as batch_op:
            batch_op.drop_index(_CLAIM_INDEX)
            batch_op.drop_constraint(_CLAIM_CHECK, type_="check")
            batch_op.drop_column(_CLAIM_GROUP)
            batch_op.drop_column(_CLAIM_KEY)


@contextmanager
def _sqlite_batch_foreign_keys_suspended() -> Iterator[None]:
    context = op.get_context()
    if context.dialect.name != "sqlite":
        yield
        return
    if context.as_sql:
        raise RuntimeError(
            "SQLite Tool Call execution claim migration requires an online database "
            "for batch table reflection and foreign-key verification"
        )

    connection = op.get_bind()
    foreign_keys_enabled = bool(connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one())
    if not foreign_keys_enabled:
        yield
        return

    with context.autocommit_block():
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        if connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() != 0:
            raise RuntimeError("could not suspend SQLite foreign key enforcement")
        try:
            yield
            violations = connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise RuntimeError(
                    "Tool Call execution claim migration produced SQLite foreign key violations"
                )
        finally:
            connection.exec_driver_sql("PRAGMA foreign_keys=ON")
            if connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() != 1:
                raise RuntimeError("could not restore SQLite foreign key enforcement")
