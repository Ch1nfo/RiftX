"""Atomic draft creation and aggregate reads for RiftX Code Audit."""

from __future__ import annotations

import hmac
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import PurePosixPath
from typing import NoReturn
from uuid import UUID

from sqlalchemy import and_, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from riftx.application.errors import (
    AuditIdempotencyConflictError,
    EntityNotFoundError,
    RepositoryConflictError,
    RepositoryIntegrityError,
    RepositoryUnavailableError,
)
from riftx.application.ports import (
    AuditAggregate,
    AuditAuthorizationBinding,
    AuditBindingAuthorizer,
    AuditDraftAggregateFactory,
    AuditDraftCreationEnvelope,
    AuditEngagementScope,
    StoredAuditEntity,
)
from riftx.domain import (
    AuditClientRequest,
    AuditContractRecord,
    AuditLifecycleStatus,
    AuditMode,
    AuditProject,
    AuditRunStateMappingPolicy,
    AuditScan,
    Engagement,
    Run,
    RunEvent,
    RunKind,
    RunStatus,
)

from .audit_mappers import (
    audit_client_request_from_record,
    audit_client_request_to_record,
    audit_contract_from_record,
    audit_project_from_record,
    source_snapshot_from_record,
)
from .audit_repositories import (
    create_audit_project,
    create_scan_contract_pair,
    load_validated_audit_scan,
    validate_audit_scan_record_bundle,
)
from .mappers import (
    engagement_from_record,
    engagement_to_record,
    event_to_record,
    run_from_record,
    run_to_record,
)
from .orm import (
    AuditClientRequestRecord,
    AuditProjectRecord,
    AuditScanRecord,
    EngagementRecord,
    RunRecord,
    SourceSnapshotRecord,
)
from .orm import AuditContractRecord as AuditContractORMRecord
from .transactions import SessionFactory, consistent_read, serialized_write

_MAX_CREATE_ATTEMPTS = 4
_MAX_PAGE_SIZE = 200

type AuditCreationFailpoint = Callable[[str], None]

_OPAQUE_ID_CHARACTERS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._:@+~-"
)


@dataclass(frozen=True, slots=True)
class _AuditFactoryIdentity:
    client_request_id: str
    request_digest: str
    repository_identity_digest: str
    authorization_reference: str
    authorized_engagement_scope: AuditEngagementScope
    requested_engagement_id: str | None
    workspace_root: str
    source_repository_path: str


class _RetryAuditCreation(RuntimeError):
    """Internal rollback-and-retry signal for a Project natural-key race."""


def _opaque_conflict(message: str) -> NoReturn:
    raise RepositoryConflictError(message) from None


def _idempotency_conflict() -> NoReturn:
    raise AuditIdempotencyConflictError(
        "Audit client request is already bound to different content"
    ) from None


def _database_unavailable() -> NoReturn:
    raise RepositoryUnavailableError("Code Audit persistence operation failed") from None


def _validate_factory_identity(
    factory: AuditDraftAggregateFactory,
) -> _AuditFactoryIdentity:
    try:
        client_request_id = factory.client_request_id
        request_digest = factory.request_digest
        repository_digest = factory.repository_identity_digest
        authorization_digest = factory.authorization_reference
        authorized_scope = factory.authorized_engagement_scope
        requested_engagement_id = factory.requested_engagement_id
        workspace_root = factory.workspace_root
        source_path = factory.source_repository_path
    except (AttributeError, TypeError, ValueError, OverflowError):
        _opaque_conflict("Audit draft creation identity is invalid")
    try:
        request_uuid = UUID(client_request_id)
    except (AttributeError, TypeError, ValueError):
        request_uuid = None
    if (
        not isinstance(client_request_id, str)
        or len(client_request_id) != 36
        or request_uuid is None
        or request_uuid.int == 0
        or str(request_uuid) != client_request_id
        or not isinstance(request_digest, str)
        or len(request_digest) != 64
        or any(character not in "0123456789abcdef" for character in request_digest)
        or not isinstance(repository_digest, str)
        or len(repository_digest) != 64
        or any(character not in "0123456789abcdef" for character in repository_digest)
        or not isinstance(authorization_digest, str)
        or len(authorization_digest) != 64
        or any(character not in "0123456789abcdef" for character in authorization_digest)
        or not isinstance(authorized_scope, AuditEngagementScope)
        or not isinstance(workspace_root, str)
        or not isinstance(source_path, str)
        or not source_path
        or (
            requested_engagement_id is not None
            and (
                not isinstance(requested_engagement_id, str)
                or not 1 <= len(requested_engagement_id) <= 64
                or any(
                    character not in _OPAQUE_ID_CHARACTERS for character in requested_engagement_id
                )
            )
        )
    ):
        _opaque_conflict("Audit draft creation identity is invalid")
    root = PurePosixPath(workspace_root)
    if not root.is_absolute() or ".." in root.parts or str(root) != workspace_root:
        _opaque_conflict("Audit workspace policy is invalid")
    if source_path.startswith("/"):
        source = PurePosixPath(source_path)
        if (
            ".." in source.parts
            or str(source) != source_path
            or root == source
            or root.is_relative_to(source)
            or source.is_relative_to(root)
        ):
            _opaque_conflict("Audit workspace policy is invalid")
    return _AuditFactoryIdentity(
        client_request_id=client_request_id,
        request_digest=request_digest,
        repository_identity_digest=repository_digest,
        authorization_reference=authorization_digest,
        authorized_engagement_scope=authorized_scope,
        requested_engagement_id=requested_engagement_id,
        workspace_root=workspace_root,
        source_repository_path=source_path,
    )


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


