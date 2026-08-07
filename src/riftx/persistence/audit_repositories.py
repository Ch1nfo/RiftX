"""Historical Code Audit repositories retained for snapshots and Safety Stop."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Never

from sqlalchemy import Select, and_, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from riftx.application.errors import (
    EntityNotFoundError,
    RepositoryConflictError,
    RepositoryIntegrityError,
)
from riftx.application.ports import StoredAuditEntity
from riftx.domain import (
    AuditContractRecord,
    AuditLifecycleStatus,
    AuditProject,
    AuditPublicationStatus,
    AuditScan,
    RunStatus,
    SourceSnapshot,
)
from riftx.domain.audit_contract_v2 import AuditContractRecordV2

from .audit_mappers import (
    audit_contract_from_record,
    audit_project_from_record,
    audit_scan_from_record,
    audit_scan_to_record,
    source_snapshot_from_record,
    source_snapshot_to_record,
)
from .orm import (
    AuditContractRecord as AuditContractORMRecord,
)
from .orm import (
    AuditProjectRecord,
    AuditScanRecord,
    Base,
    EngagementRecord,
    RunRecord,
    SourceSnapshotRecord,
)
from .transactions import serialized_write

SessionFactory = async_sessionmaker[AsyncSession]


_MAX_PAGE_SIZE = 1000


def _validate_page(*, limit: int, offset: int = 0) -> None:
    if limit < 1 or limit > _MAX_PAGE_SIZE:
        raise ValueError(f"limit must be between 1 and {_MAX_PAGE_SIZE}")
    if offset < 0:
        raise ValueError("offset must not be negative")


def _conflict(entity: str, entity_id: str, reason: str = "state conflict") -> Never:
    # Domain/model/driver exceptions can carry source paths, canonical contracts,
    # or SQL parameters.  Repository callers receive only this bounded conflict;
    # the originating exception is never rendered as part of the public chain.
    raise RepositoryConflictError(f"{entity} {entity_id!r} {reason}") from None


def _record_values(record: Base, *, excluding: frozenset[str]) -> dict[str, object]:
    return {
        column.name: getattr(record, column.name)
        for column in record.__table__.columns
        if column.name not in excluding
    }


def _same_fields(left: object, right: object, fields: tuple[str, ...]) -> bool:
    return all(getattr(left, field, None) == getattr(right, field, None) for field in fields)


_SNAPSHOT_CREATION_FIELDS = (
    "project_id",
    "source_kind",
    "parent_snapshot_id",
    "base_tree_digest",
    "patch_digest",
    "commit_sha",
    "base_commit_sha",
    "working_tree_digest",
    "tree_digest",
    "capture_policy_digest",
    "materializer_schema_version",
    "snapshot_digest",
    "snapshot_store_version",
    "content_storage_key",
    "manifest_storage_key",
    "manifest_digest",
    "file_count",
    "total_bytes",
)


def _same_snapshot_creation(
    existing: SourceSnapshot,
    requested: SourceSnapshot,
) -> bool:
    return _same_fields(existing, requested, _SNAPSHOT_CREATION_FIELDS)


async def _single_create_candidate[T](
    session: AsyncSession,
    statement: Select[tuple[T]],
    *,
    entity: str,
    entity_id: str,
) -> T | None:
    # All callers query a disjunction of independently unique identities.  A
    # request can nevertheless make its surrogate ID collide with one row and
    # its natural key collide with another.  Returning whichever row SQLite or
    # PostgreSQL happens to visit first would make create idempotency ambiguous.
    records = (await session.scalars(statement.limit(2))).all()
    if len(records) > 1:
        _conflict(entity, entity_id, "has ambiguous identity collisions")
    return records[0] if records else None


def _validate_contract_binding(
    scan: AuditScan,
    contract: AuditContractRecord | AuditContractRecordV2,
) -> None:
    try:
        if isinstance(contract, AuditContractRecordV2):
            frozen = contract.contract()
            checks = (
                (contract.contract_id, scan.contract_id),
                (contract.contract_digest, scan.contract_digest),
                (contract.audit_id, scan.id),
                (frozen.project_id, scan.project_id),
                (frozen.mode, scan.mode),
                (frozen.analysis_profile, scan.analysis_profile),
                (frozen.baseline_audit_id, scan.baseline_audit_id),
                (frozen.model_profile, scan.model_profile),
                (frozen.source_binding.source_node_id, scan.selected_node_id),
                (None, scan.required_backend_id),
                (None, scan.policy_digest),
                (None, scan.config_digest),
                (frozen.budget.budget_digest, scan.budget_digest),
            )
            if scan.started_at is not None or any(left != right for left, right in checks):
                raise ValueError("v2 Audit draft binding mismatch")
        else:
            scan.validate_contract_record(contract)
    except (TypeError, ValueError):
        _conflict("AuditScan", scan.id, "does not match its immutable contract")


def _reject_distribution_facts(scan: AuditScan) -> None:
    if (
        scan.publication_status is AuditPublicationStatus.PUBLISHED
        or scan.initial_distribution_revision_id is not None
        or scan.latest_distribution_revision_id is not None
        or scan.publication_finished_at is not None
    ):
        _conflict("AuditScan", scan.id, "contains publication facts reserved for AUD-506")


async def _validated_project(
    session: AsyncSession,
    project_id: str,
    *,
    for_update: bool = False,
) -> tuple[AuditProjectRecord, AuditProject]:
    statement = select(AuditProjectRecord).where(AuditProjectRecord.id == project_id)
    if for_update:
        statement = statement.with_for_update()
    record = await session.scalar(statement)
    if record is None:
        raise EntityNotFoundError("AuditProject", project_id)
    engagement_id = await session.scalar(
        select(EngagementRecord.id).where(EngagementRecord.id == record.engagement_id)
    )
    if engagement_id is None:
        raise RepositoryIntegrityError(
            "AuditProject",
            project_id,
            reason_code="owner_binding_mismatch",
        )
    return record, audit_project_from_record(record)


async def _related_audit_belongs_to_project(
    session: AsyncSession,
    audit_id: str,
    project_id: str,
) -> bool:
    return (
        await session.scalar(
            select(AuditScanRecord.id).where(
                AuditScanRecord.id == audit_id,
                AuditScanRecord.project_id == project_id,
            )
        )
        is not None
    )


async def _validated_snapshot(
    session: AsyncSession,
    snapshot_id: str,
    *,
    project_id: str | None = None,
) -> tuple[SourceSnapshotRecord, SourceSnapshot] | None:
    statement = select(SourceSnapshotRecord).where(SourceSnapshotRecord.id == snapshot_id)
    if project_id is not None:
        statement = statement.where(SourceSnapshotRecord.project_id == project_id)
    record = await session.scalar(statement)
    if record is None:
        return None
    try:
        await _validated_project(session, record.project_id)
    except EntityNotFoundError:
        raise RepositoryIntegrityError(
            "SourceSnapshot",
            snapshot_id,
            reason_code="owner_binding_mismatch",
        ) from None
    snapshot = source_snapshot_from_record(record)
    if record.parent_snapshot_id is not None:
        parent = await session.scalar(
            select(SourceSnapshotRecord.id).where(
                SourceSnapshotRecord.id == record.parent_snapshot_id,
                SourceSnapshotRecord.project_id == record.project_id,
            )
        )
        if parent is None:
            raise RepositoryIntegrityError(
                "SourceSnapshot",
                snapshot.id,
                reason_code="owner_binding_mismatch",
            )
    return record, snapshot


def validate_audit_scan_record_bundle(
    scan_record: AuditScanRecord,
    contract_record: AuditContractORMRecord,
    project_record: AuditProjectRecord,
    run_record: RunRecord,
    *,
    allow_unrecorded_run_terminal: bool = False,
) -> AuditScan:
    """Strictly reconstruct the query-independent core of an Audit aggregate."""

    audit_id = scan_record.id
    scan = audit_scan_from_record(
        scan_record,
        contract_record,
        run_engagement_id=run_record.engagement_id,
        run_kind=run_record.kind,
        project_engagement_id=project_record.engagement_id,
    )
    try:
        run_status = RunStatus(run_record.status)
    except ValueError:
        raise RepositoryIntegrityError(
            "AuditScan",
            audit_id,
            reason_code="run_binding_mismatch",
        ) from None
    if (
        run_record.node_id != scan.selected_node_id
        or run_record.model_profile != scan.model_profile
        or run_record.temporal_workflow_id != scan.temporal_workflow_id
    ):
        raise RepositoryIntegrityError(
            "AuditScan",
            audit_id,
            reason_code="run_binding_mismatch",
        )
    terminal_run_statuses = {
        RunStatus.COMPLETED,
        RunStatus.FAILED,
        RunStatus.CANCELLED,
    }
    if scan.run_terminal_status is None:
        if run_status in terminal_run_statuses and not allow_unrecorded_run_terminal:
            raise RepositoryIntegrityError(
                "AuditScan",
                audit_id,
                reason_code="run_terminal_binding_mismatch",
            )
    elif scan.run_terminal_status is not run_status:
        raise RepositoryIntegrityError(
            "AuditScan",
            audit_id,
            reason_code="run_terminal_binding_mismatch",
        )
    if (run_status in terminal_run_statuses) != (run_record.finished_at is not None):
        raise RepositoryIntegrityError(
            "AuditScan",
            audit_id,
            reason_code="run_terminal_binding_mismatch",
        )
    return scan


async def _validated_scan(
    session: AsyncSession,
    audit_id: str,
    *,
    project_id: str | None = None,
    expected_state_version: int | None = None,
    for_update: bool = False,
    allow_unrecorded_run_terminal: bool = False,
) -> (
    tuple[
        AuditScanRecord,
        AuditContractORMRecord,
        AuditProjectRecord,
        RunRecord,
        AuditScan,
    ]
    | None
):
    statement = select(AuditScanRecord).where(AuditScanRecord.id == audit_id)
    if project_id is not None:
        statement = statement.where(AuditScanRecord.project_id == project_id)
    if expected_state_version is not None:
        statement = statement.where(AuditScanRecord.state_version == expected_state_version)
    if for_update:
        statement = statement.with_for_update()
    scan_record = await session.scalar(statement)
    if scan_record is None:
        if expected_state_version is None and project_id is None:
            orphan_contract_id = await session.scalar(
                select(AuditContractORMRecord.contract_id).where(
                    AuditContractORMRecord.audit_id == audit_id
                )
            )
            if orphan_contract_id is not None:
                raise RepositoryIntegrityError(
                    "AuditContractRecord",
                    orphan_contract_id,
                    reason_code="orphan_contract",
                )
        return None

    contract_record = await session.scalar(
        select(AuditContractORMRecord).where(
            AuditContractORMRecord.contract_id == scan_record.contract_id,
            AuditContractORMRecord.audit_id == scan_record.id,
            AuditContractORMRecord.contract_digest == scan_record.contract_digest,
        )
    )
    project_record = await session.scalar(
        select(AuditProjectRecord).where(AuditProjectRecord.id == scan_record.project_id)
    )
    run_record = await session.scalar(select(RunRecord).where(RunRecord.id == scan_record.run_id))
    if contract_record is None or project_record is None or run_record is None:
        raise RepositoryIntegrityError(
            "AuditScan",
            audit_id,
            reason_code="owner_binding_mismatch",
        )
    await _validated_project(session, project_record.id)
    scan = validate_audit_scan_record_bundle(
        scan_record,
        contract_record,
        project_record,
        run_record,
        allow_unrecorded_run_terminal=allow_unrecorded_run_terminal,
    )
    for snapshot_id in (scan.snapshot_id, scan.base_snapshot_id):
        if snapshot_id is None:
            continue
        snapshot = await _validated_snapshot(
            session,
            snapshot_id,
            project_id=scan.project_id,
        )
        if snapshot is None:
            raise RepositoryIntegrityError(
                "AuditScan",
                audit_id,
                reason_code="snapshot_binding_mismatch",
            )
    for related_id in (scan.baseline_audit_id, scan.parent_audit_id):
        if related_id is None:
            continue
        if not await _related_audit_belongs_to_project(
            session,
            related_id,
            scan.project_id,
        ):
            raise RepositoryIntegrityError(
                "AuditScan",
                audit_id,
                reason_code="related_audit_binding_mismatch",
            )
    return scan_record, contract_record, project_record, run_record, scan


async def load_validated_audit_scan(
    session: AsyncSession,
    audit_id: str,
    *,
    for_update: bool = False,
) -> (
    tuple[
        AuditScanRecord,
        AuditContractORMRecord,
        AuditProjectRecord,
        RunRecord,
        AuditScan,
    ]
    | None
):
    """Load the authoritative Scan bundle inside a caller-owned session.

    Aggregate application adapters use this public session-bound primitive rather
    than depending on a repository object's auto-commit boundary.
    """

    bundle = await _validated_scan(session, audit_id, for_update=for_update)
    if bundle is None:
        return None
    return bundle


async def create_source_snapshot(
    session: AsyncSession,
    snapshot: SourceSnapshot,
) -> tuple[SourceSnapshot, bool]:
    """Create or replay a SourceSnapshot inside a caller-owned transaction."""

    snapshot = SourceSnapshot.model_validate(snapshot)
    await _validated_project(session, snapshot.project_id, for_update=True)
    if snapshot.parent_snapshot_id is not None:
        parent = await _validated_snapshot(
            session,
            snapshot.parent_snapshot_id,
            project_id=snapshot.project_id,
        )
        if parent is None:
            _conflict("SourceSnapshot", snapshot.id, "has an invalid parent")
    existing = await _single_create_candidate(
        session,
        select(SourceSnapshotRecord).where(
            or_(
                SourceSnapshotRecord.id == snapshot.id,
                and_(
                    SourceSnapshotRecord.project_id == snapshot.project_id,
                    SourceSnapshotRecord.snapshot_digest == snapshot.snapshot_digest,
                ),
            )
        ),
        entity="SourceSnapshot",
        entity_id=snapshot.id,
    )
    if existing is not None:
        bundle = await _validated_snapshot(
            session,
            existing.id,
            project_id=existing.project_id,
        )
        if bundle is None:
            raise RepositoryIntegrityError("SourceSnapshot", existing.id) from None
        persisted = bundle[1]
        if _same_snapshot_creation(persisted, snapshot):
            return persisted, False
        _conflict("SourceSnapshot", snapshot.id, "identity already exists")
    session.add(source_snapshot_to_record(snapshot))
    await session.flush()
    return snapshot, True


class SQLAlchemySnapshotRepository:
    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    async def _find_create_candidate(
        self,
        session: AsyncSession,
        snapshot: SourceSnapshot,
    ) -> SourceSnapshotRecord | None:
        return await _single_create_candidate(
            session,
            select(SourceSnapshotRecord).where(
                or_(
                    SourceSnapshotRecord.id == snapshot.id,
                    and_(
                        SourceSnapshotRecord.project_id == snapshot.project_id,
                        SourceSnapshotRecord.snapshot_digest == snapshot.snapshot_digest,
                    ),
                )
            ),
            entity="SourceSnapshot",
            entity_id=snapshot.id,
        )

    async def create(self, snapshot: SourceSnapshot) -> tuple[SourceSnapshot, bool]:
        snapshot = SourceSnapshot.model_validate(snapshot)
        try:
            async with serialized_write(self._session_factory) as session:
                return await create_source_snapshot(session, snapshot)
        except IntegrityError:
            pass
        async with self._session_factory() as session:
            existing = await self._find_create_candidate(session, snapshot)
            if existing is not None:
                bundle = await _validated_snapshot(
                    session,
                    existing.id,
                    project_id=existing.project_id,
                )
                if bundle is None:
                    raise RepositoryIntegrityError(
                        "SourceSnapshot",
                        existing.id,
                    ) from None
                persisted = bundle[1]
                if _same_snapshot_creation(persisted, snapshot):
                    return persisted, False
        _conflict("SourceSnapshot", snapshot.id, "could not be created")

    async def get(self, project_id: str, snapshot_id: str) -> SourceSnapshot | None:
        async with self._session_factory() as session:
            bundle = await _validated_snapshot(
                session,
                snapshot_id,
                project_id=project_id,
            )
        return bundle[1] if bundle is not None else None

    async def get_by_digest(
        self,
        project_id: str,
        snapshot_digest: str,
    ) -> SourceSnapshot | None:
        async with self._session_factory() as session:
            record = await session.scalar(
                select(SourceSnapshotRecord).where(
                    SourceSnapshotRecord.project_id == project_id,
                    SourceSnapshotRecord.snapshot_digest == snapshot_digest,
                )
            )
            if record is None:
                return None
            bundle = await _validated_snapshot(
                session,
                record.id,
                project_id=project_id,
            )
        return bundle[1] if bundle is not None else None

    async def list(
        self,
        project_id: str,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> Sequence[SourceSnapshot]:
        _validate_page(limit=limit, offset=offset)
        async with self._session_factory() as session:
            await _validated_project(session, project_id)
            records = (
                await session.scalars(
                    select(SourceSnapshotRecord)
                    .where(SourceSnapshotRecord.project_id == project_id)
                    .order_by(SourceSnapshotRecord.created_at, SourceSnapshotRecord.id)
                    .limit(limit)
                    .offset(offset)
                )
            ).all()
            snapshots: list[SourceSnapshot] = []
            for record in records:
                bundle = await _validated_snapshot(
                    session,
                    record.id,
                    project_id=project_id,
                )
                if bundle is None:
                    raise RepositoryIntegrityError("SourceSnapshot", record.id)
                snapshots.append(bundle[1])
        return snapshots


def _validate_scan_replacement(current: AuditScan, replacement: AuditScan) -> None:
    if current == replacement:
        return

    allowed: list[AuditScan] = []

    def admit(operation: Callable[[], AuditScan]) -> None:
        try:
            candidate = operation()
        except (TypeError, ValueError):
            return
        if candidate != current:
            allowed.append(candidate)

    if current.lifecycle_status is not replacement.lifecycle_status:
        transition_at = (
            replacement.started_at
            if replacement.lifecycle_status is AuditLifecycleStatus.QUEUED
            else replacement.analysis_finished_at
            if replacement.lifecycle_status is AuditLifecycleStatus.SEALING_CORE
            else current.created_at
        )
        admit(
            lambda: current.transition_to(
                replacement.lifecycle_status,
                at=transition_at,
                terminal_outcome=(
                    replacement.terminal_outcome
                    if replacement.lifecycle_status is AuditLifecycleStatus.FINALIZING
                    else None
                ),
            )
        )
    replacement_snapshot_id = replacement.snapshot_id
    if replacement_snapshot_id is not None and (
        replacement_snapshot_id != current.snapshot_id
        or replacement.base_snapshot_id != current.base_snapshot_id
    ):
        admit(
            lambda: current.bind_snapshots(
                snapshot_id=replacement_snapshot_id,
                base_snapshot_id=replacement.base_snapshot_id,
            )
        )
    if replacement.current_phase is not current.current_phase:
        admit(lambda: current.transition_phase_to(replacement.current_phase))
    replacement_cleanup_proof = replacement.cleanup_proof_digest
    replacement_run_terminal_status = replacement.run_terminal_status
    if (
        replacement_cleanup_proof is not None
        and replacement_run_terminal_status is not None
        and (
            replacement_cleanup_proof != current.cleanup_proof_digest
            or replacement_run_terminal_status is not current.run_terminal_status
        )
    ):
        admit(
            lambda: current.record_cleanup_convergence(
                cleanup_proof_digest=replacement_cleanup_proof,
                run_terminal_status=replacement_run_terminal_status,
            )
        )
    replacement_closure = replacement.closure_status
    if replacement_closure is not None and replacement_closure is not current.closure_status:
        admit(lambda: current.record_closure(replacement_closure))
    replacement_core_seal = replacement.core_seal_root
    if replacement_core_seal is not None and replacement_core_seal != current.core_seal_root:
        admit(
            lambda: current.record_core_seal(
                core_seal_root=replacement_core_seal,
                at=replacement.sealed_at,
            )
        )
    admit(current.begin_publication_retry)
    if replacement.publication_status is not current.publication_status:
        admit(lambda: current.transition_terminal_publication_to(replacement.publication_status))
        admit(lambda: current.record_publication_failure(replacement.publication_status))

    if replacement not in allowed:
        _conflict(
            "AuditScan",
            current.id,
            "replacement is not one authoritative domain mutation",
        )


async def compare_and_set_audit_scan(
    session: AsyncSession,
    current: StoredAuditEntity[AuditScan],
    replacement: AuditScan,
    *,
    allow_run_convergence: bool = False,
) -> tuple[StoredAuditEntity[AuditScan], bool]:
    """CAS an Audit aggregate inside a caller-owned transaction.

    ``allow_run_convergence`` is reserved for the state projector UoW after it
    has locked and transitioned the associated Run to the matching terminal
    status in this same transaction.  Auto-commit Repository calls cannot use
    it, preventing a Scan from claiming a Run terminal fact independently.
    """

    replacement = AuditScan.model_validate(replacement)
    _reject_distribution_facts(replacement)
    adds_run_convergence = (
        current.value.cleanup_proof_digest is None
        and current.value.run_terminal_status is None
        and replacement.cleanup_proof_digest is not None
        and replacement.run_terminal_status is not None
    )
    if adds_run_convergence and not allow_run_convergence:
        _conflict(
            "AuditScan",
            current.value.id,
            "Run convergence requires the caller-owned state projector transaction",
        )
    bundle = await _validated_scan(
        session,
        current.value.id,
        expected_state_version=current.state_version,
        for_update=True,
        allow_unrecorded_run_terminal=adds_run_convergence and allow_run_convergence,
    )
    if bundle is None:
        latest = await _validated_scan(
            session,
            current.value.id,
            for_update=True,
        )
        if latest is None:
            raise EntityNotFoundError("AuditScan", current.value.id)
        stored = StoredAuditEntity(latest[-1], latest[0].state_version)
        if stored.value == replacement:
            return stored, False
        _conflict("AuditScan", current.value.id)
    record, contract_record, project_record, run_record, persisted_scan = bundle
    persisted = StoredAuditEntity(persisted_scan, record.state_version)
    if persisted != current:
        _conflict("AuditScan", current.value.id, "CAS token/value mismatch")
    replacement_contract = audit_contract_from_record(contract_record)
    _validate_contract_binding(replacement, replacement_contract)
    _validate_scan_replacement(persisted_scan, replacement)
    if adds_run_convergence:
        locked_run = await session.scalar(
            select(RunRecord).where(RunRecord.id == run_record.id).with_for_update()
        )
        if (
            locked_run is None
            or replacement.run_terminal_status is None
            or locked_run.status != replacement.run_terminal_status.value
        ):
            _conflict(
                "AuditScan",
                current.value.id,
                "does not match the terminal Run in the projector transaction",
            )
    if persisted_scan == replacement:
        return persisted, False
    for snapshot_id in (replacement.snapshot_id, replacement.base_snapshot_id):
        if snapshot_id is None:
            continue
        snapshot = await _validated_snapshot(
            session,
            snapshot_id,
            project_id=replacement.project_id,
        )
        if snapshot is None:
            _conflict(
                "AuditScan",
                replacement.id,
                "references an invalid Project snapshot",
            )
    candidate = audit_scan_to_record(
        replacement,
        engagement_id=project_record.engagement_id,
        state_version=current.state_version + 1,
    )
    result = await session.execute(
        update(AuditScanRecord)
        .where(
            AuditScanRecord.id == current.value.id,
            AuditScanRecord.state_version == current.state_version,
        )
        .values(**_record_values(candidate, excluding=frozenset({"id"})))
    )
    if result.rowcount != 1:  # type: ignore[attr-defined]
        _conflict("AuditScan", current.value.id)
    return StoredAuditEntity(replacement, current.state_version + 1), True


__all__ = [
    "SQLAlchemySnapshotRepository",
    "compare_and_set_audit_scan",
    "create_source_snapshot",
    "load_validated_audit_scan",
    "validate_audit_scan_record_bundle",
]
