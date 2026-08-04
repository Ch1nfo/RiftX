"""Static Snapshot effect plans and same-node mount authority contracts."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
from datetime import datetime
from enum import StrEnum
from typing import Literal, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from riftx.domain.base import new_id, utc_now
from riftx.domain.runner import RunnerPrincipal

AUDIT_STATIC_EFFECT_PLAN_SCHEMA_VERSION: Literal["riftx.audit-static-effect-plan/v1"] = (
    "riftx.audit-static-effect-plan/v1"
)
AUDIT_STATIC_EFFECT_LIMITS_SCHEMA_VERSION: Literal["riftx.audit-static-effect-limits/v1"] = (
    "riftx.audit-static-effect-limits/v1"
)
SNAPSHOT_MOUNT_LEASE_SCHEMA_VERSION: Literal["riftx.snapshot-mount-lease/v1"] = (
    "riftx.snapshot-mount-lease/v1"
)
SNAPSHOT_MOUNT_PIN_SCHEMA_VERSION: Literal["riftx.snapshot-mount-pin/v1"] = (
    "riftx.snapshot-mount-pin/v1"
)
SNAPSHOT_MOUNT_STOP_PROOF_SCHEMA_VERSION: Literal["riftx.snapshot-mount-stop-proof/v1"] = (
    "riftx.snapshot-mount-stop-proof/v1"
)
SNAPSHOT_MOUNT_BACKEND_ID = "private_materialization"
SNAPSHOT_MOUNT_NODE_ID = "local"
SNAPSHOT_STORAGE_KEY_DIGEST_SCHEMA_VERSION = "riftx.snapshot-storage-key-digest/v1"

_DIGEST_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_SAFE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@+~\-]{0,127}$")
_MOUNT_KEY_PATTERN = re.compile(r"^snapshot-mount:v1:[0-9a-f]{64}$")
_NONCE_PATTERN = re.compile(r"^[A-Za-z0-9_-]{43}$")
_MAX_COUNTER = 2**63 - 1
_MAX_ALLOWED_BLOBS = 200_000


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _domain_digest(domain: str, value: object) -> str:
    return hashlib.sha256(
        domain.encode("ascii") + b"\0" + _canonical_json(value).encode("utf-8")
    ).hexdigest()


def _require_digest(value: str, *, label: str) -> None:
    if not isinstance(value, str) or _DIGEST_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")


def _require_safe_id(value: str, *, label: str) -> None:
    if not isinstance(value, str) or _SAFE_ID_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{label} is invalid")


def _require_aware_datetime(value: datetime, *, label: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
        strict=True,
        validate_default=True,
    )

    def _validated_update(self, **updates: object) -> Self:
        """Rebuild the frozen model so every lifecycle transition is validated."""

        payload = self.model_dump(mode="python")
        payload.update(updates)
        return type(self).model_validate(payload)


class AuditStaticOperationFamily(StrEnum):
    SNAPSHOT_MATERIALIZE = "snapshot_materialize"
    SNAPSHOT_MOUNT = "snapshot_mount"


class SnapshotMountLeaseStatus(StrEnum):
    ISSUED = "issued"
    ACTIVE = "active"
    REVOCATION_PENDING = "revocation_pending"
    EXPIRATION_PENDING = "expiration_pending"
    REVOKED = "revoked"
    EXPIRED = "expired"
    OUTCOME_UNKNOWN = "outcome_unknown"


class SnapshotMountPinStatus(StrEnum):
    PENDING = "pending"
    ACTIVE = "active"
    REVOCATION_PENDING = "revocation_pending"
    REVOKED = "revoked"


class SnapshotMountStopDisposition(StrEnum):
    REVOKED = "revoked"
    EXPIRED = "expired"


class AuditStaticEffectLimits(_StrictModel):
    schema_version: Literal["riftx.audit-static-effect-limits/v1"] = (
        AUDIT_STATIC_EFFECT_LIMITS_SCHEMA_VERSION
    )
    cpu_millis: int = Field(strict=True, ge=1, le=86_400_000)
    memory_bytes: int = Field(strict=True, ge=1, le=_MAX_COUNTER)
    pids: int = Field(strict=True, ge=1, le=65_536)
    wall_seconds: int = Field(strict=True, ge=1, le=86_400)
    disk_bytes: int = Field(strict=True, ge=1, le=_MAX_COUNTER)
    file_count: int = Field(strict=True, ge=1, le=_MAX_COUNTER)
    input_bytes: int = Field(strict=True, ge=1, le=_MAX_COUNTER)
    output_bytes: int = Field(strict=True, ge=0, le=_MAX_COUNTER)


class AuditStaticReadOnlyMount(_StrictModel):
    role: Literal["source_snapshot"] = "source_snapshot"
    snapshot_id: str = Field(min_length=1, max_length=128)
    snapshot_digest: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    manifest_digest: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    read_only: Literal[True] = True


class AuditStaticEffectPlan(_StrictModel):
    """Policy-created immutable authority for one Snapshot static effect family."""

    id: str = Field(default_factory=new_id, min_length=1, max_length=128)
    schema_version: Literal["riftx.audit-static-effect-plan/v1"] = (
        AUDIT_STATIC_EFFECT_PLAN_SCHEMA_VERSION
    )
    project_id: str = Field(min_length=1, max_length=128)
    audit_id: str = Field(min_length=1, max_length=128)
    run_id: str = Field(min_length=1, max_length=128)
    snapshot_id: str = Field(min_length=1, max_length=128)
    snapshot_reference_role: Literal["primary"] = "primary"
    snapshot_digest: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    manifest_digest: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    operation_family: AuditStaticOperationFamily
    node_id: str = Field(default=SNAPSHOT_MOUNT_NODE_ID, min_length=1, max_length=128)
    backend_id: Literal["private_materialization"] = SNAPSHOT_MOUNT_BACKEND_ID
    backend_digest: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    image_digest: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    policy_digest: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    content_storage_key_digest: str = Field(
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
    )
    manifest_storage_key_digest: str = Field(
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
    )
    read_only_mounts: tuple[AuditStaticReadOnlyMount, ...] = Field(min_length=1, max_length=8)
    unique_output_root_digest: str = Field(
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
    )
    network: Literal["none"] = "none"
    clean_env_digest: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    limits: AuditStaticEffectLimits
    input_manifest_digest: str = Field(
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
    )
    output_contract_digest: str = Field(
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
    )
    created_by_policy: Literal["riftx_policy"] = "riftx_policy"
    policy_version: str = Field(min_length=1, max_length=128)
    created_at: AwareDatetime = Field(default_factory=utc_now)
    plan_digest: str = Field(
        default="",
        min_length=0,
        max_length=64,
        pattern=r"^(?:|[0-9a-f]{64})$",
    )

    @model_validator(mode="after")
    def validate_plan(self) -> AuditStaticEffectPlan:
        for label, value in (
            ("id", self.id),
            ("project_id", self.project_id),
            ("audit_id", self.audit_id),
            ("run_id", self.run_id),
            ("snapshot_id", self.snapshot_id),
            ("node_id", self.node_id),
            ("policy_version", self.policy_version),
        ):
            _require_safe_id(value, label=label)
        if len(self.read_only_mounts) != 1:
            raise ValueError("Snapshot static plan requires exactly one source mount")
        mount = self.read_only_mounts[0]
        if (
            mount.snapshot_id != self.snapshot_id
            or mount.snapshot_digest != self.snapshot_digest
            or mount.manifest_digest != self.manifest_digest
        ):
            raise ValueError("Static plan mount does not match its Snapshot")
        if self.input_manifest_digest != self.manifest_digest:
            raise ValueError("Static plan input Manifest digest differs")
        if self.limits.input_bytes < 1 or self.limits.disk_bytes < self.limits.input_bytes:
            raise ValueError("Static plan disk limit cannot contain its input")
        expected = audit_static_effect_plan_digest(self)
        if self.plan_digest:
            if not hmac.compare_digest(self.plan_digest, expected):
                raise ValueError("Static effect plan digest does not match")
        else:
            object.__setattr__(self, "plan_digest", expected)
        return self


class SnapshotMountLease(_StrictModel):
    """Durable, revocable authority for one Audit-bound private Snapshot view."""

    id: str = Field(default_factory=new_id, min_length=1, max_length=128)
    schema_version: Literal["riftx.snapshot-mount-lease/v1"] = SNAPSHOT_MOUNT_LEASE_SCHEMA_VERSION
    nonce_hash: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    project_id: str = Field(min_length=1, max_length=128)
    audit_id: str = Field(min_length=1, max_length=128)
    run_id: str = Field(min_length=1, max_length=128)
    snapshot_id: str = Field(min_length=1, max_length=128)
    snapshot_digest: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    manifest_digest: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    plan_id: str = Field(min_length=1, max_length=128)
    plan_digest: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    effect_execution_id: str = Field(min_length=1, max_length=128)
    target_runner_principal: RunnerPrincipal
    target_node_id: str = Field(default=SNAPSHOT_MOUNT_NODE_ID, min_length=1, max_length=128)
    backend_id: Literal["private_materialization"] = SNAPSHOT_MOUNT_BACKEND_ID
    backend_digest: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    allowed_blob_digests: tuple[str, ...] = Field(min_length=1, max_length=_MAX_ALLOWED_BLOBS)
    max_bytes: int = Field(strict=True, ge=1, le=_MAX_COUNTER)
    expires_at: AwareDatetime
    mount_policy_digest: str = Field(
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
    )
    status: SnapshotMountLeaseStatus = SnapshotMountLeaseStatus.ISSUED
    state_version: int = Field(default=1, strict=True, ge=1, le=_MAX_COUNTER)
    mount_key: str | None = Field(default=None, min_length=1, max_length=128, repr=False)
    mount_proof_digest: str | None = Field(
        default=None,
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
    )
    stop_proof_digest: str | None = Field(
        default=None,
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
    )
    failure_code: str | None = Field(default=None, min_length=1, max_length=128)
    created_at: AwareDatetime = Field(default_factory=utc_now)
    activated_at: AwareDatetime | None = None
    revocation_requested_at: AwareDatetime | None = None
    terminated_at: AwareDatetime | None = None
    updated_at: AwareDatetime = Field(default_factory=utc_now)
    lease_digest: str = Field(
        default="",
        min_length=0,
        max_length=64,
        pattern=r"^(?:|[0-9a-f]{64})$",
    )

    @model_validator(mode="after")
    def validate_lease(self) -> SnapshotMountLease:
        for label, value in (
            ("id", self.id),
            ("project_id", self.project_id),
            ("audit_id", self.audit_id),
            ("run_id", self.run_id),
            ("snapshot_id", self.snapshot_id),
            ("plan_id", self.plan_id),
            ("effect_execution_id", self.effect_execution_id),
            ("target_node_id", self.target_node_id),
        ):
            _require_safe_id(value, label=label)
        if self.allowed_blob_digests != tuple(sorted(set(self.allowed_blob_digests))):
            raise ValueError("Snapshot mount blob allowlist must be sorted and unique")
        for digest in self.allowed_blob_digests:
            _require_digest(digest, label="allowed_blob_digest")
        if not self.created_at < self.expires_at or self.updated_at < self.created_at:
            raise ValueError("Snapshot mount lease timestamps are invalid")
        self._validate_lifecycle_shape()
        expected = snapshot_mount_lease_digest(self)
        if self.lease_digest:
            if not hmac.compare_digest(self.lease_digest, expected):
                raise ValueError("Snapshot mount lease digest does not match")
        else:
            object.__setattr__(self, "lease_digest", expected)
        return self

    def _validate_lifecycle_shape(self) -> None:
        if self.mount_key is not None and _MOUNT_KEY_PATTERN.fullmatch(self.mount_key) is None:
            raise ValueError("Snapshot mount key is invalid")
        if self.failure_code is not None:
            _require_safe_id(self.failure_code, label="failure_code")
        if self.status is SnapshotMountLeaseStatus.ISSUED:
            if (
                self.state_version != 1
                or self.mount_key is not None
                or self.mount_proof_digest is not None
                or self.activated_at is not None
                or self.revocation_requested_at is not None
                or self.terminated_at is not None
                or self.stop_proof_digest is not None
                or self.failure_code is not None
                or self.updated_at != self.created_at
            ):
                raise ValueError("issued Snapshot mount lease shape is invalid")
            return
        has_mount_proof = (
            self.mount_key is not None
            and self.mount_proof_digest is not None
            and self.activated_at is not None
        )
        has_partial_mount_proof = (
            any(
                value is not None
                for value in (self.mount_key, self.mount_proof_digest, self.activated_at)
            )
            and not has_mount_proof
        )
        if has_partial_mount_proof:
            raise ValueError("Snapshot mount lease has a partial mount proof")
        if has_mount_proof:
            assert self.activated_at is not None
            if not self.created_at <= self.activated_at < self.expires_at:
                raise ValueError("Snapshot mount activation timestamp is invalid")
            if self.activated_at > self.updated_at:
                raise ValueError("Snapshot mount update predates activation")
        if self.status is SnapshotMountLeaseStatus.ACTIVE:
            if self.state_version != 2:
                raise ValueError("active Snapshot mount lease version is invalid")
            if not has_mount_proof:
                raise ValueError("active Snapshot mount lease lacks mount proof")
            if any(
                value is not None
                for value in (
                    self.revocation_requested_at,
                    self.terminated_at,
                    self.stop_proof_digest,
                    self.failure_code,
                )
            ):
                raise ValueError("active Snapshot mount lease shape is invalid")
            return
        if self.status in {
            SnapshotMountLeaseStatus.REVOCATION_PENDING,
            SnapshotMountLeaseStatus.EXPIRATION_PENDING,
        }:
            if (
                self.state_version != 3
                or not has_mount_proof
                or self.revocation_requested_at is None
                or self.terminated_at is not None
                or self.stop_proof_digest is not None
                or self.failure_code is not None
            ):
                raise ValueError("pending Snapshot mount revocation shape is invalid")
            assert self.activated_at is not None
            assert self.revocation_requested_at is not None
            if not self.activated_at <= self.revocation_requested_at <= self.updated_at:
                raise ValueError("Snapshot mount revocation timestamp is invalid")
            expired = self.revocation_requested_at >= self.expires_at
            if expired != (self.status is SnapshotMountLeaseStatus.EXPIRATION_PENDING):
                raise ValueError("Snapshot mount pending status differs from expiry")
            return
        if self.status in {SnapshotMountLeaseStatus.REVOKED, SnapshotMountLeaseStatus.EXPIRED}:
            if (
                self.state_version != 4
                or not has_mount_proof
                or self.revocation_requested_at is None
                or self.terminated_at is None
                or self.stop_proof_digest is None
                or self.failure_code is not None
            ):
                raise ValueError("terminal Snapshot mount lease shape is invalid")
            assert self.revocation_requested_at is not None
            assert self.terminated_at is not None
            if not self.revocation_requested_at <= self.terminated_at <= self.updated_at:
                raise ValueError("Snapshot mount termination timestamp is invalid")
            expired = self.revocation_requested_at >= self.expires_at
            if expired != (self.status is SnapshotMountLeaseStatus.EXPIRED):
                raise ValueError("Snapshot mount terminal status differs from expiry")
            return
        if self.status is SnapshotMountLeaseStatus.OUTCOME_UNKNOWN:
            if (
                self.state_version < 2
                or self.failure_code is None
                or self.stop_proof_digest is not None
                or self.terminated_at is not None
            ):
                raise ValueError("unknown Snapshot mount outcome lacks failure code")
            if self.revocation_requested_at is not None:
                if not self.created_at <= self.revocation_requested_at <= self.updated_at:
                    raise ValueError("unknown Snapshot mount revocation timestamp is invalid")
            return
        raise ValueError("unknown Snapshot mount lease status")

    @classmethod
    def issue(
        cls,
        *,
        plan: AuditStaticEffectPlan,
        effect_execution_id: str,
        target_runner_principal: RunnerPrincipal,
        allowed_blob_digests: tuple[str, ...],
        max_bytes: int,
        expires_at: datetime,
        mount_policy_digest: str,
        lease_id: str | None = None,
        created_at: datetime | None = None,
    ) -> SnapshotMountLeaseIssue:
        if plan.operation_family is not AuditStaticOperationFamily.SNAPSHOT_MOUNT:
            raise ValueError("Snapshot mount lease requires a snapshot_mount plan")
        if max_bytes > plan.limits.input_bytes:
            raise ValueError("Snapshot mount Lease byte cap exceeds its Plan input limit")
        nonce = secrets.token_urlsafe(32)
        now = created_at or utc_now()
        lease = cls(
            id=lease_id or new_id(),
            nonce_hash=snapshot_mount_nonce_hash(nonce),
            project_id=plan.project_id,
            audit_id=plan.audit_id,
            run_id=plan.run_id,
            snapshot_id=plan.snapshot_id,
            snapshot_digest=plan.snapshot_digest,
            manifest_digest=plan.manifest_digest,
            plan_id=plan.id,
            plan_digest=plan.plan_digest,
            effect_execution_id=effect_execution_id,
            target_runner_principal=target_runner_principal,
            target_node_id=plan.node_id,
            backend_id=plan.backend_id,
            backend_digest=plan.backend_digest,
            allowed_blob_digests=allowed_blob_digests,
            max_bytes=max_bytes,
            expires_at=expires_at,
            mount_policy_digest=mount_policy_digest,
            created_at=now,
            updated_at=now,
        )
        return SnapshotMountLeaseIssue(lease=lease, nonce=nonce)

    def accepts(
        self,
        *,
        nonce: str,
        principal: RunnerPrincipal,
        node_id: str,
        observed_at: datetime,
    ) -> bool:
        if (
            not isinstance(observed_at, datetime)
            or observed_at.tzinfo is None
            or observed_at.utcoffset() is None
            or self.activated_at is None
        ):
            return False
        try:
            nonce_hash = snapshot_mount_nonce_hash(nonce)
        except ValueError:
            return False
        return (
            self.status is SnapshotMountLeaseStatus.ACTIVE
            and self.activated_at <= observed_at
            and observed_at < self.expires_at
            and principal == self.target_runner_principal
            and node_id == self.target_node_id
            and hmac.compare_digest(self.nonce_hash, nonce_hash)
        )

    def activate(
        self,
        *,
        mount_key: str,
        mount_proof_digest: str,
        activated_at: datetime,
    ) -> Self:
        _require_aware_datetime(activated_at, label="activated_at")
        if self.status is not SnapshotMountLeaseStatus.ISSUED or activated_at >= self.expires_at:
            raise ValueError("Snapshot mount lease cannot activate")
        if activated_at < self.created_at:
            raise ValueError("Snapshot mount activation predates lease issuance")
        return self._validated_update(
            status=SnapshotMountLeaseStatus.ACTIVE,
            state_version=self.state_version + 1,
            mount_key=mount_key,
            mount_proof_digest=mount_proof_digest,
            activated_at=activated_at,
            updated_at=activated_at,
        )

    def begin_stop(self, *, expired: bool, requested_at: datetime) -> Self:
        _require_aware_datetime(requested_at, label="requested_at")
        if self.status is not SnapshotMountLeaseStatus.ACTIVE:
            raise ValueError("Snapshot mount lease is not active")
        if requested_at < self.updated_at or expired != (requested_at >= self.expires_at):
            raise ValueError("Snapshot mount stop request differs from lease expiry")
        return self._validated_update(
            status=(
                SnapshotMountLeaseStatus.EXPIRATION_PENDING
                if expired
                else SnapshotMountLeaseStatus.REVOCATION_PENDING
            ),
            state_version=self.state_version + 1,
            revocation_requested_at=requested_at,
            updated_at=requested_at,
        )

    def finish_stop(
        self,
        proof: SnapshotMountStopProof,
        *,
        pin: SnapshotMountPin,
    ) -> Self:
        if self.status not in {
            SnapshotMountLeaseStatus.REVOCATION_PENDING,
            SnapshotMountLeaseStatus.EXPIRATION_PENDING,
        }:
            raise ValueError("Snapshot mount lease is not awaiting stop proof")
        expected_status = (
            SnapshotMountLeaseStatus.EXPIRED
            if proof.disposition is SnapshotMountStopDisposition.EXPIRED
            else SnapshotMountLeaseStatus.REVOKED
        )
        if (
            proof.lease_id != self.id
            or proof.lease_digest != self.lease_digest
            or proof.pin_id != pin.id
            or proof.pin_digest != pin.pin_digest
            or proof.plan_id != self.plan_id
            or proof.plan_digest != self.plan_digest
            or proof.effect_execution_id != self.effect_execution_id
            or proof.project_id != self.project_id
            or proof.audit_id != self.audit_id
            or proof.run_id != self.run_id
            or proof.snapshot_id != self.snapshot_id
            or proof.snapshot_digest != self.snapshot_digest
            or proof.manifest_digest != self.manifest_digest
            or proof.node_id != self.target_node_id
            or proof.backend_id != self.backend_id
            or proof.backend_digest != self.backend_digest
            or proof.principal != self.target_runner_principal
            or (
                self.mount_key is not None
                and proof.mount_key_digest != snapshot_mount_key_digest(self.mount_key)
            )
        ):
            raise ValueError("Snapshot mount stop proof owner binding differs")
        pin._require_lease_binding(self)
        if pin.status is not SnapshotMountPinStatus.REVOCATION_PENDING:
            raise ValueError("Snapshot mount pin is not awaiting stop proof")
        if proof.stopped_at < self.updated_at:
            raise ValueError("Snapshot mount stop proof predates revocation")
        if (proof.disposition is SnapshotMountStopDisposition.EXPIRED) != (
            self.status is SnapshotMountLeaseStatus.EXPIRATION_PENDING
        ):
            raise ValueError("Snapshot mount stop proof disposition differs")
        return self._validated_update(
            status=expected_status,
            state_version=self.state_version + 1,
            stop_proof_digest=proof.proof_digest,
            terminated_at=proof.stopped_at,
            updated_at=proof.stopped_at,
        )

    def mark_outcome_unknown(self, *, failure_code: str, observed_at: datetime) -> Self:
        _require_aware_datetime(observed_at, label="observed_at")
        if self.status in {SnapshotMountLeaseStatus.REVOKED, SnapshotMountLeaseStatus.EXPIRED}:
            raise ValueError("terminal Snapshot mount lease cannot become unknown")
        if observed_at < self.updated_at:
            raise ValueError("Snapshot mount unknown outcome predates durable state")
        return self._validated_update(
            status=SnapshotMountLeaseStatus.OUTCOME_UNKNOWN,
            state_version=self.state_version + 1,
            failure_code=failure_code,
            updated_at=observed_at,
        )


class SnapshotMountLeaseIssue(_StrictModel):
    lease: SnapshotMountLease
    nonce: str = Field(min_length=43, max_length=43, repr=False)

    @model_validator(mode="after")
    def validate_issue(self) -> SnapshotMountLeaseIssue:
        if _NONCE_PATTERN.fullmatch(self.nonce) is None:
            raise ValueError("Snapshot mount nonce is invalid")
        if not hmac.compare_digest(self.lease.nonce_hash, snapshot_mount_nonce_hash(self.nonce)):
            raise ValueError("Snapshot mount nonce does not match lease")
        return self


class SnapshotMountPin(_StrictModel):
    id: str = Field(default_factory=new_id, min_length=1, max_length=128)
    schema_version: Literal["riftx.snapshot-mount-pin/v1"] = SNAPSHOT_MOUNT_PIN_SCHEMA_VERSION
    lease_id: str = Field(min_length=1, max_length=128)
    lease_digest: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    plan_id: str = Field(min_length=1, max_length=128)
    plan_digest: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    effect_execution_id: str = Field(min_length=1, max_length=128)
    project_id: str = Field(min_length=1, max_length=128)
    audit_id: str = Field(min_length=1, max_length=128)
    run_id: str = Field(min_length=1, max_length=128)
    snapshot_id: str = Field(min_length=1, max_length=128)
    snapshot_digest: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    manifest_digest: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    node_id: str = Field(default=SNAPSHOT_MOUNT_NODE_ID, min_length=1, max_length=128)
    backend_id: Literal["private_materialization"] = SNAPSHOT_MOUNT_BACKEND_ID
    backend_digest: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    target_runner_principal: RunnerPrincipal
    status: SnapshotMountPinStatus = SnapshotMountPinStatus.PENDING
    state_version: int = Field(default=1, strict=True, ge=1, le=_MAX_COUNTER)
    mount_key: str | None = Field(default=None, min_length=1, max_length=128, repr=False)
    mount_proof_digest: str | None = Field(
        default=None,
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
    )
    created_at: AwareDatetime = Field(default_factory=utc_now)
    updated_at: AwareDatetime = Field(default_factory=utc_now)
    revoked_at: AwareDatetime | None = None
    pin_digest: str = Field(
        default="",
        min_length=0,
        max_length=64,
        pattern=r"^(?:|[0-9a-f]{64})$",
    )

    @model_validator(mode="after")
    def validate_pin(self) -> SnapshotMountPin:
        for label, value in (
            ("id", self.id),
            ("lease_id", self.lease_id),
            ("plan_id", self.plan_id),
            ("effect_execution_id", self.effect_execution_id),
            ("project_id", self.project_id),
            ("audit_id", self.audit_id),
            ("run_id", self.run_id),
            ("snapshot_id", self.snapshot_id),
            ("node_id", self.node_id),
        ):
            _require_safe_id(value, label=label)
        if self.updated_at < self.created_at:
            raise ValueError("Snapshot mount pin timestamp moved backwards")
        if self.status is SnapshotMountPinStatus.PENDING:
            if (
                self.state_version != 1
                or self.mount_key is not None
                or self.mount_proof_digest is not None
                or self.revoked_at
                or self.updated_at != self.created_at
            ):
                raise ValueError("pending Snapshot mount pin shape is invalid")
        elif self.status is SnapshotMountPinStatus.ACTIVE:
            if (
                self.state_version != 2
                or self.mount_key is None
                or self.mount_proof_digest is None
                or self.revoked_at
            ):
                raise ValueError("active Snapshot mount pin shape is invalid")
        elif self.status is SnapshotMountPinStatus.REVOCATION_PENDING:
            if (
                self.state_version != 3
                or self.mount_key is None
                or self.mount_proof_digest is None
                or self.revoked_at
            ):
                raise ValueError("pending Snapshot pin revocation shape is invalid")
        elif self.status is SnapshotMountPinStatus.REVOKED:
            if (
                self.state_version != 4
                or self.mount_key is None
                or self.mount_proof_digest is None
                or self.revoked_at is None
                or self.revoked_at != self.updated_at
            ):
                raise ValueError("revoked Snapshot mount pin shape is invalid")
        else:
            raise ValueError("unknown Snapshot mount pin status")
        expected = snapshot_mount_pin_digest(self)
        if self.pin_digest:
            if not hmac.compare_digest(self.pin_digest, expected):
                raise ValueError("Snapshot mount pin digest does not match")
        else:
            object.__setattr__(self, "pin_digest", expected)
        return self

    @classmethod
    def for_lease(cls, lease: SnapshotMountLease, *, pin_id: str | None = None) -> Self:
        if lease.status is not SnapshotMountLeaseStatus.ISSUED:
            raise ValueError("Snapshot mount pin requires an issued lease")
        return cls(
            id=pin_id or new_id(),
            lease_id=lease.id,
            lease_digest=lease.lease_digest,
            plan_id=lease.plan_id,
            plan_digest=lease.plan_digest,
            effect_execution_id=lease.effect_execution_id,
            project_id=lease.project_id,
            audit_id=lease.audit_id,
            run_id=lease.run_id,
            snapshot_id=lease.snapshot_id,
            snapshot_digest=lease.snapshot_digest,
            manifest_digest=lease.manifest_digest,
            node_id=lease.target_node_id,
            backend_id=lease.backend_id,
            backend_digest=lease.backend_digest,
            target_runner_principal=lease.target_runner_principal,
            created_at=lease.created_at,
            updated_at=lease.created_at,
        )

    def activate(self, lease: SnapshotMountLease) -> Self:
        if (
            self.status is not SnapshotMountPinStatus.PENDING
            or lease.status is not SnapshotMountLeaseStatus.ACTIVE
        ):
            raise ValueError("Snapshot mount pin cannot activate")
        self._require_lease_binding(lease)
        return self._validated_update(
            status=SnapshotMountPinStatus.ACTIVE,
            state_version=self.state_version + 1,
            mount_key=lease.mount_key,
            mount_proof_digest=lease.mount_proof_digest,
            updated_at=lease.updated_at,
        )

    def begin_revocation(self, lease: SnapshotMountLease) -> Self:
        if self.status is not SnapshotMountPinStatus.ACTIVE or lease.status not in {
            SnapshotMountLeaseStatus.REVOCATION_PENDING,
            SnapshotMountLeaseStatus.EXPIRATION_PENDING,
        }:
            raise ValueError("Snapshot mount pin cannot begin revocation")
        self._require_lease_binding(lease)
        return self._validated_update(
            status=SnapshotMountPinStatus.REVOCATION_PENDING,
            state_version=self.state_version + 1,
            updated_at=lease.updated_at,
        )

    def revoke(self, lease: SnapshotMountLease) -> Self:
        if self.status is not SnapshotMountPinStatus.REVOCATION_PENDING or lease.status not in {
            SnapshotMountLeaseStatus.REVOKED,
            SnapshotMountLeaseStatus.EXPIRED,
        }:
            raise ValueError("Snapshot mount pin cannot revoke")
        self._require_lease_binding(lease)
        assert lease.terminated_at is not None
        return self._validated_update(
            status=SnapshotMountPinStatus.REVOKED,
            state_version=self.state_version + 1,
            updated_at=lease.terminated_at,
            revoked_at=lease.terminated_at,
        )

    def _require_lease_binding(self, lease: SnapshotMountLease) -> None:
        if (
            self.lease_id != lease.id
            or self.lease_digest != lease.lease_digest
            or self.plan_id != lease.plan_id
            or self.plan_digest != lease.plan_digest
            or self.effect_execution_id != lease.effect_execution_id
            or self.project_id != lease.project_id
            or self.audit_id != lease.audit_id
            or self.run_id != lease.run_id
            or self.snapshot_id != lease.snapshot_id
            or self.snapshot_digest != lease.snapshot_digest
            or self.manifest_digest != lease.manifest_digest
            or self.node_id != lease.target_node_id
            or self.backend_id != lease.backend_id
            or self.backend_digest != lease.backend_digest
            or self.target_runner_principal != lease.target_runner_principal
        ):
            raise ValueError("Snapshot mount pin differs from lease authority")


class SnapshotMountStopProof(_StrictModel):
    id: str = Field(default_factory=new_id, min_length=1, max_length=128)
    schema_version: Literal["riftx.snapshot-mount-stop-proof/v1"] = (
        SNAPSHOT_MOUNT_STOP_PROOF_SCHEMA_VERSION
    )
    lease_id: str = Field(min_length=1, max_length=128)
    lease_digest: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    pin_id: str = Field(min_length=1, max_length=128)
    pin_digest: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    plan_id: str = Field(min_length=1, max_length=128)
    plan_digest: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    effect_execution_id: str = Field(min_length=1, max_length=128)
    project_id: str = Field(min_length=1, max_length=128)
    audit_id: str = Field(min_length=1, max_length=128)
    run_id: str = Field(min_length=1, max_length=128)
    snapshot_id: str = Field(min_length=1, max_length=128)
    snapshot_digest: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    manifest_digest: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    node_id: str = Field(default=SNAPSHOT_MOUNT_NODE_ID, min_length=1, max_length=128)
    backend_id: Literal["private_materialization"] = SNAPSHOT_MOUNT_BACKEND_ID
    backend_digest: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    principal: RunnerPrincipal
    mount_key_digest: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    disposition: SnapshotMountStopDisposition
    active_fd_count: Literal[0] = 0
    active_process_count: Literal[0] = 0
    mount_namespace_unmounted: Literal[True] = True
    lease_revoked: Literal[True] = True
    pin_revoked: Literal[True] = True
    worker_path_inaccessible: Literal[True] = True
    stopped_at: AwareDatetime = Field(default_factory=utc_now)
    proof_digest: str = Field(
        default="",
        min_length=0,
        max_length=64,
        pattern=r"^(?:|[0-9a-f]{64})$",
    )

    @classmethod
    def for_pending_stop(
        cls,
        *,
        lease: SnapshotMountLease,
        pin: SnapshotMountPin,
        disposition: SnapshotMountStopDisposition,
        stopped_at: datetime,
        proof_id: str | None = None,
    ) -> Self:
        _require_aware_datetime(stopped_at, label="stopped_at")
        if lease.status not in {
            SnapshotMountLeaseStatus.REVOCATION_PENDING,
            SnapshotMountLeaseStatus.EXPIRATION_PENDING,
        }:
            raise ValueError("Snapshot mount stop proof requires a pending lease")
        if pin.status is not SnapshotMountPinStatus.REVOCATION_PENDING:
            raise ValueError("Snapshot mount stop proof requires a pending pin")
        pin._require_lease_binding(lease)
        if lease.mount_key is None:
            raise ValueError("Snapshot mount stop proof requires a mount key")
        if (disposition is SnapshotMountStopDisposition.EXPIRED) != (
            lease.status is SnapshotMountLeaseStatus.EXPIRATION_PENDING
        ):
            raise ValueError("Snapshot mount stop disposition differs from lease")
        if stopped_at < lease.updated_at:
            raise ValueError("Snapshot mount stop proof predates revocation")
        return cls(
            id=proof_id or new_id(),
            lease_id=lease.id,
            lease_digest=lease.lease_digest,
            pin_id=pin.id,
            pin_digest=pin.pin_digest,
            plan_id=lease.plan_id,
            plan_digest=lease.plan_digest,
            effect_execution_id=lease.effect_execution_id,
            project_id=lease.project_id,
            audit_id=lease.audit_id,
            run_id=lease.run_id,
            snapshot_id=lease.snapshot_id,
            snapshot_digest=lease.snapshot_digest,
            manifest_digest=lease.manifest_digest,
            node_id=lease.target_node_id,
            backend_id=lease.backend_id,
            backend_digest=lease.backend_digest,
            principal=lease.target_runner_principal,
            mount_key_digest=snapshot_mount_key_digest(lease.mount_key),
            disposition=disposition,
            stopped_at=stopped_at,
        )

    @model_validator(mode="after")
    def validate_proof(self) -> SnapshotMountStopProof:
        for label, value in (
            ("id", self.id),
            ("lease_id", self.lease_id),
            ("pin_id", self.pin_id),
            ("plan_id", self.plan_id),
            ("effect_execution_id", self.effect_execution_id),
            ("project_id", self.project_id),
            ("audit_id", self.audit_id),
            ("run_id", self.run_id),
            ("snapshot_id", self.snapshot_id),
            ("node_id", self.node_id),
        ):
            _require_safe_id(value, label=label)
        expected = snapshot_mount_stop_proof_digest(self)
        if self.proof_digest:
            if not hmac.compare_digest(self.proof_digest, expected):
                raise ValueError("Snapshot mount stop proof digest does not match")
        else:
            object.__setattr__(self, "proof_digest", expected)
        return self


def audit_static_effect_plan_digest(plan: AuditStaticEffectPlan) -> str:
    return _domain_digest(
        AUDIT_STATIC_EFFECT_PLAN_SCHEMA_VERSION,
        {
            "audit_id": plan.audit_id,
            "backend_digest": plan.backend_digest,
            "backend_id": plan.backend_id,
            "clean_env_digest": plan.clean_env_digest,
            "content_storage_key_digest": plan.content_storage_key_digest,
            "created_at": plan.created_at.isoformat(),
            "created_by_policy": plan.created_by_policy,
            "id": plan.id,
            "image_digest": plan.image_digest,
            "input_manifest_digest": plan.input_manifest_digest,
            "limits": plan.limits.model_dump(mode="json"),
            "manifest_digest": plan.manifest_digest,
            "manifest_storage_key_digest": plan.manifest_storage_key_digest,
            "network": plan.network,
            "node_id": plan.node_id,
            "operation_family": plan.operation_family.value,
            "output_contract_digest": plan.output_contract_digest,
            "policy_digest": plan.policy_digest,
            "policy_version": plan.policy_version,
            "project_id": plan.project_id,
            "read_only_mounts": [mount.model_dump(mode="json") for mount in plan.read_only_mounts],
            "run_id": plan.run_id,
            "schema_version": plan.schema_version,
            "snapshot_digest": plan.snapshot_digest,
            "snapshot_id": plan.snapshot_id,
            "snapshot_reference_role": plan.snapshot_reference_role,
            "unique_output_root_digest": plan.unique_output_root_digest,
        },
    )


def snapshot_mount_nonce_hash(nonce: str) -> str:
    if not isinstance(nonce, str) or _NONCE_PATTERN.fullmatch(nonce) is None:
        raise ValueError("Snapshot mount nonce is invalid")
    return hashlib.sha256(b"riftx.snapshot-mount-nonce/v1\0" + nonce.encode("ascii")).hexdigest()


def snapshot_storage_key_digest(storage_key: str, *, role: Literal["content", "manifest"]) -> str:
    if not isinstance(storage_key, str) or not storage_key or len(storage_key) > 4096:
        raise ValueError("Snapshot storage key is invalid")
    return _domain_digest(
        SNAPSHOT_STORAGE_KEY_DIGEST_SCHEMA_VERSION,
        {"role": role, "storage_key": storage_key},
    )


def snapshot_mount_key_digest(mount_key: str) -> str:
    if not isinstance(mount_key, str) or _MOUNT_KEY_PATTERN.fullmatch(mount_key) is None:
        raise ValueError("Snapshot mount key is invalid")
    return hashlib.sha256(b"riftx.snapshot-mount-key/v1\0" + mount_key.encode("ascii")).hexdigest()


def snapshot_mount_lease_digest(lease: SnapshotMountLease) -> str:
    return _domain_digest(
        SNAPSHOT_MOUNT_LEASE_SCHEMA_VERSION,
        {
            "allowed_blob_digests": list(lease.allowed_blob_digests),
            "audit_id": lease.audit_id,
            "backend_digest": lease.backend_digest,
            "backend_id": lease.backend_id,
            "created_at": lease.created_at.isoformat(),
            "effect_execution_id": lease.effect_execution_id,
            "expires_at": lease.expires_at.isoformat(),
            "id": lease.id,
            "manifest_digest": lease.manifest_digest,
            "max_bytes": lease.max_bytes,
            "mount_policy_digest": lease.mount_policy_digest,
            "nonce_hash": lease.nonce_hash,
            "plan_digest": lease.plan_digest,
            "plan_id": lease.plan_id,
            "project_id": lease.project_id,
            "run_id": lease.run_id,
            "schema_version": lease.schema_version,
            "snapshot_digest": lease.snapshot_digest,
            "snapshot_id": lease.snapshot_id,
            "target_node_id": lease.target_node_id,
            "target_runner_principal": lease.target_runner_principal.model_dump(mode="json"),
        },
    )


def snapshot_mount_pin_digest(pin: SnapshotMountPin) -> str:
    return _domain_digest(
        SNAPSHOT_MOUNT_PIN_SCHEMA_VERSION,
        {
            "audit_id": pin.audit_id,
            "backend_digest": pin.backend_digest,
            "backend_id": pin.backend_id,
            "created_at": pin.created_at.isoformat(),
            "effect_execution_id": pin.effect_execution_id,
            "id": pin.id,
            "lease_digest": pin.lease_digest,
            "lease_id": pin.lease_id,
            "manifest_digest": pin.manifest_digest,
            "node_id": pin.node_id,
            "plan_digest": pin.plan_digest,
            "plan_id": pin.plan_id,
            "project_id": pin.project_id,
            "run_id": pin.run_id,
            "schema_version": pin.schema_version,
            "snapshot_digest": pin.snapshot_digest,
            "snapshot_id": pin.snapshot_id,
            "target_runner_principal": pin.target_runner_principal.model_dump(mode="json"),
        },
    )


def snapshot_mount_stop_proof_digest(proof: SnapshotMountStopProof) -> str:
    return _domain_digest(
        SNAPSHOT_MOUNT_STOP_PROOF_SCHEMA_VERSION,
        {
            "active_fd_count": proof.active_fd_count,
            "active_process_count": proof.active_process_count,
            "audit_id": proof.audit_id,
            "backend_digest": proof.backend_digest,
            "backend_id": proof.backend_id,
            "disposition": proof.disposition.value,
            "effect_execution_id": proof.effect_execution_id,
            "id": proof.id,
            "lease_digest": proof.lease_digest,
            "lease_id": proof.lease_id,
            "lease_revoked": proof.lease_revoked,
            "manifest_digest": proof.manifest_digest,
            "mount_key_digest": proof.mount_key_digest,
            "mount_namespace_unmounted": proof.mount_namespace_unmounted,
            "node_id": proof.node_id,
            "pin_id": proof.pin_id,
            "pin_digest": proof.pin_digest,
            "pin_revoked": proof.pin_revoked,
            "plan_digest": proof.plan_digest,
            "plan_id": proof.plan_id,
            "principal": proof.principal.model_dump(mode="json"),
            "project_id": proof.project_id,
            "run_id": proof.run_id,
            "schema_version": proof.schema_version,
            "snapshot_digest": proof.snapshot_digest,
            "snapshot_id": proof.snapshot_id,
            "stopped_at": proof.stopped_at.isoformat(),
            "worker_path_inaccessible": proof.worker_path_inaccessible,
        },
    )


__all__ = [
    "AUDIT_STATIC_EFFECT_LIMITS_SCHEMA_VERSION",
    "AUDIT_STATIC_EFFECT_PLAN_SCHEMA_VERSION",
    "SNAPSHOT_MOUNT_BACKEND_ID",
    "SNAPSHOT_MOUNT_LEASE_SCHEMA_VERSION",
    "SNAPSHOT_MOUNT_NODE_ID",
    "SNAPSHOT_MOUNT_PIN_SCHEMA_VERSION",
    "SNAPSHOT_MOUNT_STOP_PROOF_SCHEMA_VERSION",
    "SNAPSHOT_STORAGE_KEY_DIGEST_SCHEMA_VERSION",
    "AuditStaticEffectLimits",
    "AuditStaticEffectPlan",
    "AuditStaticOperationFamily",
    "AuditStaticReadOnlyMount",
    "SnapshotMountLease",
    "SnapshotMountLeaseIssue",
    "SnapshotMountLeaseStatus",
    "SnapshotMountPin",
    "SnapshotMountPinStatus",
    "SnapshotMountStopDisposition",
    "SnapshotMountStopProof",
    "audit_static_effect_plan_digest",
    "snapshot_mount_key_digest",
    "snapshot_mount_lease_digest",
    "snapshot_mount_nonce_hash",
    "snapshot_mount_pin_digest",
    "snapshot_mount_stop_proof_digest",
    "snapshot_storage_key_digest",
]
