"""add remote Runner owner fencing

Revision ID: f6a1d9c3e805
Revises: f1c7a9e3d502
Create Date: 2026-08-01 00:00:00
"""

from collections.abc import Sequence
from uuid import uuid4

import sqlalchemy as sa
from alembic import op

revision: str = "f6a1d9c3e805"
down_revision: str | None = "f1c7a9e3d502"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CREDENTIALS_V2 = "runner_credentials_owner_fencing"
_CREDENTIALS_LEGACY = "runner_credentials_legacy"


def upgrade() -> None:
    op.add_column(
        "nodes",
        sa.Column("current_runner_instance_id", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "nodes",
        sa.Column(
            "current_runner_epoch",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.create_index(
        "ix_nodes_current_runner_instance_id",
        "nodes",
        ["current_runner_instance_id"],
    )

    op.add_column(
        "runner_commands",
        sa.Column("target_runner_instance_id", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "runner_commands",
        sa.Column("target_runner_epoch", sa.BigInteger(), nullable=True),
    )
    op.create_index(
        "ix_runner_commands_target_runner_instance_id",
        "runner_commands",
        ["target_runner_instance_id"],
    )
    op.create_index(
        "ix_runner_commands_target_poll",
        "runner_commands",
        [
            "node_id",
            "target_runner_instance_id",
            "target_runner_epoch",
            "status",
            "created_at",
        ],
    )

    op.add_column(
        "executions",
        sa.Column("owner_runner_instance_id", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "executions",
        sa.Column("owner_runner_epoch", sa.BigInteger(), nullable=True),
    )
    op.create_index(
        "ix_executions_owner_runner_instance_id",
        "executions",
        ["owner_runner_instance_id"],
    )

    _create_fenced_credential_table(_CREDENTIALS_V2)
    connection = op.get_bind()
    credentials = connection.execute(
        sa.text(
            "SELECT node_id, token_hash, token_prefix, created_at, rotated_at, revoked_at "
            "FROM runner_credentials"
        )
    ).mappings()
    for credential in credentials:
        instance_id = str(uuid4())
        connection.execute(
            sa.text(
                f"INSERT INTO {_CREDENTIALS_V2} "
                "(runner_instance_id, node_id, runner_epoch, token_hash, token_prefix, "
                "created_at, rotated_at, revoked_at) "
                "VALUES (:runner_instance_id, :node_id, 1, :token_hash, :token_prefix, "
                ":created_at, :rotated_at, :revoked_at)"
            ),
            {
                "runner_instance_id": instance_id,
                **dict(credential),
            },
        )
        connection.execute(
            sa.text(
                "UPDATE nodes "
                "SET current_runner_instance_id = :runner_instance_id, "
                "current_runner_epoch = 1 "
                "WHERE id = :node_id"
            ),
            {
                "runner_instance_id": instance_id,
                "node_id": credential["node_id"],
            },
        )
        # Existing durable commands belong to the only Runner generation that
        # existed before fencing. New commands are bound at enqueue time.
        connection.execute(
            sa.text(
                "UPDATE runner_commands "
                "SET target_runner_instance_id = :runner_instance_id, "
                "target_runner_epoch = 1 "
                "WHERE node_id = :node_id"
            ),
            {
                "runner_instance_id": instance_id,
                "node_id": credential["node_id"],
            },
        )

    op.drop_table("runner_credentials")
    op.rename_table(_CREDENTIALS_V2, "runner_credentials")
    op.create_index(
        "ix_runner_credentials_node_id",
        "runner_credentials",
        ["node_id"],
    )


def downgrade() -> None:
    op.create_table(
        _CREDENTIALS_LEGACY,
        sa.Column("node_id", sa.String(length=64), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("token_prefix", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("rotated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["node_id"], ["nodes.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("node_id"),
    )
    # The legacy schema can store one credential per node. Preserve the
    # credential selected by the node's current owner pointer.
    op.execute(
        sa.text(
            f"INSERT INTO {_CREDENTIALS_LEGACY} "
            "(node_id, token_hash, token_prefix, created_at, rotated_at, revoked_at) "
            "SELECT credential.node_id, credential.token_hash, credential.token_prefix, "
            "credential.created_at, credential.rotated_at, credential.revoked_at "
            "FROM runner_credentials AS credential "
            "JOIN nodes AS node "
            "ON node.id = credential.node_id "
            "AND node.current_runner_instance_id = credential.runner_instance_id "
            "AND node.current_runner_epoch = credential.runner_epoch"
        )
    )
    op.drop_table("runner_credentials")
    op.rename_table(_CREDENTIALS_LEGACY, "runner_credentials")

    op.drop_index(
        "ix_runner_commands_target_poll",
        table_name="runner_commands",
    )
    op.drop_index(
        "ix_runner_commands_target_runner_instance_id",
        table_name="runner_commands",
    )
    op.drop_column("runner_commands", "target_runner_epoch")
    op.drop_column("runner_commands", "target_runner_instance_id")

    op.drop_index(
        "ix_executions_owner_runner_instance_id",
        table_name="executions",
    )
    op.drop_column("executions", "owner_runner_epoch")
    op.drop_column("executions", "owner_runner_instance_id")

    op.drop_index(
        "ix_nodes_current_runner_instance_id",
        table_name="nodes",
    )
    op.drop_column("nodes", "current_runner_epoch")
    op.drop_column("nodes", "current_runner_instance_id")


def _create_fenced_credential_table(table_name: str) -> None:
    op.create_table(
        table_name,
        sa.Column("runner_instance_id", sa.String(length=64), nullable=False),
        sa.Column("node_id", sa.String(length=64), nullable=False),
        sa.Column("runner_epoch", sa.BigInteger(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("token_prefix", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("rotated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["node_id"], ["nodes.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("runner_instance_id"),
        sa.UniqueConstraint(
            "node_id",
            "runner_epoch",
            name="uq_runner_credentials_node_epoch",
        ),
        sa.UniqueConstraint(
            "node_id",
            "token_hash",
            name="uq_runner_credentials_node_token_hash",
        ),
    )
