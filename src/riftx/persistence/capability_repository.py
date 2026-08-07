"""Durable, idempotent persistence for Capability versions, candidates, and packs."""

from __future__ import annotations

from datetime import datetime
from json import JSONDecodeError
from typing import TYPE_CHECKING
from uuid import NAMESPACE_URL, uuid4, uuid5

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from riftx.application.errors import (
    EntityNotFoundError,
    RepositoryConflictError,
    RepositoryError,
    RepositoryIntegrityError,
    RepositoryUnavailableError,
)
from riftx.capabilities import (
    Capability,
    CapabilityDependency,
    CapabilityDependencyKind,
    CapabilityKind,
    CapabilityManifest,
    CapabilityPack,
    CapabilityPackManifest,
    CapabilityPackMember,
    CapabilityPermission,
    CapabilitySource,
    CapabilityVersion,
    CapabilityVersionStatus,
    ConfirmationPolicy,
    EvidenceContract,
    PackInstall,
    PackInstallStatus,
    PackLock,
    PackLockOwnerKind,
    PackStatus,
)
from riftx.capabilities.models import (
    CapabilityCandidate,
    CapabilityCandidateStatus,
    CapabilityEvaluationResult,
    EvaluationResultStatus,
    PromotionRun,
    PromotionStatus,
)

from .capability_records import (
    CapabilityCandidateRecord,
    CapabilityDependencyRecord,
    CapabilityEvaluationResultRecord,
    CapabilityEvidenceContractRecord,
    CapabilityPackInstallRecord,
    CapabilityPackLockRecord,
    CapabilityPackMemberRecord,
    CapabilityPackRecord,
    CapabilityPermissionRecord,
    CapabilityPromotionRunRecord,
    CapabilityRecord,
    CapabilityVersionRecord,
)
from .transactions import SessionFactory, serialized_write

if TYPE_CHECKING:
    from riftx.packs import OfficialPackBundle

_LOCKED_VERSION_STATUSES = frozenset(
    {
        CapabilityVersionStatus.DISABLED,
        CapabilityVersionStatus.DEPRECATED,
        CapabilityVersionStatus.ARCHIVED,
    }
)
_VERSION_TRANSITIONS = {
    CapabilityVersionStatus.APPROVED: frozenset(
        {CapabilityVersionStatus.ACTIVE, CapabilityVersionStatus.ARCHIVED}
    ),
    CapabilityVersionStatus.ACTIVE: frozenset(
        {
            CapabilityVersionStatus.DISABLED,
            CapabilityVersionStatus.DEGRADED,
            CapabilityVersionStatus.DEPRECATED,
        }
    ),
    CapabilityVersionStatus.DISABLED: frozenset(
        {CapabilityVersionStatus.ACTIVE, CapabilityVersionStatus.ARCHIVED}
    ),
    CapabilityVersionStatus.DEGRADED: frozenset(
        {
            CapabilityVersionStatus.ACTIVE,
            CapabilityVersionStatus.DISABLED,
            CapabilityVersionStatus.DEPRECATED,
        }
    ),
    CapabilityVersionStatus.DEPRECATED: frozenset(
        {CapabilityVersionStatus.ACTIVE, CapabilityVersionStatus.ARCHIVED}
    ),
    CapabilityVersionStatus.ARCHIVED: frozenset(),
}