async def _read_client_request(
    session: AsyncSession,
    identity: _AuditFactoryIdentity,
    *,
    for_update: bool,
) -> AuditAggregate | None:
    statement = select(AuditClientRequestRecord).where(
        AuditClientRequestRecord.client_request_id == identity.client_request_id
    )
    if for_update:
        statement = statement.with_for_update()
    records = (await session.scalars(statement.limit(2))).all()
    if len(records) > 1:
        raise RepositoryIntegrityError(
            "AuditClientRequest",
            identity.client_request_id,
            reason_code="ambiguous_client_request",
        )
    if not records:
        return None
    record = records[0]
    request = audit_client_request_from_record(record)
    if not hmac.compare_digest(request.request_digest, identity.request_digest):
        _idempotency_conflict()
    aggregate = await _read_aggregate(
        session,
        request.audit_id,
        client_request_record=record,
        for_update=for_update,
    )
    if aggregate is None:
        raise RepositoryIntegrityError(
            "AuditClientRequest",
            request.client_request_id,
            reason_code="orphan_audit_binding",
        )
    return aggregate


async def _validated_engagement(
    session: AsyncSession,
    engagement_id: str,
    *,
    for_update: bool,
) -> tuple[EngagementRecord, Engagement] | None:
    statement = select(EngagementRecord).where(EngagementRecord.id == engagement_id)
    if for_update:
        statement = statement.with_for_update()
    record = await session.scalar(statement)
    if record is None:
        return None
    try:
        return record, engagement_from_record(record)
    except (AttributeError, TypeError, ValueError, OverflowError):
        raise RepositoryIntegrityError(
            "Engagement",
            engagement_id,
            reason_code="invalid_persisted_state",
        ) from None


async def _project_by_repository_identity(
    session: AsyncSession,
    repository_identity_digest: str,
    *,
    for_update: bool,
) -> AuditProjectRecord | None:
    statement = select(AuditProjectRecord).where(
        AuditProjectRecord.repository_identity_digest == repository_identity_digest
    )
    if for_update:
        statement = statement.with_for_update()
    records = (await session.scalars(statement.limit(2))).all()
    if len(records) > 1:
        raise RepositoryIntegrityError(
            "AuditProject",
            "duplicate-repository-identity",
            reason_code="ambiguous_natural_identity",
        )
    return records[0] if records else None


def _require_authorized_engagement(
    engagement: Engagement,
    identity: _AuditFactoryIdentity,
) -> None:
    if (
        not identity.authorized_engagement_scope.permits(engagement.id)
        or
        engagement.authorization_reference is None
        or not hmac.compare_digest(
            engagement.authorization_reference,
            identity.authorization_reference,
        )
        or (
            identity.requested_engagement_id is not None
            and engagement.id != identity.requested_engagement_id
        )
    ):
        _opaque_conflict("Audit project authorization domain conflicts with the request")


