"""Durable same-node static-effect plans and Snapshot mount authority."""

from __future__ import annotations

import json
from collections.abc import Sequence

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    String,
    Text,
    UniqueConstraint,
    select,
    update,
)
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from riftx.application.errors import (
    RepositoryConflictError,
    RepositoryIntegrityError,
    RepositoryUnavailableError,
)
from riftx.audit.static_effect import (
    AUDIT_STATIC_EFFECT_PLAN_SCHEMA_VERSION,
    SNAPSHOT_MOUNT_BACKEND_ID,
    SNAPSHOT_MOUNT_LEASE_SCHEMA_VERSION,
    SNAPSHOT_MOUNT_PIN_SCHEMA_VERSION,
    SNAPSHOT_MOUNT_STOP_PROOF_SCHEMA_VERSION,
    AuditStaticEffectPlan,
    AuditStaticOperationFamily,
    SnapshotMountLease,
    SnapshotMountLeaseIssue,
    SnapshotMountLeaseStatus,
    SnapshotMountPin,
    SnapshotMountPinStatus,
    SnapshotMountStopProof,
    snapshot_storage_key_digest,
)

from .orm import (
    AuditScanRecord,
    Base,
    NodeRecord,
    RunnerCredentialRecord,
    RunRecord,
    SnapshotReferenceRecord,
    SourceSnapshotRecord,
    UTCDateTime,
)
from .transactions import SessionFactory, serialized_write

_MAX_CANONICAL_BYTES = 512 * 1024
_TERMINAL_LEASE_STATUSES = frozenset(
    {SnapshotMountLeaseStatus.REVOKED, SnapshotMountLeaseStatus.EXPIRED}
)
_RECONCILABLE_LEASE_STATUSES = tuple(
    status.value for status in SnapshotMountLeaseStatus if status not in _TERMINAL_LEASE_STATUSES
)


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


