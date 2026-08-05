"""Persistence adapters for durable owner-bound Snapshot references."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from riftx.application.errors import RepositoryConflictError, RepositoryIntegrityError
from riftx.application.ports import StoredAuditEntity
from riftx.audit.snapshot import (
    SNAPSHOT_REFERENCE_SCHEMA_VERSION,
    SnapshotReference,
    SnapshotReferenceRole,
)
from riftx.domain import AuditScan, SourceSnapshot

from .audit_repositories import (
    compare_and_set_audit_scan,
    create_source_snapshot,
    load_validated_audit_scan,
)
from .orm import AuditScanRecord, SnapshotReferenceRecord, SourceSnapshotRecord
from .transactions import SessionFactory, serialized_write

_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@+~\-]{0,127}$")


def _require_id(value: str, *, label: str) -> None:
    if not isinstance(value, str) or _ID_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{label} is invalid")


def _conflict(reference: SnapshotReference | None = None) -> None:
    reference_id = (
        f"{reference.audit_id}:{reference.snapshot_id}:{reference.role.value}"
        if reference is not None
        else "invalid-reference"
    )
    raise RepositoryConflictError(
        f"SnapshotReference {reference_id!r} conflicts with durable ownership"
    ) from None


def _to_record(reference: SnapshotReference) -> SnapshotReferenceRecord:
    return SnapshotReferenceRecord(
        audit_id=reference.audit_id,
        snapshot_id=reference.snapshot_id,
        role=reference.role.value,
        project_id=reference.project_id,
        schema_version=reference.schema_version,
        reference_digest=reference.reference_digest,
        created_at=reference.created_at,
    )


def _from_record(record: SnapshotReferenceRecord) -> SnapshotReference:
    opaque_id = f"{record.audit_id}:{record.snapshot_id}:{record.role}"
    try:
        reference = SnapshotReference(
            audit_id=record.audit_id,
            snapshot_id=record.snapshot_id,
            project_id=record.project_id,
            role=SnapshotReferenceRole(record.role),
            created_at=record.created_at,
        )
        if (
            record.schema_version != SNAPSHOT_REFERENCE_SCHEMA_VERSION
            or record.reference_digest != reference.reference_digest
        ):
            raise ValueError("Snapshot reference digest does not match")
        return reference
    except (TypeError, ValueError) as exc:
        raise RepositoryIntegrityError(
            "SnapshotReference",
            opaque_id,
        ) from exc


async def add_snapshot_reference(
    session: AsyncSession,
    reference: SnapshotReference,
) -> tuple[SnapshotReference, bool]:
    """Create or replay one Snapshot reference in a caller-owned transaction."""

    if not isinstance(reference, SnapshotReference):
        raise TypeError("reference must be a SnapshotReference")
    key = (reference.audit_id, reference.snapshot_id, reference.role.value)
    audit = await session.get(AuditScanRecord, reference.audit_id)
    snapshot = await session.get(
        SourceSnapshotRecord,
        reference.snapshot_id,
    )
    if (
        audit is None
        or snapshot is None
        or audit.project_id != reference.project_id
        or snapshot.project_id != reference.project_id
    ):
        _conflict(reference)
    existing = await session.get(SnapshotReferenceRecord, key)
    if existing is not None:
        persisted = _from_record(existing)
        if persisted == reference:
            return persisted, False
        _conflict(reference)
    session.add(_to_record(reference))
    await session.flush()
    return reference, True


@dataclass(frozen=True, slots=True)
class SourceSnapshotSealResult:
    snapshot: SourceSnapshot
    reference: SnapshotReference
    audit: StoredAuditEntity[AuditScan]


class SQLAlchemySourceSnapshotSealUnitOfWork:
    """Atomically persist a Snapshot, its primary reference, and Audit binding."""

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    async def seal_primary(
        self,
        *,
        audit_id: str,
        snapshot: SourceSnapshot,
    ) -> SourceSnapshotSealResult:
        _require_id(audit_id, label="audit_id")
        snapshot = SourceSnapshot.model_validate(snapshot)
        try:
            async with serialized_write(self._session_factory) as session:
                persisted_snapshot, _ = await create_source_snapshot(session, snapshot)
                bundle = await load_validated_audit_scan(
                    session,
                    audit_id,
                    for_update=True,
                )
                if bundle is None or bundle[-1].project_id != snapshot.project_id:
                    _conflict()
                record, _contract, _project, _run, scan = bundle
                existing_primary = (
                    await session.scalars(
                        select(SnapshotReferenceRecord).where(
                            SnapshotReferenceRecord.audit_id == audit_id,
                            SnapshotReferenceRecord.role
                            == SnapshotReferenceRole.PRIMARY.value,
                        )
                    )
                ).all()
                if any(
                    item.snapshot_id != persisted_snapshot.id
                    or item.project_id != persisted_snapshot.project_id
                    for item in existing_primary
                ):
                    _conflict()
                reference = SnapshotReference(
                    audit_id=audit_id,
                    snapshot_id=persisted_snapshot.id,
                    project_id=persisted_snapshot.project_id,
                    role=SnapshotReferenceRole.PRIMARY,
                    created_at=persisted_snapshot.sealed_at,
                )
                await add_snapshot_reference(session, reference)
                try:
                    replacement = scan.bind_snapshots(snapshot_id=persisted_snapshot.id)
                except ValueError:
                    _conflict()
                stored_audit, _ = await compare_and_set_audit_scan(
                    session,
                    StoredAuditEntity(scan, record.state_version),
                    replacement,
                )
                return SourceSnapshotSealResult(
                    snapshot=persisted_snapshot,
                    reference=reference,
                    audit=stored_audit,
                )
        except IntegrityError:
            _conflict()


class SQLAlchemySnapshotReferenceRepository:
    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    async def add(
        self,
        reference: SnapshotReference,
    ) -> tuple[SnapshotReference, bool]:
        if not isinstance(reference, SnapshotReference):
            raise TypeError("reference must be a SnapshotReference")
        key = (reference.audit_id, reference.snapshot_id, reference.role.value)
        try:
            async with serialized_write(self._session_factory) as session:
                return await add_snapshot_reference(session, reference)
        except IntegrityError:
            pass

        async with self._session_factory() as session:
            existing = await session.get(SnapshotReferenceRecord, key)
            if existing is not None:
                persisted = _from_record(existing)
                if persisted == reference:
                    return persisted, False
        _conflict(reference)

    async def release(
        self,
        *,
        audit_id: str,
        snapshot_id: str,
        role: SnapshotReferenceRole,
    ) -> bool:
        _require_id(audit_id, label="audit_id")
        _require_id(snapshot_id, label="snapshot_id")
        if not isinstance(role, SnapshotReferenceRole):
            raise ValueError("snapshot reference role is invalid")
        async with serialized_write(self._session_factory) as session:
            result = await session.execute(
                delete(SnapshotReferenceRecord).where(
                    SnapshotReferenceRecord.audit_id == audit_id,
                    SnapshotReferenceRecord.snapshot_id == snapshot_id,
                    SnapshotReferenceRecord.role == role.value,
                )
            )
            return result.rowcount == 1  # type: ignore[attr-defined]

    async def list_for_snapshot(
        self,
        snapshot_id: str,
        *,
        project_id: str,
    ) -> tuple[SnapshotReference, ...]:
        _require_id(snapshot_id, label="snapshot_id")
        _require_id(project_id, label="project_id")
        async with self._session_factory() as session:
            records: Sequence[SnapshotReferenceRecord] = (
                await session.scalars(
                    select(SnapshotReferenceRecord)
                    .where(
                        SnapshotReferenceRecord.snapshot_id == snapshot_id,
                        SnapshotReferenceRecord.project_id == project_id,
                    )
                    .order_by(
                        SnapshotReferenceRecord.created_at,
                        SnapshotReferenceRecord.audit_id,
                        SnapshotReferenceRecord.role,
                    )
                )
            ).all()
        return tuple(_from_record(record) for record in records)


__all__ = [
    "SQLAlchemySnapshotReferenceRepository",
    "SQLAlchemySourceSnapshotSealUnitOfWork",
    "SourceSnapshotSealResult",
    "add_snapshot_reference",
]
