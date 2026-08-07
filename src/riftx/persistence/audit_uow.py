"""Historical Code Audit aggregate reads retained for compatibility and Safety Stop."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import NoReturn

from sqlalchemy import and_, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from riftx.application.errors import RepositoryIntegrityError, RepositoryUnavailableError
from riftx.application.ports import (
    AuditAggregate,
    AuditAuthorizationBinding,
    AuditBindingAuthorizer,
    AuditEngagementScope,
    StoredAuditEntity,
)
from riftx.domain import AuditLifecycleStatus, AuditMode, AuditScan, RunStatus

from .audit_mappers import (
    audit_client_request_from_record,
    audit_contract_from_record,
    audit_project_from_record,
    source_snapshot_from_record,
)
from .audit_repositories import (
    load_validated_audit_scan,
    validate_audit_scan_record_bundle,
)
from .mappers import engagement_from_record, run_from_record
from .orm import (
    AuditClientRequestRecord,
    AuditProjectRecord,
    AuditScanRecord,
    EngagementRecord,
    RunRecord,
    SourceSnapshotRecord,
)
from .orm import AuditContractRecord as AuditContractORMRecord
from .transactions import SessionFactory, consistent_read

_MAX_PAGE_SIZE = 200


def _database_unavailable() -> NoReturn:
    raise RepositoryUnavailableError("Code Audit persistence operation failed") from None


def _validate_page(*, limit: int, offset: int) -> None:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= _MAX_PAGE_SIZE:
        raise ValueError(f"limit must be between 1 and {_MAX_PAGE_SIZE}")
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise ValueError("offset must not be negative")


def _validate_created_range(
    created_from: datetime | None,
    created_to: datetime | None,
) -> None:
    for value in (created_from, created_to):
        if value is not None and value.utcoffset() is None:
            raise ValueError("Audit created-time filters must be timezone-aware")
    if created_from is not None and created_to is not None and created_from > created_to:
        raise ValueError("created_from must not be later than created_to")


async def _read_aggregate(
    session: AsyncSession,
    audit_id: str,
    *,
    client_request_record: AuditClientRequestRecord | None = None,
    for_update: bool = False,
) -> AuditAggregate | None:
    bundle = await load_validated_audit_scan(session, audit_id, for_update=for_update)
    if bundle is None:
        return None
    scan_record, contract_record, project_record, run_record, scan = bundle
    if client_request_record is None:
        statement = select(AuditClientRequestRecord).where(
            AuditClientRequestRecord.audit_id == audit_id
        )
        if for_update:
            statement = statement.with_for_update()
        request_records = (await session.scalars(statement.limit(2))).all()
        if len(request_records) > 1:
            raise RepositoryIntegrityError(
                "AuditScan",
                audit_id,
                reason_code="ambiguous_client_request",
            )
        client_request_record = request_records[0] if request_records else None
    if client_request_record is None:
        raise RepositoryIntegrityError(
            "AuditScan",
            audit_id,
            reason_code="orphan_client_request",
        )
    engagement_record = await session.get(EngagementRecord, project_record.engagement_id)
    if engagement_record is None:
        raise RepositoryIntegrityError(
            "AuditScan",
            audit_id,
            reason_code="owner_binding_mismatch",
        )
    return _aggregate_from_records(
        scan_record,
        contract_record,
        project_record,
        run_record,
        engagement_record,
        client_request_record,
        scan,
    )


def _aggregate_from_records(
    scan_record: AuditScanRecord,
    contract_record: AuditContractORMRecord,
    project_record: AuditProjectRecord,
    run_record: RunRecord,
    engagement_record: EngagementRecord,
    client_request_record: AuditClientRequestRecord,
    scan: AuditScan,
) -> AuditAggregate:
    try:
        return AuditAggregate(
            audit=StoredAuditEntity(scan, scan_record.state_version),
            contract=StoredAuditEntity(
                audit_contract_from_record(contract_record),
                contract_record.state_version,
            ),
            project=StoredAuditEntity(
                audit_project_from_record(project_record),
                project_record.state_version,
            ),
            run=run_from_record(run_record),
            engagement=engagement_from_record(engagement_record),
            client_request=audit_client_request_from_record(client_request_record),
        )
    except RepositoryIntegrityError:
        raise
    except (AttributeError, TypeError, ValueError, OverflowError):
        raise RepositoryIntegrityError(
            "AuditScan",
            scan.id,
            reason_code="aggregate_binding_mismatch",
        ) from None


async def _validate_batched_scan_relations(
    session: AsyncSession,
    scans: Sequence[AuditScan],
) -> None:
    snapshot_bindings = [
        (snapshot_id, scan.project_id, scan.id)
        for scan in scans
        for snapshot_id in (scan.snapshot_id, scan.base_snapshot_id)
        if snapshot_id is not None
    ]
    if snapshot_bindings:
        snapshot_ids = {binding[0] for binding in snapshot_bindings}
        snapshot_records = (
            await session.scalars(
                select(SourceSnapshotRecord).where(SourceSnapshotRecord.id.in_(snapshot_ids))
            )
        ).all()
        snapshots = {record.id: source_snapshot_from_record(record) for record in snapshot_records}
        for snapshot_id, project_id, audit_id in snapshot_bindings:
            snapshot = snapshots.get(snapshot_id)
            if snapshot is None or snapshot.project_id != project_id:
                raise RepositoryIntegrityError(
                    "AuditScan",
                    audit_id,
                    reason_code="snapshot_binding_mismatch",
                )

        parent_ids = {
            snapshot.parent_snapshot_id
            for snapshot in snapshots.values()
            if snapshot.parent_snapshot_id is not None
        }
        missing_parent_ids = parent_ids - snapshots.keys()
        if missing_parent_ids:
            parent_records = (
                await session.scalars(
                    select(SourceSnapshotRecord).where(
                        SourceSnapshotRecord.id.in_(missing_parent_ids)
                    )
                )
            ).all()
            snapshots.update(
                {record.id: source_snapshot_from_record(record) for record in parent_records}
            )
        for snapshot_id, _, audit_id in snapshot_bindings:
            snapshot = snapshots[snapshot_id]
            if snapshot.parent_snapshot_id is None:
                continue
            parent = snapshots.get(snapshot.parent_snapshot_id)
            if parent is None or parent.project_id != snapshot.project_id:
                raise RepositoryIntegrityError(
                    "AuditScan",
                    audit_id,
                    reason_code="snapshot_binding_mismatch",
                )

    related_bindings = [
        (related_id, scan.project_id, scan.id)
        for scan in scans
        for related_id in (scan.baseline_audit_id, scan.parent_audit_id)
        if related_id is not None
    ]
    if related_bindings:
        related_ids = {binding[0] for binding in related_bindings}
        related_rows = (
            await session.execute(
                select(AuditScanRecord.id, AuditScanRecord.project_id).where(
                    AuditScanRecord.id.in_(related_ids)
                )
            )
        ).all()
        related_projects = {audit_id: project_id for audit_id, project_id in related_rows}
        for related_id, project_id, audit_id in related_bindings:
            if related_projects.get(related_id) != project_id:
                raise RepositoryIntegrityError(
                    "AuditScan",
                    audit_id,
                    reason_code="related_audit_binding_mismatch",
                )


async def _read_aggregates(
    session: AsyncSession,
    audit_ids: Sequence[str],
) -> Sequence[AuditAggregate]:
    if not audit_ids:
        return ()
    statement = (
        select(
            AuditScanRecord,
            AuditContractORMRecord,
            AuditProjectRecord,
            RunRecord,
            EngagementRecord,
            AuditClientRequestRecord,
        )
        .select_from(AuditScanRecord)
        .outerjoin(
            AuditContractORMRecord,
            and_(
                AuditContractORMRecord.contract_id == AuditScanRecord.contract_id,
                AuditContractORMRecord.audit_id == AuditScanRecord.id,
                AuditContractORMRecord.contract_digest == AuditScanRecord.contract_digest,
            ),
        )
        .outerjoin(AuditProjectRecord, AuditProjectRecord.id == AuditScanRecord.project_id)
        .outerjoin(RunRecord, RunRecord.id == AuditScanRecord.run_id)
        .outerjoin(
            EngagementRecord,
            EngagementRecord.id == AuditProjectRecord.engagement_id,
        )
        .outerjoin(
            AuditClientRequestRecord,
            AuditClientRequestRecord.audit_id == AuditScanRecord.id,
        )
        .where(AuditScanRecord.id.in_(audit_ids))
    )
    rows = (await session.execute(statement)).all()
    aggregates: dict[str, AuditAggregate] = {}
    scans: list[AuditScan] = []
    for row in rows:
        scan_record = row[0]
        audit_id = scan_record.id
        if audit_id in aggregates:
            raise RepositoryIntegrityError(
                "AuditScan",
                audit_id,
                reason_code="ambiguous_aggregate_binding",
            )
        contract_record = row[1]
        project_record = row[2]
        run_record = row[3]
        engagement_record = row[4]
        client_request_record = row[5]
        if (
            contract_record is None
            or project_record is None
            or run_record is None
            or engagement_record is None
            or client_request_record is None
        ):
            raise RepositoryIntegrityError(
                "AuditScan",
                audit_id,
                reason_code="owner_binding_mismatch",
            )
        scan = validate_audit_scan_record_bundle(
            scan_record,
            contract_record,
            project_record,
            run_record,
        )
        scans.append(scan)
        aggregates[audit_id] = _aggregate_from_records(
            scan_record,
            contract_record,
            project_record,
            run_record,
            engagement_record,
            client_request_record,
            scan,
        )
    if len(aggregates) != len(audit_ids):
        missing_id = next(audit_id for audit_id in audit_ids if audit_id not in aggregates)
        raise RepositoryIntegrityError(
            "AuditScan",
            missing_id,
            reason_code="aggregate_disappeared",
        )
    await _validate_batched_scan_relations(session, scans)
    return tuple(aggregates[audit_id] for audit_id in audit_ids)


async def _read_audit_authorization_binding(
    session: AsyncSession,
    audit_id: str,
) -> AuditAuthorizationBinding | None:
    """Read only raw owner columns; never select canonical Contract content."""

    statement = (
        select(
            AuditScanRecord.id,
            AuditScanRecord.run_id,
            AuditScanRecord.project_id,
            AuditScanRecord.engagement_id,
            AuditScanRecord.contract_id,
            AuditScanRecord.contract_digest,
            RunRecord.id,
            RunRecord.engagement_id,
            RunRecord.kind,
            AuditProjectRecord.id,
            AuditProjectRecord.engagement_id,
            EngagementRecord.id,
            AuditContractORMRecord.contract_id,
            AuditContractORMRecord.audit_id,
            AuditContractORMRecord.contract_digest,
            AuditClientRequestRecord.audit_id,
            AuditClientRequestRecord.run_id,
            AuditClientRequestRecord.project_id,
            AuditClientRequestRecord.engagement_id,
            AuditClientRequestRecord.contract_id,
            AuditClientRequestRecord.contract_digest,
        )
        .select_from(AuditScanRecord)
        .outerjoin(RunRecord, RunRecord.id == AuditScanRecord.run_id)
        .outerjoin(AuditProjectRecord, AuditProjectRecord.id == AuditScanRecord.project_id)
        .outerjoin(
            EngagementRecord,
            EngagementRecord.id == AuditProjectRecord.engagement_id,
        )
        .outerjoin(
            AuditContractORMRecord,
            AuditContractORMRecord.contract_id == AuditScanRecord.contract_id,
        )
        .outerjoin(
            AuditClientRequestRecord,
            AuditClientRequestRecord.audit_id == AuditScanRecord.id,
        )
        .where(AuditScanRecord.id == audit_id)
        .limit(2)
    )
    rows = (await session.execute(statement)).all()
    if len(rows) > 1:
        raise RepositoryIntegrityError(
            "AuditScan",
            audit_id,
            reason_code="ambiguous_authorization_binding",
        )
    if not rows:
        return None
    row = rows[0]
    return AuditAuthorizationBinding(
        requested_audit_id=audit_id,
        audit_id=row[0],
        scan_run_id=row[1],
        scan_project_id=row[2],
        scan_engagement_id=row[3],
        scan_contract_id=row[4],
        scan_contract_digest=row[5],
        run_id=row[6],
        run_engagement_id=row[7],
        run_kind=row[8],
        project_id=row[9],
        project_engagement_id=row[10],
        engagement_id=row[11],
        contract_id=row[12],
        contract_audit_id=row[13],
        contract_digest=row[14],
        request_audit_id=row[15],
        request_run_id=row[16],
        request_project_id=row[17],
        request_engagement_id=row[18],
        request_contract_id=row[19],
        request_contract_digest=row[20],
    )


def _require_aggregate_matches_authorized_binding(
    aggregate: AuditAggregate,
    binding: AuditAuthorizationBinding,
) -> None:
    scan = aggregate.audit.value
    contract = aggregate.contract.value
    project = aggregate.project.value
    actual = (
        scan.id,
        scan.run_id,
        scan.project_id,
        scan.contract_id,
        scan.contract_digest,
        aggregate.run.id,
        aggregate.run.engagement_id,
        aggregate.run.kind.value,
        project.id,
        project.engagement_id,
        aggregate.engagement.id,
        contract.contract_id,
        contract.audit_id,
        contract.contract_digest,
        aggregate.client_request.audit_id,
        aggregate.client_request.run_id,
        aggregate.client_request.project_id,
        aggregate.client_request.engagement_id,
        aggregate.client_request.contract_id,
        aggregate.client_request.contract_digest,
    )
    expected = (
        binding.audit_id,
        binding.scan_run_id,
        binding.scan_project_id,
        binding.scan_contract_id,
        binding.scan_contract_digest,
        binding.run_id,
        binding.run_engagement_id,
        binding.run_kind,
        binding.project_id,
        binding.project_engagement_id,
        binding.engagement_id,
        binding.contract_id,
        binding.contract_audit_id,
        binding.contract_digest,
        binding.request_audit_id,
        binding.request_run_id,
        binding.request_project_id,
        binding.request_engagement_id,
        binding.request_contract_id,
        binding.request_contract_digest,
    )
    if actual != expected:
        raise RepositoryIntegrityError(
            "AuditScan",
            binding.requested_audit_id,
            reason_code="authorized_binding_changed",
        )


class _AuditFilterMismatch(RuntimeError):
    """Internal signal used to preserve the pre-authorization legacy read API."""


class SQLAlchemyAuditAggregateReadRepository:
    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    async def get(
        self,
        audit_id: str,
        *,
        project_id: str | None = None,
        engagement_id: str | None = None,
    ) -> AuditAggregate | None:
        def authorize_filter(binding: AuditAuthorizationBinding) -> None:
            if (
                project_id is not None and binding.project_id != project_id
            ) or (
                engagement_id is not None and binding.engagement_id != engagement_id
            ):
                raise _AuditFilterMismatch

        try:
            return await self.get_authorized(audit_id, authorize=authorize_filter)
        except _AuditFilterMismatch:
            return None

    async def get_authorized(
        self,
        audit_id: str,
        *,
        authorize: AuditBindingAuthorizer,
    ) -> AuditAggregate | None:
        try:
            async with consistent_read(self._session_factory) as session:
                binding = await _read_audit_authorization_binding(session, audit_id)
                if binding is None:
                    return None
                authorize(binding)
                aggregates = await _read_aggregates(session, (binding.audit_id,))
                aggregate = aggregates[0]
                _require_aggregate_matches_authorized_binding(aggregate, binding)
                return aggregate
        except SQLAlchemyError:
            _database_unavailable()

    async def get_by_run_authorized(
        self,
        run_id: str,
        *,
        authorize: AuditBindingAuthorizer,
    ) -> AuditAggregate | None:
        """Authorize raw owner columns before parsing the Audit aggregate."""

        try:
            async with consistent_read(self._session_factory) as session:
                audit_ids = (
                    await session.scalars(
                        select(AuditScanRecord.id)
                        .where(AuditScanRecord.run_id == run_id)
                        .limit(2)
                    )
                ).all()
                if len(audit_ids) > 1:
                    raise RepositoryIntegrityError(
                        "AuditScan",
                        run_id,
                        reason_code="ambiguous_run_authorization_binding",
                    )
                if not audit_ids:
                    return None
                binding = await _read_audit_authorization_binding(session, audit_ids[0])
                if binding is None:
                    raise RepositoryIntegrityError(
                        "AuditScan",
                        audit_ids[0],
                        reason_code="authorization_binding_disappeared",
                    )
                authorize(binding)
                aggregates = await _read_aggregates(session, (binding.audit_id,))
                aggregate = aggregates[0]
                _require_aggregate_matches_authorized_binding(aggregate, binding)
                return aggregate
        except SQLAlchemyError:
            _database_unavailable()

    async def list(
        self,
        *,
        run_id: str | None = None,
        project_id: str | None = None,
        engagement_id: str | None = None,
        lifecycle_status: AuditLifecycleStatus | None = None,
        mode: AuditMode | None = None,
        run_status: RunStatus | None = None,
        created_from: datetime | None = None,
        created_to: datetime | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Sequence[AuditAggregate]:
        scope = (
            AuditEngagementScope(
                all_engagements=False,
                engagement_ids=frozenset({engagement_id}),
                can_create_engagement=False,
            )
            if engagement_id is not None
            else AuditEngagementScope.profile_a()
        )
        return await self.list_authorized(
            authorized_scope=scope,
            run_id=run_id,
            project_id=project_id,
            lifecycle_status=lifecycle_status,
            mode=mode,
            run_status=run_status,
            created_from=created_from,
            created_to=created_to,
            limit=limit,
            offset=offset,
        )

    async def list_authorized(
        self,
        *,
        authorized_scope: AuditEngagementScope,
        run_id: str | None = None,
        project_id: str | None = None,
        engagement_id: str | None = None,
        lifecycle_status: AuditLifecycleStatus | None = None,
        mode: AuditMode | None = None,
        run_status: RunStatus | None = None,
        created_from: datetime | None = None,
        created_to: datetime | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Sequence[AuditAggregate]:
        _validate_page(limit=limit, offset=offset)
        _validate_created_range(created_from, created_to)
        if not isinstance(authorized_scope, AuditEngagementScope):
            raise TypeError("authorized_scope must be AuditEngagementScope")
        statement = (
            select(AuditScanRecord.id)
            .outerjoin(
                AuditProjectRecord,
                AuditProjectRecord.id == AuditScanRecord.project_id,
            )
            .outerjoin(
                RunRecord,
                RunRecord.id == AuditScanRecord.run_id,
            )
        )
        if not authorized_scope.all_engagements:
            if not authorized_scope.engagement_ids:
                return ()
            statement = statement.where(
                AuditProjectRecord.engagement_id.in_(authorized_scope.engagement_ids)
            )
        if run_id is not None:
            statement = statement.where(AuditScanRecord.run_id == run_id)
        if project_id is not None:
            statement = statement.where(AuditScanRecord.project_id == project_id)
        if engagement_id is not None:
            statement = statement.where(AuditProjectRecord.engagement_id == engagement_id)
        if lifecycle_status is not None:
            statement = statement.where(AuditScanRecord.lifecycle_status == lifecycle_status.value)
        if mode is not None:
            statement = statement.where(AuditScanRecord.mode == mode.value)
        if run_status is not None:
            statement = statement.where(RunRecord.status == run_status.value)
        if created_from is not None:
            statement = statement.where(AuditScanRecord.created_at >= created_from)
        if created_to is not None:
            statement = statement.where(AuditScanRecord.created_at <= created_to)
        statement = (
            statement.order_by(
                AuditScanRecord.created_at.desc(),
                AuditScanRecord.id.desc(),
            )
            .limit(limit)
            .offset(offset)
        )
        try:
            async with consistent_read(self._session_factory) as session:
                audit_ids = (await session.scalars(statement)).all()
                return await _read_aggregates(session, audit_ids)
        except SQLAlchemyError:
            _database_unavailable()

__all__ = ["SQLAlchemyAuditAggregateReadRepository"]