class SQLAlchemyCapabilityRepository:
    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    async def register_version(
        self,
        capability: Capability,
        version: CapabilityVersion,
    ) -> CapabilityVersion:
        try:
            async with serialized_write(self._session_factory) as session:
                return await _register_version(session, capability, version)
        except RepositoryError:
            raise
        except IntegrityError:
            raise RepositoryConflictError("Capability version registration conflicted") from None
        except SQLAlchemyError:
            raise RepositoryUnavailableError("Capability persistence is unavailable") from None

    async def get_version(self, version_id: str) -> CapabilityVersion | None:
        try:
            async with self._session_factory() as session:
                record = await session.get(CapabilityVersionRecord, version_id)
                return await _from_version_record(session, record) if record else None
        except RepositoryError:
            raise
        except (JSONDecodeError, TypeError, ValueError):
            raise RepositoryIntegrityError("CapabilityVersion", version_id) from None
        except SQLAlchemyError:
            raise RepositoryUnavailableError("Capability persistence is unavailable") from None

    async def list_versions(self, capability_id: str) -> tuple[CapabilityVersion, ...]:
        statement = (
            select(CapabilityVersionRecord)
            .where(CapabilityVersionRecord.capability_id == capability_id)
            .order_by(CapabilityVersionRecord.created_at, CapabilityVersionRecord.id)
        )
        try:
            async with self._session_factory() as session:
                records = (await session.scalars(statement)).all()
                return tuple(
                    [await _from_version_record(session, record) for record in records]
                )
        except RepositoryError:
            raise
        except (JSONDecodeError, TypeError, ValueError):
            raise RepositoryIntegrityError("Capability", capability_id) from None
        except SQLAlchemyError:
            raise RepositoryUnavailableError("Capability persistence is unavailable") from None

    async def list_active_versions(
        self,
        kind: CapabilityKind,
    ) -> tuple[CapabilityVersion, ...]:
        statement = (
            select(CapabilityVersionRecord)
            .join(
                CapabilityRecord,
                CapabilityRecord.id == CapabilityVersionRecord.capability_id,
            )
            .where(
                CapabilityRecord.kind == kind.value,
                CapabilityVersionRecord.status == CapabilityVersionStatus.ACTIVE.value,
            )
            .order_by(CapabilityVersionRecord.capability_id, CapabilityVersionRecord.id)
        )
        try:
            async with self._session_factory() as session:
                records = (await session.scalars(statement)).all()
                return tuple(
                    [await _from_version_record(session, record) for record in records]
                )
        except RepositoryError:
            raise
        except (JSONDecodeError, TypeError, ValueError):
            raise RepositoryIntegrityError("Capability", kind.value) from None
        except SQLAlchemyError:
            raise RepositoryUnavailableError("Capability persistence is unavailable") from None

    async def list_versions_by_kind(
        self,
        kind: CapabilityKind,
    ) -> tuple[CapabilityVersion, ...]:
        statement = (
            select(CapabilityVersionRecord)
            .join(
                CapabilityRecord,
                CapabilityRecord.id == CapabilityVersionRecord.capability_id,
            )
            .where(CapabilityRecord.kind == kind.value)
            .order_by(CapabilityVersionRecord.capability_id, CapabilityVersionRecord.created_at)
        )
        try:
            async with self._session_factory() as session:
                records = (await session.scalars(statement)).all()
                return tuple(
                    [await _from_version_record(session, record) for record in records]
                )
        except RepositoryError:
            raise
        except (JSONDecodeError, TypeError, ValueError):
            raise RepositoryIntegrityError("Capability", kind.value) from None
        except SQLAlchemyError:
            raise RepositoryUnavailableError("Capability persistence is unavailable") from None

    async def set_version_status(
        self,
        version_id: str,
        status: CapabilityVersionStatus,
        *,
        changed_at: datetime,
    ) -> CapabilityVersion:
        try:
            async with serialized_write(self._session_factory) as session:
                record = await session.get(
                    CapabilityVersionRecord,
                    version_id,
                    with_for_update=True,
                )
                if record is None:
                    raise EntityNotFoundError("CapabilityVersion", version_id)
                current = CapabilityVersionStatus(record.status)
                if current is status:
                    return await _from_version_record(session, record)
                if status not in _VERSION_TRANSITIONS[current]:
                    raise RepositoryConflictError(
                        f"CapabilityVersion {version_id!r} cannot transition "
                        f"from {current.value!r} to {status.value!r}"
                    )
                if status in _LOCKED_VERSION_STATUSES and await _has_active_lock(
                    session, version_id
                ):
                    raise RepositoryConflictError(
                        f"CapabilityVersion {version_id!r} is locked by an active owner"
                    )
                record.status = status.value
                if status is CapabilityVersionStatus.ACTIVE:
                    record.activated_at = record.activated_at or changed_at
                    record.retired_at = None
                elif status in _LOCKED_VERSION_STATUSES:
                    record.retired_at = changed_at if record.activated_at is not None else None
                await session.flush()
                return await _from_version_record(session, record)
        except RepositoryError:
            raise
        except (JSONDecodeError, TypeError, ValueError):
            raise RepositoryIntegrityError("CapabilityVersion", version_id) from None
        except SQLAlchemyError:
            raise RepositoryUnavailableError("Capability persistence is unavailable") from None

    async def create_candidate(
        self,
        candidate: CapabilityCandidate,
    ) -> CapabilityCandidate:
        try:
            async with serialized_write(self._session_factory) as session:
                existing = await session.get(CapabilityCandidateRecord, candidate.candidate_id)
                if existing is not None:
                    loaded = _from_candidate_record(existing)
                    if loaded == candidate:
                        return loaded
                    raise RepositoryConflictError("Capability Candidate ID already exists")
                identity = await session.scalar(
                    select(CapabilityCandidateRecord).where(
                        CapabilityCandidateRecord.capability_id
                        == candidate.proposed_manifest.capability_id,
                        CapabilityCandidateRecord.proposed_version
                        == candidate.proposed_manifest.version,
                        CapabilityCandidateRecord.candidate_digest
                        == candidate.candidate_digest,
                    )
                )
                if identity is not None:
                    return _from_candidate_record(identity)
                session.add(_candidate_record(candidate))
                await session.flush()
                return candidate
        except RepositoryError:
            raise
        except IntegrityError:
            raise RepositoryConflictError("Capability Candidate creation conflicted") from None
        except SQLAlchemyError:
            raise RepositoryUnavailableError("Capability persistence is unavailable") from None

    async def get_candidate(self, candidate_id: str) -> CapabilityCandidate | None:
        try:
            async with self._session_factory() as session:
                record = await session.get(CapabilityCandidateRecord, candidate_id)
            return _from_candidate_record(record) if record else None
        except (JSONDecodeError, TypeError, ValueError):
            raise RepositoryIntegrityError("CapabilityCandidate", candidate_id) from None
        except SQLAlchemyError:
            raise RepositoryUnavailableError("Capability persistence is unavailable") from None

    async def create_promotion(self, promotion: PromotionRun) -> PromotionRun:
        try:
            async with serialized_write(self._session_factory) as session:
                existing = await session.get(
                    CapabilityPromotionRunRecord,
                    promotion.promotion_id,
                )
                if existing is not None:
                    loaded = _from_promotion_record(existing)
                    if loaded == promotion:
                        return loaded
                    raise RepositoryConflictError("Capability Promotion ID already exists")
                if await session.get(CapabilityCandidateRecord, promotion.candidate_id) is None:
                    raise EntityNotFoundError("CapabilityCandidate", promotion.candidate_id)
                session.add(_promotion_record(promotion))
                await session.flush()
                return promotion
        except RepositoryError:
            raise
        except IntegrityError:
            raise RepositoryConflictError("Capability Promotion creation conflicted") from None
        except SQLAlchemyError:
            raise RepositoryUnavailableError("Capability persistence is unavailable") from None

    async def add_evaluation_result(
        self,
        result: CapabilityEvaluationResult,
    ) -> CapabilityEvaluationResult:
        try:
            async with serialized_write(self._session_factory) as session:
                existing = await session.get(CapabilityEvaluationResultRecord, result.result_id)
                if existing is not None:
                    loaded = _from_evaluation_record(existing)
                    if loaded == result:
                        return loaded
                    raise RepositoryConflictError("Capability Evaluation ID already exists")
                if (
                    await session.get(CapabilityPromotionRunRecord, result.promotion_id)
                    is None
                ):
                    raise EntityNotFoundError("CapabilityPromotion", result.promotion_id)
                session.add(_evaluation_record(result))
                await session.flush()
                return result
        except RepositoryError:
            raise
        except IntegrityError:
            raise RepositoryConflictError("Capability Evaluation creation conflicted") from None
        except SQLAlchemyError:
            raise RepositoryUnavailableError("Capability persistence is unavailable") from None

    async def promote_candidate(
        self,
        candidate_id: str,
        promotion_id: str,
        capability: Capability,
        version: CapabilityVersion,
        *,
        approval_reference: str,
        promoted_at: datetime,
    ) -> CapabilityVersion:
        try:
            async with serialized_write(self._session_factory) as session:
                candidate = await session.get(
                    CapabilityCandidateRecord,
                    candidate_id,
                    with_for_update=True,
                )
                promotion = await session.get(
                    CapabilityPromotionRunRecord,
                    promotion_id,
                    with_for_update=True,
                )
                if candidate is None:
                    raise EntityNotFoundError("CapabilityCandidate", candidate_id)
                if promotion is None or promotion.candidate_id != candidate_id:
                    raise EntityNotFoundError("CapabilityPromotion", promotion_id)
                if candidate.status == CapabilityCandidateStatus.PROMOTED.value:
                    if (
                        candidate.promoted_version_id == version.version_id
                        and promotion.promoted_version_id == version.version_id
                    ):
                        existing = await session.get(
                            CapabilityVersionRecord,
                            version.version_id,
                        )
                        if existing is None:
                            raise RepositoryIntegrityError(
                                "CapabilityCandidate",
                                candidate_id,
                            )
                        return await _from_version_record(session, existing)
                    raise RepositoryConflictError("Capability Candidate is already promoted")
                if candidate.status != CapabilityCandidateStatus.APPROVED.value:
                    raise RepositoryConflictError("Capability Candidate is not approved")
                if promotion.status != PromotionStatus.APPROVED.value:
                    raise RepositoryConflictError("Capability Promotion is not approved")
                if version.manifest_digest != candidate.candidate_digest:
                    raise RepositoryConflictError(
                        "promoted version must preserve the approved Candidate content"
                    )
                results = (
                    await session.scalars(
                        select(CapabilityEvaluationResultRecord).where(
                            CapabilityEvaluationResultRecord.promotion_id == promotion_id
                        )
                    )
                ).all()
                if not results or any(
                    result.status != EvaluationResultStatus.PASSED.value for result in results
                ):
                    raise RepositoryConflictError(
                        "Capability Promotion requires only passing evaluation results"
                    )
                promoted = await _register_version(session, capability, version)
                candidate.status = CapabilityCandidateStatus.PROMOTED.value
                candidate.promoted_version_id = promoted.version_id
                candidate.updated_at = promoted_at
                promotion.status = PromotionStatus.PROMOTED.value
                promotion.approval_reference = approval_reference
                promotion.promoted_version_id = promoted.version_id
                promotion.updated_at = promoted_at
                await session.flush()
                return promoted
        except RepositoryError:
            raise
        except IntegrityError:
            raise RepositoryConflictError("Capability Promotion conflicted") from None
        except SQLAlchemyError:
            raise RepositoryUnavailableError("Capability persistence is unavailable") from None

    async def register_pack(self, pack: CapabilityPack) -> CapabilityPack:
        try:
            async with serialized_write(self._session_factory) as session:
                return await _register_pack(session, pack)
        except RepositoryError:
            raise
        except IntegrityError:
            raise RepositoryConflictError("Capability Pack registration conflicted") from None
        except SQLAlchemyError:
            raise RepositoryUnavailableError("Capability persistence is unavailable") from None

    async def reconcile_official_packs(
        self,
        bundles: tuple[OfficialPackBundle, ...],
        *,
        scope_id: str,
        changed_at: datetime,
    ) -> tuple[PackInstall, ...]:
        """Atomically reconcile mutable Official Pack projections."""

        try:
            async with serialized_write(self._session_factory) as session:
                desired: list[tuple[CapabilityPack, PackInstall, tuple[PackLock, ...]]] = []
                for bundle in bundles:
                    registered_versions: dict[str, CapabilityVersion] = {}
                    for version in bundle.capability_versions:
                        registered_versions[
                            version.manifest.capability_id
                        ] = await _register_version(
                            session,
                            Capability(
                                capability_id=version.manifest.capability_id,
                                kind=version.manifest.kind,
                                created_at=version.created_at,
                            ),
                            version,
                        )
                    pack = await _register_pack(session, bundle.pack)
                    install = _official_install(pack, scope_id)
                    locks = _official_locks(pack, install, registered_versions)
                    await _validate_pack_locks(session, pack, install, locks)
                    desired.append((pack, install, locks))

                records = tuple(
                    (
                        await session.scalars(
                            select(CapabilityPackInstallRecord)
                            .where(
                                CapabilityPackInstallRecord.scope_type
                                == CapabilitySource.OFFICIAL.value,
                                CapabilityPackInstallRecord.scope_id == scope_id,
                            )
                            .with_for_update()
                        )
                    ).all()
                )
                expected_pack_ids = {pack.manifest.pack_id for pack, _, _ in desired}
                unexpected = sorted(
                    record.pack_id for record in records if record.pack_id not in expected_pack_ids
                )
                if unexpected:
                    raise RepositoryConflictError(
                        "Official Pack persistence contains unexpected installs: "
                        + ", ".join(unexpected)
                    )
                by_pack_id = {record.pack_id: record for record in records}
                installs: list[PackInstall] = []
                for pack, expected_install, expected_locks in desired:
                    record = by_pack_id.get(pack.manifest.pack_id)
                    if record is None:
                        orphaned_locks = tuple(
                            (
                                await session.scalars(
                                    select(CapabilityPackLockRecord)
                                    .where(
                                        CapabilityPackLockRecord.owner_kind
                                        == PackLockOwnerKind.PACK_INSTALL.value,
                                        CapabilityPackLockRecord.owner_id
                                        == expected_install.install_id,
                                        CapabilityPackLockRecord.released_at.is_(None),
                                    )
                                    .with_for_update()
                                )
                            ).all()
                        )
                        repair_at = max(changed_at, expected_install.installed_at)
                        for lock in orphaned_locks:
                            acquired_at = lock.acquired_at
                            if not isinstance(acquired_at, datetime):
                                raise RepositoryIntegrityError(
                                    "CapabilityPackLock",
                                    lock.id,
                                )
                            repair_at = max(repair_at, acquired_at)
                        for lock in orphaned_locks:
                            lock.released_at = repair_at
                        session.add(_install_record(expected_install))
                        replacement_locks: list[PackLock] = []
                        for expected_lock in expected_locks:
                            existing_lock_id = await session.get(
                                CapabilityPackLockRecord,
                                expected_lock.lock_id,
                            )
                            replacement_locks.append(
                                expected_lock
                                if existing_lock_id is None and not orphaned_locks
                                else expected_lock.model_copy(
                                    update={
                                        "lock_id": str(uuid4()),
                                        "acquired_at": repair_at,
                                    }
                                )
                            )
                        session.add_all(_lock_record(lock) for lock in replacement_locks)
                        installs.append(expected_install)
                        continue

                    active_locks = tuple(
                        (
                            await session.scalars(
                                select(CapabilityPackLockRecord)
                                .where(
                                    CapabilityPackLockRecord.owner_kind
                                    == PackLockOwnerKind.PACK_INSTALL.value,
                                    CapabilityPackLockRecord.owner_id == record.id,
                                    CapabilityPackLockRecord.released_at.is_(None),
                                )
                                .with_for_update()
                            )
                        ).all()
                    )
                    install_drift = (
                        record.pack_version_id != pack.pack_version_id
                        or record.pack_version != pack.manifest.version
                        or record.pack_digest != pack.manifest_digest
                        or record.status != PackInstallStatus.INSTALLED.value
                        or record.disabled_at is not None
                    )
                    lock_drift = not _locks_match_expected(active_locks, expected_locks)
                    if not install_drift and not lock_drift:
                        installs.append(_from_install_record(record))
                        continue

                    installed_at = record.installed_at
                    if not isinstance(installed_at, datetime):
                        raise RepositoryIntegrityError(
                            "CapabilityPackInstall",
                            record.id,
                        )
                    repair_at = max(changed_at, installed_at)
                    for lock in active_locks:
                        acquired_at = lock.acquired_at
                        if not isinstance(acquired_at, datetime):
                            raise RepositoryIntegrityError(
                                "CapabilityPackLock",
                                lock.id,
                            )
                        repair_at = max(repair_at, acquired_at)
                    previous_pack_version_id = record.previous_pack_version_id
                    if record.pack_version_id != pack.pack_version_id:
                        previous_pack_version_id = record.pack_version_id
                    record.pack_version_id = pack.pack_version_id
                    record.pack_version = pack.manifest.version
                    record.pack_digest = pack.manifest_digest
                    record.status = PackInstallStatus.INSTALLED.value
                    record.state_version += 1
                    record.previous_pack_version_id = previous_pack_version_id
                    record.updated_at = repair_at
                    record.disabled_at = None
                    if lock_drift:
                        for lock in active_locks:
                            lock.released_at = repair_at
                        session.add_all(
                            _lock_record(
                                lock.model_copy(
                                    update={
                                        "lock_id": str(uuid4()),
                                        "owner_id": record.id,
                                        "acquired_at": repair_at,
                                    }
                                )
                            )
                            for lock in expected_locks
                        )
                    installs.append(_from_install_record(record))
                await session.flush()
                return tuple(installs)
        except RepositoryError:
            raise
        except IntegrityError:
            raise RepositoryConflictError("Official Pack reconciliation conflicted") from None
        except (JSONDecodeError, TypeError, ValueError):
            raise RepositoryIntegrityError("OfficialPack", scope_id) from None
        except SQLAlchemyError:
            raise RepositoryUnavailableError("Capability persistence is unavailable") from None

    async def install_pack(
        self,
        install: PackInstall,
        locks: tuple[PackLock, ...],
    ) -> PackInstall:
        try:
            async with serialized_write(self._session_factory) as session:
                pack = await _require_pack(session, install.pack_version_id)
                _validate_install_matches_pack(install, pack)
                await _validate_pack_locks(session, pack, install, locks)
                existing = await session.get(CapabilityPackInstallRecord, install.install_id)
                if existing is not None:
                    loaded = _from_install_record(existing)
                    if loaded == install:
                        return loaded
                    raise RepositoryConflictError("Capability Pack install ID already exists")
                scoped = await session.scalar(
                    select(CapabilityPackInstallRecord).where(
                        CapabilityPackInstallRecord.scope_type == install.scope_type.value,
                        CapabilityPackInstallRecord.scope_id == install.scope_id,
                        CapabilityPackInstallRecord.pack_id == install.pack_id,
                    )
                )
                if scoped is not None:
                    loaded = _from_install_record(scoped)
                    if (
                        loaded.pack_version_id == install.pack_version_id
                        and loaded.status is PackInstallStatus.INSTALLED
                    ):
                        return loaded
                    raise RepositoryConflictError(
                        "Capability Pack is already installed at a different version"
                    )
                session.add(_install_record(install))
                session.add_all(_lock_record(lock) for lock in locks)
                await session.flush()
                return install
        except RepositoryError:
            raise
        except IntegrityError:
            raise RepositoryConflictError("Capability Pack installation conflicted") from None
        except SQLAlchemyError:
            raise RepositoryUnavailableError("Capability persistence is unavailable") from None

    async def disable_pack_install(
        self,
        install_id: str,
        *,
        disabled_at: datetime,
    ) -> PackInstall:
        try:
            async with serialized_write(self._session_factory) as session:
                record = await session.get(
                    CapabilityPackInstallRecord,
                    install_id,
                    with_for_update=True,
                )
                if record is None:
                    raise EntityNotFoundError("CapabilityPackInstall", install_id)
                if record.status == PackInstallStatus.DISABLED.value:
                    return _from_install_record(record)
                record.status = PackInstallStatus.DISABLED.value
                record.state_version += 1
                record.updated_at = disabled_at
                record.disabled_at = disabled_at
                await _release_locks(
                    session,
                    PackLockOwnerKind.PACK_INSTALL,
                    install_id,
                    disabled_at,
                )
                await session.flush()
                return _from_install_record(record)
        except RepositoryError:
            raise
        except SQLAlchemyError:
            raise RepositoryUnavailableError("Capability persistence is unavailable") from None

    async def rollback_pack_install(
        self,
        install_id: str,
        target_pack: CapabilityPack,
        locks: tuple[PackLock, ...],
        *,
        changed_at: datetime,
    ) -> PackInstall:
        try:
            async with serialized_write(self._session_factory) as session:
                record = await session.get(
                    CapabilityPackInstallRecord,
                    install_id,
                    with_for_update=True,
                )
                if record is None:
                    raise EntityNotFoundError("CapabilityPackInstall", install_id)
                persisted_pack = await _require_pack(session, target_pack.pack_version_id)
                if persisted_pack != target_pack:
                    raise RepositoryConflictError("rollback target Pack content differs")
                if record.pack_id != target_pack.manifest.pack_id:
                    raise RepositoryConflictError("rollback target must use the same Pack ID")
                if (
                    record.pack_version_id == target_pack.pack_version_id
                    and record.status == PackInstallStatus.INSTALLED.value
                ):
                    return _from_install_record(record)
                projected = PackInstall(
                    install_id=record.id,
                    scope_type=CapabilitySource(record.scope_type),
                    scope_id=record.scope_id,
                    pack_id=record.pack_id,
                    pack_version_id=target_pack.pack_version_id,
                    pack_version=target_pack.manifest.version,
                    pack_digest=target_pack.manifest_digest,
                    status=PackInstallStatus.INSTALLED,
                    state_version=record.state_version + 1,
                    previous_pack_version_id=record.pack_version_id,
                    installed_at=record.installed_at,
                    updated_at=changed_at,
                )
                await _validate_pack_locks(session, target_pack, projected, locks)
                await _release_locks(
                    session,
                    PackLockOwnerKind.PACK_INSTALL,
                    install_id,
                    changed_at,
                )
                record.previous_pack_version_id = record.pack_version_id
                record.pack_version_id = target_pack.pack_version_id
                record.pack_version = target_pack.manifest.version
                record.pack_digest = target_pack.manifest_digest
                record.status = PackInstallStatus.INSTALLED.value
                record.state_version += 1
                record.updated_at = changed_at
                record.disabled_at = None
                session.add_all(_lock_record(lock) for lock in locks)
                await session.flush()
                return _from_install_record(record)
        except RepositoryError:
            raise
        except IntegrityError:
            raise RepositoryConflictError("Capability Pack rollback conflicted") from None
        except SQLAlchemyError:
            raise RepositoryUnavailableError("Capability persistence is unavailable") from None

    async def acquire_version_lock(self, lock: PackLock) -> PackLock:
        try:
            async with serialized_write(self._session_factory) as session:
                return await _acquire_lock(session, lock)
        except RepositoryError:
            raise
        except IntegrityError:
            raise RepositoryConflictError("Capability version lock conflicted") from None
        except SQLAlchemyError:
            raise RepositoryUnavailableError("Capability persistence is unavailable") from None

    async def release_version_locks(
        self,
        owner_kind: PackLockOwnerKind,
        owner_id: str,
        *,
        released_at: datetime,
    ) -> tuple[PackLock, ...]:
        try:
            async with serialized_write(self._session_factory) as session:
                await _release_locks(session, owner_kind, owner_id, released_at)
                records = (
                    await session.scalars(
                        select(CapabilityPackLockRecord)
                        .where(
                            CapabilityPackLockRecord.owner_kind == owner_kind.value,
                            CapabilityPackLockRecord.owner_id == owner_id,
                        )
                        .order_by(
                            CapabilityPackLockRecord.acquired_at,
                            CapabilityPackLockRecord.id,
                        )
                    )
                ).all()
                return tuple(_from_lock_record(record) for record in records)
        except RepositoryError:
            raise
        except SQLAlchemyError:
            raise RepositoryUnavailableError("Capability persistence is unavailable") from None


