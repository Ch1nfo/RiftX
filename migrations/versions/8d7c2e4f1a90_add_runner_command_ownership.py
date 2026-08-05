"""add immutable Runner effect ownership, quarantine, and stop receipts

Revision ID: 8d7c2e4f1a90
Revises: 91e6f4a2c8b7
Create Date: 2026-08-03
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager

import sqlalchemy as sa
from alembic import op

revision: str = "8d7c2e4f1a90"
down_revision: str | Sequence[str] | None = "91e6f4a2c8b7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_BINDING_TABLE = "runner_effect_bindings"
_OWNERSHIP_TABLE = "runner_command_ownerships"
_STOP_RECEIPT_TABLE = "runner_stop_receipts"
_STOP_PROJECTION_TABLE = "runner_stop_projections"
_EXECUTION_AUDIT_FK = "fk_executions_audit"
_EXECUTION_AUDIT_PAIR = "ck_executions_audit_plan_pair"
_EXECUTION_PLAN_DIGEST = "ck_executions_plan_digest"
_EXECUTION_RUNNER_SHAPE = "ck_executions_runner_binding_shape"
_EXECUTION_RUNNER_BINDING_DIGEST = "ck_executions_runner_binding_digest"
_EXECUTION_RUNNER_ENVELOPE_DIGEST = "ck_executions_runner_envelope_digest"
_EXECUTION_RUNNER_COMMAND_FK = "fk_executions_runner_command"
_EXECUTION_RUNNER_BINDING_FK = "fk_executions_runner_effect_binding"
_EXECUTION_RUNNER_COMMAND_UNIQUE = "uq_executions_runner_command"
_COMMAND_STATE_VERSION = "ck_runner_commands_state_version"


def upgrade() -> None:
    with _serialized_runner_ownership_schema_change():
        _upgrade()


def _upgrade() -> None:
    connection = op.get_bind()
    _acquire_upgrade_lock(connection)

    with op.batch_alter_table("executions") as batch_op:
        batch_op.add_column(sa.Column("audit_id", sa.String(length=128), nullable=True))
        batch_op.add_column(sa.Column("plan_digest", sa.String(length=64), nullable=True))
        batch_op.create_foreign_key(
            _EXECUTION_AUDIT_FK,
            "audit_scans",
            ["audit_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch_op.create_check_constraint(
            _EXECUTION_AUDIT_PAIR,
            "(audit_id IS NULL AND plan_digest IS NULL) OR "
            "(audit_id IS NOT NULL AND plan_digest IS NOT NULL)",
        )
        batch_op.create_check_constraint(
            _EXECUTION_PLAN_DIGEST,
            _optional_lower_hex_digest_check("plan_digest"),
        )
        batch_op.create_index("ix_executions_audit_id", ["audit_id"])

    with op.batch_alter_table("runner_commands") as batch_op:
        batch_op.add_column(
            sa.Column(
                "state_version",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("0"),
            )
        )
        batch_op.create_check_constraint(_COMMAND_STATE_VERSION, "state_version >= 0")

    with op.batch_alter_table("runner_credentials") as batch_op:
        batch_op.add_column(
            sa.Column(
                "protocol_capabilities_json",
                sa.JSON(),
                nullable=False,
                server_default=sa.text("'[]'"),
            )
        )

    op.create_table(
        _BINDING_TABLE,
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("schema_version", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("run_kind", sa.String(length=32), nullable=False),
        sa.Column("node_id", sa.String(length=64), nullable=False),
        sa.Column("target_runner_instance_id", sa.String(length=64), nullable=False),
        sa.Column("target_runner_epoch", sa.BigInteger(), nullable=False),
        sa.Column("origin", sa.String(length=32), nullable=False),
        sa.Column("operation_family", sa.String(length=32), nullable=False),
        sa.Column("execution_id", sa.String(length=128), nullable=True),
        sa.Column("resource_kind", sa.String(length=32), nullable=False),
        sa.Column("resource_id", sa.String(length=128), nullable=False),
        sa.Column("audit_id", sa.String(length=128), nullable=True),
        sa.Column("plan_digest", sa.String(length=64), nullable=True),
        sa.Column("binding_digest", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "schema_version = 'riftx.runner-effect-binding/v1'",
            name="ck_runner_effect_bindings_schema",
        ),
        sa.CheckConstraint(
            "run_kind IN ('general', 'code_audit')",
            name="ck_runner_effect_bindings_run_kind",
        ),
        sa.CheckConstraint(
            "origin IN ('application_service', 'temporal_worker', "
            "'control_plane_reconciler', 'worker_reconciler', 'safety_reconciler')",
            name="ck_runner_effect_bindings_origin",
        ),
        sa.CheckConstraint(
            "operation_family IN ('execution', 'terminal', 'browser', 'target_http', "
            "'connector', 'safety_stop')",
            name="ck_runner_effect_bindings_family",
        ),
        sa.CheckConstraint(
            "resource_kind IN ('execution', 'terminal_session', 'browser_session', "
            "'target_http_intent', 'connector_session')",
            name="ck_runner_effect_bindings_resource_kind",
        ),
        sa.CheckConstraint(
            "(run_kind = 'general' AND audit_id IS NULL AND plan_digest IS NULL) OR "
            "(run_kind = 'code_audit' AND audit_id IS NOT NULL AND plan_digest IS NOT NULL)",
            name="ck_runner_effect_bindings_run_owner_shape",
        ),
        sa.CheckConstraint(
            _optional_lower_hex_digest_check("plan_digest"),
            name="ck_runner_effect_bindings_plan_digest",
        ),
        sa.CheckConstraint(
            _lower_hex_digest_check("binding_digest"),
            name="ck_runner_effect_bindings_binding_digest",
        ),
        sa.CheckConstraint(
            "resource_kind <> 'execution' OR "
            "(execution_id IS NOT NULL AND resource_id = execution_id)",
            name="ck_runner_effect_bindings_execution_identity",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"], ["runs.id"], name="fk_runner_effect_bindings_run", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["node_id"],
            ["nodes.id"],
            name="fk_runner_effect_bindings_node",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["target_runner_instance_id"],
            ["runner_credentials.runner_instance_id"],
            name="fk_runner_effect_bindings_target",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["audit_id"],
            ["audit_scans.id"],
            name="fk_runner_effect_bindings_audit",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    _create_binding_indexes()

    with op.batch_alter_table("executions") as batch_op:
        batch_op.add_column(sa.Column("runner_command_id", sa.String(64), nullable=True))
        batch_op.add_column(sa.Column("runner_effect_binding_id", sa.String(64), nullable=True))
        batch_op.add_column(sa.Column("runner_binding_digest", sa.String(64), nullable=True))
        batch_op.add_column(sa.Column("runner_envelope_digest", sa.String(64), nullable=True))
        batch_op.create_foreign_key(
            _EXECUTION_RUNNER_COMMAND_FK,
            "runner_commands",
            ["runner_command_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch_op.create_foreign_key(
            _EXECUTION_RUNNER_BINDING_FK,
            _BINDING_TABLE,
            ["runner_effect_binding_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch_op.create_unique_constraint(
            _EXECUTION_RUNNER_COMMAND_UNIQUE,
            ["runner_command_id"],
        )
        batch_op.create_check_constraint(
            _EXECUTION_RUNNER_SHAPE,
            "(runner_command_id IS NULL AND runner_effect_binding_id IS NULL "
            "AND runner_binding_digest IS NULL AND runner_envelope_digest IS NULL) OR "
            "(runner_command_id IS NOT NULL AND runner_effect_binding_id IS NOT NULL "
            "AND runner_binding_digest IS NOT NULL AND runner_envelope_digest IS NOT NULL)",
        )
        batch_op.create_check_constraint(
            _EXECUTION_RUNNER_BINDING_DIGEST,
            _optional_lower_hex_digest_check("runner_binding_digest"),
        )
        batch_op.create_check_constraint(
            _EXECUTION_RUNNER_ENVELOPE_DIGEST,
            _optional_lower_hex_digest_check("runner_envelope_digest"),
        )
        batch_op.create_index(
            "ix_executions_runner_command_id",
            ["runner_command_id"],
        )
        batch_op.create_index(
            "ix_executions_runner_effect_binding_id",
            ["runner_effect_binding_id"],
        )

    op.create_table(
        _OWNERSHIP_TABLE,
        sa.Column("command_id", sa.String(length=64), nullable=False),
        sa.Column("verification_state", sa.String(length=32), nullable=False),
        sa.Column("schema_version", sa.String(length=64), nullable=True),
        sa.Column("effect_binding_id", sa.String(length=64), nullable=True),
        sa.Column("operation", sa.String(length=32), nullable=True),
        sa.Column("operation_family", sa.String(length=32), nullable=True),
        sa.Column("payload_digest", sa.String(length=64), nullable=True),
        sa.Column("output_contract_json", sa.JSON(), nullable=True),
        sa.Column("output_contract_digest", sa.String(length=64), nullable=True),
        sa.Column("envelope_digest", sa.String(length=64), nullable=True),
        sa.Column("quarantine_reason", sa.String(length=255), nullable=True),
        sa.Column("quarantined_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reconciliation_state", sa.String(length=32), nullable=True),
        sa.Column("replacement_command_id", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "verification_state IN ('verified', 'quarantined')",
            name="ck_runner_command_ownerships_state",
        ),
        sa.CheckConstraint(
            "operation_family IS NULL OR operation_family IN "
            "('execution', 'terminal', 'browser', 'target_http', 'connector', 'safety_stop')",
            name="ck_runner_command_ownerships_family",
        ),
        sa.CheckConstraint(
            _optional_lower_hex_digest_check("payload_digest"),
            name="ck_runner_command_ownerships_payload_digest",
        ),
        sa.CheckConstraint(
            _optional_lower_hex_digest_check("output_contract_digest"),
            name="ck_runner_command_ownerships_output_contract_digest",
        ),
        sa.CheckConstraint(
            _optional_lower_hex_digest_check("envelope_digest"),
            name="ck_runner_command_ownerships_envelope_digest",
        ),
        sa.CheckConstraint(
            "(verification_state = 'verified' "
            "AND schema_version = 'riftx.runner-command-ownership/v1' "
            "AND effect_binding_id IS NOT NULL AND operation IS NOT NULL "
            "AND operation_family IS NOT NULL AND payload_digest IS NOT NULL "
            "AND output_contract_json IS NOT NULL AND output_contract_digest IS NOT NULL "
            "AND envelope_digest IS NOT NULL AND quarantine_reason IS NULL "
            "AND quarantined_at IS NULL AND reconciliation_state IS NULL) OR "
            "(verification_state = 'quarantined' AND quarantine_reason IS NOT NULL "
            "AND quarantined_at IS NOT NULL AND reconciliation_state IN "
            "('untouched', 'pending', 'replaced', 'manual'))",
            name="ck_runner_command_ownerships_verification_shape",
        ),
        sa.ForeignKeyConstraint(
            ["command_id"],
            ["runner_commands.id"],
            name="fk_runner_command_ownerships_command",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["effect_binding_id"],
            ["runner_effect_bindings.id"],
            name="fk_runner_command_ownerships_binding",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["replacement_command_id"],
            ["runner_commands.id"],
            name="fk_runner_command_ownerships_replacement",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("command_id"),
        sa.UniqueConstraint(
            "effect_binding_id",
            name="uq_runner_command_ownerships_effect_binding",
        ),
    )
    op.create_index(
        "ix_runner_command_ownerships_state_family",
        _OWNERSHIP_TABLE,
        ["verification_state", "operation_family"],
    )
    op.create_index(
        "ix_runner_command_ownerships_reconciliation",
        _OWNERSHIP_TABLE,
        ["reconciliation_state", "quarantined_at"],
    )
    for column_name in (
        "effect_binding_id",
        "envelope_digest",
        "reconciliation_state",
    ):
        op.create_index(
            f"ix_runner_command_ownerships_{column_name}",
            _OWNERSHIP_TABLE,
            [column_name],
        )

    op.create_table(
        _STOP_RECEIPT_TABLE,
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("schema_version", sa.String(length=64), nullable=False),
        sa.Column("command_id", sa.String(length=64), nullable=False),
        sa.Column("effect_binding_id", sa.String(length=64), nullable=False),
        sa.Column("envelope_digest", sa.String(length=64), nullable=False),
        sa.Column("binding_digest", sa.String(length=64), nullable=False),
        sa.Column("operation", sa.String(length=32), nullable=False),
        sa.Column("operation_family", sa.String(length=32), nullable=False),
        sa.Column("resource_kind", sa.String(length=32), nullable=False),
        sa.Column("resource_id", sa.String(length=128), nullable=False),
        sa.Column("execution_id", sa.String(length=128), nullable=True),
        sa.Column("node_id", sa.String(length=64), nullable=False),
        sa.Column("principal_instance_id", sa.String(length=64), nullable=False),
        sa.Column("principal_epoch", sa.BigInteger(), nullable=False),
        sa.Column("ack_digest", sa.String(length=64), nullable=False),
        sa.Column("ack_json", sa.JSON(), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "schema_version = 'riftx.runner-stop-receipt/v1'",
            name="ck_runner_stop_receipts_schema",
        ),
        sa.CheckConstraint(_lower_hex_digest_check("envelope_digest"), name="ck_stop_envelope"),
        sa.CheckConstraint(_lower_hex_digest_check("binding_digest"), name="ck_stop_binding"),
        sa.CheckConstraint(_lower_hex_digest_check("ack_digest"), name="ck_stop_ack"),
        sa.ForeignKeyConstraint(
            ["command_id"],
            ["runner_commands.id"],
            name="fk_runner_stop_receipts_command",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["effect_binding_id"],
            ["runner_effect_bindings.id"],
            name="fk_runner_stop_receipts_binding",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("command_id", name="uq_runner_stop_receipts_command"),
    )
    for column_name in ("command_id", "effect_binding_id", "resource_id", "execution_id"):
        op.create_index(
            f"ix_runner_stop_receipts_{column_name}",
            _STOP_RECEIPT_TABLE,
            [column_name],
        )

    op.create_table(
        _STOP_PROJECTION_TABLE,
        sa.Column("receipt_id", sa.String(length=64), nullable=False),
        sa.Column("projection_state", sa.String(length=32), nullable=False),
        sa.Column("state_version", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "projection_state IN ('pending', 'applied', 'manual')",
            name="ck_runner_stop_projections_state",
        ),
        sa.CheckConstraint(
            "state_version >= 0",
            name="ck_runner_stop_projections_version",
        ),
        sa.ForeignKeyConstraint(
            ["receipt_id"],
            ["runner_stop_receipts.id"],
            name="fk_runner_stop_projections_receipt",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("receipt_id"),
    )
    op.create_index(
        "ix_runner_stop_projections_projection_state",
        _STOP_PROJECTION_TABLE,
        ["projection_state"],
    )

    # Deliberately use only command identity and timestamps. Payload, kind,
    # target, paths and idempotency keys are not ownership evidence.
    op.execute(
        sa.text(
            "INSERT INTO runner_command_ownerships "
            "(command_id, verification_state, schema_version, effect_binding_id, operation, "
            "operation_family, payload_digest, output_contract_json, output_contract_digest, "
            "envelope_digest, quarantine_reason, quarantined_at, reconciliation_state, "
            "replacement_command_id, created_at) "
            "SELECT id, 'quarantined', NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, "
            "'legacy_ownership_missing', CURRENT_TIMESTAMP, 'untouched', NULL, created_at "
            "FROM runner_commands"
        )
    )


def downgrade() -> None:
    with _serialized_runner_ownership_schema_change():
        _downgrade()


def _downgrade() -> None:
    context = op.get_context()
    if context.as_sql:
        raise RuntimeError(
            "Runner ownership downgrade requires an online database to prove "
            "that no ownership, reconciliation, or stop-proof facts would be lost"
        )
    connection = op.get_bind()
    _acquire_upgrade_lock(connection, include_ownership_tables=True)
    unsafe = connection.execute(
        sa.text(
            "SELECT 1 FROM runner_command_ownerships WHERE "
            "verification_state <> 'quarantined' OR schema_version IS NOT NULL OR "
            "effect_binding_id IS NOT NULL OR operation IS NOT NULL OR "
            "operation_family IS NOT NULL OR payload_digest IS NOT NULL OR "
            "output_contract_json IS NOT NULL OR output_contract_digest IS NOT NULL OR "
            "envelope_digest IS NOT NULL OR reconciliation_state <> 'untouched' OR "
            "replacement_command_id IS NOT NULL LIMIT 1"
        )
    ).first()
    if unsafe is not None:
        raise RuntimeError(
            "cannot downgrade Runner ownership after verified or reconciled ownership facts exist"
        )
    if (
        connection.execute(
            sa.text("SELECT 1 FROM runner_commands WHERE state_version <> 0 LIMIT 1")
        ).first()
        is not None
    ):
        raise RuntimeError(
            "cannot downgrade Runner ownership while post-migration command state exists"
        )
    if (
        connection.execute(
            sa.text(
                "SELECT 1 FROM runner_credentials "
                "WHERE protocol_capabilities_json IS NOT NULL "
                "AND CAST(protocol_capabilities_json AS TEXT) NOT IN ('[]', 'null') LIMIT 1"
            )
        ).first()
        is not None
    ):
        raise RuntimeError(
            "cannot downgrade Runner ownership while protocol capability facts exist"
        )
    for table_name in (_BINDING_TABLE, _STOP_RECEIPT_TABLE, _STOP_PROJECTION_TABLE):
        if connection.execute(sa.text(f'SELECT 1 FROM "{table_name}" LIMIT 1')).first() is not None:
            raise RuntimeError(
                "cannot downgrade Runner ownership while effect or stop proof facts exist"
            )
    if (
        connection.execute(
            sa.text(
                "SELECT 1 FROM executions "
                "WHERE audit_id IS NOT NULL OR plan_digest IS NOT NULL OR "
                "runner_command_id IS NOT NULL OR runner_effect_binding_id IS NOT NULL OR "
                "runner_binding_digest IS NOT NULL OR runner_envelope_digest IS NOT NULL LIMIT 1"
            )
        ).first()
        is not None
    ):
        raise RuntimeError("cannot downgrade Runner ownership while Audit Execution bindings exist")

    op.drop_table(_STOP_PROJECTION_TABLE)
    op.drop_table(_STOP_RECEIPT_TABLE)
    op.drop_table(_OWNERSHIP_TABLE)
    with op.batch_alter_table("runner_credentials") as batch_op:
        batch_op.drop_column("protocol_capabilities_json")
    with op.batch_alter_table("runner_commands") as batch_op:
        batch_op.drop_constraint(_COMMAND_STATE_VERSION, type_="check")
        batch_op.drop_column("state_version")
    with op.batch_alter_table("executions") as batch_op:
        batch_op.drop_index("ix_executions_runner_effect_binding_id")
        batch_op.drop_index("ix_executions_runner_command_id")
        batch_op.drop_constraint(_EXECUTION_RUNNER_ENVELOPE_DIGEST, type_="check")
        batch_op.drop_constraint(_EXECUTION_RUNNER_BINDING_DIGEST, type_="check")
        batch_op.drop_constraint(_EXECUTION_RUNNER_SHAPE, type_="check")
        batch_op.drop_constraint(_EXECUTION_RUNNER_COMMAND_UNIQUE, type_="unique")
        batch_op.drop_constraint(_EXECUTION_RUNNER_BINDING_FK, type_="foreignkey")
        batch_op.drop_constraint(_EXECUTION_RUNNER_COMMAND_FK, type_="foreignkey")
        batch_op.drop_column("runner_envelope_digest")
        batch_op.drop_column("runner_binding_digest")
        batch_op.drop_column("runner_effect_binding_id")
        batch_op.drop_column("runner_command_id")
        batch_op.drop_index("ix_executions_audit_id")
        batch_op.drop_constraint(_EXECUTION_PLAN_DIGEST, type_="check")
        batch_op.drop_constraint(_EXECUTION_AUDIT_PAIR, type_="check")
        batch_op.drop_constraint(_EXECUTION_AUDIT_FK, type_="foreignkey")
        batch_op.drop_column("plan_digest")
        batch_op.drop_column("audit_id")
    op.drop_table(_BINDING_TABLE)


def _create_binding_indexes() -> None:
    op.create_index(
        "ix_runner_effect_bindings_family_resource",
        _BINDING_TABLE,
        ["operation_family", "resource_kind", "resource_id"],
    )
    op.create_index(
        "ix_runner_effect_bindings_run_execution",
        _BINDING_TABLE,
        ["run_id", "execution_id"],
    )
    for column_name in (
        "run_id",
        "node_id",
        "target_runner_instance_id",
        "execution_id",
        "resource_id",
        "audit_id",
        "binding_digest",
    ):
        op.create_index(
            f"ix_runner_effect_bindings_{column_name}",
            _BINDING_TABLE,
            [column_name],
        )


@contextmanager
def _serialized_runner_ownership_schema_change() -> Iterator[None]:
    """Keep Runner ownership verification and DDL in one serialized transaction.

    PostgreSQL uses Alembic's transaction and the existing ACCESS EXCLUSIVE
    table locks acquired by ``_acquire_upgrade_lock``. SQLite must suspend
    foreign-key enforcement outside a transaction before Alembic can rebuild
    ``executions``, which is referenced by ``artifacts``. One explicit
    ``BEGIN EXCLUSIVE`` then owns all validation, batch DDL, legacy quarantine
    seeding, and the final foreign-key check. Raw COMMIT/ROLLBACK keeps the
    revision atomic even though Alembic otherwise treats SQLite DDL as
    non-transactional.
    """

    context = op.get_context()
    if context.dialect.name != "sqlite":
        yield
        return
    if context.as_sql:
        raise RuntimeError(
            "SQLite Runner ownership migration requires an online database for "
            "batch reflection and foreign-key verification"
        )

    connection = op.get_bind()
    with context.autocommit_block():
        foreign_keys_enabled = bool(connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one())
        transaction_started = False
        try:
            if foreign_keys_enabled:
                connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
            if connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() != 0:
                raise RuntimeError("could not suspend SQLite foreign key enforcement")

            connection.exec_driver_sql("BEGIN EXCLUSIVE")
            transaction_started = True
            yield
            _verify_foreign_keys(connection)
            connection.exec_driver_sql("COMMIT")
            transaction_started = False
        except BaseException:
            if transaction_started:
                connection.exec_driver_sql("ROLLBACK")
                transaction_started = False
            raise
        finally:
            if transaction_started:
                connection.exec_driver_sql("ROLLBACK")
            if foreign_keys_enabled:
                connection.exec_driver_sql("PRAGMA foreign_keys=ON")
            restored = bool(connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one())
            if restored is not foreign_keys_enabled:
                raise RuntimeError("could not restore SQLite foreign key enforcement")


def _acquire_upgrade_lock(
    connection: sa.Connection,
    *,
    include_ownership_tables: bool = False,
) -> None:
    context = op.get_context()
    if context.dialect.name == "postgresql":
        table_names = ["runner_commands", "executions"]
        if include_ownership_tables:
            table_names.extend(
                [
                    "runner_credentials",
                    _OWNERSHIP_TABLE,
                    _BINDING_TABLE,
                    _STOP_RECEIPT_TABLE,
                    _STOP_PROJECTION_TABLE,
                ]
            )
        statement = "LOCK TABLE " + ", ".join(table_names) + " IN ACCESS EXCLUSIVE MODE"
        if context.as_sql:
            op.execute(sa.text(statement))
        else:
            connection.exec_driver_sql(statement)
    elif context.dialect.name == "sqlite":
        # The schema-change wrapper already owns BEGIN EXCLUSIVE. Keep lock
        # acquisition here for PostgreSQL so its existing semantics remain
        # unchanged without attempting a nested SQLite transaction.
        return
    else:
        raise RuntimeError(
            "Runner ownership migration does not support dialect "
            f"{context.dialect.name!r}"
        )


def _verify_foreign_keys(connection: sa.Connection) -> None:
    if connection.dialect.name != "sqlite":
        return
    violations = connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
    if violations:
        raise RuntimeError(f"Runner ownership migration introduced FK violations: {violations!r}")


def _lower_hex_digest_check(column: str) -> str:
    remainder = column
    for character in "0123456789abcdef":
        remainder = f"replace({remainder}, '{character}', '')"
    return f"length({column}) = 64 AND length({remainder}) = 0"


def _optional_lower_hex_digest_check(column: str) -> str:
    return f"{column} IS NULL OR ({_lower_hex_digest_check(column)})"
