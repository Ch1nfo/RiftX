"""Historical Code Audit static-effect ORM records retained for compatibility."""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from .orm import Base, UTCDateTime

AUDIT_STATIC_EFFECT_PLAN_SCHEMA_VERSION = "riftx.audit-static-effect-plan/v1"
SNAPSHOT_MOUNT_LEASE_SCHEMA_VERSION = "riftx.snapshot-mount-lease/v1"
SNAPSHOT_MOUNT_PIN_SCHEMA_VERSION = "riftx.snapshot-mount-pin/v1"
SNAPSHOT_MOUNT_STOP_PROOF_SCHEMA_VERSION = "riftx.snapshot-mount-stop-proof/v1"
SNAPSHOT_MOUNT_BACKEND_ID = "private_materialization"


def _lower_hex_digest_check(column: str) -> str:
    remainder = column
    for character in "0123456789abcdef":
        remainder = f"replace({remainder}, '{character}', '')"
    return f"length({column}) = 64 AND length({remainder}) = 0"


class AuditStaticEffectPlanRecord(Base):
    __tablename__ = "audit_static_effect_plans"
    __table_args__ = (
        ForeignKeyConstraint(
            ["audit_id", "project_id"],
            ["audit_scans.id", "audit_scans.project_id"],
            name="fk_audit_static_effect_plans_audit_project",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["snapshot_id", "project_id"],
            ["source_snapshots.id", "source_snapshots.project_id"],
            name="fk_audit_static_effect_plans_snapshot_project",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["audit_id", "snapshot_id", "snapshot_reference_role"],
            [
                "snapshot_references.audit_id",
                "snapshot_references.snapshot_id",
                "snapshot_references.role",
            ],
            name="fk_audit_static_effect_plans_snapshot_reference",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            f"schema_version = '{AUDIT_STATIC_EFFECT_PLAN_SCHEMA_VERSION}'",
            name="ck_audit_static_effect_plans_schema_version",
        ),
        CheckConstraint(
            "operation_family IN ('snapshot_materialize', 'snapshot_mount')",
            name="ck_audit_static_effect_plans_operation_family",
        ),
        CheckConstraint(
            "snapshot_reference_role = 'primary'",
            name="ck_audit_static_effect_plans_reference_role",
        ),
        CheckConstraint(
            f"backend_id = '{SNAPSHOT_MOUNT_BACKEND_ID}'",
            name="ck_audit_static_effect_plans_backend",
        ),
        CheckConstraint(
            "created_by_policy = 'riftx_policy'",
            name="ck_audit_static_effect_plans_policy_owner",
        ),
        CheckConstraint(
            _lower_hex_digest_check("plan_digest"),
            name="ck_audit_static_effect_plans_digest",
        ),
        CheckConstraint(
            _lower_hex_digest_check("snapshot_digest")
            + " AND "
            + _lower_hex_digest_check("manifest_digest")
            + " AND "
            + _lower_hex_digest_check("backend_digest"),
            name="ck_audit_static_effect_plans_owner_digests",
        ),
        UniqueConstraint(
            "id",
            "plan_digest",
            name="uq_audit_static_effect_plans_id_digest",
        ),
        Index(
            "ix_audit_static_effect_plans_audit_family_created",
            "audit_id",
            "operation_family",
            "created_at",
            "id",
        ),
    )

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    schema_version: Mapped[str] = mapped_column(String(64), nullable=False)
    canonical_json: Mapped[str] = mapped_column(Text, nullable=False)
    plan_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    project_id: Mapped[str] = mapped_column(String(128), nullable=False)
    audit_id: Mapped[str] = mapped_column(String(128), nullable=False)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="RESTRICT"), nullable=False)
    snapshot_id: Mapped[str] = mapped_column(String(128), nullable=False)
    snapshot_reference_role: Mapped[str] = mapped_column(String(32), nullable=False)
    snapshot_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    manifest_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    operation_family: Mapped[str] = mapped_column(String(32), nullable=False)
    node_id: Mapped[str] = mapped_column(
        ForeignKey("nodes.id", ondelete="RESTRICT"), nullable=False
    )
    backend_id: Mapped[str] = mapped_column(String(64), nullable=False)
    backend_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    created_by_policy: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[object] = mapped_column(UTCDateTime(), nullable=False)