class SQLAlchemyAuditStaticEffectAuthorityRepository:
    """Strict authority repository with atomic Lease/Pin lifecycle transitions."""

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    async def create_plan(
        self,
        plan: AuditStaticEffectPlan,
    ) -> tuple[AuditStaticEffectPlan, bool]:
        if not isinstance(plan, AuditStaticEffectPlan):
            raise TypeError("plan must be an AuditStaticEffectPlan")
        plan = AuditStaticEffectPlan.model_validate(plan.model_dump(mode="python"))
        try:
            async with serialized_write(self._session_factory) as session:
                await _require_authoritative_plan_binding(session, plan)
                existing = await _load_plan(session, plan.id)
                if existing is not None:
                    return _require_exact(existing, plan, entity="AuditStaticEffectPlan"), False
                session.add(_plan_to_record(plan))
                await session.flush()
                return plan, True
        except (RepositoryConflictError, RepositoryIntegrityError):
            raise
        except IntegrityError as exc:
            return await self._resolve_plan_insert_conflict(plan, exc)
        except SQLAlchemyError as exc:
            raise RepositoryUnavailableError("Static effect Plan creation is unavailable") from exc

    async def _resolve_plan_insert_conflict(
        self,
        plan: AuditStaticEffectPlan,
        cause: IntegrityError,
    ) -> tuple[AuditStaticEffectPlan, bool]:
        try:
            async with self._session_factory() as session:
                existing = await _load_plan(session, plan.id)
            if existing is not None:
                return _require_exact(existing, plan, entity="AuditStaticEffectPlan"), False
        except (RepositoryConflictError, RepositoryIntegrityError):
            raise
        except SQLAlchemyError as exc:
            raise RepositoryUnavailableError("Static effect Plan replay is unavailable") from exc
        raise RepositoryConflictError(
            "Static effect Plan conflicts with durable ownership"
        ) from cause

    async def get_plan(self, plan_id: str) -> AuditStaticEffectPlan | None:
        try:
            async with self._session_factory() as session:
                plan = await _load_plan(session, plan_id)
                if plan is not None:
                    await _require_authoritative_plan_binding(
                        session,
                        plan,
                        integrity_id=plan.id,
                    )
                return plan
        except RepositoryIntegrityError:
            raise
        except SQLAlchemyError as exc:
            raise RepositoryUnavailableError("Static effect Plan lookup is unavailable") from exc

    async def issue_mount(
        self,
        issue: SnapshotMountLeaseIssue,
        pin: SnapshotMountPin,
    ) -> tuple[SnapshotMountLeaseIssue, SnapshotMountPin, bool]:
        if not isinstance(issue, SnapshotMountLeaseIssue) or not isinstance(pin, SnapshotMountPin):
            raise TypeError("mount issuance requires a Lease issue and Pin")
        issue = SnapshotMountLeaseIssue.model_validate(issue.model_dump(mode="python"))
        pin = SnapshotMountPin.model_validate(pin.model_dump(mode="python"))
        lease = issue.lease
        if lease.status is not SnapshotMountLeaseStatus.ISSUED:
            raise ValueError("new Snapshot mount Lease must be issued")
        if pin.status is not SnapshotMountPinStatus.PENDING:
            raise ValueError("new Snapshot mount Pin must be pending")
        pin._require_lease_binding(lease)
        try:
            async with serialized_write(self._session_factory) as session:
                plan = await _load_plan(session, lease.plan_id)
                if plan is None:
                    raise RepositoryConflictError("Snapshot mount Plan does not exist")
                _require_plan_lease_binding(plan, lease)
                await _require_authoritative_plan_binding(
                    session,
                    plan,
                    integrity_id=plan.id,
                )
                await _require_runner_principal(session, lease, require_current=True)
                existing = await _load_mount_by_effect(session, lease.effect_execution_id)
                if existing is not None:
                    stored_lease, stored_pin = existing
                    _require_exact(stored_lease, lease, entity="SnapshotMountLease")
                    _require_exact(stored_pin, pin, entity="SnapshotMountPin")
                    return issue, stored_pin, False
                session.add_all((_lease_to_record(lease), _pin_to_record(pin)))
                await session.flush()
                return issue, pin, True
        except (RepositoryConflictError, RepositoryIntegrityError):
            raise
        except IntegrityError as exc:
            try:
                async with self._session_factory() as session:
                    existing = await _load_mount_by_effect(
                        session,
                        lease.effect_execution_id,
                    )
                if existing is not None:
                    stored_lease, stored_pin = existing
                    _require_exact(stored_lease, lease, entity="SnapshotMountLease")
                    _require_exact(stored_pin, pin, entity="SnapshotMountPin")
                    return issue, stored_pin, False
            except (RepositoryConflictError, RepositoryIntegrityError):
                raise
            except SQLAlchemyError as read_exc:
                raise RepositoryUnavailableError(
                    "Snapshot mount issuance replay is unavailable"
                ) from read_exc
            raise RepositoryConflictError(
                "Snapshot mount issuance conflicts with durable ownership"
            ) from exc
        except SQLAlchemyError as exc:
            raise RepositoryUnavailableError("Snapshot mount issuance is unavailable") from exc

    async def get_mount(
        self,
        lease_id: str,
    ) -> tuple[SnapshotMountLease, SnapshotMountPin] | None:
        try:
            async with self._session_factory() as session:
                mount = await _load_mount(session, lease_id)
                if mount is None:
                    return None
                lease, pin = mount
                plan = await _load_plan(session, lease.plan_id)
                if plan is None:
                    raise RepositoryIntegrityError("SnapshotMountLease", lease.id)
                _require_plan_lease_binding(plan, lease, integrity_id=lease.id)
                await _require_authoritative_plan_binding(
                    session,
                    plan,
                    integrity_id=plan.id,
                )
                await _require_runner_principal(
                    session,
                    lease,
                    require_current=False,
                    integrity_id=lease.id,
                )
                return lease, pin
        except RepositoryIntegrityError:
            raise
        except SQLAlchemyError as exc:
            raise RepositoryUnavailableError("Snapshot mount lookup is unavailable") from exc

    async def get_stop_proof(self, lease_id: str) -> SnapshotMountStopProof | None:
        try:
            async with self._session_factory() as session:
                proof = await _load_stop_proof_for_lease(session, lease_id)
                mount = await _load_mount(session, lease_id)
                if proof is None:
                    if mount is not None and mount[0].status in _TERMINAL_LEASE_STATUSES:
                        raise RepositoryIntegrityError("SnapshotMountLease", lease_id)
                    return None
                if (
                    mount is None
                    or mount[0].status not in _TERMINAL_LEASE_STATUSES
                    or mount[0].stop_proof_digest != proof.proof_digest
                ):
                    raise RepositoryIntegrityError("SnapshotMountStopProof", proof.id)
                return proof
        except RepositoryIntegrityError:
            raise
        except SQLAlchemyError as exc:
            raise RepositoryUnavailableError("Snapshot mount stop proof lookup failed") from exc

    async def compare_and_set_mount(
        self,
        *,
        previous_lease: SnapshotMountLease,
        updated_lease: SnapshotMountLease,
        previous_pin: SnapshotMountPin,
        updated_pin: SnapshotMountPin,
    ) -> tuple[SnapshotMountLease, SnapshotMountPin, bool]:
        previous_lease = SnapshotMountLease.model_validate(previous_lease.model_dump(mode="python"))
        updated_lease = SnapshotMountLease.model_validate(updated_lease.model_dump(mode="python"))
        previous_pin = SnapshotMountPin.model_validate(previous_pin.model_dump(mode="python"))
        updated_pin = SnapshotMountPin.model_validate(updated_pin.model_dump(mode="python"))
        _validate_mount_transition(previous_lease, updated_lease, previous_pin, updated_pin)
        try:
            async with serialized_write(self._session_factory) as session:
                stored = await _load_mount(session, previous_lease.id, for_update=True)
                if stored is None:
                    raise RepositoryConflictError("Snapshot mount authority no longer exists")
                stored_lease, stored_pin = stored
                if stored_lease == updated_lease and stored_pin == updated_pin:
                    return stored_lease, stored_pin, False
                if stored_lease != previous_lease or stored_pin != previous_pin:
                    raise RepositoryConflictError("Snapshot mount authority changed before CAS")
                await _cas_mount_records(
                    session,
                    previous_lease=previous_lease,
                    updated_lease=updated_lease,
                    previous_pin=previous_pin,
                    updated_pin=updated_pin,
                )
                return updated_lease, updated_pin, True
        except (RepositoryConflictError, RepositoryIntegrityError):
            raise
        except IntegrityError as exc:
            raise RepositoryConflictError(
                "Snapshot mount CAS conflicts with durable facts"
            ) from exc
        except SQLAlchemyError as exc:
            raise RepositoryUnavailableError("Snapshot mount CAS is unavailable") from exc

    async def record_stop(
        self,
        *,
        previous_lease: SnapshotMountLease,
        stopped_lease: SnapshotMountLease,
        previous_pin: SnapshotMountPin,
        stopped_pin: SnapshotMountPin,
        proof: SnapshotMountStopProof,
    ) -> tuple[SnapshotMountLease, SnapshotMountPin, SnapshotMountStopProof, bool]:
        previous_lease = SnapshotMountLease.model_validate(previous_lease.model_dump(mode="python"))
        stopped_lease = SnapshotMountLease.model_validate(stopped_lease.model_dump(mode="python"))
        previous_pin = SnapshotMountPin.model_validate(previous_pin.model_dump(mode="python"))
        stopped_pin = SnapshotMountPin.model_validate(stopped_pin.model_dump(mode="python"))
        proof = SnapshotMountStopProof.model_validate(proof.model_dump(mode="python"))
        _validate_mount_transition(previous_lease, stopped_lease, previous_pin, stopped_pin)
        if (
            stopped_lease.status not in _TERMINAL_LEASE_STATUSES
            or stopped_pin.status is not SnapshotMountPinStatus.REVOKED
            or stopped_lease.stop_proof_digest != proof.proof_digest
            or proof.lease_id != stopped_lease.id
            or proof.pin_id != stopped_pin.id
        ):
            raise ValueError("terminal Snapshot mount transition lacks exact stop proof")
        try:
            async with serialized_write(self._session_factory) as session:
                stored = await _load_mount(session, previous_lease.id, for_update=True)
                existing_proof = await _load_stop_proof_for_lease(session, previous_lease.id)
                if stored == (stopped_lease, stopped_pin) and existing_proof == proof:
                    return stopped_lease, stopped_pin, proof, False
                if stored != (previous_lease, previous_pin) or existing_proof is not None:
                    raise RepositoryConflictError("Snapshot mount stop facts changed before CAS")
                session.add(_proof_to_record(proof))
                await _cas_mount_records(
                    session,
                    previous_lease=previous_lease,
                    updated_lease=stopped_lease,
                    previous_pin=previous_pin,
                    updated_pin=stopped_pin,
                )
                await session.flush()
                return stopped_lease, stopped_pin, proof, True
        except (RepositoryConflictError, RepositoryIntegrityError):
            raise
        except IntegrityError as exc:
            raise RepositoryConflictError(
                "Snapshot mount stop proof conflicts with durable facts"
            ) from exc
        except SQLAlchemyError as exc:
            raise RepositoryUnavailableError(
                "Snapshot mount stop persistence is unavailable"
            ) from exc

    async def list_reconcilable(
        self,
        *,
        node_id: str,
        limit: int = 100,
    ) -> tuple[tuple[SnapshotMountLease, SnapshotMountPin], ...]:
        if limit < 1 or limit > 1000:
            raise ValueError("reconciliation limit must be between 1 and 1000")
        try:
            async with self._session_factory() as session:
                lease_ids: Sequence[str] = (
                    await session.scalars(
                        select(SnapshotMountLeaseRecord.id)
                        .where(
                            SnapshotMountLeaseRecord.target_node_id == node_id,
                            SnapshotMountLeaseRecord.status.in_(_RECONCILABLE_LEASE_STATUSES),
                        )
                        .order_by(
                            SnapshotMountLeaseRecord.updated_at,
                            SnapshotMountLeaseRecord.id,
                        )
                        .limit(limit)
                    )
                ).all()
                mounts = []
                for lease_id in lease_ids:
                    mount = await _load_mount(session, lease_id)
                    if mount is None:
                        raise RepositoryIntegrityError("SnapshotMountLease", lease_id)
                    mounts.append(mount)
                return tuple(mounts)
        except RepositoryIntegrityError:
            raise
        except SQLAlchemyError as exc:
            raise RepositoryUnavailableError("Snapshot mount reconciliation lookup failed") from exc