async def _register_version(
    session: AsyncSession,
    capability: Capability,
    version: CapabilityVersion,
) -> CapabilityVersion:
    if capability.capability_id != version.manifest.capability_id:
        raise RepositoryConflictError("Capability and Version identities differ")
    if capability.kind is not version.manifest.kind:
        raise RepositoryConflictError("Capability and Version kinds differ")
    existing_id = await session.get(CapabilityVersionRecord, version.version_id)
    if existing_id is not None:
        loaded = await _from_version_record(session, existing_id)
        if loaded == version:
            return loaded
        raise RepositoryConflictError("Capability Version ID already exists")
    existing_identity = await session.scalar(
        select(CapabilityVersionRecord).where(
            CapabilityVersionRecord.capability_id == capability.capability_id,
            CapabilityVersionRecord.version == version.manifest.version,
        )
    )
    if existing_identity is not None:
        loaded = await _from_version_record(session, existing_identity)
        if (
            loaded.manifest_digest == version.manifest_digest
            and loaded.manifest == version.manifest
        ):
            return loaded
        raise RepositoryConflictError(
            "Capability version cannot be overwritten with different content"
        )
    capability_record = await session.get(CapabilityRecord, capability.capability_id)
    if capability_record is None:
        session.add(
            CapabilityRecord(
                id=capability.capability_id,
                kind=capability.kind.value,
                created_at=capability.created_at,
            )
        )
    elif capability_record.kind != capability.kind.value:
        raise RepositoryConflictError("Capability kind is immutable")
    session.add(_version_record(version))
    # These records intentionally avoid ORM relationships. Flush the immutable
    # parent rows before their normalized child facts so SQLAlchemy cannot emit
    # a child INSERT before the version foreign key exists.
    await session.flush()
    session.add_all(
        CapabilityDependencyRecord(
            version_id=version.version_id,
            position=position,
            kind=dependency.kind.value,
            reference=dependency.reference,
            version_constraint=dependency.version_constraint,
            optional=dependency.optional,
        )
        for position, dependency in enumerate(version.manifest.dependencies)
    )
    permission = version.manifest.permission
    session.add(
        CapabilityPermissionRecord(
            version_id=version.version_id,
            effect_class=permission.effect_class.value,
            approval_level=permission.approval_level.value,
            requires_scope=permission.requires_scope,
            credential_references_json=list(permission.credential_references),
        )
    )
    contract = version.manifest.evidence_contract
    session.add(
        CapabilityEvidenceContractRecord(
            version_id=version.version_id,
            required_refs_json=list(contract.required_refs),
            minimum_independent_sources=contract.minimum_independent_sources,
            confirmation_policy=contract.confirmation_policy.value,
        )
    )
    await session.flush()
    return version


