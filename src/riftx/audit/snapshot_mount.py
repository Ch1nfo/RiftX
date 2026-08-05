"""Same-node Snapshot mount orchestration over durable Lease/Pin authority."""

from __future__ import annotations

import hmac
import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from riftx.domain.runner import RunnerPrincipal

from .snapshot import (
    SnapshotBlobMetadata,
    SnapshotCASBinding,
    SnapshotCASDescriptor,
    SnapshotStore,
    SnapshotStoreError,
)
from .static_effect import (
    AuditStaticEffectPlan,
    SnapshotMountLease,
    SnapshotMountLeaseStatus,
    SnapshotMountPin,
    SnapshotMountPinStatus,
    SnapshotMountStopDisposition,
    SnapshotMountStopProof,
    snapshot_mount_key_digest,
    snapshot_storage_key_digest,
)

_DIGEST_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_SAFE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@+~\-]{0,127}$")


def _require_digest(value: str, *, label: str) -> None:
    if not isinstance(value, str) or _DIGEST_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")


def _require_safe_id(value: str, *, label: str) -> None:
    if not isinstance(value, str) or _SAFE_ID_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{label} is invalid")


def _require_aware_datetime(value: datetime, *, label: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")


class SnapshotMountFailure(StrEnum):
    AUTHORITY_MISSING = "audit_snapshot_mount_authority_missing"
    AUTHENTICATION_FAILED = "audit_snapshot_mount_authentication_failed"
    OWNER_MISMATCH = "audit_snapshot_mount_owner_mismatch"
    SOURCE_INTEGRITY = "audit_snapshot_mount_source_integrity"
    BACKEND_UNAVAILABLE = "audit_snapshot_mount_backend_unavailable"
    BACKEND_STATE_UNKNOWN = "audit_snapshot_mount_backend_state_unknown"
    STOP_UNCONFIRMED = "audit_snapshot_mount_stop_unconfirmed"
    CROSS_NODE_NOT_SUPPORTED = "audit_cross_node_not_supported"


class SnapshotMountError(RuntimeError):
    """Stable, path-free mount orchestration failure."""

    def __init__(self, failure: SnapshotMountFailure) -> None:
        super().__init__(failure.value)
        self.failure = failure


class SnapshotMountBackendError(RuntimeError):
    def __init__(
        self,
        failure: SnapshotMountFailure,
        *,
        outcome_unknown: bool,
    ) -> None:
        if not isinstance(failure, SnapshotMountFailure):
            raise TypeError("failure must be a SnapshotMountFailure")
        if not isinstance(outcome_unknown, bool):
            raise TypeError("outcome_unknown must be a bool")
        super().__init__(failure.value)
        self.failure = failure
        self.outcome_unknown = outcome_unknown


class SnapshotMountBackendState(StrEnum):
    ABSENT = "absent"
    PREPARED = "prepared"
    ACTIVE = "active"
    STOPPING = "stopping"
    STOPPED = "stopped"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class SnapshotMountSource:
    """Trusted internal CAS source; raw locator is never an API projection."""

    binding: SnapshotCASBinding
    content_storage_key: str
    descriptor: SnapshotCASDescriptor
    store: SnapshotStore

    def __post_init__(self) -> None:
        if not isinstance(self.binding, SnapshotCASBinding):
            raise TypeError("Snapshot mount source binding is invalid")
        if not isinstance(self.descriptor, SnapshotCASDescriptor):
            raise TypeError("Snapshot mount source descriptor is invalid")
        if (
            self.content_storage_key != self.descriptor.content_storage_key
            or not self.binding.accepts(self.descriptor)
        ):
            raise ValueError("Snapshot mount source owner binding differs")

    @staticmethod
    def require_authority(
        *,
        plan: AuditStaticEffectPlan,
        lease: SnapshotMountLease,
    ) -> None:
        if (
            plan.id != lease.plan_id
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
        ):
            raise SnapshotMountError(SnapshotMountFailure.OWNER_MISMATCH)

    def accepts(
        self,
        *,
        plan: AuditStaticEffectPlan,
        lease: SnapshotMountLease,
    ) -> bool:
        try:
            self.require_authority(plan=plan, lease=lease)
            storage_key_digest = snapshot_storage_key_digest(
                self.content_storage_key,
                role="content",
            )
        except (SnapshotMountError, ValueError):
            return False
        allowed = tuple(sorted({blob.blob_digest for blob in self.descriptor.blobs}))
        return (
            hmac.compare_digest(plan.content_storage_key_digest, storage_key_digest)
            and self.descriptor.project_id == lease.project_id
            and self.descriptor.snapshot_digest == lease.snapshot_digest
            and self.descriptor.manifest_digest == lease.manifest_digest
            and allowed == lease.allowed_blob_digests
            and self.descriptor.total_bytes <= lease.max_bytes
            and self.descriptor.file_count <= plan.limits.file_count
        )

    @classmethod
    def resolve(
        cls,
        *,
        plan: AuditStaticEffectPlan,
        lease: SnapshotMountLease,
        content_storage_key: str,
        store: SnapshotStore,
    ) -> SnapshotMountSource:
        cls.require_authority(plan=plan, lease=lease)
        if not hmac.compare_digest(
            plan.content_storage_key_digest,
            snapshot_storage_key_digest(content_storage_key, role="content"),
        ):
            raise SnapshotMountError(SnapshotMountFailure.OWNER_MISMATCH)
        binding = SnapshotCASBinding(
            project_id=plan.project_id,
            snapshot_digest=plan.snapshot_digest,
            manifest_digest=plan.manifest_digest,
        )
        try:
            descriptor = store.describe(binding, content_storage_key)
        except SnapshotStoreError as exc:
            raise SnapshotMountError(SnapshotMountFailure.SOURCE_INTEGRITY) from exc
        try:
            source = cls(
                binding=binding,
                content_storage_key=content_storage_key,
                descriptor=descriptor,
                store=store,
            )
        except (TypeError, ValueError) as exc:
            raise SnapshotMountError(SnapshotMountFailure.SOURCE_INTEGRITY) from exc
        if not source.accepts(plan=plan, lease=lease):
            raise SnapshotMountError(SnapshotMountFailure.SOURCE_INTEGRITY)
        return source

    def read_blob(self, metadata: SnapshotBlobMetadata, *, max_bytes: int) -> bytes:
        if metadata not in self.descriptor.blobs or metadata.size > max_bytes:
            raise SnapshotMountError(SnapshotMountFailure.SOURCE_INTEGRITY)
        try:
            reader = self.store.open_blob(
                self.binding,
                self.content_storage_key,
                metadata.relative_path,
                metadata.blob_digest,
                max_bytes=max_bytes,
            )
            try:
                chunks: list[bytes] = []
                remaining = metadata.size
                while remaining:
                    chunk = reader.read(min(64 * 1024, remaining))
                    if not chunk:
                        raise SnapshotMountError(SnapshotMountFailure.SOURCE_INTEGRITY)
                    chunks.append(chunk)
                    remaining -= len(chunk)
                reader.verify_complete()
                return b"".join(chunks)
            finally:
                reader.close()
        except SnapshotMountError:
            raise
        except SnapshotStoreError as exc:
            raise SnapshotMountError(SnapshotMountFailure.SOURCE_INTEGRITY) from exc


@dataclass(frozen=True, slots=True)
class PreparedSnapshotMount:
    lease_id: str
    lease_digest: str
    pin_id: str
    pin_digest: str
    plan_id: str
    plan_digest: str
    effect_execution_id: str
    node_id: str
    backend_id: str
    backend_digest: str
    principal: RunnerPrincipal
    mount_key: str
    mount_proof_digest: str
    descriptor_digest: str
    file_count: int
    total_bytes: int
    prepared_at: datetime

    def __post_init__(self) -> None:
        for label, value in (
            ("lease_id", self.lease_id),
            ("pin_id", self.pin_id),
            ("plan_id", self.plan_id),
            ("effect_execution_id", self.effect_execution_id),
            ("node_id", self.node_id),
            ("backend_id", self.backend_id),
        ):
            _require_safe_id(value, label=label)
        if not isinstance(self.principal, RunnerPrincipal):
            raise TypeError("prepared Snapshot mount principal is invalid")
        snapshot_mount_key_digest(self.mount_key)
        for digest in (
            self.lease_digest,
            self.pin_digest,
            self.plan_digest,
            self.backend_digest,
            self.mount_proof_digest,
            self.descriptor_digest,
        ):
            _require_digest(digest, label="prepared Snapshot mount digest")
        if self.file_count < 0 or self.total_bytes < 0:
            raise ValueError("prepared Snapshot mount counters are invalid")
        _require_aware_datetime(self.prepared_at, label="prepared_at")

    def matches(
        self,
        lease: SnapshotMountLease,
        pin: SnapshotMountPin,
        source: SnapshotMountSource,
        *,
        observed_at: datetime,
    ) -> bool:
        return (
            self.lease_id == lease.id
            and self.lease_digest == lease.lease_digest
            and self.pin_id == pin.id
            and self.pin_digest == pin.pin_digest
            and self.plan_id == lease.plan_id
            and self.plan_digest == lease.plan_digest
            and self.effect_execution_id == lease.effect_execution_id
            and self.node_id == lease.target_node_id
            and self.backend_id == lease.backend_id
            and self.backend_digest == lease.backend_digest
            and self.principal == lease.target_runner_principal
            and self.descriptor_digest == source.descriptor.descriptor_digest
            and self.file_count == source.descriptor.file_count
            and self.total_bytes == source.descriptor.total_bytes
            and lease.created_at <= self.prepared_at <= observed_at
        )


@dataclass(frozen=True, slots=True)
class SnapshotMountInspection:
    state: SnapshotMountBackendState
    lease_id: str
    lease_digest: str
    pin_id: str
    pin_digest: str
    node_id: str
    backend_id: str
    backend_digest: str
    principal: RunnerPrincipal
    observed_at: datetime
    mount_key: str | None = None
    mount_proof_digest: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.state, SnapshotMountBackendState):
            raise TypeError("Snapshot mount inspection state is invalid")
        for label, value in (
            ("lease_id", self.lease_id),
            ("pin_id", self.pin_id),
            ("node_id", self.node_id),
            ("backend_id", self.backend_id),
        ):
            _require_safe_id(value, label=label)
        for label, value in (
            ("lease_digest", self.lease_digest),
            ("pin_digest", self.pin_digest),
            ("backend_digest", self.backend_digest),
        ):
            _require_digest(value, label=label)
        if not isinstance(self.principal, RunnerPrincipal):
            raise TypeError("Snapshot mount inspection principal is invalid")
        _require_aware_datetime(self.observed_at, label="observed_at")
        has_mount = self.mount_key is not None and self.mount_proof_digest is not None
        if self.state in {
            SnapshotMountBackendState.PREPARED,
            SnapshotMountBackendState.ACTIVE,
            SnapshotMountBackendState.STOPPING,
        }:
            if not has_mount:
                raise ValueError("live Snapshot mount inspection lacks mount proof")
            assert self.mount_key is not None
            snapshot_mount_key_digest(self.mount_key)
            assert self.mount_proof_digest is not None
            _require_digest(self.mount_proof_digest, label="mount_proof_digest")
        elif self.mount_key is not None or self.mount_proof_digest is not None:
            raise ValueError("absent Snapshot mount inspection carries mount proof")

    def matches(
        self,
        lease: SnapshotMountLease,
        pin: SnapshotMountPin,
        *,
        observed_at: datetime,
    ) -> bool:
        return (
            self.lease_id == lease.id
            and self.lease_digest == lease.lease_digest
            and self.pin_id == pin.id
            and self.pin_digest == pin.pin_digest
            and self.node_id == lease.target_node_id
            and self.backend_id == lease.backend_id
            and self.backend_digest == lease.backend_digest
            and self.principal == lease.target_runner_principal
            and self.observed_at == observed_at
            and (lease.mount_key is None or self.mount_key == lease.mount_key)
            and (
                lease.mount_proof_digest is None
                or self.mount_proof_digest == lease.mount_proof_digest
            )
        )