async def _require_authoritative_plan_binding(
    session: AsyncSession,
    plan: AuditStaticEffectPlan,
    *,
    integrity_id: str | None = None,
) -> None:
    audit = await session.get(AuditScanRecord, plan.audit_id)
    run = await session.get(RunRecord, plan.run_id)
    snapshot = await session.get(SourceSnapshotRecord, plan.snapshot_id)
    node = await session.get(NodeRecord, plan.node_id)
    reference = await session.get(
        SnapshotReferenceRecord,
        (plan.audit_id, plan.snapshot_id, plan.snapshot_reference_role),
    )
    if any(item is None for item in (audit, run, snapshot, node, reference)):
        _raise_owner_binding_failure(
            "Static effect Plan owner facts are incomplete",
            integrity_entity="AuditStaticEffectPlan",
            integrity_id=integrity_id,
        )
    assert audit is not None and run is not None and snapshot is not None and reference is not None
    if (
        audit.project_id != plan.project_id
        or audit.run_id != plan.run_id
        or audit.snapshot_id != plan.snapshot_id
        or audit.selected_node_id != plan.node_id
        or run.kind != "code_audit"
        or run.node_id != plan.node_id
        or snapshot.project_id != plan.project_id
        or snapshot.snapshot_digest != plan.snapshot_digest
        or snapshot.manifest_digest != plan.manifest_digest
        or reference.project_id != plan.project_id
        or plan.content_storage_key_digest
        != snapshot_storage_key_digest(snapshot.content_storage_key, role="content")
        or plan.manifest_storage_key_digest
        != snapshot_storage_key_digest(snapshot.manifest_storage_key, role="manifest")
        or plan.limits.input_bytes < snapshot.total_bytes
    ):
        _raise_owner_binding_failure(
            "Static effect Plan differs from authoritative facts",
            integrity_entity="AuditStaticEffectPlan",
            integrity_id=integrity_id,
        )