async def _register_pack(session: AsyncSession, pack: CapabilityPack) -> CapabilityPack:
    existing_id = await session.get(CapabilityPackRecord, pack.pack_version_id)
    if existing_id is not None:
        loaded = await _from_pack_record(session, existing_id)
        if loaded == pack:
            return loaded
        raise RepositoryConflictError("Capability Pack version ID already exists")
    identity = await session.scalar(
        select(CapabilityPackRecord).where(
            CapabilityPackRecord.pack_id == pack.manifest.pack_id,
            CapabilityPackRecord.version == pack.manifest.version,
        )
    )
    if identity is not None:
        loaded = await _from_pack_record(session, identity)
        if loaded.manifest_digest == pack.manifest_digest and loaded.manifest == pack.manifest:
            return loaded
        raise RepositoryConflictError("Capability Pack version cannot be overwritten")
    members: list[tuple[CapabilityPackMember, CapabilityVersionRecord]] = []
    for member in pack.manifest.members:
        version_record = await session.scalar(
            select(CapabilityVersionRecord).where(
                CapabilityVersionRecord.capability_id == member.capability_id,
                CapabilityVersionRecord.version == member.version,
            )
        )
        if version_record is None or version_record.manifest_digest != member.version_digest:
            raise RepositoryConflictError(
                f"Capability Pack member {member.capability_id!r} is not registered exactly"
            )
        members.append((member, version_record))
    session.add(_pack_record(pack))
    await session.flush()
    session.add_all(
        CapabilityPackMemberRecord(
            pack_version_id=pack.pack_version_id,
            position=position,
            capability_id=member.capability_id,
            capability_version_id=version_record.id,
            capability_version=member.version,
            capability_digest=member.version_digest,
        )
        for position, (member, version_record) in enumerate(members)
    )
    await session.flush()
    return pack