class SnapshotMountLeaseRecord(Base):
    __tablename__ = "snapshot_mount_leases"
    __table_args__ = (
        ForeignKeyConstraint(
            ["plan_id", "plan_digest"],
            ["audit_static_effect_plans.id", "audit_static_effect_plans.plan_digest"],
            name="fk_snapshot_mount_leases_plan_binding",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            f"schema_version = '{SNAPSHOT_MOUNT_LEASE_SCHEMA_VERSION}'",
            name="ck_snapshot_mount_leases_schema_version",
        ),
        CheckConstraint(
            "status IN ('issued', 'active', 'revocation_pending', 'expiration_pending', "
            "'revoked', 'expired', 'outcome_unknown')",
            name="ck_snapshot_mount_leases_status",
        ),
        CheckConstraint("state_version >= 1", name="ck_snapshot_mount_leases_state_version"),
        CheckConstraint(
            f"backend_id = '{SNAPSHOT_MOUNT_BACKEND_ID}'",
            name="ck_snapshot_mount_leases_backend",
        ),
        CheckConstraint(
            _lower_hex_digest_check("lease_digest")
            + " AND "
            + _lower_hex_digest_check("nonce_hash")
            + " AND "
            + _lower_hex_digest_check("plan_digest")
            + " AND "
            + _lower_hex_digest_check("backend_digest"),
            name="ck_snapshot_mount_leases_digests",
        ),
        CheckConstraint(
            "expires_at > created_at AND updated_at >= created_at",
            name="ck_snapshot_mount_leases_timestamp_order",
        ),
        UniqueConstraint("effect_execution_id", name="uq_snapshot_mount_leases_effect"),
        UniqueConstraint("id", "lease_digest", name="uq_snapshot_mount_leases_id_digest"),
        Index(
            "ix_snapshot_mount_leases_reconcile",
            "target_node_id",
            "status",
            "expires_at",
            "updated_at",
            "id",
        ),
    )

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    schema_version: Mapped[str] = mapped_column(String(64), nullable=False)
    canonical_json: Mapped[str] = mapped_column(Text, nullable=False)
    lease_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    nonce_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    project_id: Mapped[str] = mapped_column(String(128), nullable=False)
    audit_id: Mapped[str] = mapped_column(String(128), nullable=False)
    run_id: Mapped[str] = mapped_column(String(128), nullable=False)
    snapshot_id: Mapped[str] = mapped_column(String(128), nullable=False)
    snapshot_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    manifest_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    plan_id: Mapped[str] = mapped_column(String(128), nullable=False)
    plan_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    effect_execution_id: Mapped[str] = mapped_column(String(128), nullable=False)
    target_runner_instance_id: Mapped[str] = mapped_column(
        ForeignKey("runner_credentials.runner_instance_id", ondelete="RESTRICT"), nullable=False
    )
    target_runner_epoch: Mapped[int] = mapped_column(BigInteger, nullable=False)
    target_node_id: Mapped[str] = mapped_column(String(128), nullable=False)
    backend_id: Mapped[str] = mapped_column(String(64), nullable=False)
    backend_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    state_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    mount_key: Mapped[str | None] = mapped_column(String(128))
    mount_proof_digest: Mapped[str | None] = mapped_column(String(64))
    stop_proof_digest: Mapped[str | None] = mapped_column(String(64))
    failure_code: Mapped[str | None] = mapped_column(String(128))
    expires_at: Mapped[object] = mapped_column(UTCDateTime(), nullable=False)
    created_at: Mapped[object] = mapped_column(UTCDateTime(), nullable=False)
    activated_at: Mapped[object | None] = mapped_column(UTCDateTime())
    revocation_requested_at: Mapped[object | None] = mapped_column(UTCDateTime())
    terminated_at: Mapped[object | None] = mapped_column(UTCDateTime())
    updated_at: Mapped[object] = mapped_column(UTCDateTime(), nullable=False)