async def _require_runner_principal(
    session: AsyncSession,
    lease: SnapshotMountLease,
    *,
    require_current: bool,
    integrity_id: str | None = None,
) -> None:
    principal = lease.target_runner_principal
    credential = await session.get(RunnerCredentialRecord, principal.instance_id)
    node = await session.get(NodeRecord, lease.target_node_id)
    if credential is None or node is None:
        _raise_owner_binding_failure(
            "Snapshot mount Runner principal does not exist",
            integrity_entity="SnapshotMountLease",
            integrity_id=integrity_id,
        )
    if (
        credential.node_id != lease.target_node_id
        or credential.runner_epoch != principal.epoch
        or (require_current and node.current_runner_instance_id != principal.instance_id)
        or (require_current and node.current_runner_epoch != principal.epoch)
        or (require_current and credential.revoked_at is not None)
    ):
        _raise_owner_binding_failure(
            "Snapshot mount Runner principal is not current",
            integrity_entity="SnapshotMountLease",
            integrity_id=integrity_id,
        )


def _require_plan_lease_binding(
    plan: AuditStaticEffectPlan,
    lease: SnapshotMountLease,
    *,
    integrity_id: str | None = None,
) -> None:
    if (
        plan.operation_family is not AuditStaticOperationFamily.SNAPSHOT_MOUNT
        or plan.id != lease.plan_id
        or plan.plan_digest != lease.plan_digest
        or plan.project_id != lease.project_id
        or plan.audit_id != lease.audit_id
        or plan.run_id != lease.run_id
        or plan.snapshot_id != lease.snapshot_id
        or plan.snapshot_digest != lease.snapshot_digest
        or plan.manifest_digest != lease.manifest_digest
        or plan.node_id != lease.target_node_id
        or plan.backend_id != lease.backend_id
        or plan.backend_digest != lease.backend_digest
        or lease.max_bytes > plan.limits.input_bytes
    ):
        _raise_owner_binding_failure(
            "Snapshot mount Lease differs from its Plan",
            integrity_entity="SnapshotMountLease",
            integrity_id=integrity_id,
        )