async def _require_pack(session: AsyncSession, pack_version_id: str) -> CapabilityPack:
    record = await session.get(CapabilityPackRecord, pack_version_id)
    if record is None:
        raise EntityNotFoundError("CapabilityPack", pack_version_id)
    return await _from_pack_record(session, record)


async def _validate_pack_locks(
    session: AsyncSession,
    pack: CapabilityPack,
    install: PackInstall,
    locks: tuple[PackLock, ...],
) -> None:
    if len(locks) != len(pack.manifest.members):
        raise RepositoryConflictError("Pack locks must cover every Pack member")
    by_capability = {lock.capability_id: lock for lock in locks}
    if len(by_capability) != len(locks):
        raise RepositoryConflictError("Pack locks must use unique Capability IDs")
    for member in pack.manifest.members:
        lock = by_capability.get(member.capability_id)
        if lock is None:
            raise RepositoryConflictError("Pack lock coverage is incomplete")
        if (
            lock.owner_kind is not PackLockOwnerKind.PACK_INSTALL
            or lock.owner_id != install.install_id
            or lock.capability_version != member.version
            or lock.capability_digest != member.version_digest
            or lock.released_at is not None
        ):
            raise RepositoryConflictError("Pack lock does not match the resolved member")
        version = await session.get(CapabilityVersionRecord, lock.capability_version_id)
        if (
            version is None
            or version.capability_id != member.capability_id
            or version.version != member.version
            or version.manifest_digest != member.version_digest
        ):
            raise RepositoryConflictError("Pack lock references the wrong Capability version")