class SnapshotMountPinRecord(Base):
    __tablename__ = "snapshot_mount_pins"
    __table_args__ = (
        ForeignKeyConstraint(
            ["lease_id", "lease_digest"],
            ["snapshot_mount_leases.id", "snapshot_mount_leases.lease_digest"],
            name="fk_snapshot_mount_pins_lease_binding",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["plan_id", "plan_digest"],
            ["audit_static_effect_plans.id", "audit_static_effect_plans.plan_digest"],
            name="fk_snapshot_mount_pins_plan_binding",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            f"schema_version = '{SNAPSHOT_MOUNT_PIN_SCHEMA_VERSION}'",
            name="ck_snapshot_mount_pins_schema_version",
        ),
        CheckConstraint(
            "status IN ('pending', 'active', 'revocation_pending', 'revoked')",
            name="ck_snapshot_mount_pins_status",
        ),
        CheckConstraint("state_version >= 1", name="ck_snapshot_mount_pins_state_version"),
        CheckConstraint(
            f"backend_id = '{SNAPSHOT_MOUNT_BACKEND_ID}'",
            name="ck_snapshot_mount_pins_backend",
        ),
        CheckConstraint(
            _lower_hex_digest_check("pin_digest")
            + " AND "
            + _lower_hex_digest_check("lease_digest")
            + " AND "
            + _lower_hex_digest_check("plan_digest"),
            name="ck_snapshot_mount_pins_digests",
        ),
        UniqueConstraint("lease_id", name="uq_snapshot_mount_pins_lease"),
        UniqueConstraint("id", "pin_digest", name="uq_snapshot_mount_pins_id_digest"),
        Index("ix_snapshot_mount_pins_status_updated", "status", "updated_at", "id"),
    )

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    schema_version: Mapped[str] = mapped_column(String(64), nullable=False)
    canonical_json: Mapped[str] = mapped_column(Text, nullable=False)
    pin_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    lease_id: Mapped[str] = mapped_column(String(128), nullable=False)
    lease_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    plan_id: Mapped[str] = mapped_column(String(128), nullable=False)
    plan_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    effect_execution_id: Mapped[str] = mapped_column(String(128), nullable=False)
    project_id: Mapped[str] = mapped_column(String(128), nullable=False)
    audit_id: Mapped[str] = mapped_column(String(128), nullable=False)
    run_id: Mapped[str] = mapped_column(String(128), nullable=False)
    snapshot_id: Mapped[str] = mapped_column(String(128), nullable=False)
    node_id: Mapped[str] = mapped_column(String(128), nullable=False)
    backend_id: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    state_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    mount_key: Mapped[str | None] = mapped_column(String(128))
    mount_proof_digest: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[object] = mapped_column(UTCDateTime(), nullable=False)
    updated_at: Mapped[object] = mapped_column(UTCDateTime(), nullable=False)
    revoked_at: Mapped[object | None] = mapped_column(UTCDateTime())


class SnapshotMountStopProofRecord(Base):
    __tablename__ = "snapshot_mount_stop_proofs"
    __table_args__ = (
        ForeignKeyConstraint(
            ["lease_id", "lease_digest"],
            ["snapshot_mount_leases.id", "snapshot_mount_leases.lease_digest"],
            name="fk_snapshot_mount_stop_proofs_lease_binding",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["pin_id", "pin_digest"],
            ["snapshot_mount_pins.id", "snapshot_mount_pins.pin_digest"],
            name="fk_snapshot_mount_stop_proofs_pin_binding",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            f"schema_version = '{SNAPSHOT_MOUNT_STOP_PROOF_SCHEMA_VERSION}'",
            name="ck_snapshot_mount_stop_proofs_schema_version",
        ),
        CheckConstraint(
            "disposition IN ('revoked', 'expired')",
            name="ck_snapshot_mount_stop_proofs_disposition",
        ),
        CheckConstraint(
            _lower_hex_digest_check("proof_digest")
            + " AND "
            + _lower_hex_digest_check("lease_digest")
            + " AND "
            + _lower_hex_digest_check("pin_digest"),
            name="ck_snapshot_mount_stop_proofs_digests",
        ),
        UniqueConstraint("lease_id", name="uq_snapshot_mount_stop_proofs_lease"),
        UniqueConstraint("pin_id", name="uq_snapshot_mount_stop_proofs_pin"),
        Index("ix_snapshot_mount_stop_proofs_stopped", "stopped_at", "id"),
    )

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    schema_version: Mapped[str] = mapped_column(String(64), nullable=False)
    canonical_json: Mapped[str] = mapped_column(Text, nullable=False)
    proof_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    lease_id: Mapped[str] = mapped_column(String(128), nullable=False)
    lease_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    pin_id: Mapped[str] = mapped_column(String(128), nullable=False)
    pin_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    plan_id: Mapped[str] = mapped_column(String(128), nullable=False)
    plan_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    effect_execution_id: Mapped[str] = mapped_column(String(128), nullable=False)
    audit_id: Mapped[str] = mapped_column(String(128), nullable=False)
    run_id: Mapped[str] = mapped_column(String(128), nullable=False)
    snapshot_id: Mapped[str] = mapped_column(String(128), nullable=False)
    node_id: Mapped[str] = mapped_column(String(128), nullable=False)
    backend_id: Mapped[str] = mapped_column(String(64), nullable=False)
    disposition: Mapped[str] = mapped_column(String(32), nullable=False)
    stopped_at: Mapped[object] = mapped_column(UTCDateTime(), nullable=False)