async def _resolve_project(
    session: AsyncSession,
    factory: AuditDraftAggregateFactory,
    identity: _AuditFactoryIdentity,
    *,
    failpoint: AuditCreationFailpoint | None,
) -> tuple[AuditProject, Engagement]:
    record = await _project_by_repository_identity(
        session,
        identity.repository_identity_digest,
        for_update=True,
    )
    if record is not None:
        try:
            project = audit_project_from_record(record)
        except RepositoryIntegrityError:
            raise
        engagement_bundle = await _validated_engagement(
            session,
            project.engagement_id,
            for_update=True,
        )
        if engagement_bundle is None:
            raise RepositoryIntegrityError(
                "AuditProject",
                project.id,
                reason_code="owner_binding_mismatch",
            )
        engagement = engagement_bundle[1]
        _require_authorized_engagement(engagement, identity)
        return project, engagement

    if identity.requested_engagement_id is not None:
        if not identity.authorized_engagement_scope.permits(
            identity.requested_engagement_id
        ):
            _opaque_conflict("Audit Engagement is outside the authorized scope")
        engagement_bundle = await _validated_engagement(
            session,
            identity.requested_engagement_id,
            for_update=True,
        )
        if engagement_bundle is None:
            raise EntityNotFoundError("Engagement", identity.requested_engagement_id)
        engagement = engagement_bundle[1]
        _require_authorized_engagement(engagement, identity)
    else:
        if not identity.authorized_engagement_scope.can_create_engagement:
            _opaque_conflict("Audit Engagement creation is outside the authorized scope")
        try:
            engagement = Engagement.model_validate(factory.build_engagement())
        except (AttributeError, TypeError, ValueError, OverflowError):
            _opaque_conflict("Audit Engagement candidate is invalid")
        if engagement.authorization_reference is None or not hmac.compare_digest(
            engagement.authorization_reference,
            identity.authorization_reference,
        ):
            _opaque_conflict("Audit Engagement candidate is outside its authorization domain")
        existing_engagement = await _validated_engagement(
            session,
            engagement.id,
            for_update=True,
        )
        if existing_engagement is None:
            session.add(engagement_to_record(engagement))
            await session.flush()
        else:
            raise _RetryAuditCreation from None
        _hit(failpoint, "after_engagement")

    try:
        candidate = AuditProject.model_validate(factory.build_project(engagement))
    except (AttributeError, TypeError, ValueError, OverflowError):
        _opaque_conflict("Audit Project candidate is invalid")
    if candidate.engagement_id != engagement.id or not hmac.compare_digest(
        candidate.repository_identity_digest,
        identity.repository_identity_digest,
    ):
        _opaque_conflict("Audit Project candidate has an invalid owner or identity")
    try:
        stored, _ = await create_audit_project(session, candidate)
    except RepositoryConflictError:
        # PostgreSQL has no gap lock for the initial natural-key miss. A winner
        # can commit between that miss and create_audit_project's own lookup,
        # which reports an identity conflict rather than raising IntegrityError.
        # Roll back the temporary Engagement and resolve the winner afresh.
        winner = await _project_by_repository_identity(
            session,
            identity.repository_identity_digest,
            for_update=False,
        )
        candidate_id_owner = await session.get(AuditProjectRecord, candidate.id)
        if winner is not None or candidate_id_owner is not None:
            raise _RetryAuditCreation from None
        raise
    _hit(failpoint, "after_project")
    if stored.value.engagement_id != engagement.id:
        _opaque_conflict("Audit Project resolved outside the authorization domain")
    return stored.value, engagement


def _hit(failpoint: AuditCreationFailpoint | None, stage: str) -> None:
    if failpoint is not None:
        failpoint(stage)


def _validate_creation_envelope(
    envelope: AuditDraftCreationEnvelope,
    *,
    project: AuditProject,
    engagement: Engagement,
    identity: _AuditFactoryIdentity,
) -> AuditDraftCreationEnvelope:
    run = Run.model_validate(envelope.run)
    scan = AuditScan.model_validate(envelope.audit)
    run_event = RunEvent.model_validate(envelope.run_created_event)
    audit_event = RunEvent.model_validate(envelope.audit_created_event)
    client_request = AuditClientRequest.model_validate(envelope.client_request)
    validated = AuditDraftCreationEnvelope(
        engagement=Engagement.model_validate(envelope.engagement),
        project=AuditProject.model_validate(envelope.project),
        run=run,
        run_created_event=run_event,
        audit=scan,
        contract=AuditContractRecord.model_validate(envelope.contract),
        audit_created_event=audit_event,
        client_request=client_request,
    )
    expected_audit_payload = {
        "audit_id": scan.id,
        "project_id": project.id,
        "lifecycle_status": scan.lifecycle_status.value,
        "mode": scan.mode.value,
        "analysis_profile": scan.analysis_profile.value,
        "contract_digest": scan.contract_digest,
    }
    frozen_source_path = validated.contract.contract().source_target.repository_path
    workspace_root = PurePosixPath(identity.workspace_root)
    source_path = PurePosixPath(frozen_source_path)
    workspace_overlaps_source = frozen_source_path.startswith("/") and (
        workspace_root == source_path
        or workspace_root.is_relative_to(source_path)
        or source_path.is_relative_to(workspace_root)
    )
    try:
        expected_run_status = AuditRunStateMappingPolicy.expected_run_status(scan)
    except ValueError:
        expected_run_status = None
    if (
        validated.engagement != engagement
        or validated.project != project
        or run.kind is not RunKind.CODE_AUDIT
        or run.status is not RunStatus.CREATED
        or expected_run_status is not RunStatus.CREATED
        or run.workspace_path != str(workspace_root / scan.id)
        or frozen_source_path != identity.source_repository_path
        or workspace_overlaps_source
        or run.started_at is not None
        or run.finished_at is not None
        or run.temporal_workflow_id != scan.temporal_workflow_id
        or run_event.payload != {"status": RunStatus.CREATED.value}
        or audit_event.payload != expected_audit_payload
        or not hmac.compare_digest(client_request.request_digest, identity.request_digest)
        or client_request.client_request_id != identity.client_request_id
    ):
        _opaque_conflict("Audit draft creation envelope is invalid")
    return validated