def _validate_install_matches_pack(install: PackInstall, pack: CapabilityPack) -> None:
    if (
        install.pack_id != pack.manifest.pack_id
        or install.pack_version_id != pack.pack_version_id
        or install.pack_version != pack.manifest.version
        or install.pack_digest != pack.manifest_digest
        or install.status is not PackInstallStatus.INSTALLED
        or install.previous_pack_version_id is not None
    ):
        raise RepositoryConflictError("Pack install does not match the registered Pack")


async def _acquire_lock(session: AsyncSession, lock: PackLock) -> PackLock:
    version = await session.get(CapabilityVersionRecord, lock.capability_version_id)
    if (
        version is None
        or version.capability_id != lock.capability_id
        or version.version != lock.capability_version
        or version.manifest_digest != lock.capability_digest
    ):
        raise RepositoryConflictError("Capability lock does not match a registered version")
    existing = await session.get(CapabilityPackLockRecord, lock.lock_id)
    if existing is not None:
        loaded = _from_lock_record(existing)
        if loaded == lock:
            return loaded
        raise RepositoryConflictError("Capability lock ID already exists")
    active = await session.scalar(
        select(CapabilityPackLockRecord).where(
            CapabilityPackLockRecord.owner_kind == lock.owner_kind.value,
            CapabilityPackLockRecord.owner_id == lock.owner_id,
            CapabilityPackLockRecord.capability_id == lock.capability_id,
            CapabilityPackLockRecord.released_at.is_(None),
        )
    )
    if active is not None:
        loaded = _from_lock_record(active)
        if (
            loaded.capability_version_id == lock.capability_version_id
            and loaded.capability_digest == lock.capability_digest
        ):
            return loaded
        raise RepositoryConflictError("Capability owner already holds a different active lock")
    session.add(_lock_record(lock))
    await session.flush()
    return lock


async def _release_locks(
    session: AsyncSession,
    owner_kind: PackLockOwnerKind,
    owner_id: str,
    released_at: datetime,
) -> None:
    records = (
        await session.scalars(
            select(CapabilityPackLockRecord).where(
                CapabilityPackLockRecord.owner_kind == owner_kind.value,
                CapabilityPackLockRecord.owner_id == owner_id,
                CapabilityPackLockRecord.released_at.is_(None),
            )
        )
    ).all()
    for record in records:
        if released_at < record.acquired_at:
            raise RepositoryConflictError("Capability lock release predates acquisition")
        record.released_at = released_at


async def _has_active_lock(session: AsyncSession, version_id: str) -> bool:
    return (
        await session.scalar(
            select(CapabilityPackLockRecord.id).where(
                CapabilityPackLockRecord.capability_version_id == version_id,
                CapabilityPackLockRecord.released_at.is_(None),
            )
        )
    ) is not None