@dataclass(frozen=True, slots=True)
class SnapshotMountStopEvidence:
    lease_id: str
    lease_digest: str
    pin_id: str
    pin_digest: str
    node_id: str
    backend_id: str
    backend_digest: str
    principal: RunnerPrincipal
    mount_key: str
    stopped_at: datetime
    active_fd_count: int
    active_process_count: int
    mount_namespace_unmounted: bool
    lease_revoked: bool
    pin_revoked: bool
    worker_path_inaccessible: bool

    def __post_init__(self) -> None:
        for label, value in (
            ("lease_id", self.lease_id),
            ("pin_id", self.pin_id),
            ("node_id", self.node_id),
            ("backend_id", self.backend_id),
        ):
            _require_safe_id(value, label=label)
        for label, value in (
            ("lease_digest", self.lease_digest),
            ("pin_digest", self.pin_digest),
            ("backend_digest", self.backend_digest),
        ):
            _require_digest(value, label=label)
        if not isinstance(self.principal, RunnerPrincipal):
            raise TypeError("Snapshot mount stop principal is invalid")
        snapshot_mount_key_digest(self.mount_key)
        _require_aware_datetime(self.stopped_at, label="stopped_at")
        if self.active_fd_count < 0 or self.active_process_count < 0:
            raise ValueError("Snapshot mount stop counters must not be negative")
        for label, value in (
            ("mount_namespace_unmounted", self.mount_namespace_unmounted),
            ("lease_revoked", self.lease_revoked),
            ("pin_revoked", self.pin_revoked),
            ("worker_path_inaccessible", self.worker_path_inaccessible),
        ):
            if not isinstance(value, bool):
                raise TypeError(f"{label} must be a bool")

    @property
    def affirmative(self) -> bool:
        return (
            self.active_fd_count == 0
            and self.active_process_count == 0
            and self.mount_namespace_unmounted
            and self.lease_revoked
            and self.pin_revoked
            and self.worker_path_inaccessible
        )

    def matches(self, lease: SnapshotMountLease, pin: SnapshotMountPin) -> bool:
        return (
            self.lease_id == lease.id
            and self.lease_digest == lease.lease_digest
            and self.pin_id == pin.id
            and self.pin_digest == pin.pin_digest
            and self.node_id == lease.target_node_id
            and self.backend_id == lease.backend_id
            and self.backend_digest == lease.backend_digest
            and self.principal == lease.target_runner_principal
            and (lease.mount_key is None or lease.mount_key == self.mount_key)
            and self.stopped_at >= lease.updated_at
        )