def _raise_owner_binding_failure(
    message: str,
    *,
    integrity_entity: str,
    integrity_id: str | None,
) -> None:
    if integrity_id is not None:
        raise RepositoryIntegrityError(integrity_entity, integrity_id) from None
    raise RepositoryConflictError(message) from None


def _canonical_json(model: object) -> str:
    payload = model.model_dump(mode="json")  # type: ignore[attr-defined]
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    if len(encoded.encode("utf-8")) > _MAX_CANONICAL_BYTES:
        raise ValueError("static effect authority exceeds canonical byte limit")
    return encoded


def _parse_model(canonical_json: str, model_type: type[object], entity: str, entity_id: str):
    try:
        if (
            not isinstance(canonical_json, str)
            or len(canonical_json.encode("utf-8")) > _MAX_CANONICAL_BYTES
        ):
            raise ValueError("invalid canonical JSON")
        model = model_type.model_validate_json(canonical_json)  # type: ignore[attr-defined]
        if _canonical_json(model) != canonical_json:
            raise ValueError("noncanonical JSON")
        return model
    except (TypeError, ValueError):
        raise RepositoryIntegrityError(entity, entity_id) from None


def _require_exact(stored, requested, *, entity: str):
    if stored != requested:
        raise RepositoryConflictError(f"{entity} conflicts with durable ownership")
    return stored


def _plan_to_record(plan: AuditStaticEffectPlan) -> AuditStaticEffectPlanRecord:
    return AuditStaticEffectPlanRecord(
        id=plan.id,
        schema_version=plan.schema_version,
        canonical_json=_canonical_json(plan),
        plan_digest=plan.plan_digest,
        project_id=plan.project_id,
        audit_id=plan.audit_id,
        run_id=plan.run_id,
        snapshot_id=plan.snapshot_id,
        snapshot_reference_role=plan.snapshot_reference_role,
        snapshot_digest=plan.snapshot_digest,
        manifest_digest=plan.manifest_digest,
        operation_family=plan.operation_family.value,
        node_id=plan.node_id,
        backend_id=plan.backend_id,
        backend_digest=plan.backend_digest,
        created_by_policy=plan.created_by_policy,
        created_at=plan.created_at,
    )