async def _insert_run_and_events(
    session: AsyncSession,
    envelope: AuditDraftCreationEnvelope,
    *,
    failpoint: AuditCreationFailpoint | None,
) -> None:
    if await session.get(RunRecord, envelope.run.id) is not None:
        raise _RetryAuditCreation from None
    session.add(run_to_record(envelope.run))
    await session.flush()
    _hit(failpoint, "after_run")

    session.add(event_to_record(envelope.run_created_event))
    await session.flush()
    _hit(failpoint, "after_run_event")

    await create_scan_contract_pair(
        session,
        envelope.audit,
        envelope.contract,
        flush_failpoint=failpoint,
    )
    _hit(failpoint, "after_contract_scan")

    session.add(event_to_record(envelope.audit_created_event))
    await session.flush()
    _hit(failpoint, "after_audit_event")

    session.add(audit_client_request_to_record(envelope.client_request))
    await session.flush()
    _hit(failpoint, "after_client_request")


class SQLAlchemyAuditCreationUnitOfWork:
    """Create every durable draft fact in one serialized database transaction."""

    def __init__(
        self,
        session_factory: SessionFactory,
        *,
        creation_failpoint: AuditCreationFailpoint | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._creation_failpoint = creation_failpoint

    async def create_draft(
        self,
        factory: AuditDraftAggregateFactory,
    ) -> tuple[AuditAggregate, bool]:
        identity = _validate_factory_identity(factory)
        for attempt in range(_MAX_CREATE_ATTEMPTS):
            database_failure = False
            try:
                async with serialized_write(self._session_factory) as session:
                    existing = await _read_client_request(
                        session,
                        identity,
                        for_update=True,
                    )
                    if existing is not None:
                        _require_authorized_engagement(existing.engagement, identity)
                        return existing, False
                    project, engagement = await _resolve_project(
                        session,
                        factory,
                        identity,
                        failpoint=self._creation_failpoint,
                    )
                    try:
                        envelope = _validate_creation_envelope(
                            factory.build(project, engagement),
                            project=project,
                            engagement=engagement,
                            identity=identity,
                        )
                    except RepositoryConflictError:
                        raise
                    except (AttributeError, TypeError, ValueError, OverflowError):
                        _opaque_conflict("Audit draft creation envelope is invalid")
                    await _insert_run_and_events(
                        session,
                        envelope,
                        failpoint=self._creation_failpoint,
                    )
                    aggregate = await _read_aggregate(
                        session,
                        envelope.audit.id,
                        for_update=True,
                    )
                    if aggregate is None:
                        raise RepositoryIntegrityError(
                            "AuditScan",
                            envelope.audit.id,
                            reason_code="aggregate_create_missing",
                        )
                    return aggregate, True
            except (IntegrityError, _RetryAuditCreation):
                # Leave the driver handler before recovery.  SQL parameters can
                # contain the canonical Contract and its sensitive source path.
                pass
            except SQLAlchemyError:
                database_failure = True

            if database_failure:
                _database_unavailable()

            recovery_failure = False
            try:
                async with consistent_read(self._session_factory) as recovery_session:
                    existing = await _read_client_request(
                        recovery_session,
                        identity,
                        for_update=False,
                    )
                    if existing is not None:
                        _require_authorized_engagement(existing.engagement, identity)
                        return existing, False
            except SQLAlchemyError:
                recovery_failure = True
            if recovery_failure:
                _database_unavailable()
            if attempt + 1 == _MAX_CREATE_ATTEMPTS:
                _opaque_conflict("Audit draft could not be created after a concurrent conflict")

        raise AssertionError("unreachable Audit creation retry state")


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


__all__ = [
    "AuditCreationFailpoint",
    "SQLAlchemyAuditAggregateReadRepository",
    "SQLAlchemyAuditCreationUnitOfWork",
]