class SnapshotMountAuthorityRepository(Protocol):
    async def get_plan(self, plan_id: str) -> AuditStaticEffectPlan | None: ...

    async def get_mount(
        self,
        lease_id: str,
    ) -> tuple[SnapshotMountLease, SnapshotMountPin] | None: ...

    async def get_stop_proof(self, lease_id: str) -> SnapshotMountStopProof | None: ...

    async def compare_and_set_mount(
        self,
        *,
        previous_lease: SnapshotMountLease,
        updated_lease: SnapshotMountLease,
        previous_pin: SnapshotMountPin,
        updated_pin: SnapshotMountPin,
    ) -> tuple[SnapshotMountLease, SnapshotMountPin, bool]: ...

    async def record_stop(
        self,
        *,
        previous_lease: SnapshotMountLease,
        stopped_lease: SnapshotMountLease,
        previous_pin: SnapshotMountPin,
        stopped_pin: SnapshotMountPin,
        proof: SnapshotMountStopProof,
    ) -> tuple[SnapshotMountLease, SnapshotMountPin, SnapshotMountStopProof, bool]: ...

    async def list_reconcilable(
        self,
        *,
        node_id: str,
        limit: int = 100,
    ) -> tuple[tuple[SnapshotMountLease, SnapshotMountPin], ...]: ...


