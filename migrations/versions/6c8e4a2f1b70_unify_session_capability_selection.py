"""Unify Session Tool, Skill, and Technique selections.

Revision ID: 6c8e4a2f1b70
Revises: 9a4d6e2b7c11
Create Date: 2026-08-06 11:30:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import context, op

revision = "6c8e4a2f1b70"
down_revision = "9a4d6e2b7c11"
branch_labels = None
depends_on = None


def _lower_hex_digest_check(column: str) -> str:
    remainder = column
    for character in "0123456789abcdef":
        remainder = f"replace({remainder}, '{character}', '')"
    return f"length({column}) = 64 AND length({remainder}) = 0"


def upgrade() -> None:
    op.create_table(
        "agent_capability_scopes",
        sa.Column("session_id", sa.String(length=36), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("agent_id", sa.String(length=128), nullable=False),
        sa.Column("allowed_capability_ids_json", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "kind IN ('tool', 'skill', 'technique')",
            name="ck_agent_capability_scopes_kind",
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["agent_sessions.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("session_id", "kind"),
    )
    op.create_index(
        "ix_agent_capability_scopes_run_id",
        "agent_capability_scopes",
        ["run_id"],
        unique=False,
    )
    op.create_table(
        "agent_capability_selections",
        sa.Column("session_id", sa.String(length=36), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("capability_id", sa.String(length=128), nullable=False),
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("agent_id", sa.String(length=128), nullable=False),
        sa.Column("version", sa.String(length=1024), nullable=False),
        sa.Column("capability_digest", sa.String(length=64), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("reason", sa.String(length=1000), nullable=False),
        sa.Column("snapshot_json", sa.JSON(), nullable=False),
        sa.Column("state_json", sa.JSON(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("selected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("unloaded_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "kind IN ('tool', 'skill', 'technique')",
            name="ck_agent_capability_selections_kind",
        ),
        sa.CheckConstraint(
            "source IN ('official', 'operator', 'organization', 'engagement')",
            name="ck_agent_capability_selections_source",
        ),
        sa.CheckConstraint(
            _lower_hex_digest_check("capability_digest"),
            name="ck_agent_capability_selections_digest",
        ),
        sa.CheckConstraint(
            "(active = 1 AND unloaded_at IS NULL) OR (active = 0 AND unloaded_at IS NOT NULL)",
            name="ck_agent_capability_selections_active_shape",
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["agent_sessions.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("session_id", "kind", "capability_id"),
    )
    op.create_index(
        "ix_agent_capability_selections_run_active",
        "agent_capability_selections",
        ["run_id", "active", "session_id"],
        unique=False,
    )
    _backfill_skills(op.get_bind())


def downgrade() -> None:
    if context.is_offline_mode():
        raise RuntimeError("offline downgrade cannot prove Capability selections are preserved")
    connection = op.get_bind()
    _require_legacy_skill_mirror(connection)
    op.drop_index(
        "ix_agent_capability_selections_run_active",
        table_name="agent_capability_selections",
    )
    op.drop_table("agent_capability_selections")
    op.drop_index(
        "ix_agent_capability_scopes_run_id",
        table_name="agent_capability_scopes",
    )
    op.drop_table("agent_capability_scopes")


def _backfill_skills(connection: sa.Connection) -> None:
    tables = _selection_tables(connection)
    scopes = connection.execute(sa.select(tables["legacy_scopes"])).mappings()
    for row in scopes:
        connection.execute(
            sa.text(
                "INSERT INTO agent_capability_scopes "
                "(session_id, kind, run_id, agent_id, allowed_capability_ids_json, updated_at) "
                "VALUES (:session_id, 'skill', :run_id, :agent_id, :allowed_ids, :updated_at)"
            ).bindparams(sa.bindparam("allowed_ids", type_=sa.JSON())),
            {
                "session_id": row["session_id"],
                "run_id": row["run_id"],
                "agent_id": row["agent_id"],
                "allowed_ids": row["allowed_skill_ids_json"],
                "updated_at": row["updated_at"],
            },
        )

    selections = connection.execute(sa.select(tables["legacy_selections"])).mappings()
    statement = sa.text(
        "INSERT INTO agent_capability_selections "
        "(session_id, kind, capability_id, run_id, agent_id, version, capability_digest, "
        "source, reason, snapshot_json, state_json, active, selected_at, updated_at, "
        "unloaded_at) VALUES (:session_id, 'skill', :capability_id, :run_id, :agent_id, "
        ":version, :digest, :source, :reason, :snapshot, :state, :active, :selected_at, "
        ":updated_at, :unloaded_at)"
    ).bindparams(
        sa.bindparam("snapshot", type_=sa.JSON()),
        sa.bindparam("state", type_=sa.JSON()),
    )
    for row in selections:
        connection.execute(
            statement,
            {
                "session_id": row["session_id"],
                "capability_id": row["skill_id"],
                "run_id": row["run_id"],
                "agent_id": row["agent_id"],
                "version": row["version"],
                "digest": row["skill_digest"],
                "source": row["source"],
                "reason": row["reason"],
                "snapshot": {
                    "document": row["document_json"],
                    "reference": row["reference_json"],
                },
                "state": {"references_loaded": row["references_loaded"]},
                "active": row["active"],
                "selected_at": row["selected_at"],
                "updated_at": row["updated_at"],
                "unloaded_at": row["unloaded_at"],
            },
        )


def _require_legacy_skill_mirror(connection: sa.Connection) -> None:
    tables = _selection_tables(connection)
    scopes = list(connection.execute(sa.select(tables["scopes"])).mappings())
    selections = list(connection.execute(sa.select(tables["selections"])).mappings())
    if any(row["kind"] != "skill" for row in scopes + selections):
        raise RuntimeError("cannot downgrade while Tool or Technique Session selections exist")

    legacy_scopes = {
        row["session_id"]: row
        for row in connection.execute(sa.select(tables["legacy_scopes"])).mappings()
    }
    for row in scopes:
        legacy = legacy_scopes.get(row["session_id"])
        if legacy is None or any(
            legacy[legacy_key] != row[new_key]
            for legacy_key, new_key in (
                ("run_id", "run_id"),
                ("agent_id", "agent_id"),
                ("allowed_skill_ids_json", "allowed_capability_ids_json"),
                ("updated_at", "updated_at"),
            )
        ):
            raise RuntimeError("cannot downgrade after unified Skill allowlist state changed")

    legacy_selections = {
        (row["session_id"], row["skill_id"]): row
        for row in connection.execute(sa.select(tables["legacy_selections"])).mappings()
    }
    for row in selections:
        legacy = legacy_selections.get((row["session_id"], row["capability_id"]))
        snapshot = row["snapshot_json"]
        state = row["state_json"]
        if (
            legacy is None
            or not isinstance(snapshot, dict)
            or not isinstance(state, dict)
            or snapshot.get("document") != legacy["document_json"]
            or snapshot.get("reference") != legacy["reference_json"]
            or state.get("references_loaded") != legacy["references_loaded"]
            or any(
                legacy[legacy_key] != row[new_key]
                for legacy_key, new_key in (
                    ("run_id", "run_id"),
                    ("agent_id", "agent_id"),
                    ("version", "version"),
                    ("skill_digest", "capability_digest"),
                    ("source", "source"),
                    ("reason", "reason"),
                    ("active", "active"),
                    ("selected_at", "selected_at"),
                    ("updated_at", "updated_at"),
                    ("unloaded_at", "unloaded_at"),
                )
            )
        ):
            raise RuntimeError("cannot downgrade after unified Skill selection state changed")


def _selection_tables(connection: sa.Connection) -> dict[str, sa.Table]:
    metadata = sa.MetaData()
    return {
        "legacy_scopes": sa.Table("agent_skill_scopes", metadata, autoload_with=connection),
        "legacy_selections": sa.Table("agent_skill_selections", metadata, autoload_with=connection),
        "scopes": sa.Table("agent_capability_scopes", metadata, autoload_with=connection),
        "selections": sa.Table("agent_capability_selections", metadata, autoload_with=connection),
    }