async def _load_plan(session: AsyncSession, plan_id: str) -> AuditStaticEffectPlan | None:
    record = await session.get(AuditStaticEffectPlanRecord, plan_id)
    if record is None:
        return None
    plan = _parse_model(
        record.canonical_json, AuditStaticEffectPlan, "AuditStaticEffectPlan", plan_id
    )
    assert isinstance(plan, AuditStaticEffectPlan)
    expected = _plan_to_record(plan)
    _require_record_fields(record, expected, entity="AuditStaticEffectPlan", entity_id=plan_id)
    return plan


def _lease_to_record(lease: SnapshotMountLease) -> SnapshotMountLeaseRecord:
    return SnapshotMountLeaseRecord(
        id=lease.id,
        schema_version=lease.schema_version,
        canonical_json=_canonical_json(lease),
        lease_digest=lease.lease_digest,
        nonce_hash=lease.nonce_hash,
        project_id=lease.project_id,
        audit_id=lease.audit_id,
        run_id=lease.run_id,
        snapshot_id=lease.snapshot_id,
        snapshot_digest=lease.snapshot_digest,
        manifest_digest=lease.manifest_digest,
        plan_id=lease.plan_id,
        plan_digest=lease.plan_digest,
        effect_execution_id=lease.effect_execution_id,
        target_runner_instance_id=lease.target_runner_principal.instance_id,
        target_runner_epoch=lease.target_runner_principal.epoch,
        target_node_id=lease.target_node_id,
        backend_id=lease.backend_id,
        backend_digest=lease.backend_digest,
        status=lease.status.value,
        state_version=lease.state_version,
        mount_key=lease.mount_key,
        mount_proof_digest=lease.mount_proof_digest,
        stop_proof_digest=lease.stop_proof_digest,
        failure_code=lease.failure_code,
        expires_at=lease.expires_at,
        created_at=lease.created_at,
        activated_at=lease.activated_at,
        revocation_requested_at=lease.revocation_requested_at,
        terminated_at=lease.terminated_at,
        updated_at=lease.updated_at,
    )


def _pin_to_record(pin: SnapshotMountPin) -> SnapshotMountPinRecord:
    return SnapshotMountPinRecord(
        id=pin.id,
        schema_version=pin.schema_version,
        canonical_json=_canonical_json(pin),
        pin_digest=pin.pin_digest,
        lease_id=pin.lease_id,
        lease_digest=pin.lease_digest,
        plan_id=pin.plan_id,
        plan_digest=pin.plan_digest,
        effect_execution_id=pin.effect_execution_id,
        project_id=pin.project_id,
        audit_id=pin.audit_id,
        run_id=pin.run_id,
        snapshot_id=pin.snapshot_id,
        node_id=pin.node_id,
        backend_id=pin.backend_id,
        status=pin.status.value,
        state_version=pin.state_version,
        mount_key=pin.mount_key,
        mount_proof_digest=pin.mount_proof_digest,
        created_at=pin.created_at,
        updated_at=pin.updated_at,
        revoked_at=pin.revoked_at,
    )


def _proof_to_record(proof: SnapshotMountStopProof) -> SnapshotMountStopProofRecord:
    return SnapshotMountStopProofRecord(
        id=proof.id,
        schema_version=proof.schema_version,
        canonical_json=_canonical_json(proof),
        proof_digest=proof.proof_digest,
        lease_id=proof.lease_id,
        lease_digest=proof.lease_digest,
        pin_id=proof.pin_id,
        pin_digest=proof.pin_digest,
        plan_id=proof.plan_id,
        plan_digest=proof.plan_digest,
        effect_execution_id=proof.effect_execution_id,
        audit_id=proof.audit_id,
        run_id=proof.run_id,
        snapshot_id=proof.snapshot_id,
        node_id=proof.node_id,
        backend_id=proof.backend_id,
        disposition=proof.disposition.value,
        stopped_at=proof.stopped_at,
    )