async def _from_version_record(
    session: AsyncSession,
    record: CapabilityVersionRecord,
) -> CapabilityVersion:
    manifest = CapabilityManifest.model_validate(record.manifest_json)
    dependencies = (
        await session.scalars(
            select(CapabilityDependencyRecord)
            .where(CapabilityDependencyRecord.version_id == record.id)
            .order_by(CapabilityDependencyRecord.position)
        )
    ).all()
    permission = await session.get(CapabilityPermissionRecord, record.id)
    evidence = await session.get(CapabilityEvidenceContractRecord, record.id)
    if permission is None or evidence is None:
        raise RepositoryIntegrityError("CapabilityVersion", record.id)
    normalized_dependencies = tuple(
        CapabilityDependency(
            kind=CapabilityDependencyKind(item.kind),
            reference=item.reference,
            version_constraint=item.version_constraint,
            optional=item.optional,
        )
        for item in dependencies
    )
    normalized_permission = CapabilityPermission(
        effect_class=manifest.permission.effect_class.__class__(permission.effect_class),
        approval_level=manifest.permission.approval_level.__class__(permission.approval_level),
        requires_scope=permission.requires_scope,
        credential_references=tuple(permission.credential_references_json),
    )
    normalized_evidence = EvidenceContract(
        required_refs=tuple(evidence.required_refs_json),
        minimum_independent_sources=evidence.minimum_independent_sources,
        confirmation_policy=ConfirmationPolicy(evidence.confirmation_policy),
    )
    if (
        normalized_dependencies != manifest.dependencies
        or normalized_permission != manifest.permission
        or normalized_evidence != manifest.evidence_contract
        or record.capability_id != manifest.capability_id
        or record.version != manifest.version
        or record.schema_version != manifest.schema_version
        or record.manifest_digest != _manifest_digest(manifest)
        or record.provenance_json != manifest.provenance.model_dump(mode="json")
        or record.source != manifest.provenance.source.value
        or record.publisher != manifest.provenance.publisher
    ):
        raise RepositoryIntegrityError("CapabilityVersion", record.id)
    return CapabilityVersion(
        version_id=record.id,
        manifest=manifest,
        manifest_digest=record.manifest_digest,
        status=CapabilityVersionStatus(record.status),
        created_at=record.created_at,
        activated_at=record.activated_at,
        retired_at=record.retired_at,
    )


async def _from_pack_record(
    session: AsyncSession,
    record: CapabilityPackRecord,
) -> CapabilityPack:
    manifest = CapabilityPackManifest.model_validate(record.manifest_json)
    members = (
        await session.scalars(
            select(CapabilityPackMemberRecord)
            .where(CapabilityPackMemberRecord.pack_version_id == record.id)
            .order_by(CapabilityPackMemberRecord.position)
        )
    ).all()
    normalized_members = tuple(
        CapabilityPackMember(
            capability_id=member.capability_id,
            version=member.capability_version,
            version_digest=member.capability_digest,
        )
        for member in members
    )
    if (
        normalized_members != manifest.members
        or record.pack_id != manifest.pack_id
        or record.version != manifest.version
        or record.schema_version != manifest.schema_version
        or record.manifest_digest != _pack_digest(manifest)
        or record.provenance_json != manifest.provenance.model_dump(mode="json")
        or record.source != manifest.source.value
        or record.publisher != manifest.publisher
    ):
        raise RepositoryIntegrityError("CapabilityPack", record.id)
    return CapabilityPack(
        pack_version_id=record.id,
        manifest=manifest,
        manifest_digest=record.manifest_digest,
        status=PackStatus(record.status),
        created_at=record.created_at,
    )


def _version_record(version: CapabilityVersion) -> CapabilityVersionRecord:
    manifest = version.manifest
    return CapabilityVersionRecord(
        id=version.version_id,
        capability_id=manifest.capability_id,
        version=manifest.version,
        schema_version=manifest.schema_version,
        status=version.status.value,
        manifest_json=manifest.model_dump(mode="json"),
        manifest_digest=version.manifest_digest,
        provenance_json=manifest.provenance.model_dump(mode="json"),
        source=manifest.provenance.source.value,
        publisher=manifest.provenance.publisher,
        created_at=version.created_at,
        activated_at=version.activated_at,
        retired_at=version.retired_at,
    )


def _candidate_record(candidate: CapabilityCandidate) -> CapabilityCandidateRecord:
    manifest = candidate.proposed_manifest
    return CapabilityCandidateRecord(
        id=candidate.candidate_id,
        capability_id=manifest.capability_id,
        proposed_version=manifest.version,
        kind=manifest.kind.value,
        status=candidate.status.value,
        manifest_json=manifest.model_dump(mode="json"),
        candidate_digest=candidate.candidate_digest,
        provenance_json=manifest.provenance.model_dump(mode="json"),
        proposed_by=candidate.proposed_by,
        source_run_id=candidate.source_run_id,
        promoted_version_id=candidate.promoted_version_id,
        created_at=candidate.created_at,
        updated_at=candidate.updated_at,
    )


