"""SQLAlchemy repositories for RiftX-owned Code Audit ledgers.

The database is treated as an untrusted persistence boundary.  Every returned
row is rebuilt through the strict Audit mappers, and every mutation uses a
monotonic ``state_version`` compare-and-set predicate.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime
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
    AuditPhase,
    AuditPhaseRun,
    AuditPhaseRunStatus,
    AuditProject,
    AuditPublicationStatus,
    AuditRiskTier,
    AuditScan,
    AuditScopeKind,
    AuditScopeStatus,
    AuditScopeUnit,
    AuditStartIntent,
    AuditStartIntentStatus,
    AuditWorkItem,
    AuditWorkStatus,
    RunKind,
    RunStatus,
    SourceSnapshot,
)

from .audit_mappers import (
    audit_contract_from_record,
    audit_contract_to_record,
    audit_phase_run_from_record,
    audit_phase_run_to_record,
    audit_project_from_record,
    audit_project_to_record,
    audit_scan_from_record,
    audit_scan_to_record,
    audit_scope_unit_from_record,
    audit_scope_unit_to_record,
    audit_start_intent_from_record,
    audit_start_intent_to_record,
    audit_work_item_from_record,
    audit_work_item_to_record,
    source_snapshot_from_record,
    source_snapshot_to_record,
)
from .orm import (
    ArtifactRecord,
    AuditPhaseRunRecord,
    AuditProjectRecord,
    AuditScanRecord,
    AuditScopeUnitRecord,
    AuditStartIntentRecord,
    AuditWorkItemRecord,
    Base,
    EngagementRecord,
    RunRecord,
    SourceSnapshotRecord,
)
from .orm import (
    AuditContractRecord as AuditContractORMRecord,
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
    return all(getattr(left, field) == getattr(right, field) for field in fields)


_PROJECT_CREATION_FIELDS = (
    "engagement_id",
    "vcs_kind",
    "repository_identity_digest",
)
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
_CONTRACT_CREATION_FIELDS = (
    "audit_id",
    "schema_version",
    "canonical_contract_json",
    "contract_digest",
    "source_target_digest",
    "source_node_id",
    "source_ingest_backend_digest",
    "source_prepare_proof_digest",
    "selected_node_id",
    "required_backend_id",
    "snapshot_hydration_policy_digest",
)
_SCAN_CREATION_FIELDS = (
    "run_id",
    "project_id",
    "baseline_audit_id",
    "purpose",
    "parent_audit_id",
    "mode",
    "analysis_profile",
    "model_profile",
    "selected_node_id",
    "required_backend_id",
    "policy_digest",
    "budget_digest",
    "config_digest",
    "contract_digest",
    "temporal_workflow_id",
)
_INTENT_CREATION_FIELDS = (
    "audit_id",
    "run_id",
    "start_request_id",
    "contract_digest",
    "workflow_id",
    "task_queue",
)
_PHASE_CREATION_FIELDS = (
    "audit_id",
    "phase",
    "attempt",
    "idempotency_key",
    "input_digest",
    "config_digest",
)
_SCOPE_CREATION_FIELDS = (
    "audit_id",
    "snapshot_id",
    "kind",
    "relative_path",
    "blob_digest",
    "symbol_anchor",
    "required_analyses",
    "stable_key",
)
_WORK_CREATION_FIELDS = (
    "audit_id",
    "phase",
    "epoch",
    "primary_scope_unit_id",
    "strategy",
    "stable_key",
    "risk_tier",
    "input_digest",
    "required_coverage_plan_artifact_id",
    "required_coverage_plan_digest",
)

_RISK_RANK = {
    AuditRiskTier.LOW: 0,
    AuditRiskTier.MEDIUM: 1,
    AuditRiskTier.HIGH: 2,
    AuditRiskTier.CRITICAL: 3,
}


def _same_scan_creation(existing: AuditScan, requested: AuditScan) -> bool:
    if not _same_fields(existing, requested, _SCAN_CREATION_FIELDS):
        return False
    return (requested.snapshot_id is None or existing.snapshot_id == requested.snapshot_id) and (
        requested.base_snapshot_id is None
        or existing.base_snapshot_id == requested.base_snapshot_id
    )


def _same_snapshot_creation(
    existing: SourceSnapshot,
    requested: SourceSnapshot,
) -> bool:
    return _same_fields(existing, requested, _SNAPSHOT_CREATION_FIELDS)


def _same_scope_creation(existing: AuditScopeUnit, requested: AuditScopeUnit) -> bool:
    return (
        _same_fields(existing, requested, _SCOPE_CREATION_FIELDS)
        and _RISK_RANK[existing.risk_tier] >= _RISK_RANK[requested.risk_tier]
    )


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


def _stored_project(record: AuditProjectRecord) -> StoredAuditEntity[AuditProject]:
    return StoredAuditEntity(audit_project_from_record(record), record.state_version)


def _stored_contract(
    record: AuditContractORMRecord,
) -> StoredAuditEntity[AuditContractRecord]:
    return StoredAuditEntity(audit_contract_from_record(record), record.state_version)


def _stored_intent(
    record: AuditStartIntentRecord,
) -> StoredAuditEntity[AuditStartIntent]:
    return StoredAuditEntity(audit_start_intent_from_record(record), record.state_version)


def _stored_work(record: AuditWorkItemRecord) -> StoredAuditEntity[AuditWorkItem]:
    return StoredAuditEntity(audit_work_item_from_record(record), record.state_version)


def _validate_contract_binding(scan: AuditScan, contract: AuditContractRecord) -> None:
    try:
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


async def _orphan_contract_id(session: AsyncSession) -> str | None:
    return await session.scalar(
        select(AuditContractORMRecord.contract_id)
        .outerjoin(
            AuditScanRecord,
            and_(
                AuditScanRecord.id == AuditContractORMRecord.audit_id,
                AuditScanRecord.contract_id == AuditContractORMRecord.contract_id,
                AuditScanRecord.contract_digest == AuditContractORMRecord.contract_digest,
            ),
        )
        .where(AuditScanRecord.id.is_(None))
        .limit(1)
    )


async def _require_no_orphan_contracts(session: AsyncSession) -> None:
    orphan_id = await _orphan_contract_id(session)
    if orphan_id is not None:
        raise RepositoryIntegrityError(
            "AuditContractRecord",
            orphan_id,
            reason_code="orphan_contract",
        )


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


async def _validated_scope(
    session: AsyncSession,
    audit_id: str,
    scope_unit_id: str,
    *,
    expected_state_version: int | None = None,
    for_update: bool = False,
) -> tuple[AuditScopeUnitRecord, AuditScopeUnit] | None:
    statement = select(AuditScopeUnitRecord).where(
        AuditScopeUnitRecord.id == scope_unit_id,
        AuditScopeUnitRecord.audit_id == audit_id,
    )
    if expected_state_version is not None:
        statement = statement.where(AuditScopeUnitRecord.state_version == expected_state_version)
    if for_update:
        statement = statement.with_for_update()
    record = await session.scalar(statement)
    if record is None:
        return None
    scan_bundle = await _validated_scan(session, audit_id)
    if scan_bundle is None:
        raise RepositoryIntegrityError(
            "AuditScopeUnit",
            scope_unit_id,
            reason_code="owner_binding_mismatch",
        )
    scan = scan_bundle[-1]
    scope = audit_scope_unit_from_record(record, project_id=scan.project_id)
    if scope.snapshot_id not in {scan.snapshot_id, scan.base_snapshot_id}:
        raise RepositoryIntegrityError(
            "AuditScopeUnit",
            scope.id,
            reason_code="snapshot_binding_mismatch",
        )
    return record, scope


async def _phase_outputs_belong_to_run(
    session: AsyncSession,
    phase_run: AuditPhaseRun,
    *,
    run_id: str,
    for_update: bool = False,
) -> bool:
    if not phase_run.output_artifact_ids:
        return True
    statement = select(ArtifactRecord).where(ArtifactRecord.id.in_(phase_run.output_artifact_ids))
    if for_update:
        statement = statement.with_for_update()
    records = (await session.scalars(statement)).all()
    return len(records) == len(phase_run.output_artifact_ids) and all(
        record.run_id == run_id for record in records
    )


async def _validated_phase(
    session: AsyncSession,
    audit_id: str,
    phase_run_id: str,
    *,
    expected_state_version: int | None = None,
    for_update: bool = False,
) -> tuple[AuditPhaseRunRecord, AuditPhaseRun, AuditScan] | None:
    statement = select(AuditPhaseRunRecord).where(
        AuditPhaseRunRecord.id == phase_run_id,
        AuditPhaseRunRecord.audit_id == audit_id,
    )
    if expected_state_version is not None:
        statement = statement.where(AuditPhaseRunRecord.state_version == expected_state_version)
    if for_update:
        statement = statement.with_for_update()
    record = await session.scalar(statement)
    if record is None:
        return None
    phase_run = audit_phase_run_from_record(record)
    scan_bundle = await _validated_scan(session, audit_id)
    if scan_bundle is None:
        raise RepositoryIntegrityError(
            "AuditPhaseRun",
            phase_run.id,
            reason_code="owner_binding_mismatch",
        )
    scan = scan_bundle[-1]
    if not await _phase_outputs_belong_to_run(
        session,
        phase_run,
        run_id=scan.run_id,
    ):
        raise RepositoryIntegrityError(
            "AuditPhaseRun",
            phase_run.id,
            reason_code="output_artifact_binding_mismatch",
        )
    return record, phase_run, scan


async def _reject_ambiguous_contract_replay(
    session: AsyncSession,
    *,
    requested_contract_id: str,
    persisted_contract: AuditContractORMRecord,
    audit_id: str,
) -> None:
    requested_id_owner = await session.scalar(
        select(AuditContractORMRecord).where(
            AuditContractORMRecord.contract_id == requested_contract_id
        )
    )
    if requested_id_owner is not None and (
        requested_id_owner.contract_id != persisted_contract.contract_id
        or requested_id_owner.audit_id != persisted_contract.audit_id
    ):
        _conflict(
            "AuditScan",
            audit_id,
            "has an ambiguous Contract identity collision",
        )


async def create_scan_contract_pair(
    session: AsyncSession,
    scan: AuditScan,
    contract: AuditContractRecord,
    *,
    flush_failpoint: Callable[[str], None] | None = None,
) -> tuple[StoredAuditEntity[AuditScan], bool]:
    """Create Contract then Scan in one caller-owned transaction.

    The function flushes but never commits.  AUD-103 can therefore compose it
    with Project, Run, RunEvent, request-id, and preflight-reservation writes.
    """

    scan = AuditScan.model_validate(scan)
    contract = AuditContractRecord.model_validate(contract)
    _validate_contract_binding(scan, contract)
    _reject_distribution_facts(scan)
    if scan.lifecycle_status is not AuditLifecycleStatus.DRAFT:
        _conflict("AuditScan", scan.id, "must be created in draft lifecycle")
    if contract.sealed_at is not None:
        _conflict("AuditContractRecord", contract.contract_id, "must be created unsealed")

    existing = await _validated_scan(session, scan.id, for_update=True)
    if existing is not None:
        existing_record, existing_contract_record, _, _, existing_scan = existing
        existing_contract = audit_contract_from_record(existing_contract_record)
        await _reject_ambiguous_contract_replay(
            session,
            requested_contract_id=contract.contract_id,
            persisted_contract=existing_contract_record,
            audit_id=scan.id,
        )
        if _same_scan_creation(existing_scan, scan) and _same_fields(
            existing_contract,
            contract,
            _CONTRACT_CREATION_FIELDS,
        ):
            return StoredAuditEntity(existing_scan, existing_record.state_version), False
        _conflict("AuditScan", scan.id, "already exists with different content")

    orphan = await _single_create_candidate(
        session,
        select(AuditContractORMRecord).where(
            or_(
                AuditContractORMRecord.audit_id == scan.id,
                AuditContractORMRecord.contract_id == contract.contract_id,
            )
        ),
        entity="AuditContractRecord",
        entity_id=contract.contract_id,
    )
    if orphan is not None:
        raise RepositoryIntegrityError(
            "AuditContractRecord",
            contract.contract_id,
            reason_code="orphan_contract",
        )

    project_record, _ = await _validated_project(session, scan.project_id)
    run_record = await session.scalar(select(RunRecord).where(RunRecord.id == scan.run_id))
    if (
        run_record is None
        or run_record.kind != RunKind.CODE_AUDIT.value
        or run_record.engagement_id != project_record.engagement_id
        or run_record.node_id != scan.selected_node_id
        or run_record.model_profile != scan.model_profile
        or run_record.status != RunStatus.CREATED.value
        or run_record.finished_at is not None
        or run_record.temporal_workflow_id != scan.temporal_workflow_id
    ):
        _conflict("AuditScan", scan.id, "is outside the Run/Project authorization domain")

    for snapshot_id in (scan.snapshot_id, scan.base_snapshot_id):
        if snapshot_id is None:
            continue
        snapshot = await _validated_snapshot(
            session,
            snapshot_id,
            project_id=scan.project_id,
        )
        if snapshot is None:
            _conflict("AuditScan", scan.id, "references an invalid Project snapshot")
    for related_id in (scan.baseline_audit_id, scan.parent_audit_id):
        if related_id is None:
            continue
        if not await _related_audit_belongs_to_project(
            session,
            related_id,
            scan.project_id,
        ):
            _conflict("AuditScan", scan.id, "references an invalid related Audit")

    session.add(audit_contract_to_record(contract, state_version=1))
    await session.flush()
    if flush_failpoint is not None:
        flush_failpoint("after_contract")
    session.add(
        audit_scan_to_record(
            scan,
            engagement_id=project_record.engagement_id,
            state_version=1,
        )
    )
    await session.flush()
    if flush_failpoint is not None:
        flush_failpoint("after_scan")
    return StoredAuditEntity(scan, 1), True


async def _find_audit_project_create_candidate(
    session: AsyncSession,
    project: AuditProject,
) -> AuditProjectRecord | None:
    return await _single_create_candidate(
        session,
        select(AuditProjectRecord).where(
            or_(
                AuditProjectRecord.id == project.id,
                AuditProjectRecord.repository_identity_digest == project.repository_identity_digest,
            )
        ),
        entity="AuditProject",
        entity_id=project.id,
    )


async def create_audit_project(
    session: AsyncSession,
    project: AuditProject,
) -> tuple[StoredAuditEntity[AuditProject], bool]:
    """Create/reuse a Project inside a caller-owned transaction."""

    project = AuditProject.model_validate(project)
    existing = await _find_audit_project_create_candidate(session, project)
    if existing is not None:
        _, validated = await _validated_project(session, existing.id)
        stored = StoredAuditEntity(validated, existing.state_version)
        if _same_fields(stored.value, project, _PROJECT_CREATION_FIELDS):
            return stored, False
        _conflict("AuditProject", project.id, "identity already exists")
    engagement_id = await session.scalar(
        select(EngagementRecord.id).where(EngagementRecord.id == project.engagement_id)
    )
    if engagement_id is None:
        _conflict("AuditProject", project.id, "is outside the Engagement authorization domain")
    session.add(audit_project_to_record(project))
    await session.flush()
    return StoredAuditEntity(project, 1), True


class SQLAlchemyAuditProjectRepository:
    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    async def create(
        self,
        project: AuditProject,
    ) -> tuple[StoredAuditEntity[AuditProject], bool]:
        project = AuditProject.model_validate(project)
        try:
            async with serialized_write(self._session_factory) as session:
                return await create_audit_project(session, project)
        except IntegrityError:
            # Recover after leaving the driver exception handler so SQL text and
            # bound parameters cannot remain attached to the public exception.
            pass
        async with self._session_factory() as session:
            existing = await _find_audit_project_create_candidate(session, project)
            if existing is not None:
                _, validated = await _validated_project(session, existing.id)
                stored = StoredAuditEntity(validated, existing.state_version)
                if _same_fields(stored.value, project, _PROJECT_CREATION_FIELDS):
                    return stored, False
        _conflict("AuditProject", project.id, "could not be created")

    async def get(self, project_id: str) -> StoredAuditEntity[AuditProject] | None:
        async with self._session_factory() as session:
            record = await session.scalar(
                select(AuditProjectRecord).where(AuditProjectRecord.id == project_id)
            )
            if record is None:
                return None
            _, project = await _validated_project(session, project_id)
            return StoredAuditEntity(project, record.state_version)

    async def get_by_identity(
        self,
        repository_identity_digest: str,
        *,
        engagement_id: str,
    ) -> StoredAuditEntity[AuditProject] | None:
        async with self._session_factory() as session:
            record = await session.scalar(
                select(AuditProjectRecord).where(
                    AuditProjectRecord.repository_identity_digest == repository_identity_digest,
                    AuditProjectRecord.engagement_id == engagement_id,
                )
            )
            if record is None:
                return None
            _, project = await _validated_project(session, record.id)
            return StoredAuditEntity(project, record.state_version)

    async def list(
        self,
        *,
        engagement_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> Sequence[StoredAuditEntity[AuditProject]]:
        _validate_page(limit=limit, offset=offset)
        statement = select(AuditProjectRecord)
        if engagement_id is not None:
            statement = statement.where(AuditProjectRecord.engagement_id == engagement_id)
        statement = (
            statement.order_by(
                AuditProjectRecord.created_at,
                AuditProjectRecord.id,
            )
            .limit(limit)
            .offset(offset)
        )
        async with self._session_factory() as session:
            records = (await session.scalars(statement)).all()
            results: list[StoredAuditEntity[AuditProject]] = []
            for record in records:
                _, project = await _validated_project(session, record.id)
                results.append(StoredAuditEntity(project, record.state_version))
        return results

    async def compare_and_set(
        self,
        current: StoredAuditEntity[AuditProject],
        replacement: AuditProject,
    ) -> tuple[StoredAuditEntity[AuditProject], bool]:
        replacement = AuditProject.model_validate(replacement)
        async with serialized_write(self._session_factory) as session:
            record = await session.scalar(
                select(AuditProjectRecord)
                .where(
                    AuditProjectRecord.id == current.value.id,
                    AuditProjectRecord.state_version == current.state_version,
                )
                .with_for_update()
            )
            if record is None:
                latest = await session.scalar(
                    select(AuditProjectRecord)
                    .where(AuditProjectRecord.id == current.value.id)
                    .with_for_update()
                )
                if latest is None:
                    raise EntityNotFoundError("AuditProject", current.value.id)
                _, latest_project = await _validated_project(
                    session,
                    latest.id,
                    for_update=True,
                )
                stored = StoredAuditEntity(latest_project, latest.state_version)
                if stored.value == replacement:
                    return stored, False
                _conflict("AuditProject", current.value.id)
            _, persisted_project = await _validated_project(
                session,
                record.id,
                for_update=True,
            )
            persisted = StoredAuditEntity(persisted_project, record.state_version)
            if persisted != current:
                _conflict("AuditProject", current.value.id, "CAS token/value mismatch")
            immutable = (
                persisted.value.id,
                persisted.value.engagement_id,
                persisted.value.vcs_kind,
                persisted.value.repository_identity_digest,
                persisted.value.created_at,
            )
            replacement_immutable = (
                replacement.id,
                replacement.engagement_id,
                replacement.vcs_kind,
                replacement.repository_identity_digest,
                replacement.created_at,
            )
            if (
                immutable != replacement_immutable
                or replacement.updated_at < persisted.value.updated_at
            ):
                _conflict("AuditProject", current.value.id, "immutable state changed")
            if persisted.value == replacement:
                return persisted, False
            candidate = audit_project_to_record(
                replacement,
                state_version=current.state_version + 1,
            )
            result = await session.execute(
                update(AuditProjectRecord)
                .where(
                    AuditProjectRecord.id == current.value.id,
                    AuditProjectRecord.state_version == current.state_version,
                )
                .values(**_record_values(candidate, excluding=frozenset({"id"})))
            )
            if result.rowcount != 1:  # type: ignore[attr-defined]
                _conflict("AuditProject", current.value.id)
        return StoredAuditEntity(replacement, current.state_version + 1), True


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
                await _validated_project(session, snapshot.project_id)
                if snapshot.parent_snapshot_id is not None:
                    parent = await _validated_snapshot(
                        session,
                        snapshot.parent_snapshot_id,
                        project_id=snapshot.project_id,
                    )
                    if parent is None:
                        _conflict("SourceSnapshot", snapshot.id, "has an invalid parent")
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
                    _conflict("SourceSnapshot", snapshot.id, "identity already exists")
                session.add(source_snapshot_to_record(snapshot))
                await session.flush()
        except IntegrityError:
            pass
        else:
            return snapshot, True
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


class SQLAlchemyAuditRepository:
    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    async def create(
        self,
        scan: AuditScan,
        contract: AuditContractRecord,
    ) -> tuple[StoredAuditEntity[AuditScan], bool]:
        scan = AuditScan.model_validate(scan)
        contract = AuditContractRecord.model_validate(contract)
        try:
            async with serialized_write(self._session_factory) as session:
                return await create_scan_contract_pair(session, scan, contract)
        except IntegrityError:
            pass
        async with self._session_factory() as session:
            bundle = await _validated_scan(
                session,
                scan.id,
                project_id=scan.project_id,
            )
            if bundle is not None:
                record, contract_record, _, _, persisted_scan = bundle
                await _reject_ambiguous_contract_replay(
                    session,
                    requested_contract_id=contract.contract_id,
                    persisted_contract=contract_record,
                    audit_id=scan.id,
                )
                persisted_contract = audit_contract_from_record(contract_record)
                if _same_scan_creation(persisted_scan, scan) and _same_fields(
                    persisted_contract,
                    contract,
                    _CONTRACT_CREATION_FIELDS,
                ):
                    return StoredAuditEntity(
                        persisted_scan,
                        record.state_version,
                    ), False
        _conflict("AuditScan", scan.id, "could not be created")

    async def get(
        self,
        audit_id: str,
        *,
        project_id: str | None = None,
    ) -> StoredAuditEntity[AuditScan] | None:
        async with self._session_factory() as session:
            bundle = await _validated_scan(
                session,
                audit_id,
                project_id=project_id,
            )
        if bundle is None:
            return None
        return StoredAuditEntity(bundle[-1], bundle[0].state_version)

    async def get_contract(
        self,
        audit_id: str,
        *,
        project_id: str | None = None,
    ) -> AuditContractRecord | None:
        async with self._session_factory() as session:
            bundle = await _validated_scan(
                session,
                audit_id,
                project_id=project_id,
            )
        return audit_contract_from_record(bundle[1]) if bundle is not None else None

    async def list(
        self,
        *,
        project_id: str | None = None,
        lifecycle_status: AuditLifecycleStatus | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> Sequence[StoredAuditEntity[AuditScan]]:
        _validate_page(limit=limit, offset=offset)
        statement = select(AuditScanRecord)
        if project_id is not None:
            statement = statement.where(AuditScanRecord.project_id == project_id)
        if lifecycle_status is not None:
            statement = statement.where(AuditScanRecord.lifecycle_status == lifecycle_status.value)
        statement = (
            statement.order_by(
                AuditScanRecord.created_at,
                AuditScanRecord.id,
            )
            .limit(limit)
            .offset(offset)
        )
        async with self._session_factory() as session:
            if project_id is None:
                # Recovery/list operations without an authorization scope must
                # never silently skip a Contract whose Scan half is missing.
                # Project-scoped reads intentionally do not expose unrelated
                # orphan existence because an orphan Contract has no trusted
                # Project binding of its own.
                await _require_no_orphan_contracts(session)
            records = (await session.scalars(statement)).all()
            results: list[StoredAuditEntity[AuditScan]] = []
            for record in records:
                bundle = await _validated_scan(
                    session,
                    record.id,
                    project_id=project_id,
                )
                if bundle is None:
                    raise RepositoryIntegrityError("AuditScan", record.id)
                results.append(StoredAuditEntity(bundle[-1], bundle[0].state_version))
        return results

    async def compare_and_set(
        self,
        current: StoredAuditEntity[AuditScan],
        replacement: AuditScan,
    ) -> tuple[StoredAuditEntity[AuditScan], bool]:
        async with serialized_write(self._session_factory) as session:
            return await compare_and_set_audit_scan(
                session,
                current,
                replacement,
            )


async def compare_and_set_audit_contract(
    session: AsyncSession,
    current: StoredAuditEntity[AuditContractRecord],
    replacement: AuditContractRecord,
) -> tuple[StoredAuditEntity[AuditContractRecord], bool]:
    """CAS-seal a Contract inside a caller-owned transaction."""

    replacement = AuditContractRecord.model_validate(replacement)
    record = await session.scalar(
        select(AuditContractORMRecord)
        .where(
            AuditContractORMRecord.contract_id == current.value.contract_id,
            AuditContractORMRecord.state_version == current.state_version,
        )
        .with_for_update()
    )
    if record is None:
        latest = await session.scalar(
            select(AuditContractORMRecord)
            .where(AuditContractORMRecord.contract_id == current.value.contract_id)
            .with_for_update()
        )
        if latest is None:
            raise EntityNotFoundError(
                "AuditContractRecord",
                current.value.contract_id,
            )
        stored = _stored_contract(latest)
        latest_scan = await _validated_scan(session, stored.value.audit_id)
        if latest_scan is None:
            raise RepositoryIntegrityError(
                "AuditContractRecord",
                current.value.contract_id,
                reason_code="orphan_contract",
            )
        _validate_contract_binding(latest_scan[-1], stored.value)
        if stored.value == replacement:
            return stored, False
        _conflict("AuditContractRecord", current.value.contract_id)
    persisted = _stored_contract(record)
    if persisted != current:
        _conflict(
            "AuditContractRecord",
            current.value.contract_id,
            "CAS token/value mismatch",
        )
    scan_bundle = await _validated_scan(session, persisted.value.audit_id)
    if scan_bundle is None:
        raise RepositoryIntegrityError(
            "AuditContractRecord",
            current.value.contract_id,
            reason_code="orphan_contract",
        )
    _validate_contract_binding(scan_bundle[-1], replacement)
    expected = (
        persisted.value
        if replacement == persisted.value
        else persisted.value.seal(at=replacement.sealed_at)
    )
    if expected != replacement:
        _conflict(
            "AuditContractRecord",
            current.value.contract_id,
            "only an immutable seal transition is allowed",
        )
    if replacement == persisted.value:
        return persisted, False
    candidate = audit_contract_to_record(
        replacement,
        state_version=current.state_version + 1,
    )
    result = await session.execute(
        update(AuditContractORMRecord)
        .where(
            AuditContractORMRecord.contract_id == current.value.contract_id,
            AuditContractORMRecord.state_version == current.state_version,
        )
        .values(**_record_values(candidate, excluding=frozenset({"contract_id"})))
    )
    if result.rowcount != 1:  # type: ignore[attr-defined]
        _conflict("AuditContractRecord", current.value.contract_id)
    return StoredAuditEntity(replacement, current.state_version + 1), True


class SQLAlchemyAuditContractRepository:
    """Read and CAS-seal contracts; creation is intentionally unavailable."""

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    async def get(
        self,
        audit_id: str,
        *,
        project_id: str | None = None,
    ) -> StoredAuditEntity[AuditContractRecord] | None:
        async with self._session_factory() as session:
            bundle = await _validated_scan(
                session,
                audit_id,
                project_id=project_id,
            )
        return _stored_contract(bundle[1]) if bundle is not None else None

    async def compare_and_set(
        self,
        current: StoredAuditEntity[AuditContractRecord],
        replacement: AuditContractRecord,
    ) -> tuple[StoredAuditEntity[AuditContractRecord], bool]:
        async with serialized_write(self._session_factory) as session:
            return await compare_and_set_audit_contract(
                session,
                current,
                replacement,
            )


def _intent_owner_matches(intent: AuditStartIntent, scan: AuditScan) -> bool:
    return (
        intent.audit_id == scan.id
        and intent.run_id == scan.run_id
        and intent.contract_digest == scan.contract_digest
        and intent.workflow_id == scan.temporal_workflow_id
    )


def _validate_intent_owner(intent: AuditStartIntent, scan: AuditScan) -> None:
    if not _intent_owner_matches(intent, scan):
        _conflict("AuditStartIntent", intent.id, "does not match its Audit binding")


def _without_fields(value: object, *fields: str) -> dict[str, object]:
    payload = value.model_dump(mode="python")  # type: ignore[attr-defined]
    for field in fields:
        payload.pop(field, None)
    return payload


def _validate_intent_replacement(
    current: AuditStartIntent,
    replacement: AuditStartIntent,
) -> None:
    immutable_fields = (
        "id",
        "audit_id",
        "run_id",
        "start_request_id",
        "contract_digest",
        "workflow_id",
        "task_queue",
        "created_at",
    )
    if any(getattr(current, field) != getattr(replacement, field) for field in immutable_fields):
        _conflict("AuditStartIntent", current.id, "immutable identity changed")
    if current == replacement:
        return
    if current.status is replacement.status:
        if current.status is not AuditStartIntentStatus.CLAIMED:
            _conflict("AuditStartIntent", current.id, "same-state rewrite is not allowed")
        comparable_fields = (
            "lease_owner",
            "lease_expires_at",
            "attempt",
            "updated_at",
        )
        if _without_fields(current, *comparable_fields) != _without_fields(
            replacement,
            *comparable_fields,
        ):
            _conflict("AuditStartIntent", current.id, "lease rewrite changed durable facts")
        renewal = (
            replacement.lease_owner == current.lease_owner
            and replacement.attempt == current.attempt
            and current.lease_expires_at is not None
            and replacement.lease_expires_at is not None
            and replacement.lease_expires_at > current.lease_expires_at
            and replacement.updated_at >= current.updated_at
        )
        reclaim = (
            current.lease_expires_at is not None
            and current.lease_expires_at <= replacement.updated_at
            and replacement.attempt == current.attempt + 1
            and replacement.updated_at >= current.updated_at
        )
        if not (renewal or reclaim):
            _conflict("AuditStartIntent", current.id, "invalid lease renewal or reclaim")
        return
    try:
        expected = current.transition_to(
            replacement.status,
            at=replacement.updated_at,
            lease_owner=replacement.lease_owner,
            lease_expires_at=replacement.lease_expires_at,
            next_attempt_at=replacement.next_attempt_at,
            last_error_code=replacement.last_error_code,
        )
    except (TypeError, ValueError):
        _conflict("AuditStartIntent", current.id, "contains a disallowed transition")
    if expected != replacement:
        _conflict("AuditStartIntent", current.id, "transition payload is not canonical")


async def _find_start_intent_create_candidate(
    session: AsyncSession,
    intent: AuditStartIntent,
) -> AuditStartIntentRecord | None:
    return await _single_create_candidate(
        session,
        select(AuditStartIntentRecord).where(
            or_(
                AuditStartIntentRecord.intent_id == intent.id,
                AuditStartIntentRecord.audit_id == intent.audit_id,
                AuditStartIntentRecord.start_request_id == intent.start_request_id,
                AuditStartIntentRecord.workflow_id == intent.workflow_id,
            )
        ),
        entity="AuditStartIntent",
        entity_id=intent.id,
    )


async def create_audit_start_intent(
    session: AsyncSession,
    intent: AuditStartIntent,
) -> tuple[StoredAuditEntity[AuditStartIntent], bool]:
    """Create a pending start outbox fact inside a caller-owned transaction."""

    intent = AuditStartIntent.model_validate(intent)
    if intent.status is not AuditStartIntentStatus.PENDING:
        _conflict("AuditStartIntent", intent.id, "must be created pending")
    scan_bundle = await _validated_scan(session, intent.audit_id)
    if scan_bundle is None:
        raise EntityNotFoundError("AuditScan", intent.audit_id)
    _validate_intent_owner(intent, scan_bundle[-1])
    existing = await _find_start_intent_create_candidate(session, intent)
    if existing is not None:
        stored = _stored_intent(existing)
        stored_scan_bundle = await _validated_scan(session, stored.value.audit_id)
        if stored_scan_bundle is None or not _intent_owner_matches(
            stored.value,
            stored_scan_bundle[-1],
        ):
            raise RepositoryIntegrityError(
                "AuditStartIntent",
                stored.value.id,
                reason_code="owner_binding_mismatch",
            )
        if _same_fields(stored.value, intent, _INTENT_CREATION_FIELDS):
            return stored, False
        _conflict("AuditStartIntent", intent.id, "identity already exists")
    if scan_bundle[-1].lifecycle_status is not AuditLifecycleStatus.QUEUED:
        _conflict(
            "AuditStartIntent",
            intent.id,
            "requires an Audit in queued lifecycle",
        )
    session.add(audit_start_intent_to_record(intent))
    await session.flush()
    return StoredAuditEntity(intent, 1), True


class SQLAlchemyAuditStartIntentRepository:
    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    async def create(
        self,
        intent: AuditStartIntent,
    ) -> tuple[StoredAuditEntity[AuditStartIntent], bool]:
        intent = AuditStartIntent.model_validate(intent)
        try:
            async with serialized_write(self._session_factory) as session:
                return await create_audit_start_intent(session, intent)
        except IntegrityError:
            pass
        async with self._session_factory() as session:
            existing = await _find_start_intent_create_candidate(session, intent)
            if existing is not None:
                stored = _stored_intent(existing)
                scan_bundle = await _validated_scan(session, stored.value.audit_id)
                if scan_bundle is None or not _intent_owner_matches(
                    stored.value,
                    scan_bundle[-1],
                ):
                    raise RepositoryIntegrityError(
                        "AuditStartIntent",
                        stored.value.id,
                        reason_code="owner_binding_mismatch",
                    ) from None
                if _same_fields(stored.value, intent, _INTENT_CREATION_FIELDS):
                    return stored, False
        _conflict("AuditStartIntent", intent.id, "could not be created")

    async def get(
        self,
        audit_id: str,
        intent_id: str,
    ) -> StoredAuditEntity[AuditStartIntent] | None:
        async with self._session_factory() as session:
            record = await session.scalar(
                select(AuditStartIntentRecord).where(
                    AuditStartIntentRecord.intent_id == intent_id,
                    AuditStartIntentRecord.audit_id == audit_id,
                )
            )
            if record is None:
                return None
            scan_bundle = await _validated_scan(session, audit_id)
            if scan_bundle is None:
                raise RepositoryIntegrityError(
                    "AuditStartIntent",
                    intent_id,
                    reason_code="owner_binding_mismatch",
                )
            stored = _stored_intent(record)
            if not _intent_owner_matches(stored.value, scan_bundle[-1]):
                raise RepositoryIntegrityError(
                    "AuditStartIntent",
                    intent_id,
                    reason_code="owner_binding_mismatch",
                )
        return stored

    async def get_for_audit(
        self,
        audit_id: str,
    ) -> StoredAuditEntity[AuditStartIntent] | None:
        async with self._session_factory() as session:
            record = await session.scalar(
                select(AuditStartIntentRecord).where(AuditStartIntentRecord.audit_id == audit_id)
            )
        return await self.get(audit_id, record.intent_id) if record is not None else None

    async def list_ready(
        self,
        *,
        now: datetime,
        limit: int = 100,
    ) -> Sequence[StoredAuditEntity[AuditStartIntent]]:
        _validate_page(limit=limit)
        statement = (
            select(AuditStartIntentRecord)
            .where(
                or_(
                    AuditStartIntentRecord.status == AuditStartIntentStatus.PENDING.value,
                    and_(
                        AuditStartIntentRecord.status == AuditStartIntentStatus.RETRYABLE.value,
                        AuditStartIntentRecord.next_attempt_at <= now,
                    ),
                    and_(
                        AuditStartIntentRecord.status == AuditStartIntentStatus.CLAIMED.value,
                        AuditStartIntentRecord.lease_expires_at <= now,
                    ),
                )
            )
            .order_by(
                AuditStartIntentRecord.created_at,
                AuditStartIntentRecord.intent_id,
            )
            .limit(limit)
        )
        async with self._session_factory() as session:
            records = (await session.scalars(statement)).all()
            results: list[StoredAuditEntity[AuditStartIntent]] = []
            for record in records:
                scan_bundle = await _validated_scan(session, record.audit_id)
                if scan_bundle is None:
                    raise RepositoryIntegrityError(
                        "AuditStartIntent",
                        record.intent_id,
                        reason_code="owner_binding_mismatch",
                    )
                stored = _stored_intent(record)
                if not _intent_owner_matches(stored.value, scan_bundle[-1]):
                    raise RepositoryIntegrityError(
                        "AuditStartIntent",
                        record.intent_id,
                        reason_code="owner_binding_mismatch",
                    )
                results.append(stored)
        return results

    async def compare_and_set(
        self,
        current: StoredAuditEntity[AuditStartIntent],
        replacement: AuditStartIntent,
    ) -> tuple[StoredAuditEntity[AuditStartIntent], bool]:
        replacement = AuditStartIntent.model_validate(replacement)
        async with serialized_write(self._session_factory) as session:
            record = await session.scalar(
                select(AuditStartIntentRecord)
                .where(
                    AuditStartIntentRecord.intent_id == current.value.id,
                    AuditStartIntentRecord.state_version == current.state_version,
                )
                .with_for_update()
            )
            if record is None:
                latest = await session.scalar(
                    select(AuditStartIntentRecord)
                    .where(AuditStartIntentRecord.intent_id == current.value.id)
                    .with_for_update()
                )
                if latest is None:
                    raise EntityNotFoundError("AuditStartIntent", current.value.id)
                stored = _stored_intent(latest)
                latest_scan = await _validated_scan(session, stored.value.audit_id)
                if latest_scan is None or not _intent_owner_matches(
                    stored.value,
                    latest_scan[-1],
                ):
                    raise RepositoryIntegrityError(
                        "AuditStartIntent",
                        current.value.id,
                        reason_code="owner_binding_mismatch",
                    )
                if stored.value == replacement:
                    return stored, False
                _conflict("AuditStartIntent", current.value.id)
            persisted = _stored_intent(record)
            if persisted != current:
                _conflict("AuditStartIntent", current.value.id, "CAS token/value mismatch")
            scan_bundle = await _validated_scan(session, persisted.value.audit_id)
            if scan_bundle is None:
                raise RepositoryIntegrityError(
                    "AuditStartIntent",
                    current.value.id,
                    reason_code="owner_binding_mismatch",
                )
            _validate_intent_owner(replacement, scan_bundle[-1])
            _validate_intent_replacement(persisted.value, replacement)
            if persisted.value == replacement:
                return persisted, False
            candidate = audit_start_intent_to_record(
                replacement,
                state_version=current.state_version + 1,
            )
            result = await session.execute(
                update(AuditStartIntentRecord)
                .where(
                    AuditStartIntentRecord.intent_id == current.value.id,
                    AuditStartIntentRecord.state_version == current.state_version,
                )
                .values(**_record_values(candidate, excluding=frozenset({"intent_id"})))
            )
            if result.rowcount != 1:  # type: ignore[attr-defined]
                _conflict("AuditStartIntent", current.value.id)
        return StoredAuditEntity(replacement, current.state_version + 1), True


def _validate_phase_replacement(current: AuditPhaseRun, replacement: AuditPhaseRun) -> None:
    immutable_fields = (
        "id",
        "audit_id",
        "phase",
        "attempt",
        "idempotency_key",
        "input_digest",
        "config_digest",
        "created_at",
    )
    if any(getattr(current, field) != getattr(replacement, field) for field in immutable_fields):
        _conflict("AuditPhaseRun", current.id, "immutable identity changed")
    if current == replacement:
        return
    try:
        expected = current.transition_to(
            replacement.status,
            at=replacement.updated_at,
            output_artifact_ids=replacement.output_artifact_ids,
            summary_counts=replacement.summary_counts,
            error_code=replacement.error_code,
            error_summary=replacement.error_summary,
        )
    except (TypeError, ValueError):
        _conflict("AuditPhaseRun", current.id, "contains a disallowed transition")
    if expected != replacement:
        _conflict("AuditPhaseRun", current.id, "transition payload is not canonical")


class SQLAlchemyAuditPhaseRepository:
    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    async def _find_create_candidate(
        self,
        session: AsyncSession,
        phase_run: AuditPhaseRun,
    ) -> AuditPhaseRunRecord | None:
        return await _single_create_candidate(
            session,
            select(AuditPhaseRunRecord).where(
                or_(
                    AuditPhaseRunRecord.id == phase_run.id,
                    and_(
                        AuditPhaseRunRecord.audit_id == phase_run.audit_id,
                        AuditPhaseRunRecord.phase == phase_run.phase.value,
                        AuditPhaseRunRecord.idempotency_key == phase_run.idempotency_key,
                    ),
                )
            ),
            entity="AuditPhaseRun",
            entity_id=phase_run.id,
        )

    async def create(
        self,
        phase_run: AuditPhaseRun,
    ) -> tuple[StoredAuditEntity[AuditPhaseRun], bool]:
        phase_run = AuditPhaseRun.model_validate(phase_run)
        if phase_run.status is not AuditPhaseRunStatus.QUEUED:
            _conflict("AuditPhaseRun", phase_run.id, "must be created queued")
        try:
            async with serialized_write(self._session_factory) as session:
                if await _validated_scan(session, phase_run.audit_id) is None:
                    raise EntityNotFoundError("AuditScan", phase_run.audit_id)
                existing = await self._find_create_candidate(session, phase_run)
                if existing is not None:
                    existing_bundle = await _validated_phase(
                        session,
                        existing.audit_id,
                        existing.id,
                    )
                    if existing_bundle is None:
                        raise RepositoryIntegrityError(
                            "AuditPhaseRun",
                            existing.id,
                            reason_code="owner_binding_mismatch",
                        ) from None
                    stored = StoredAuditEntity(
                        existing_bundle[1],
                        existing_bundle[0].state_version,
                    )
                    if _same_fields(stored.value, phase_run, _PHASE_CREATION_FIELDS):
                        return stored, False
                    _conflict("AuditPhaseRun", phase_run.id, "identity already exists")
                session.add(audit_phase_run_to_record(phase_run))
                await session.flush()
        except IntegrityError:
            pass
        else:
            return StoredAuditEntity(phase_run, 1), True
        async with self._session_factory() as session:
            existing = await self._find_create_candidate(session, phase_run)
            if existing is not None:
                existing_bundle = await _validated_phase(
                    session,
                    existing.audit_id,
                    existing.id,
                )
                if existing_bundle is None:
                    raise RepositoryIntegrityError(
                        "AuditPhaseRun",
                        existing.id,
                        reason_code="owner_binding_mismatch",
                    ) from None
                stored = StoredAuditEntity(
                    existing_bundle[1],
                    existing_bundle[0].state_version,
                )
                if _same_fields(stored.value, phase_run, _PHASE_CREATION_FIELDS):
                    return stored, False
        _conflict("AuditPhaseRun", phase_run.id, "could not be created")

    async def get(
        self,
        audit_id: str,
        phase_run_id: str,
    ) -> StoredAuditEntity[AuditPhaseRun] | None:
        async with self._session_factory() as session:
            bundle = await _validated_phase(session, audit_id, phase_run_id)
        if bundle is None:
            return None
        return StoredAuditEntity(bundle[1], bundle[0].state_version)

    async def list(
        self,
        audit_id: str,
        *,
        phase: AuditPhase | None = None,
        status: AuditPhaseRunStatus | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> Sequence[StoredAuditEntity[AuditPhaseRun]]:
        _validate_page(limit=limit, offset=offset)
        statement = select(AuditPhaseRunRecord).where(AuditPhaseRunRecord.audit_id == audit_id)
        if phase is not None:
            statement = statement.where(AuditPhaseRunRecord.phase == phase.value)
        if status is not None:
            statement = statement.where(AuditPhaseRunRecord.status == status.value)
        statement = (
            statement.order_by(
                AuditPhaseRunRecord.created_at,
                AuditPhaseRunRecord.id,
            )
            .limit(limit)
            .offset(offset)
        )
        async with self._session_factory() as session:
            records = (await session.scalars(statement)).all()
            results: list[StoredAuditEntity[AuditPhaseRun]] = []
            for record in records:
                bundle = await _validated_phase(session, audit_id, record.id)
                if bundle is None:
                    raise RepositoryIntegrityError("AuditPhaseRun", record.id)
                results.append(StoredAuditEntity(bundle[1], bundle[0].state_version))
        return results

    async def compare_and_set(
        self,
        current: StoredAuditEntity[AuditPhaseRun],
        replacement: AuditPhaseRun,
    ) -> tuple[StoredAuditEntity[AuditPhaseRun], bool]:
        replacement = AuditPhaseRun.model_validate(replacement)
        async with serialized_write(self._session_factory) as session:
            bundle = await _validated_phase(
                session,
                current.value.audit_id,
                current.value.id,
                expected_state_version=current.state_version,
                for_update=True,
            )
            if bundle is None:
                latest = await _validated_phase(
                    session,
                    current.value.audit_id,
                    current.value.id,
                    for_update=True,
                )
                if latest is None:
                    raise EntityNotFoundError("AuditPhaseRun", current.value.id)
                stored = StoredAuditEntity(latest[1], latest[0].state_version)
                if stored.value == replacement:
                    return stored, False
                _conflict("AuditPhaseRun", current.value.id)
            record, persisted_phase, scan = bundle
            persisted = StoredAuditEntity(persisted_phase, record.state_version)
            if persisted != current:
                _conflict("AuditPhaseRun", current.value.id, "CAS token/value mismatch")
            _validate_phase_replacement(persisted.value, replacement)
            if persisted.value == replacement:
                return persisted, False
            if not await _phase_outputs_belong_to_run(
                session,
                replacement,
                run_id=scan.run_id,
                for_update=True,
            ):
                _conflict(
                    "AuditPhaseRun",
                    replacement.id,
                    "contains output Artifacts outside its Run",
                )
            candidate = audit_phase_run_to_record(
                replacement,
                state_version=current.state_version + 1,
            )
            result = await session.execute(
                update(AuditPhaseRunRecord)
                .where(
                    AuditPhaseRunRecord.id == current.value.id,
                    AuditPhaseRunRecord.state_version == current.state_version,
                )
                .values(**_record_values(candidate, excluding=frozenset({"id"})))
            )
            if result.rowcount != 1:  # type: ignore[attr-defined]
                _conflict("AuditPhaseRun", current.value.id)
        return StoredAuditEntity(replacement, current.state_version + 1), True


def _validate_scope_replacement(current: AuditScopeUnit, replacement: AuditScopeUnit) -> None:
    immutable_fields = (
        "id",
        "audit_id",
        "snapshot_id",
        "kind",
        "relative_path",
        "blob_digest",
        "symbol_anchor",
        "required_analyses",
        "stable_key",
        "created_at",
    )
    if any(getattr(current, field) != getattr(replacement, field) for field in immutable_fields):
        _conflict("AuditScopeUnit", current.id, "immutable identity changed")
    if current == replacement:
        return
    if current.status is not replacement.status:
        if current.risk_tier is not replacement.risk_tier:
            _conflict("AuditScopeUnit", current.id, "risk and closure changed together")
        try:
            expected = current.transition_to(
                replacement.status,
                closure_code=replacement.closure_code or "invalid",
                closure_reason=replacement.closure_reason or "invalid",
                receipt_count=replacement.receipt_count,
                at=replacement.updated_at,
            )
        except (TypeError, ValueError):
            _conflict("AuditScopeUnit", current.id, "contains a disallowed transition")
    elif current.risk_tier is not replacement.risk_tier:
        try:
            expected = current.elevate_risk(
                replacement.risk_tier,
                at=replacement.updated_at,
            )
        except (TypeError, ValueError):
            _conflict("AuditScopeUnit", current.id, "contains a risk regression")
    else:
        _conflict("AuditScopeUnit", current.id, "same-state rewrite is not allowed")
    if expected != replacement:
        _conflict("AuditScopeUnit", current.id, "transition payload is not canonical")


class SQLAlchemyAuditScopeRepository:
    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    async def _find_create_candidate(
        self,
        session: AsyncSession,
        scope_unit: AuditScopeUnit,
    ) -> AuditScopeUnitRecord | None:
        return await _single_create_candidate(
            session,
            select(AuditScopeUnitRecord).where(
                or_(
                    AuditScopeUnitRecord.id == scope_unit.id,
                    and_(
                        AuditScopeUnitRecord.audit_id == scope_unit.audit_id,
                        AuditScopeUnitRecord.snapshot_id == scope_unit.snapshot_id,
                        AuditScopeUnitRecord.kind == scope_unit.kind.value,
                        AuditScopeUnitRecord.stable_key == scope_unit.stable_key,
                    ),
                )
            ),
            entity="AuditScopeUnit",
            entity_id=scope_unit.id,
        )

    async def create(
        self,
        scope_unit: AuditScopeUnit,
    ) -> tuple[StoredAuditEntity[AuditScopeUnit], bool]:
        scope_unit = AuditScopeUnit.model_validate(scope_unit)
        try:
            async with serialized_write(self._session_factory) as session:
                scan_bundle = await _validated_scan(session, scope_unit.audit_id)
                if scan_bundle is None:
                    raise EntityNotFoundError("AuditScan", scope_unit.audit_id)
                scan = scan_bundle[-1]
                if scope_unit.snapshot_id not in {scan.snapshot_id, scan.base_snapshot_id}:
                    _conflict(
                        "AuditScopeUnit",
                        scope_unit.id,
                        "references a Snapshot not bound to its Audit",
                    )
                existing = await self._find_create_candidate(session, scope_unit)
                if existing is not None:
                    existing_bundle = await _validated_scope(
                        session,
                        existing.audit_id,
                        existing.id,
                    )
                    if existing_bundle is None:
                        raise RepositoryIntegrityError(
                            "AuditScopeUnit",
                            existing.id,
                            reason_code="owner_binding_mismatch",
                        ) from None
                    stored = StoredAuditEntity(
                        existing_bundle[1],
                        existing_bundle[0].state_version,
                    )
                    if _same_scope_creation(stored.value, scope_unit):
                        return stored, False
                    _conflict("AuditScopeUnit", scope_unit.id, "identity already exists")
                session.add(
                    audit_scope_unit_to_record(
                        scope_unit,
                        project_id=scan.project_id,
                    )
                )
                await session.flush()
        except IntegrityError:
            pass
        else:
            return StoredAuditEntity(scope_unit, 1), True
        async with self._session_factory() as session:
            existing = await self._find_create_candidate(session, scope_unit)
            if existing is not None:
                existing_bundle = await _validated_scope(
                    session,
                    existing.audit_id,
                    existing.id,
                )
                if existing_bundle is None:
                    raise RepositoryIntegrityError(
                        "AuditScopeUnit",
                        existing.id,
                        reason_code="owner_binding_mismatch",
                    ) from None
                stored = StoredAuditEntity(
                    existing_bundle[1],
                    existing_bundle[0].state_version,
                )
                if _same_scope_creation(stored.value, scope_unit):
                    return stored, False
        _conflict("AuditScopeUnit", scope_unit.id, "could not be created")

    async def get(
        self,
        audit_id: str,
        scope_unit_id: str,
    ) -> StoredAuditEntity[AuditScopeUnit] | None:
        async with self._session_factory() as session:
            bundle = await _validated_scope(session, audit_id, scope_unit_id)
        if bundle is None:
            return None
        return StoredAuditEntity(bundle[1], bundle[0].state_version)

    async def list(
        self,
        audit_id: str,
        *,
        kind: AuditScopeKind | None = None,
        status: AuditScopeStatus | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> Sequence[StoredAuditEntity[AuditScopeUnit]]:
        _validate_page(limit=limit, offset=offset)
        statement = select(AuditScopeUnitRecord).where(AuditScopeUnitRecord.audit_id == audit_id)
        if kind is not None:
            statement = statement.where(AuditScopeUnitRecord.kind == kind.value)
        if status is not None:
            statement = statement.where(AuditScopeUnitRecord.status == status.value)
        statement = (
            statement.order_by(
                AuditScopeUnitRecord.created_at,
                AuditScopeUnitRecord.id,
            )
            .limit(limit)
            .offset(offset)
        )
        async with self._session_factory() as session:
            records = (await session.scalars(statement)).all()
            if not records:
                return []
            scan_bundle = await _validated_scan(session, audit_id)
            if scan_bundle is None:
                raise RepositoryIntegrityError(
                    "AuditScopeUnit",
                    records[0].id,
                    reason_code="owner_binding_mismatch",
                )
            scan = scan_bundle[-1]
            results: list[StoredAuditEntity[AuditScopeUnit]] = []
            for record in records:
                scope = audit_scope_unit_from_record(
                    record,
                    project_id=scan.project_id,
                )
                if scope.snapshot_id not in {scan.snapshot_id, scan.base_snapshot_id}:
                    raise RepositoryIntegrityError(
                        "AuditScopeUnit",
                        scope.id,
                        reason_code="snapshot_binding_mismatch",
                    )
                results.append(StoredAuditEntity(scope, record.state_version))
        return results

    async def compare_and_set(
        self,
        current: StoredAuditEntity[AuditScopeUnit],
        replacement: AuditScopeUnit,
    ) -> tuple[StoredAuditEntity[AuditScopeUnit], bool]:
        replacement = AuditScopeUnit.model_validate(replacement)
        async with serialized_write(self._session_factory) as session:
            bundle = await _validated_scope(
                session,
                current.value.audit_id,
                current.value.id,
                expected_state_version=current.state_version,
                for_update=True,
            )
            if bundle is None:
                latest = await _validated_scope(
                    session,
                    current.value.audit_id,
                    current.value.id,
                    for_update=True,
                )
                if latest is None:
                    raise EntityNotFoundError("AuditScopeUnit", current.value.id)
                stored = StoredAuditEntity(latest[1], latest[0].state_version)
                if stored.value == replacement:
                    return stored, False
                _conflict("AuditScopeUnit", current.value.id)
            record, persisted_scope = bundle
            persisted = StoredAuditEntity(persisted_scope, record.state_version)
            if persisted != current:
                _conflict("AuditScopeUnit", current.value.id, "CAS token/value mismatch")
            _validate_scope_replacement(persisted_scope, replacement)
            if persisted_scope == replacement:
                return persisted, False
            candidate = audit_scope_unit_to_record(
                replacement,
                project_id=record.project_id,
                state_version=current.state_version + 1,
            )
            result = await session.execute(
                update(AuditScopeUnitRecord)
                .where(
                    AuditScopeUnitRecord.id == current.value.id,
                    AuditScopeUnitRecord.state_version == current.state_version,
                )
                .values(**_record_values(candidate, excluding=frozenset({"id"})))
            )
            if result.rowcount != 1:  # type: ignore[attr-defined]
                _conflict("AuditScopeUnit", current.value.id)
        return StoredAuditEntity(replacement, current.state_version + 1), True


async def _validated_work(
    session: AsyncSession,
    audit_id: str,
    work_item_id: str,
    *,
    expected_state_version: int | None = None,
    for_update: bool = False,
) -> tuple[AuditWorkItemRecord, AuditWorkItem] | None:
    statement = select(AuditWorkItemRecord).where(
        AuditWorkItemRecord.id == work_item_id,
        AuditWorkItemRecord.audit_id == audit_id,
    )
    if expected_state_version is not None:
        statement = statement.where(AuditWorkItemRecord.state_version == expected_state_version)
    if for_update:
        statement = statement.with_for_update()
    record = await session.scalar(statement)
    if record is None:
        return None
    work_item = audit_work_item_from_record(record)
    scope = await _validated_scope(
        session,
        audit_id,
        work_item.primary_scope_unit_id,
    )
    if scope is None:
        raise RepositoryIntegrityError(
            "AuditWorkItem",
            work_item.id,
            reason_code="scope_binding_mismatch",
        )
    scan_bundle = await _validated_scan(session, audit_id)
    if scan_bundle is None:
        raise RepositoryIntegrityError(
            "AuditWorkItem",
            work_item.id,
            reason_code="owner_binding_mismatch",
        )
    artifact = await session.scalar(
        select(ArtifactRecord).where(
            ArtifactRecord.id == work_item.required_coverage_plan_artifact_id
        )
    )
    if (
        artifact is None
        or artifact.run_id != scan_bundle[-1].run_id
        or artifact.sha256 != work_item.required_coverage_plan_digest
    ):
        raise RepositoryIntegrityError(
            "AuditWorkItem",
            work_item.id,
            reason_code="coverage_plan_binding_mismatch",
        )
    return record, work_item


def _validate_work_replacement(current: AuditWorkItem, replacement: AuditWorkItem) -> None:
    immutable_fields = (
        "id",
        "audit_id",
        "phase",
        "epoch",
        "primary_scope_unit_id",
        "strategy",
        "stable_key",
        "risk_tier",
        "input_digest",
        "required_coverage_plan_artifact_id",
        "required_coverage_plan_digest",
        "created_at",
    )
    if any(getattr(current, field) != getattr(replacement, field) for field in immutable_fields):
        _conflict("AuditWorkItem", current.id, "immutable identity changed")
    if current == replacement:
        return
    if current.status is replacement.status:
        if current.status not in {AuditWorkStatus.LEASED, AuditWorkStatus.RUNNING}:
            _conflict("AuditWorkItem", current.id, "same-state rewrite is not allowed")
        comparable_fields = (
            "lease_owner",
            "lease_expires_at",
            "attempt",
            "updated_at",
        )
        if _without_fields(current, *comparable_fields) != _without_fields(
            replacement,
            *comparable_fields,
        ):
            _conflict("AuditWorkItem", current.id, "lease rewrite changed durable facts")
        renewal = (
            replacement.lease_owner == current.lease_owner
            and replacement.attempt == current.attempt
            and current.lease_expires_at is not None
            and replacement.lease_expires_at is not None
            and replacement.lease_expires_at > current.lease_expires_at
            and replacement.updated_at >= current.updated_at
        )
        reclaim = (
            current.status is AuditWorkStatus.LEASED
            and current.lease_expires_at is not None
            and current.lease_expires_at <= replacement.updated_at
            and replacement.attempt == current.attempt + 1
            and replacement.updated_at >= current.updated_at
        )
        if not (renewal or reclaim):
            _conflict("AuditWorkItem", current.id, "invalid lease renewal or reclaim")
        return
    try:
        expected = current.transition_to(
            replacement.status,
            at=replacement.updated_at,
            lease_owner=replacement.lease_owner,
            lease_expires_at=replacement.lease_expires_at,
            receipt_id=replacement.receipt_id,
        )
    except (TypeError, ValueError):
        _conflict("AuditWorkItem", current.id, "contains a disallowed transition")
    if expected != replacement:
        _conflict("AuditWorkItem", current.id, "transition payload is not canonical")


class SQLAlchemyAuditWorkRepository:
    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    async def _find_create_candidate(
        self,
        session: AsyncSession,
        work_item: AuditWorkItem,
    ) -> AuditWorkItemRecord | None:
        return await _single_create_candidate(
            session,
            select(AuditWorkItemRecord).where(
                or_(
                    AuditWorkItemRecord.id == work_item.id,
                    and_(
                        AuditWorkItemRecord.audit_id == work_item.audit_id,
                        AuditWorkItemRecord.phase == work_item.phase.value,
                        AuditWorkItemRecord.epoch == work_item.epoch,
                        AuditWorkItemRecord.stable_key == work_item.stable_key,
                    ),
                )
            ),
            entity="AuditWorkItem",
            entity_id=work_item.id,
        )

    async def create(
        self,
        work_item: AuditWorkItem,
    ) -> tuple[StoredAuditEntity[AuditWorkItem], bool]:
        work_item = AuditWorkItem.model_validate(work_item)
        if work_item.status is not AuditWorkStatus.QUEUED:
            _conflict("AuditWorkItem", work_item.id, "must be created queued")
        try:
            async with serialized_write(self._session_factory) as session:
                scope = await _validated_scope(
                    session,
                    work_item.audit_id,
                    work_item.primary_scope_unit_id,
                )
                if scope is None:
                    _conflict("AuditWorkItem", work_item.id, "has an invalid primary Scope")
                scan_bundle = await _validated_scan(session, work_item.audit_id)
                if scan_bundle is None:
                    raise EntityNotFoundError("AuditScan", work_item.audit_id)
                coverage_plan = await session.scalar(
                    select(ArtifactRecord).where(
                        ArtifactRecord.id == work_item.required_coverage_plan_artifact_id
                    )
                )
                if (
                    coverage_plan is None
                    or coverage_plan.run_id != scan_bundle[-1].run_id
                    or coverage_plan.sha256 != work_item.required_coverage_plan_digest
                ):
                    _conflict(
                        "AuditWorkItem",
                        work_item.id,
                        "has an invalid required coverage plan",
                    )
                existing = await self._find_create_candidate(session, work_item)
                if existing is not None:
                    existing_bundle = await _validated_work(
                        session,
                        existing.audit_id,
                        existing.id,
                    )
                    if existing_bundle is None:
                        raise RepositoryIntegrityError(
                            "AuditWorkItem",
                            existing.id,
                        ) from None
                    stored = StoredAuditEntity(
                        existing_bundle[1],
                        existing_bundle[0].state_version,
                    )
                    if _same_fields(stored.value, work_item, _WORK_CREATION_FIELDS):
                        return stored, False
                    _conflict("AuditWorkItem", work_item.id, "identity already exists")
                session.add(audit_work_item_to_record(work_item))
                await session.flush()
        except IntegrityError:
            pass
        else:
            return StoredAuditEntity(work_item, 1), True
        async with self._session_factory() as session:
            existing = await self._find_create_candidate(session, work_item)
            if existing is not None:
                existing_bundle = await _validated_work(
                    session,
                    existing.audit_id,
                    existing.id,
                )
                if existing_bundle is None:
                    raise RepositoryIntegrityError(
                        "AuditWorkItem",
                        existing.id,
                    ) from None
                stored = StoredAuditEntity(
                    existing_bundle[1],
                    existing_bundle[0].state_version,
                )
                if _same_fields(stored.value, work_item, _WORK_CREATION_FIELDS):
                    return stored, False
        _conflict("AuditWorkItem", work_item.id, "could not be created")

    async def get(
        self,
        audit_id: str,
        work_item_id: str,
    ) -> StoredAuditEntity[AuditWorkItem] | None:
        async with self._session_factory() as session:
            bundle = await _validated_work(session, audit_id, work_item_id)
        if bundle is None:
            return None
        return StoredAuditEntity(bundle[1], bundle[0].state_version)

    async def list(
        self,
        audit_id: str,
        *,
        phase: AuditPhase | None = None,
        status: AuditWorkStatus | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> Sequence[StoredAuditEntity[AuditWorkItem]]:
        _validate_page(limit=limit, offset=offset)
        statement = select(AuditWorkItemRecord).where(AuditWorkItemRecord.audit_id == audit_id)
        if phase is not None:
            statement = statement.where(AuditWorkItemRecord.phase == phase.value)
        if status is not None:
            statement = statement.where(AuditWorkItemRecord.status == status.value)
        statement = (
            statement.order_by(
                AuditWorkItemRecord.created_at,
                AuditWorkItemRecord.id,
            )
            .limit(limit)
            .offset(offset)
        )
        async with self._session_factory() as session:
            records = (await session.scalars(statement)).all()
            results: list[StoredAuditEntity[AuditWorkItem]] = []
            for record in records:
                bundle = await _validated_work(session, audit_id, record.id)
                if bundle is None:
                    raise RepositoryIntegrityError("AuditWorkItem", record.id)
                results.append(StoredAuditEntity(bundle[1], bundle[0].state_version))
        return results

    async def compare_and_set(
        self,
        current: StoredAuditEntity[AuditWorkItem],
        replacement: AuditWorkItem,
    ) -> tuple[StoredAuditEntity[AuditWorkItem], bool]:
        replacement = AuditWorkItem.model_validate(replacement)
        async with serialized_write(self._session_factory) as session:
            bundle = await _validated_work(
                session,
                current.value.audit_id,
                current.value.id,
                expected_state_version=current.state_version,
                for_update=True,
            )
            if bundle is None:
                latest = await _validated_work(
                    session,
                    current.value.audit_id,
                    current.value.id,
                    for_update=True,
                )
                if latest is None:
                    raise EntityNotFoundError("AuditWorkItem", current.value.id)
                stored = StoredAuditEntity(latest[1], latest[0].state_version)
                if stored.value == replacement:
                    return stored, False
                _conflict("AuditWorkItem", current.value.id)
            record, persisted_work = bundle
            persisted = StoredAuditEntity(persisted_work, record.state_version)
            if persisted != current:
                _conflict("AuditWorkItem", current.value.id, "CAS token/value mismatch")
            _validate_work_replacement(persisted_work, replacement)
            if persisted_work == replacement:
                return persisted, False
            candidate = audit_work_item_to_record(
                replacement,
                state_version=current.state_version + 1,
            )
            result = await session.execute(
                update(AuditWorkItemRecord)
                .where(
                    AuditWorkItemRecord.id == current.value.id,
                    AuditWorkItemRecord.state_version == current.state_version,
                )
                .values(**_record_values(candidate, excluding=frozenset({"id"})))
            )
            if result.rowcount != 1:  # type: ignore[attr-defined]
                _conflict("AuditWorkItem", current.value.id)
        return StoredAuditEntity(replacement, current.state_version + 1), True


__all__ = [
    "SQLAlchemyAuditContractRepository",
    "SQLAlchemyAuditPhaseRepository",
    "SQLAlchemyAuditProjectRepository",
    "SQLAlchemyAuditRepository",
    "SQLAlchemyAuditScopeRepository",
    "SQLAlchemyAuditStartIntentRepository",
    "SQLAlchemyAuditWorkRepository",
    "SQLAlchemySnapshotRepository",
    "compare_and_set_audit_contract",
    "compare_and_set_audit_scan",
    "create_audit_project",
    "create_audit_start_intent",
    "create_scan_contract_pair",
    "load_validated_audit_scan",
    "validate_audit_scan_record_bundle",
]