async def _load_mount(
    session: AsyncSession,
    lease_id: str,
    *,
    for_update: bool = False,
) -> tuple[SnapshotMountLease, SnapshotMountPin] | None:
    statement = (
        select(SnapshotMountLeaseRecord, SnapshotMountPinRecord)
        .join(
            SnapshotMountPinRecord, SnapshotMountPinRecord.lease_id == SnapshotMountLeaseRecord.id
        )
        .where(SnapshotMountLeaseRecord.id == lease_id)
    )
    if for_update:
        statement = statement.with_for_update()
    row = (await session.execute(statement)).one_or_none()
    if row is None:
        if await session.get(SnapshotMountLeaseRecord, lease_id) is not None:
            raise RepositoryIntegrityError("SnapshotMountLease", lease_id)
        return None
    lease_record, pin_record = row
    lease = _parse_model(
        lease_record.canonical_json,
        SnapshotMountLease,
        "SnapshotMountLease",
        lease_id,
    )
    pin = _parse_model(
        pin_record.canonical_json, SnapshotMountPin, "SnapshotMountPin", pin_record.id
    )
    assert isinstance(lease, SnapshotMountLease) and isinstance(pin, SnapshotMountPin)
    _require_record_fields(
        lease_record,
        _lease_to_record(lease),
        entity="SnapshotMountLease",
        entity_id=lease.id,
    )
    _require_record_fields(
        pin_record,
        _pin_to_record(pin),
        entity="SnapshotMountPin",
        entity_id=pin.id,
    )
    try:
        pin._require_lease_binding(lease)
    except ValueError:
        raise RepositoryIntegrityError("SnapshotMountPin", pin.id) from None
    return lease, pin


async def _load_mount_by_effect(
    session: AsyncSession,
    effect_execution_id: str,
) -> tuple[SnapshotMountLease, SnapshotMountPin] | None:
    lease_id = await session.scalar(
        select(SnapshotMountLeaseRecord.id).where(
            SnapshotMountLeaseRecord.effect_execution_id == effect_execution_id
        )
    )
    return None if lease_id is None else await _load_mount(session, lease_id)


async def _load_stop_proof_for_lease(
    session: AsyncSession,
    lease_id: str,
) -> SnapshotMountStopProof | None:
    record = await session.scalar(
        select(SnapshotMountStopProofRecord).where(
            SnapshotMountStopProofRecord.lease_id == lease_id
        )
    )
    if record is None:
        return None
    proof = _parse_model(
        record.canonical_json,
        SnapshotMountStopProof,
        "SnapshotMountStopProof",
        record.id,
    )
    assert isinstance(proof, SnapshotMountStopProof)
    _require_record_fields(
        record,
        _proof_to_record(proof),
        entity="SnapshotMountStopProof",
        entity_id=proof.id,
    )
    return proof


def _require_record_fields(record, expected, *, entity: str, entity_id: str) -> None:
    for column in record.__table__.columns:
        if getattr(record, column.name) != getattr(expected, column.name):
            raise RepositoryIntegrityError(entity, entity_id)