def _from_candidate_record(record: CapabilityCandidateRecord) -> CapabilityCandidate:
    manifest = CapabilityManifest.model_validate(record.manifest_json)
    if (
        record.capability_id != manifest.capability_id
        or record.proposed_version != manifest.version
        or record.kind != manifest.kind.value
        or record.candidate_digest != _manifest_digest(manifest)
        or record.provenance_json != manifest.provenance.model_dump(mode="json")
    ):
        raise RepositoryIntegrityError("CapabilityCandidate", record.id)
    return CapabilityCandidate(
        candidate_id=record.id,
        proposed_manifest=manifest,
        candidate_digest=record.candidate_digest,
        status=CapabilityCandidateStatus(record.status),
        proposed_by=record.proposed_by,
        source_run_id=record.source_run_id,
        promoted_version_id=record.promoted_version_id,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


def _promotion_record(promotion: PromotionRun) -> CapabilityPromotionRunRecord:
    return CapabilityPromotionRunRecord(
        id=promotion.promotion_id,
        candidate_id=promotion.candidate_id,
        status=promotion.status.value,
        requested_by=promotion.requested_by,
        approval_reference=promotion.approval_reference,
        promoted_version_id=promotion.promoted_version_id,
        created_at=promotion.created_at,
        updated_at=promotion.updated_at,
    )


def _from_promotion_record(record: CapabilityPromotionRunRecord) -> PromotionRun:
    return PromotionRun(
        promotion_id=record.id,
        candidate_id=record.candidate_id,
        status=PromotionStatus(record.status),
        requested_by=record.requested_by,
        approval_reference=record.approval_reference,
        promoted_version_id=record.promoted_version_id,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


def _evaluation_record(
    result: CapabilityEvaluationResult,
) -> CapabilityEvaluationResultRecord:
    return CapabilityEvaluationResultRecord(
        id=result.result_id,
        promotion_id=result.promotion_id,
        evaluator=result.evaluator,
        status=result.status.value,
        scenario_ids_json=list(result.scenario_ids),
        report_json=result.report,
        report_digest=result.report_digest,
        created_at=result.created_at,
    )


def _from_evaluation_record(
    record: CapabilityEvaluationResultRecord,
) -> CapabilityEvaluationResult:
    return CapabilityEvaluationResult(
        result_id=record.id,
        promotion_id=record.promotion_id,
        evaluator=record.evaluator,
        status=EvaluationResultStatus(record.status),
        scenario_ids=tuple(record.scenario_ids_json),
        report=record.report_json,
        report_digest=record.report_digest,
        created_at=record.created_at,
    )


def _pack_record(pack: CapabilityPack) -> CapabilityPackRecord:
    manifest = pack.manifest
    return CapabilityPackRecord(
        id=pack.pack_version_id,
        pack_id=manifest.pack_id,
        version=manifest.version,
        schema_version=manifest.schema_version,
        status=pack.status.value,
        manifest_json=manifest.model_dump(mode="json"),
        manifest_digest=pack.manifest_digest,
        source=manifest.source.value,
        publisher=manifest.publisher,
        provenance_json=manifest.provenance.model_dump(mode="json"),
        created_at=pack.created_at,
    )


def _install_record(install: PackInstall) -> CapabilityPackInstallRecord:
    return CapabilityPackInstallRecord(
        id=install.install_id,
        scope_type=install.scope_type.value,
        scope_id=install.scope_id,
        pack_id=install.pack_id,
        pack_version_id=install.pack_version_id,
        pack_version=install.pack_version,
        pack_digest=install.pack_digest,
        status=install.status.value,
        state_version=install.state_version,
        previous_pack_version_id=install.previous_pack_version_id,
        installed_at=install.installed_at,
        updated_at=install.updated_at,
        disabled_at=install.disabled_at,
    )


def _from_install_record(record: CapabilityPackInstallRecord) -> PackInstall:
    return PackInstall(
        install_id=record.id,
        scope_type=CapabilitySource(record.scope_type),
        scope_id=record.scope_id,
        pack_id=record.pack_id,
        pack_version_id=record.pack_version_id,
        pack_version=record.pack_version,
        pack_digest=record.pack_digest,
        status=PackInstallStatus(record.status),
        state_version=record.state_version,
        previous_pack_version_id=record.previous_pack_version_id,
        installed_at=record.installed_at,
        updated_at=record.updated_at,
        disabled_at=record.disabled_at,
    )


def _lock_record(lock: PackLock) -> CapabilityPackLockRecord:
    return CapabilityPackLockRecord(
        id=lock.lock_id,
        owner_kind=lock.owner_kind.value,
        owner_id=lock.owner_id,
        capability_id=lock.capability_id,
        capability_version_id=lock.capability_version_id,
        capability_version=lock.capability_version,
        capability_digest=lock.capability_digest,
        acquired_at=lock.acquired_at,
        released_at=lock.released_at,
    )


def _from_lock_record(record: CapabilityPackLockRecord) -> PackLock:
    return PackLock(
        lock_id=record.id,
        owner_kind=PackLockOwnerKind(record.owner_kind),
        owner_id=record.owner_id,
        capability_id=record.capability_id,
        capability_version_id=record.capability_version_id,
        capability_version=record.capability_version,
        capability_digest=record.capability_digest,
        acquired_at=record.acquired_at,
        released_at=record.released_at,
    )


def _official_install(pack: CapabilityPack, scope_id: str) -> PackInstall:
    install_id = _stable_official_id(
        "official-pack-install",
        pack.manifest.pack_id,
        pack.manifest.version,
    )
    return PackInstall(
        install_id=install_id,
        scope_type=CapabilitySource.OFFICIAL,
        scope_id=scope_id,
        pack_id=pack.manifest.pack_id,
        pack_version_id=pack.pack_version_id,
        pack_version=pack.manifest.version,
        pack_digest=pack.manifest_digest,
        status=PackInstallStatus.INSTALLED,
        state_version=1,
        installed_at=pack.created_at,
        updated_at=pack.created_at,
    )


def _official_locks(
    pack: CapabilityPack,
    install: PackInstall,
    versions: dict[str, CapabilityVersion],
) -> tuple[PackLock, ...]:
    return tuple(
        PackLock(
            lock_id=_stable_official_id(
                "official-pack-lock",
                pack.manifest.pack_id,
                pack.manifest.version,
                member.capability_id,
            ),
            owner_kind=PackLockOwnerKind.PACK_INSTALL,
            owner_id=install.install_id,
            capability_id=member.capability_id,
            capability_version_id=versions[member.capability_id].version_id,
            capability_version=member.version,
            capability_digest=member.version_digest,
            acquired_at=pack.created_at,
        )
        for member in pack.manifest.members
    )


def _locks_match_expected(
    records: tuple[CapabilityPackLockRecord, ...],
    expected: tuple[PackLock, ...],
) -> bool:
    by_capability: dict[str, list[CapabilityPackLockRecord]] = {}
    for record in records:
        by_capability.setdefault(record.capability_id, []).append(record)
    if set(by_capability) != {lock.capability_id for lock in expected}:
        return False
    return all(
        len(by_capability[lock.capability_id]) == 1
        and by_capability[lock.capability_id][0].capability_version_id == lock.capability_version_id
        and by_capability[lock.capability_id][0].capability_version == lock.capability_version
        and by_capability[lock.capability_id][0].capability_digest == lock.capability_digest
        for lock in expected
    )


def _stable_official_id(kind: str, *parts: str) -> str:
    return str(uuid5(NAMESPACE_URL, ":".join(("riftx", kind, *parts))))


def _manifest_digest(manifest: CapabilityManifest) -> str:
    from riftx.capabilities import capability_manifest_digest

    return capability_manifest_digest(manifest)


def _pack_digest(manifest: CapabilityPackManifest) -> str:
    from riftx.capabilities import capability_pack_digest

    return capability_pack_digest(manifest)