class SnapshotMountSourceResolver(Protocol):
    async def resolve(
        self,
        *,
        plan: AuditStaticEffectPlan,
        lease: SnapshotMountLease,
    ) -> SnapshotMountSource: ...


class SnapshotMountBackend(Protocol):
    node_id: str
    backend_id: str
    backend_digest: str

    async def prepare(
        self,
        *,
        plan: AuditStaticEffectPlan,
        lease: SnapshotMountLease,
        pin: SnapshotMountPin,
        source: SnapshotMountSource,
        prepared_at: datetime,
    ) -> PreparedSnapshotMount: ...

    async def inspect(
        self,
        *,
        plan: AuditStaticEffectPlan,
        lease: SnapshotMountLease,
        pin: SnapshotMountPin,
        observed_at: datetime,
    ) -> SnapshotMountInspection: ...

    async def stop(
        self,
        *,
        plan: AuditStaticEffectPlan,
        lease: SnapshotMountLease,
        pin: SnapshotMountPin,
        stopped_at: datetime,
    ) -> SnapshotMountStopEvidence: ...


@dataclass(frozen=True, slots=True)
class SnapshotMountReconciliationResult:
    examined: int
    retained_active: int
    stopped: int
    outcome_unknown: int


class SnapshotMountCoordinator:
    """Coordinates backend effects with durable Lease/Pin CAS transitions."""

    def __init__(
        self,
        *,
        authority: SnapshotMountAuthorityRepository,
        sources: SnapshotMountSourceResolver,
        backend: SnapshotMountBackend,
    ) -> None:
        self._authority = authority
        self._sources = sources
        self._backend = backend

    async def activate(
        self,
        *,
        lease_id: str,
        nonce: str,
        principal: RunnerPrincipal,
        node_id: str,
        observed_at: datetime,
    ) -> tuple[SnapshotMountLease, SnapshotMountPin, bool]:
        _require_aware_datetime(observed_at, label="observed_at")
        lease, pin = await self._require_mount(lease_id)
        self._require_backend_owner(lease)
        if node_id != self._backend.node_id:
            raise SnapshotMountError(SnapshotMountFailure.CROSS_NODE_NOT_SUPPORTED)
        if not lease.authenticates(
            nonce=nonce,
            principal=principal,
            node_id=node_id,
            observed_at=observed_at,
        ):
            raise SnapshotMountError(SnapshotMountFailure.AUTHENTICATION_FAILED)
        if lease.status is SnapshotMountLeaseStatus.ACTIVE:
            return lease, pin, False
        if lease.status is not SnapshotMountLeaseStatus.ISSUED:
            raise SnapshotMountError(SnapshotMountFailure.OWNER_MISMATCH)
        plan = await self._authority.get_plan(lease.plan_id)
        if plan is None:
            raise SnapshotMountError(SnapshotMountFailure.AUTHORITY_MISSING)
        source = await self._sources.resolve(plan=plan, lease=lease)
        if not source.accepts(plan=plan, lease=lease):
            raise SnapshotMountError(SnapshotMountFailure.SOURCE_INTEGRITY)
        try:
            prepared = await self._backend.prepare(
                plan=plan,
                lease=lease,
                pin=pin,
                source=source,
                prepared_at=observed_at,
            )
        except SnapshotMountBackendError as exc:
            if exc.outcome_unknown:
                await self._mark_unknown(
                    lease,
                    pin,
                    failure_code=exc.failure.value,
                    observed_at=observed_at,
                )
            raise SnapshotMountError(exc.failure) from None
        if not prepared.matches(lease, pin, source, observed_at=observed_at):
            await self._cleanup_uncommitted_prepare(
                plan,
                lease,
                pin,
                observed_at=observed_at,
            )
            raise SnapshotMountError(SnapshotMountFailure.OWNER_MISMATCH)
        active_lease = lease.activate(
            mount_key=prepared.mount_key,
            mount_proof_digest=prepared.mount_proof_digest,
            activated_at=observed_at,
        )
        active_pin = pin.activate(active_lease)
        try:
            return await self._authority.compare_and_set_mount(
                previous_lease=lease,
                updated_lease=active_lease,
                previous_pin=pin,
                updated_pin=active_pin,
            )
        except Exception:
            current = await self._authority.get_mount(lease.id)
            if current == (active_lease, active_pin):
                return active_lease, active_pin, False
            if current is not None:
                current_lease, current_pin = current
                if (
                    current_lease.status is SnapshotMountLeaseStatus.ACTIVE
                    and current_pin.status is SnapshotMountPinStatus.ACTIVE
                    and current_lease.mount_key == prepared.mount_key
                    and current_lease.mount_proof_digest == prepared.mount_proof_digest
                    and current_pin.mount_key == prepared.mount_key
                    and current_pin.mount_proof_digest == prepared.mount_proof_digest
                ):
                    return current_lease, current_pin, False
                if current != (lease, pin):
                    raise
            await self._cleanup_uncommitted_prepare(
                plan,
                lease,
                pin,
                observed_at=observed_at,
            )
            raise

    async def stop(
        self,
        *,
        lease_id: str,
        requested_at: datetime,
    ) -> tuple[SnapshotMountLease, SnapshotMountPin, SnapshotMountStopProof, bool]:
        _require_aware_datetime(requested_at, label="requested_at")
        lease, pin = await self._require_mount(lease_id)
        self._require_backend_owner(lease)
        if lease.status in {SnapshotMountLeaseStatus.REVOKED, SnapshotMountLeaseStatus.EXPIRED}:
            proof = await self._authority.get_stop_proof(lease.id)
            if proof is None or lease.stop_proof_digest != proof.proof_digest:
                raise SnapshotMountError(SnapshotMountFailure.AUTHORITY_MISSING)
            return lease, pin, proof, False
        plan = await self._require_plan(lease)
        if lease.status is SnapshotMountLeaseStatus.ACTIVE:
            pending_lease = lease.begin_stop(
                expired=requested_at >= lease.expires_at,
                requested_at=requested_at,
            )
            pending_pin = pin.begin_revocation(pending_lease)
            pending_lease, pending_pin, _ = await self._authority.compare_and_set_mount(
                previous_lease=lease,
                updated_lease=pending_lease,
                previous_pin=pin,
                updated_pin=pending_pin,
            )
        elif lease.status in {
            SnapshotMountLeaseStatus.REVOCATION_PENDING,
            SnapshotMountLeaseStatus.EXPIRATION_PENDING,
        }:
            pending_lease, pending_pin = lease, pin
        else:
            raise SnapshotMountError(SnapshotMountFailure.STOP_UNCONFIRMED)
        try:
            evidence = await self._backend.stop(
                plan=plan,
                lease=pending_lease,
                pin=pending_pin,
                stopped_at=requested_at,
            )
        except SnapshotMountBackendError as exc:
            if exc.outcome_unknown:
                await self._mark_unknown(
                    pending_lease,
                    pending_pin,
                    failure_code=exc.failure.value,
                    observed_at=requested_at,
                )
            raise SnapshotMountError(exc.failure) from None
        if not evidence.matches(pending_lease, pending_pin) or not evidence.affirmative:
            await self._mark_unknown(
                pending_lease,
                pending_pin,
                failure_code=SnapshotMountFailure.STOP_UNCONFIRMED.value,
                observed_at=requested_at,
            )
            raise SnapshotMountError(SnapshotMountFailure.STOP_UNCONFIRMED)
        disposition = (
            SnapshotMountStopDisposition.EXPIRED
            if pending_lease.status is SnapshotMountLeaseStatus.EXPIRATION_PENDING
            else SnapshotMountStopDisposition.REVOKED
        )
        proof = SnapshotMountStopProof.for_pending_stop(
            lease=pending_lease,
            pin=pending_pin,
            disposition=disposition,
            stopped_at=evidence.stopped_at,
        )
        stopped_lease = pending_lease.finish_stop(proof, pin=pending_pin)
        stopped_pin = pending_pin.revoke(stopped_lease)
        return await self._authority.record_stop(
            previous_lease=pending_lease,
            stopped_lease=stopped_lease,
            previous_pin=pending_pin,
            stopped_pin=stopped_pin,
            proof=proof,
        )

    async def reconcile(
        self,
        *,
        node_id: str,
        observed_at: datetime,
        limit: int = 100,
    ) -> SnapshotMountReconciliationResult:
        _require_aware_datetime(observed_at, label="observed_at")
        if node_id != self._backend.node_id:
            raise SnapshotMountError(SnapshotMountFailure.CROSS_NODE_NOT_SUPPORTED)
        mounts = await self._authority.list_reconcilable(node_id=node_id, limit=limit)
        retained = stopped = unknown = 0
        for lease, pin in mounts:
            self._require_backend_owner(lease)
            if lease.status in {
                SnapshotMountLeaseStatus.REVOCATION_PENDING,
                SnapshotMountLeaseStatus.EXPIRATION_PENDING,
            }:
                try:
                    await self.stop(lease_id=lease.id, requested_at=observed_at)
                except SnapshotMountError:
                    unknown += 1
                else:
                    stopped += 1
                continue
            plan = await self._require_plan(lease)
            try:
                inspection = await self._backend.inspect(
                    plan=plan,
                    lease=lease,
                    pin=pin,
                    observed_at=observed_at,
                )
            except SnapshotMountBackendError as exc:
                await self._mark_unknown(
                    lease,
                    pin,
                    failure_code=exc.failure.value,
                    observed_at=observed_at,
                )
                unknown += 1
                continue
            if not inspection.matches(lease, pin, observed_at=observed_at):
                await self._mark_unknown(
                    lease,
                    pin,
                    failure_code=SnapshotMountFailure.OWNER_MISMATCH.value,
                    observed_at=observed_at,
                )
                unknown += 1
                continue
            if lease.status is SnapshotMountLeaseStatus.ACTIVE:
                if (
                    inspection.state is SnapshotMountBackendState.ACTIVE
                    and observed_at < lease.expires_at
                ):
                    retained += 1
                    continue
                if inspection.state is SnapshotMountBackendState.ACTIVE:
                    try:
                        await self.stop(lease_id=lease.id, requested_at=observed_at)
                    except SnapshotMountError:
                        unknown += 1
                    else:
                        stopped += 1
                    continue
                await self._mark_unknown(
                    lease,
                    pin,
                    failure_code=SnapshotMountFailure.BACKEND_STATE_UNKNOWN.value,
                    observed_at=observed_at,
                )
                unknown += 1
                continue
            if lease.status is SnapshotMountLeaseStatus.ISSUED:
                if inspection.state is SnapshotMountBackendState.ABSENT:
                    continue
                await self._cleanup_uncommitted_prepare(
                    plan,
                    lease,
                    pin,
                    observed_at=observed_at,
                )
                unknown += 1
                continue
            if lease.status is SnapshotMountLeaseStatus.OUTCOME_UNKNOWN:
                if inspection.state not in {
                    SnapshotMountBackendState.ABSENT,
                    SnapshotMountBackendState.STOPPED,
                }:
                    try:
                        await self._backend.stop(
                            plan=plan,
                            lease=lease,
                            pin=pin,
                            stopped_at=observed_at,
                        )
                    except SnapshotMountBackendError:
                        pass
                unknown += 1
        return SnapshotMountReconciliationResult(
            examined=len(mounts),
            retained_active=retained,
            stopped=stopped,
            outcome_unknown=unknown,
        )

    async def _require_mount(
        self,
        lease_id: str,
    ) -> tuple[SnapshotMountLease, SnapshotMountPin]:
        mount = await self._authority.get_mount(lease_id)
        if mount is None:
            raise SnapshotMountError(SnapshotMountFailure.AUTHORITY_MISSING)
        return mount

    async def _require_plan(self, lease: SnapshotMountLease) -> AuditStaticEffectPlan:
        plan = await self._authority.get_plan(lease.plan_id)
        if plan is None:
            raise SnapshotMountError(SnapshotMountFailure.AUTHORITY_MISSING)
        SnapshotMountSource.require_authority(plan=plan, lease=lease)
        return plan

    def _require_backend_owner(self, lease: SnapshotMountLease) -> None:
        if lease.target_node_id != self._backend.node_id:
            raise SnapshotMountError(SnapshotMountFailure.CROSS_NODE_NOT_SUPPORTED)
        if (
            lease.backend_id != self._backend.backend_id
            or lease.backend_digest != self._backend.backend_digest
        ):
            raise SnapshotMountError(SnapshotMountFailure.OWNER_MISMATCH)

    async def _mark_unknown(
        self,
        lease: SnapshotMountLease,
        pin: SnapshotMountPin,
        *,
        failure_code: str,
        observed_at: datetime,
    ) -> None:
        if lease.status is SnapshotMountLeaseStatus.OUTCOME_UNKNOWN:
            return
        unknown = lease.mark_outcome_unknown(
            failure_code=failure_code,
            observed_at=observed_at,
        )
        await self._authority.compare_and_set_mount(
            previous_lease=lease,
            updated_lease=unknown,
            previous_pin=pin,
            updated_pin=pin,
        )

    async def _cleanup_uncommitted_prepare(
        self,
        plan: AuditStaticEffectPlan,
        lease: SnapshotMountLease,
        pin: SnapshotMountPin,
        *,
        observed_at: datetime,
    ) -> None:
        try:
            await self._backend.stop(
                plan=plan,
                lease=lease,
                pin=pin,
                stopped_at=observed_at,
            )
        except SnapshotMountBackendError:
            pass
        await self._mark_unknown(
            lease,
            pin,
            failure_code=SnapshotMountFailure.BACKEND_STATE_UNKNOWN.value,
            observed_at=observed_at,
        )


__all__ = [
    "PreparedSnapshotMount",
    "SnapshotMountAuthorityRepository",
    "SnapshotMountBackend",
    "SnapshotMountBackendError",
    "SnapshotMountBackendState",
    "SnapshotMountCoordinator",
    "SnapshotMountError",
    "SnapshotMountFailure",
    "SnapshotMountInspection",
    "SnapshotMountReconciliationResult",
    "SnapshotMountSource",
    "SnapshotMountSourceResolver",
    "SnapshotMountStopEvidence",
]