def _validate_mount_transition(
    previous_lease: SnapshotMountLease,
    updated_lease: SnapshotMountLease,
    previous_pin: SnapshotMountPin,
    updated_pin: SnapshotMountPin,
) -> None:
    previous_pin._require_lease_binding(previous_lease)
    updated_pin._require_lease_binding(updated_lease)
    if (
        previous_lease.id != updated_lease.id
        or previous_lease.lease_digest != updated_lease.lease_digest
    ):
        raise ValueError("Snapshot mount Lease identity changed")
    if previous_pin.id != updated_pin.id or previous_pin.pin_digest != updated_pin.pin_digest:
        raise ValueError("Snapshot mount Pin identity changed")
    lease_changed = previous_lease != updated_lease
    pin_changed = previous_pin != updated_pin
    if not lease_changed and not pin_changed:
        raise ValueError("Snapshot mount CAS must change durable state")
    if lease_changed and updated_lease.state_version != previous_lease.state_version + 1:
        raise ValueError("Snapshot mount Lease version is not consecutive")
    if not lease_changed and updated_lease.state_version != previous_lease.state_version:
        raise ValueError("unchanged Snapshot mount Lease version differs")
    if pin_changed and updated_pin.state_version != previous_pin.state_version + 1:
        raise ValueError("Snapshot mount Pin version is not consecutive")
    if not pin_changed and updated_pin.state_version != previous_pin.state_version:
        raise ValueError("unchanged Snapshot mount Pin version differs")
    allowed_lease_edges = {
        (SnapshotMountLeaseStatus.ISSUED, SnapshotMountLeaseStatus.ACTIVE),
        (SnapshotMountLeaseStatus.ISSUED, SnapshotMountLeaseStatus.OUTCOME_UNKNOWN),
        (SnapshotMountLeaseStatus.ACTIVE, SnapshotMountLeaseStatus.REVOCATION_PENDING),
        (SnapshotMountLeaseStatus.ACTIVE, SnapshotMountLeaseStatus.EXPIRATION_PENDING),
        (SnapshotMountLeaseStatus.ACTIVE, SnapshotMountLeaseStatus.OUTCOME_UNKNOWN),
        (SnapshotMountLeaseStatus.REVOCATION_PENDING, SnapshotMountLeaseStatus.REVOKED),
        (SnapshotMountLeaseStatus.EXPIRATION_PENDING, SnapshotMountLeaseStatus.EXPIRED),
        (SnapshotMountLeaseStatus.REVOCATION_PENDING, SnapshotMountLeaseStatus.OUTCOME_UNKNOWN),
        (SnapshotMountLeaseStatus.EXPIRATION_PENDING, SnapshotMountLeaseStatus.OUTCOME_UNKNOWN),
    }
    if lease_changed and (previous_lease.status, updated_lease.status) not in allowed_lease_edges:
        raise ValueError("Snapshot mount Lease transition is invalid")
    allowed_pin_edges = {
        (SnapshotMountPinStatus.PENDING, SnapshotMountPinStatus.ACTIVE),
        (SnapshotMountPinStatus.ACTIVE, SnapshotMountPinStatus.REVOCATION_PENDING),
        (SnapshotMountPinStatus.REVOCATION_PENDING, SnapshotMountPinStatus.REVOKED),
    }
    if pin_changed and (previous_pin.status, updated_pin.status) not in allowed_pin_edges:
        raise ValueError("Snapshot mount Pin transition is invalid")


async def _cas_mount_records(
    session: AsyncSession,
    *,
    previous_lease: SnapshotMountLease,
    updated_lease: SnapshotMountLease,
    previous_pin: SnapshotMountPin,
    updated_pin: SnapshotMountPin,
) -> None:
    if previous_lease != updated_lease:
        lease_values = {
            column.name: getattr(_lease_to_record(updated_lease), column.name)
            for column in SnapshotMountLeaseRecord.__table__.columns
            if column.name != "id"
        }
        result = await session.execute(
            update(SnapshotMountLeaseRecord)
            .where(
                SnapshotMountLeaseRecord.id == previous_lease.id,
                SnapshotMountLeaseRecord.status == previous_lease.status.value,
                SnapshotMountLeaseRecord.state_version == previous_lease.state_version,
            )
            .values(**lease_values)
        )
        if result.rowcount != 1:  # type: ignore[attr-defined]
            raise RepositoryConflictError("Snapshot mount Lease changed before CAS")
    if previous_pin != updated_pin:
        pin_values = {
            column.name: getattr(_pin_to_record(updated_pin), column.name)
            for column in SnapshotMountPinRecord.__table__.columns
            if column.name != "id"
        }
        result = await session.execute(
            update(SnapshotMountPinRecord)
            .where(
                SnapshotMountPinRecord.id == previous_pin.id,
                SnapshotMountPinRecord.status == previous_pin.status.value,
                SnapshotMountPinRecord.state_version == previous_pin.state_version,
            )
            .values(**pin_values)
        )
        if result.rowcount != 1:  # type: ignore[attr-defined]
            raise RepositoryConflictError("Snapshot mount Pin changed before CAS")
    await session.flush()


__all__ = [
    "AuditStaticEffectPlanRecord",
    "SQLAlchemyAuditStaticEffectAuthorityRepository",
    "SnapshotMountLeaseRecord",
    "SnapshotMountPinRecord",
    "SnapshotMountStopProofRecord",
]
