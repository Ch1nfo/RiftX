from __future__ import annotations

import asyncio
import hashlib
import traceback
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from tests.unit.domain.test_audit_domain import _contract as _domain_contract

import riftx.persistence.audit_repositories as audit_repository_module
from riftx.application.errors import RepositoryConflictError, RepositoryIntegrityError
from riftx.application.ports import StoredAuditEntity
from riftx.domain import (
    Artifact,
    ArtifactAccessClass,
    ArtifactContentTrust,
    ArtifactIngestMethod,
    ArtifactIngestProvenance,
    AuditClosureStatus,
    AuditContract,
    AuditContractRecord,
    AuditLifecycleStatus,
    AuditMode,
    AuditPhase,
    AuditPhaseRun,
    AuditPhaseRunStatus,
    AuditProject,
    AuditPublicationStatus,
    AuditPurpose,
    AuditRiskTier,
    AuditScan,
    AuditScopeKind,
    AuditScopeStatus,
    AuditScopeUnit,
    AuditStartIntent,
    AuditStartIntentStatus,
    AuditSummaryCount,
    AuditTerminalOutcome,
    AuditWorkItem,
    AuditWorkStatus,
    Engagement,
    Objective,
    Run,
    RunKind,
    RunStatus,
    SourceSnapshot,
    SourceTargetKind,
)
from riftx.persistence import (
    Database,
    SQLAlchemyArtifactRepository,
    SQLAlchemyAuditContractRepository,
    SQLAlchemyAuditPhaseRepository,
    SQLAlchemyAuditProjectRepository,
    SQLAlchemyAuditRepository,
    SQLAlchemyAuditScopeRepository,
    SQLAlchemyAuditStartIntentRepository,
    SQLAlchemyAuditWorkRepository,
    SQLAlchemyEngagementRepository,
    SQLAlchemyRunRepository,
    SQLAlchemySnapshotRepository,
    compare_and_set_audit_scan,
    create_scan_contract_pair,
)
from riftx.persistence.mappers import artifact_to_record

NOW = datetime(2026, 8, 3, 9, tzinfo=UTC)


def _digest(seed: str) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()


def _replace[T](value: T, **updates: object) -> T:
    payload = value.model_dump(mode="python")  # type: ignore[attr-defined]
    payload.update(updates)
    return type(value).model_validate(payload)  # type: ignore[attr-defined,no-any-return]


def _project(
    project_id: str = "project-1",
    *,
    engagement_id: str = "engagement-1",
    created_at: datetime = NOW,
) -> AuditProject:
    return AuditProject(
        id=project_id,
        engagement_id=engagement_id,
        display_name=f"Project {project_id}",
        repository_identity_digest=_digest(f"repository:{project_id}"),
        default_branch="main",
        created_at=created_at,
        updated_at=created_at,
    )


def _snapshot(
    snapshot_id: str = "snapshot-1",
    *,
    project_id: str = "project-1",
    seed: str | None = None,
    parent_snapshot_id: str | None = None,
    created_at: datetime = NOW,
) -> SourceSnapshot:
    seed = seed or snapshot_id
    tree_digest = _digest(f"tree:{seed}")
    capture_policy_digest = _digest(f"capture:{seed}")
    materializer_version = "materializer/v1"
    return SourceSnapshot(
        id=snapshot_id,
        project_id=project_id,
        source_kind=SourceTargetKind.REVISION,
        parent_snapshot_id=parent_snapshot_id,
        base_tree_digest=(_digest(f"base-tree:{seed}") if parent_snapshot_id is not None else None),
        patch_digest=_digest(f"patch:{seed}") if parent_snapshot_id is not None else None,
        commit_sha=_digest(f"commit:{seed}"),
        base_commit_sha=(
            _digest(f"base-commit:{seed}") if parent_snapshot_id is not None else None
        ),
        tree_digest=tree_digest,
        capture_policy_digest=capture_policy_digest,
        materializer_schema_version=materializer_version,
        snapshot_digest=SourceSnapshot.compute_snapshot_digest(
            tree_digest=tree_digest,
            capture_policy_digest=capture_policy_digest,
            materializer_schema_version=materializer_version,
        ),
        snapshot_store_version="snapshot-store/v1",
        content_storage_key=f"cas/source/{seed}",
        manifest_storage_key=f"cas/manifest/{seed}",
        manifest_digest=_digest(f"manifest:{seed}"),
        file_count=12,
        total_bytes=4_096,
        created_at=created_at,
        sealed_at=created_at + timedelta(seconds=1),
    )


def _contract(
    audit_id: str = "audit-1",
    *,
    project_id: str = "project-1",
    baseline_audit_id: str | None = None,
    mode: AuditMode = AuditMode.STANDARD,
) -> AuditContract:
    # Reuse the authoritative domain-test fixture, but keep persistence-specific
    # identities local so this file remains compact and each test is independent.
    base = _domain_contract(audit_id=audit_id, mode=mode)
    return _replace(
        base,
        project_id=project_id,
        baseline_audit_id=baseline_audit_id,
    )


def _contract_record(
    contract: AuditContract,
    *,
    contract_id: str | None = None,
    sealed_at: datetime | None = None,
) -> AuditContractRecord:
    return AuditContractRecord.from_contract(
        contract,
        contract_id=contract_id or f"contract-{contract.audit_id}",
        created_at=NOW,
        sealed_at=sealed_at,
    )


def _scan(
    contract: AuditContract,
    record: AuditContractRecord,
    *,
    run_id: str,
    snapshot_id: str | None = None,
    base_snapshot_id: str | None = None,
    parent_audit_id: str | None = None,
    purpose: AuditPurpose = AuditPurpose.PRIMARY,
    created_at: datetime = NOW,
) -> AuditScan:
    selection = contract.execution_selection
    return AuditScan(
        id=contract.audit_id,
        run_id=run_id,
        project_id=contract.project_id,
        contract_id=record.contract_id,
        snapshot_id=snapshot_id,
        base_snapshot_id=base_snapshot_id,
        baseline_audit_id=contract.baseline_audit_id,
        purpose=purpose,
        parent_audit_id=parent_audit_id,
        mode=contract.mode,
        analysis_profile=contract.analysis_profile,
        model_profile=contract.model_profile,
        selected_node_id=selection.selected_node_id,
        required_backend_id=selection.required_backend_id,
        policy_digest=contract.policy_digest,
        budget_digest=contract.budget.digest,
        config_digest=contract.config_digest,
        contract_digest=contract.contract_digest,
        temporal_workflow_id=f"riftx-code-audit-{contract.audit_id}",
        created_at=created_at,
    )


def _intent(
    scan: AuditScan,
    *,
    intent_id: str = "intent-1",
    created_at: datetime = NOW,
) -> AuditStartIntent:
    return AuditStartIntent(
        id=intent_id,
        audit_id=scan.id,
        run_id=scan.run_id,
        start_request_id=f"request-{intent_id}",
        contract_digest=scan.contract_digest,
        workflow_id=scan.temporal_workflow_id,
        task_queue="riftx-audit",
        created_at=created_at,
        updated_at=created_at,
    )


def _phase_run(
    audit_id: str,
    *,
    phase_run_id: str = "phase-1",
    created_at: datetime = NOW,
) -> AuditPhaseRun:
    return AuditPhaseRun(
        id=phase_run_id,
        audit_id=audit_id,
        phase=AuditPhase.MAP_SCOPE,
        idempotency_key=f"idempotency-{phase_run_id}",
        input_digest=_digest(f"phase-input:{phase_run_id}"),
        config_digest=_digest(f"phase-config:{phase_run_id}"),
        created_at=created_at,
        updated_at=created_at,
    )


def _scope(
    audit_id: str,
    snapshot_id: str,
    *,
    scope_id: str = "scope-1",
    created_at: datetime = NOW,
) -> AuditScopeUnit:
    return AuditScopeUnit(
        id=scope_id,
        audit_id=audit_id,
        snapshot_id=snapshot_id,
        kind=AuditScopeKind.FILE,
        relative_path=f"src/{scope_id}.py",
        blob_digest=_digest(f"blob:{scope_id}"),
        risk_tier=AuditRiskTier.MEDIUM,
        required_analyses=("agent_review", "native_rules"),
        stable_key=_digest(f"scope-key:{scope_id}"),
        created_at=created_at,
        updated_at=created_at,
    )


def _work(
    audit_id: str,
    scope_id: str,
    *,
    work_id: str = "work-1",
    created_at: datetime = NOW,
) -> AuditWorkItem:
    return AuditWorkItem(
        id=work_id,
        audit_id=audit_id,
        phase=AuditPhase.AGENT_HUNT,
        epoch=1,
        primary_scope_unit_id=scope_id,
        strategy="hunter_review",
        stable_key=_digest(f"work-key:{work_id}"),
        risk_tier=AuditRiskTier.HIGH,
        input_digest=_digest(f"work-input:{work_id}"),
        required_coverage_plan_artifact_id=f"coverage-{work_id}",
        required_coverage_plan_digest=_digest(f"coverage:{work_id}"),
        created_at=created_at,
        updated_at=created_at,
    )


def _coverage_plan(work: AuditWorkItem, *, run_id: str) -> Artifact:
    return Artifact(
        id=work.required_coverage_plan_artifact_id,
        run_id=run_id,
        audit_id=work.audit_id,
        access_class=ArtifactAccessClass.AUDIT_INTERNAL,
        content_trust=ArtifactContentTrust.GENERATED,
        name=f"Coverage plan for {work.id}",
        path=f"/audit/{work.audit_id}/{work.id}.coverage.json",
        ingest_provenance=ArtifactIngestProvenance(
            method=ArtifactIngestMethod.CONTROL_PLANE_BYTES,
        ),
        mime_type="application/json",
        sha256=work.required_coverage_plan_digest,
        size=128,
        description="Frozen server-owned coverage plan.",
        created_at=work.created_at,
    )


def _phase_output(
    phase_run: AuditPhaseRun,
    *,
    artifact_id: str,
    run_id: str,
) -> Artifact:
    return Artifact(
        id=artifact_id,
        run_id=run_id,
        audit_id=phase_run.audit_id,
        access_class=ArtifactAccessClass.AUDIT_INTERNAL,
        content_trust=ArtifactContentTrust.GENERATED,
        name=f"Phase output {artifact_id}",
        path=f"/audit/{phase_run.audit_id}/{artifact_id}.json",
        ingest_provenance=ArtifactIngestProvenance(
            method=ArtifactIngestMethod.CONTROL_PLANE_BYTES,
        ),
        mime_type="application/json",
        sha256=_digest(f"phase-output:{artifact_id}"),
        size=128,
        description="Phase output bound to its authoritative Run.",
        created_at=phase_run.created_at,
    )


async def _create_coverage_plan(
    database: Database,
    work: AuditWorkItem,
    *,
    run_id: str,
) -> Artifact:
    artifact = _coverage_plan(work, run_id=run_id)
    await SQLAlchemyArtifactRepository(database.session_factory).create(artifact)
    return artifact


async def _insert_corrupt_artifact_owner(database: Database, artifact: Artifact) -> None:
    """Bypass the Artifact repository to model a pre-existing corrupt row."""

    async with database.session_factory() as session, session.begin():
        session.add(artifact_to_record(artifact))
        await session.flush()


async def _create_engagement(database: Database, engagement_id: str) -> None:
    await SQLAlchemyEngagementRepository(database.session_factory).create(
        Engagement(id=engagement_id, name=f"Engagement {engagement_id}")
    )


async def _create_run(
    database: Database,
    run_id: str,
    *,
    engagement_id: str = "engagement-1",
    kind: RunKind = RunKind.CODE_AUDIT,
    node_id: str = "analysis-node",
    model_profile: str | None = None,
    temporal_workflow_id: str | None = None,
) -> None:
    await SQLAlchemyRunRepository(database.session_factory).create(
        Run(
            id=run_id,
            engagement_id=engagement_id,
            kind=kind,
            node_id=node_id,
            objective=Objective(description=f"Persist {run_id}"),
            model_profile=model_profile,
            workspace_path=f"/tmp/riftx/{run_id}",
            temporal_workflow_id=temporal_workflow_id,
        )
    )


async def _create_audit(
    database: Database,
    *,
    audit_id: str = "audit-1",
    run_id: str = "run-1",
    project_id: str = "project-1",
    snapshot_id: str | None = "snapshot-1",
    base_snapshot_id: str | None = None,
    created_at: datetime = NOW,
    baseline_audit_id: str | None = None,
    parent_audit_id: str | None = None,
    purpose: AuditPurpose = AuditPurpose.PRIMARY,
    mode: AuditMode = AuditMode.STANDARD,
    queued: bool = False,
) -> tuple[AuditContract, AuditContractRecord, AuditScan]:
    await _create_run(
        database,
        run_id,
        temporal_workflow_id=f"riftx-code-audit-{audit_id}",
    )
    contract = _contract(
        audit_id,
        project_id=project_id,
        baseline_audit_id=baseline_audit_id,
        mode=mode,
    )
    record = _contract_record(contract)
    scan = _scan(
        contract,
        record,
        run_id=run_id,
        snapshot_id=snapshot_id,
        base_snapshot_id=base_snapshot_id,
        parent_audit_id=parent_audit_id,
        purpose=purpose,
        created_at=created_at,
    )
    stored, created = await SQLAlchemyAuditRepository(database.session_factory).create(
        scan,
        record,
    )
    assert created is True
    assert stored.value == scan
    if queued:
        record, scan = await _queue_audit(database, record, scan)
    return contract, record, scan


async def _queue_audit(
    database: Database,
    record: AuditContractRecord,
    scan: AuditScan,
) -> tuple[AuditContractRecord, AuditScan]:
    contracts = SQLAlchemyAuditContractRepository(database.session_factory)
    audits = SQLAlchemyAuditRepository(database.session_factory)
    stored_contract = await contracts.get(scan.id)
    stored_scan = await audits.get(scan.id)
    assert stored_contract is not None and stored_scan is not None
    sealed = record.seal(at=scan.created_at)
    stored_contract, changed = await contracts.compare_and_set(stored_contract, sealed)
    assert stored_contract.value == sealed
    assert changed is True
    queued = scan.transition_to(
        AuditLifecycleStatus.QUEUED,
        at=scan.created_at + timedelta(seconds=1),
    )
    stored_scan, changed = await audits.compare_and_set(stored_scan, queued)
    assert stored_scan.value == queued
    assert changed is True
    return sealed, queued


async def _advance_to_failed_cleaning(
    audits: SQLAlchemyAuditRepository,
    scan: AuditScan,
) -> StoredAuditEntity[AuditScan]:
    current = await audits.get(scan.id)
    assert current is not None
    failing = current.value.transition_to(AuditLifecycleStatus.FAILING)
    current, changed = await audits.compare_and_set(current, failing)
    assert changed is True
    cleaning = current.value.transition_to(AuditLifecycleStatus.CLEANING)
    current, changed = await audits.compare_and_set(current, cleaning)
    assert changed is True
    return current


async def _raw_write_without_foreign_keys(
    database: Database,
    statement: str,
    parameters: dict[str, object],
) -> None:
    async with database.engine.connect() as connection:
        await connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        try:
            await connection.execute(text(statement), parameters)
            await connection.commit()
        except BaseException:
            await connection.rollback()
            raise
        finally:
            await connection.exec_driver_sql("PRAGMA foreign_keys=ON")


async def _missing_integrity_failures(
    operations: tuple[tuple[str, Callable[[], Awaitable[object]]], ...],
) -> list[str]:
    missing: list[str] = []
    for label, operation in operations:
        try:
            await operation()
        except RepositoryIntegrityError:
            continue
        missing.append(label)
    return missing


async def _insert_orphan_contract(
    database: Database,
    record: AuditContractRecord,
) -> None:
    async with database.engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO audit_contracts ("
                "contract_id, audit_id, schema_version, canonical_contract_json, "
                "contract_digest, source_target_digest, source_node_id, "
                "source_ingest_backend_digest, source_prepare_proof_digest, "
                "selected_node_id, required_backend_id, "
                "snapshot_hydration_policy_digest, state_version, created_at, sealed_at"
                ") VALUES ("
                ":contract_id, :audit_id, :schema_version, :canonical_contract_json, "
                ":contract_digest, :source_target_digest, :source_node_id, "
                ":source_ingest_backend_digest, :source_prepare_proof_digest, "
                ":selected_node_id, :required_backend_id, "
                ":snapshot_hydration_policy_digest, 1, :created_at, :sealed_at"
                ")"
            ),
            {
                "contract_id": record.contract_id,
                "audit_id": record.audit_id,
                "schema_version": record.schema_version,
                "canonical_contract_json": record.canonical_contract_json,
                "contract_digest": record.contract_digest,
                "source_target_digest": record.source_target_digest,
                "source_node_id": record.source_node_id,
                "source_ingest_backend_digest": record.source_ingest_backend_digest,
                "source_prepare_proof_digest": record.source_prepare_proof_digest,
                "selected_node_id": record.selected_node_id,
                "required_backend_id": record.required_backend_id,
                "snapshot_hydration_policy_digest": (record.snapshot_hydration_policy_digest),
                "created_at": record.created_at,
                "sealed_at": record.sealed_at,
            },
        )


async def test_all_audit_repositories_round_trip_after_database_reopen(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "audit-round-trip.db"
    database = Database(f"sqlite+aiosqlite:///{database_path}")
    await database.create_schema()
    await _create_engagement(database, "engagement-1")

    project = _project()
    project_stored, project_created = await SQLAlchemyAuditProjectRepository(
        database.session_factory
    ).create(project)
    snapshot = _snapshot()
    persisted_snapshot, snapshot_created = await SQLAlchemySnapshotRepository(
        database.session_factory
    ).create(snapshot)
    _, contract, scan = await _create_audit(database, queued=True)
    intent = _intent(scan)
    intent_stored, intent_created = await SQLAlchemyAuditStartIntentRepository(
        database.session_factory
    ).create(intent)
    phase = _phase_run(scan.id)
    phase_stored, phase_created = await SQLAlchemyAuditPhaseRepository(
        database.session_factory
    ).create(phase)
    scope = _scope(scan.id, snapshot.id)
    scope_stored, scope_created = await SQLAlchemyAuditScopeRepository(
        database.session_factory
    ).create(scope)
    work = _work(scan.id, scope.id)
    await _create_coverage_plan(database, work, run_id=scan.run_id)
    work_stored, work_created = await SQLAlchemyAuditWorkRepository(
        database.session_factory
    ).create(work)

    assert (project_created, snapshot_created, intent_created) == (True, True, True)
    assert (phase_created, scope_created, work_created) == (True, True, True)
    assert project_stored.state_version == 1
    assert persisted_snapshot == snapshot
    assert intent_stored.state_version == 1
    assert phase_stored.state_version == 1
    assert scope_stored.state_version == 1
    assert work_stored.state_version == 1
    await database.dispose()

    reopened = Database(f"sqlite+aiosqlite:///{database_path}")
    projects = SQLAlchemyAuditProjectRepository(reopened.session_factory)
    snapshots = SQLAlchemySnapshotRepository(reopened.session_factory)
    audits = SQLAlchemyAuditRepository(reopened.session_factory)
    contracts = SQLAlchemyAuditContractRepository(reopened.session_factory)
    intents = SQLAlchemyAuditStartIntentRepository(reopened.session_factory)
    phases = SQLAlchemyAuditPhaseRepository(reopened.session_factory)
    scopes = SQLAlchemyAuditScopeRepository(reopened.session_factory)
    works = SQLAlchemyAuditWorkRepository(reopened.session_factory)

    assert (await projects.get(project.id)) == project_stored
    assert (
        await projects.get_by_identity(
            project.repository_identity_digest,
            engagement_id=project.engagement_id,
        )
    ) == project_stored
    assert (await snapshots.get(project.id, snapshot.id)) == snapshot
    assert (await snapshots.get_by_digest(project.id, snapshot.snapshot_digest)) == snapshot
    assert (await audits.get(scan.id)).value == scan  # type: ignore[union-attr]
    assert await audits.get_contract(scan.id) == contract
    assert (await contracts.get(scan.id)).value == contract  # type: ignore[union-attr]
    assert await intents.get(scan.id, intent.id) == intent_stored
    assert await intents.get_for_audit(scan.id) == intent_stored
    assert await phases.get(scan.id, phase.id) == phase_stored
    assert await scopes.get(scan.id, scope.id) == scope_stored
    assert await works.get(scan.id, work.id) == work_stored

    assert [item.value for item in await projects.list()] == [project]
    assert list(await snapshots.list(project.id)) == [snapshot]
    assert [item.value for item in await audits.list(project_id=project.id)] == [scan]
    assert [item.value for item in await intents.list_ready(now=NOW)] == [intent]
    assert [item.value for item in await phases.list(scan.id)] == [phase]
    assert [item.value for item in await scopes.list(scan.id)] == [scope]
    assert [item.value for item in await works.list(scan.id)] == [work]

    async with reopened.engine.connect() as connection:
        assert (await connection.exec_driver_sql("PRAGMA foreign_keys")).scalar_one() == 1
        assert (await connection.exec_driver_sql("PRAGMA foreign_key_check")).all() == []
    await reopened.dispose()


async def test_exact_create_retries_are_idempotent_and_changed_retries_conflict(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'audit-idempotency.db'}")
    await database.create_schema()
    await _create_engagement(database, "engagement-1")
    projects = SQLAlchemyAuditProjectRepository(database.session_factory)
    snapshots = SQLAlchemySnapshotRepository(database.session_factory)
    audits = SQLAlchemyAuditRepository(database.session_factory)
    intents = SQLAlchemyAuditStartIntentRepository(database.session_factory)
    phases = SQLAlchemyAuditPhaseRepository(database.session_factory)
    scopes = SQLAlchemyAuditScopeRepository(database.session_factory)
    works = SQLAlchemyAuditWorkRepository(database.session_factory)

    project = _project()
    project_stored, _ = await projects.create(project)
    snapshot = _snapshot()
    await snapshots.create(snapshot)
    _, record, scan = await _create_audit(database)
    assert (await audits.create(scan, record))[1] is False
    changed_scan = _replace(
        scan,
        purpose=AuditPurpose.RETEST,
        parent_audit_id="audit-unrelated-parent",
    )
    with pytest.raises(RepositoryConflictError):
        await audits.create(changed_scan, record)
    _, scan = await _queue_audit(database, record, scan)
    intent = _intent(scan)
    phase = _phase_run(scan.id)
    scope = _scope(scan.id, snapshot.id)
    work = _work(scan.id, scope.id)
    await _create_coverage_plan(database, work, run_id=scan.run_id)
    await intents.create(intent)
    await phases.create(phase)
    await scopes.create(scope)
    await works.create(work)

    assert await projects.create(project) == (project_stored, False)
    assert await snapshots.create(snapshot) == (snapshot, False)
    assert (await intents.create(intent))[1] is False
    assert (await phases.create(phase))[1] is False
    assert (await scopes.create(scope))[1] is False
    assert (await works.create(work))[1] is False

    changed_project = _replace(
        project,
        repository_identity_digest=_digest("different-repository-identity"),
        updated_at=NOW + timedelta(seconds=2),
    )
    changed_snapshot = _snapshot(snapshot.id, project_id=project.id, seed="changed")
    changed_intent = _replace(intent, task_queue="changed-queue")
    changed_phase = _replace(phase, input_digest=_digest("changed-phase-input"))
    changed_scope = _replace(scope, required_analyses=("native_rules",))
    changed_work = _replace(work, strategy="skeptic_review")

    for operation in (
        lambda: projects.create(changed_project),
        lambda: snapshots.create(changed_snapshot),
        lambda: intents.create(changed_intent),
        lambda: phases.create(changed_phase),
        lambda: scopes.create(changed_scope),
        lambda: works.create(changed_work),
    ):
        with pytest.raises(RepositoryConflictError):
            await operation()
    await database.dispose()


async def test_unspecified_snapshot_create_retry_reuses_bound_progressed_scan(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'audit-bind-retry.db'}")
    await database.create_schema()
    await _create_engagement(database, "engagement-1")
    await SQLAlchemyAuditProjectRepository(database.session_factory).create(_project())
    snapshot = _snapshot()
    await SQLAlchemySnapshotRepository(database.session_factory).create(snapshot)
    _, record, draft = await _create_audit(database, snapshot_id=None)
    audits = SQLAlchemyAuditRepository(database.session_factory)
    current = await audits.get(draft.id)
    assert current is not None
    bound = draft.bind_snapshots(snapshot_id=snapshot.id)
    current, changed = await audits.compare_and_set(current, bound)
    assert changed is True
    _, queued = await _queue_audit(database, record, bound)

    retried, created = await audits.create(draft, record)

    assert created is False
    assert retried.value == queued
    explicit_match = _replace(draft, snapshot_id=snapshot.id)
    matched, created = await audits.create(explicit_match, record)
    assert created is False
    assert matched == retried
    explicit_mismatch = _replace(draft, snapshot_id="snapshot-other")
    with pytest.raises(RepositoryConflictError):
        await audits.create(explicit_mismatch, record)
    await database.dispose()


async def test_create_rejects_ambiguous_surrogate_and_natural_key_collisions(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'ambiguous-create-key.db'}")
    await database.create_schema()
    await _create_engagement(database, "engagement-1")
    projects = SQLAlchemyAuditProjectRepository(database.session_factory)
    first = _project("project-first")
    second = _project("project-second")
    await projects.create(first)
    await projects.create(second)
    ambiguous = _replace(
        first,
        repository_identity_digest=second.repository_identity_digest,
    )

    with pytest.raises(RepositoryConflictError, match="ambiguous identity collisions"):
        await projects.create(ambiguous)

    assert (await projects.get(first.id)).value == first  # type: ignore[union-attr]
    assert (await projects.get(second.id)).value == second  # type: ignore[union-attr]
    await database.dispose()


async def test_scan_replay_rejects_a_contract_id_owned_by_another_audit(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'contract-id-collision.db'}")
    await database.create_schema()
    await _create_engagement(database, "engagement-1")
    await SQLAlchemyAuditProjectRepository(database.session_factory).create(_project())
    await SQLAlchemySnapshotRepository(database.session_factory).create(_snapshot())
    contract_a, record_a, scan_a = await _create_audit(
        database,
        audit_id="audit-a",
        run_id="run-a",
    )
    _, record_b, scan_b = await _create_audit(
        database,
        audit_id="audit-b",
        run_id="run-b",
    )
    conflicting_record = _contract_record(
        contract_a,
        contract_id=record_b.contract_id,
    )
    conflicting_scan = _replace(
        scan_a,
        contract_id=conflicting_record.contract_id,
    )
    audits = SQLAlchemyAuditRepository(database.session_factory)

    with pytest.raises(RepositoryConflictError, match="ambiguous Contract identity"):
        await audits.create(conflicting_scan, conflicting_record)

    assert (await audits.get(scan_a.id)).value == scan_a  # type: ignore[union-attr]
    assert (await audits.get(scan_b.id)).value == scan_b  # type: ignore[union-attr]
    assert record_a.contract_id != record_b.contract_id
    await database.dispose()


async def test_cross_audit_scope_and_work_surrogate_collisions_are_conflicts(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'ledger-id-collision.db'}")
    await database.create_schema()
    await _create_engagement(database, "engagement-1")
    await SQLAlchemyAuditProjectRepository(database.session_factory).create(_project())
    await SQLAlchemySnapshotRepository(database.session_factory).create(_snapshot())
    _, _, scan_a = await _create_audit(
        database,
        audit_id="audit-a",
        run_id="run-a",
    )
    _, _, scan_b = await _create_audit(
        database,
        audit_id="audit-b",
        run_id="run-b",
    )
    scopes = SQLAlchemyAuditScopeRepository(database.session_factory)
    works = SQLAlchemyAuditWorkRepository(database.session_factory)
    scope_a = _scope(scan_a.id, "snapshot-1", scope_id="shared-scope")
    await scopes.create(scope_a)

    with pytest.raises(RepositoryConflictError):
        await scopes.create(
            _scope(scan_b.id, "snapshot-1", scope_id=scope_a.id)
        )

    scope_b = _scope(scan_b.id, "snapshot-1", scope_id="scope-b")
    await scopes.create(scope_b)
    work_a = _work(scan_a.id, scope_a.id, work_id="shared-work")
    await _create_coverage_plan(database, work_a, run_id=scan_a.run_id)
    stored_work_a, _ = await works.create(work_a)
    work_b = _replace(
        _work(scan_b.id, scope_b.id, work_id=work_a.id),
        required_coverage_plan_artifact_id="coverage-work-b",
        required_coverage_plan_digest=_digest("coverage:work-b"),
    )
    await _create_coverage_plan(database, work_b, run_id=scan_b.run_id)

    with pytest.raises(RepositoryConflictError):
        await works.create(work_b)

    assert await works.get(scan_a.id, work_a.id) == stored_work_a
    await database.dispose()


async def test_contract_integrity_error_recovery_drops_sensitive_driver_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'contract-driver-context.db'}")
    await database.create_schema()
    contract = _contract("audit-sensitive-driver")
    record = _contract_record(contract)
    scan = _scan(contract, record, run_id="run-sensitive-driver")
    secret = "/private/customer/repository/contract.json"

    async def fail_pair(*_args: object, **_kwargs: object) -> None:
        raise IntegrityError(
            "INSERT INTO audit_contracts",
            {"canonical_contract_json": secret},
            RuntimeError(secret),
        )

    monkeypatch.setattr(
        audit_repository_module,
        "create_scan_contract_pair",
        fail_pair,
    )
    with pytest.raises(RepositoryConflictError) as raised:
        await SQLAlchemyAuditRepository(database.session_factory).create(scan, record)

    rendered = "".join(traceback.format_exception(raised.value))
    assert raised.value.__context__ is None
    assert raised.value.__cause__ is None
    assert secret not in rendered
    await database.dispose()


async def test_snapshot_integrity_error_recovery_drops_sensitive_driver_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'snapshot-driver-context.db'}")
    await database.create_schema()
    await _create_engagement(database, "engagement-1")
    await SQLAlchemyAuditProjectRepository(database.session_factory).create(_project())
    snapshot = _snapshot()
    secret = "cas/private/customer/repository/source-object"

    def fail_snapshot_mapper(_snapshot_value: SourceSnapshot) -> None:
        raise IntegrityError(
            "INSERT INTO source_snapshots",
            {"content_storage_key": secret},
            RuntimeError(secret),
        )

    monkeypatch.setattr(
        audit_repository_module,
        "source_snapshot_to_record",
        fail_snapshot_mapper,
    )
    with pytest.raises(RepositoryConflictError) as raised:
        await SQLAlchemySnapshotRepository(database.session_factory).create(snapshot)

    rendered = "".join(traceback.format_exception(raised.value))
    assert raised.value.__context__ is None
    assert raised.value.__cause__ is None
    assert secret not in rendered
    await database.dispose()


async def test_scan_and_contract_pair_roll_back_as_one_transaction(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'audit-pair-rollback.db'}")
    await database.create_schema()
    await _create_engagement(database, "engagement-1")
    await SQLAlchemyAuditProjectRepository(database.session_factory).create(_project())
    await SQLAlchemySnapshotRepository(database.session_factory).create(_snapshot())
    await _create_run(
        database,
        "run-1",
        temporal_workflow_id="riftx-code-audit-audit-1",
    )
    contract = _contract()
    record = _contract_record(contract)
    scan = _scan(contract, record, run_id="run-1", snapshot_id="snapshot-1")

    with pytest.raises(RuntimeError, match="force caller rollback"):
        async with database.session_factory.begin() as session:
            stored, created = await create_scan_contract_pair(session, scan, record)
            assert stored.value == scan
            assert created is True
            raise RuntimeError("force caller rollback")

    async with database.engine.connect() as connection:
        assert (
            await connection.scalar(
                text("SELECT count(*) FROM audit_contracts WHERE contract_id=:id"),
                {"id": record.contract_id},
            )
            == 0
        )
        assert (
            await connection.scalar(
                text("SELECT count(*) FROM audit_scans WHERE id=:id"),
                {"id": scan.id},
            )
            == 0
        )
    await database.dispose()


async def test_audit_creation_rejects_general_runs_and_cross_engagement_runs(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'audit-run-owner.db'}")
    await database.create_schema()
    await _create_engagement(database, "engagement-1")
    await _create_engagement(database, "engagement-2")
    await SQLAlchemyAuditProjectRepository(database.session_factory).create(_project())
    await _create_run(database, "run-general", kind=RunKind.GENERAL)
    await _create_run(database, "run-cross", engagement_id="engagement-2")
    audits = SQLAlchemyAuditRepository(database.session_factory)

    for audit_id, run_id in (
        ("audit-general", "run-general"),
        ("audit-cross", "run-cross"),
    ):
        contract = _contract(audit_id)
        record = _contract_record(contract)
        scan = _scan(contract, record, run_id=run_id)
        with pytest.raises(RepositoryConflictError):
            await audits.create(scan, record)

    async with database.engine.connect() as connection:
        assert (await connection.scalar(text("SELECT count(*) FROM audit_scans"))) == 0
        assert (await connection.scalar(text("SELECT count(*) FROM audit_contracts"))) == 0
    await database.dispose()


async def test_snapshot_and_related_audit_references_cannot_cross_projects(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'audit-project-binding.db'}")
    await database.create_schema()
    await _create_engagement(database, "engagement-1")
    projects = SQLAlchemyAuditProjectRepository(database.session_factory)
    snapshots = SQLAlchemySnapshotRepository(database.session_factory)
    audits = SQLAlchemyAuditRepository(database.session_factory)
    await projects.create(_project("project-1"))
    await projects.create(_project("project-2"))
    parent_snapshot = _snapshot("snapshot-parent", project_id="project-1")
    foreign_snapshot = _snapshot("snapshot-foreign", project_id="project-2")
    await snapshots.create(parent_snapshot)
    await snapshots.create(foreign_snapshot)

    cross_project_child = _snapshot(
        "snapshot-child",
        project_id="project-2",
        parent_snapshot_id=parent_snapshot.id,
    )
    with pytest.raises(RepositoryConflictError):
        await snapshots.create(cross_project_child)

    await _create_run(database, "run-snapshot-cross")
    snapshot_contract = _contract("audit-snapshot-cross", project_id="project-1")
    snapshot_record = _contract_record(snapshot_contract)
    snapshot_scan = _scan(
        snapshot_contract,
        snapshot_record,
        run_id="run-snapshot-cross",
        snapshot_id=foreign_snapshot.id,
    )
    with pytest.raises(RepositoryConflictError):
        await audits.create(snapshot_scan, snapshot_record)

    _, _, foreign_audit = await _create_audit(
        database,
        audit_id="audit-foreign",
        run_id="run-foreign",
        project_id="project-2",
        snapshot_id=foreign_snapshot.id,
    )
    for audit_id, relation in (
        ("audit-cross-baseline", "baseline"),
        ("audit-cross-parent", "parent"),
    ):
        await _create_run(database, f"run-{audit_id}")
        contract = _contract(
            audit_id,
            project_id="project-1",
            baseline_audit_id=(foreign_audit.id if relation == "baseline" else None),
        )
        record = _contract_record(contract)
        scan = _scan(
            contract,
            record,
            run_id=f"run-{audit_id}",
            parent_audit_id=(foreign_audit.id if relation == "parent" else None),
            purpose=(AuditPurpose.RETEST if relation == "parent" else AuditPurpose.PRIMARY),
        )
        with pytest.raises(RepositoryConflictError):
            await audits.create(scan, record)
    await database.dispose()


async def test_scope_snapshot_and_work_primary_scope_must_belong_to_their_audit(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'audit-work-binding.db'}")
    await database.create_schema()
    await _create_engagement(database, "engagement-1")
    await SQLAlchemyAuditProjectRepository(database.session_factory).create(_project())
    snapshots = SQLAlchemySnapshotRepository(database.session_factory)
    bound_snapshot = _snapshot("snapshot-bound")
    other_snapshot = _snapshot("snapshot-other")
    await snapshots.create(bound_snapshot)
    await snapshots.create(other_snapshot)
    _, _, first_scan = await _create_audit(
        database,
        audit_id="audit-1",
        run_id="run-1",
        snapshot_id=bound_snapshot.id,
    )
    _, _, second_scan = await _create_audit(
        database,
        audit_id="audit-2",
        run_id="run-2",
        snapshot_id=bound_snapshot.id,
    )
    scopes = SQLAlchemyAuditScopeRepository(database.session_factory)
    works = SQLAlchemyAuditWorkRepository(database.session_factory)

    with pytest.raises(RepositoryConflictError):
        await scopes.create(_scope(first_scan.id, other_snapshot.id, scope_id="scope-bad"))

    second_scope = _scope(second_scan.id, bound_snapshot.id, scope_id="scope-second")
    await scopes.create(second_scope)
    with pytest.raises(RepositoryConflictError):
        await works.create(_work(first_scan.id, second_scope.id, work_id="work-cross"))
    await database.dispose()


async def test_repository_lists_use_stable_created_at_and_id_order(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'audit-list-order.db'}")
    await database.create_schema()
    await _create_engagement(database, "engagement-1")
    projects = SQLAlchemyAuditProjectRepository(database.session_factory)
    snapshots = SQLAlchemySnapshotRepository(database.session_factory)
    audits = SQLAlchemyAuditRepository(database.session_factory)
    intents = SQLAlchemyAuditStartIntentRepository(database.session_factory)
    phases = SQLAlchemyAuditPhaseRepository(database.session_factory)
    scopes = SQLAlchemyAuditScopeRepository(database.session_factory)
    works = SQLAlchemyAuditWorkRepository(database.session_factory)

    for project_id in ("project-z", "project-a"):
        await projects.create(_project(project_id))
    for snapshot_id in ("snapshot-z", "snapshot-a"):
        await snapshots.create(_snapshot(snapshot_id, project_id="project-a"))
    scans: dict[str, AuditScan] = {}
    for suffix in ("z", "a"):
        _, _, scans[suffix] = await _create_audit(
            database,
            audit_id=f"audit-{suffix}",
            run_id=f"run-{suffix}",
            project_id="project-a",
            snapshot_id="snapshot-a",
            queued=True,
        )
        await intents.create(_intent(scans[suffix], intent_id=f"intent-{suffix}"))
    for suffix in ("z", "a"):
        await phases.create(_phase_run("audit-a", phase_run_id=f"phase-{suffix}"))
        await scopes.create(_scope("audit-a", "snapshot-a", scope_id=f"scope-{suffix}"))
        work = _work("audit-a", f"scope-{suffix}", work_id=f"work-{suffix}")
        await _create_coverage_plan(database, work, run_id="run-a")
        await works.create(work)

    assert [item.value.id for item in await projects.list()] == ["project-a", "project-z"]
    assert [item.id for item in await snapshots.list("project-a")] == [
        "snapshot-a",
        "snapshot-z",
    ]
    assert [item.value.id for item in await audits.list(project_id="project-a")] == [
        "audit-a",
        "audit-z",
    ]
    assert [item.value.id for item in await intents.list_ready(now=NOW)] == [
        "intent-a",
        "intent-z",
    ]
    assert [item.value.id for item in await phases.list("audit-a")] == [
        "phase-a",
        "phase-z",
    ]
    assert [item.value.id for item in await scopes.list("audit-a")] == [
        "scope-a",
        "scope-z",
    ]
    assert [item.value.id for item in await works.list("audit-a")] == [
        "work-a",
        "work-z",
    ]
    await database.dispose()


async def test_cas_increments_version_and_handles_stale_exact_and_changed_retries(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'audit-cas.db'}")
    await database.create_schema()
    await _create_engagement(database, "engagement-1")
    repository = SQLAlchemyAuditProjectRepository(database.session_factory)
    original, created = await repository.create(_project())
    assert created is True
    replacement = _replace(
        original.value,
        display_name="Renamed project",
        updated_at=NOW + timedelta(seconds=1),
    )

    updated, changed = await repository.compare_and_set(original, replacement)

    assert changed is True
    assert updated.value == replacement
    assert updated.state_version == 2
    exact, exact_changed = await repository.compare_and_set(original, replacement)
    assert exact == updated
    assert exact_changed is False
    divergent = _replace(
        original.value,
        display_name="Divergent rename",
        updated_at=NOW + timedelta(seconds=2),
    )
    with pytest.raises(RepositoryConflictError):
        await repository.compare_and_set(original, divergent)
    await database.dispose()


async def test_two_sqlite_engines_allow_exactly_one_cas_winner(tmp_path: Path) -> None:
    database_path = tmp_path / "audit-cas-race.db"
    first_database = Database(f"sqlite+aiosqlite:///{database_path}")
    await first_database.create_schema()
    await _create_engagement(first_database, "engagement-1")
    first_repository = SQLAlchemyAuditProjectRepository(first_database.session_factory)
    await first_repository.create(_project())
    second_database = Database(f"sqlite+aiosqlite:///{database_path}")
    second_repository = SQLAlchemyAuditProjectRepository(second_database.session_factory)
    first_current = await first_repository.get("project-1")
    second_current = await second_repository.get("project-1")
    assert first_current is not None and second_current is not None
    first_replacement = _replace(
        first_current.value,
        display_name="First contender",
        updated_at=NOW + timedelta(seconds=1),
    )
    second_replacement = _replace(
        second_current.value,
        display_name="Second contender",
        updated_at=NOW + timedelta(seconds=1),
    )

    outcomes = await asyncio.gather(
        first_repository.compare_and_set(first_current, first_replacement),
        second_repository.compare_and_set(second_current, second_replacement),
        return_exceptions=True,
    )

    assert sum(isinstance(item, tuple) and item[1] is True for item in outcomes) == 1
    assert sum(isinstance(item, RepositoryConflictError) for item in outcomes) == 1
    winner = await first_repository.get("project-1")
    assert winner is not None
    assert winner.state_version == 2
    assert winner.value.display_name in {"First contender", "Second contender"}
    await second_database.dispose()
    await first_database.dispose()


async def test_terminal_audit_ledgers_cannot_be_revived(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'audit-terminal.db'}")
    await database.create_schema()
    await _create_engagement(database, "engagement-1")
    await SQLAlchemyAuditProjectRepository(database.session_factory).create(_project())
    await SQLAlchemySnapshotRepository(database.session_factory).create(_snapshot())
    _, _, scan = await _create_audit(database, queued=True)
    audit_repository = SQLAlchemyAuditRepository(database.session_factory)
    intent_repository = SQLAlchemyAuditStartIntentRepository(database.session_factory)
    phase_repository = SQLAlchemyAuditPhaseRepository(database.session_factory)
    scope_repository = SQLAlchemyAuditScopeRepository(database.session_factory)
    work_repository = SQLAlchemyAuditWorkRepository(database.session_factory)

    current_scan = await audit_repository.get(scan.id)
    assert current_scan is not None
    intent = _intent(scan)
    intent_stored, _ = await intent_repository.create(intent)
    for replacement in (scan.transition_to(AuditLifecycleStatus.FAILING),):
        current_scan, changed = await audit_repository.compare_and_set(
            current_scan,
            replacement,
        )
        assert changed is True
    failing = current_scan.value
    cleaning = failing.transition_to(AuditLifecycleStatus.CLEANING)
    current_scan, _ = await audit_repository.compare_and_set(current_scan, cleaning)
    converged = cleaning.record_cleanup_convergence(
        cleanup_proof_digest=_digest("cleanup-proof"),
        run_terminal_status=RunStatus.FAILED,
    )
    async with database.session_factory.begin() as session:
        await session.execute(
            text("UPDATE runs SET status='failed', finished_at=:finished_at WHERE id=:run_id"),
            {"finished_at": NOW, "run_id": scan.run_id},
        )
        current_scan, changed = await compare_and_set_audit_scan(
            session,
            current_scan,
            converged,
            allow_run_convergence=True,
        )
        assert changed is True
    closed = converged.record_closure(AuditClosureStatus.FAILED)
    current_scan, _ = await audit_repository.compare_and_set(current_scan, closed)
    sealing = closed.transition_to(
        AuditLifecycleStatus.SEALING_CORE,
        at=NOW + timedelta(seconds=1),
    )
    current_scan, _ = await audit_repository.compare_and_set(current_scan, sealing)
    publication_failed = sealing.record_publication_failure(AuditPublicationStatus.SEAL_FAILED)
    current_scan, _ = await audit_repository.compare_and_set(
        current_scan,
        publication_failed,
    )
    terminal_scan = publication_failed.transition_to(AuditLifecycleStatus.FAILED)
    terminal_stored, _ = await audit_repository.compare_and_set(current_scan, terminal_scan)
    with pytest.raises(RepositoryConflictError):
        await audit_repository.compare_and_set(terminal_stored, scan)

    claimed = intent.transition_to(
        AuditStartIntentStatus.CLAIMED,
        at=NOW + timedelta(seconds=1),
        lease_owner="dispatcher-1",
        lease_expires_at=NOW + timedelta(minutes=1),
    )
    intent_stored, _ = await intent_repository.compare_and_set(intent_stored, claimed)
    started = claimed.transition_to(
        AuditStartIntentStatus.STARTED,
        at=NOW + timedelta(seconds=2),
    )
    intent_stored, _ = await intent_repository.compare_and_set(intent_stored, started)
    with pytest.raises(RepositoryConflictError):
        await intent_repository.compare_and_set(intent_stored, intent)

    phase = _phase_run(scan.id)
    phase_stored, _ = await phase_repository.create(phase)
    running_phase = phase.transition_to(
        AuditPhaseRunStatus.RUNNING,
        at=NOW + timedelta(seconds=1),
    )
    phase_stored, _ = await phase_repository.compare_and_set(phase_stored, running_phase)
    completed_phase = running_phase.transition_to(
        AuditPhaseRunStatus.COMPLETED,
        at=NOW + timedelta(seconds=2),
    )
    phase_stored, _ = await phase_repository.compare_and_set(
        phase_stored,
        completed_phase,
    )
    with pytest.raises(RepositoryConflictError):
        await phase_repository.compare_and_set(phase_stored, phase)

    scope = _scope(scan.id, "snapshot-1")
    scope_stored, _ = await scope_repository.create(scope)
    analyzed_scope = scope.transition_to(
        AuditScopeStatus.ANALYZED,
        closure_code="analyzed",
        closure_reason="Scope analysis completed.",
        receipt_count=1,
        at=NOW + timedelta(seconds=1),
    )
    scope_stored, _ = await scope_repository.compare_and_set(scope_stored, analyzed_scope)
    with pytest.raises(RepositoryConflictError):
        await scope_repository.compare_and_set(scope_stored, scope)

    work = _work(scan.id, scope.id)
    await _create_coverage_plan(database, work, run_id=scan.run_id)
    work_stored, _ = await work_repository.create(work)
    leased_work = work.transition_to(
        AuditWorkStatus.LEASED,
        at=NOW + timedelta(seconds=1),
        lease_owner="worker-1",
        lease_expires_at=NOW + timedelta(minutes=1),
    )
    work_stored, _ = await work_repository.compare_and_set(work_stored, leased_work)
    running_work = leased_work.transition_to(
        AuditWorkStatus.RUNNING,
        at=NOW + timedelta(seconds=2),
    )
    work_stored, _ = await work_repository.compare_and_set(work_stored, running_work)
    completed_work = running_work.transition_to(
        AuditWorkStatus.COMPLETED,
        at=NOW + timedelta(seconds=3),
        receipt_id="receipt-1",
    )
    work_stored, _ = await work_repository.compare_and_set(work_stored, completed_work)
    with pytest.raises(RepositoryConflictError):
        await work_repository.compare_and_set(work_stored, work)
    await database.dispose()


async def test_raw_sql_tampering_fails_closed_without_leaking_contract_or_paths(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'audit-tamper.db'}")
    await database.create_schema()
    await _create_engagement(database, "engagement-1")
    await SQLAlchemyAuditProjectRepository(database.session_factory).create(_project())
    snapshot = _snapshot()
    await SQLAlchemySnapshotRepository(database.session_factory).create(snapshot)
    _, record, scan = await _create_audit(database)
    audits = SQLAlchemyAuditRepository(database.session_factory)
    contracts = SQLAlchemyAuditContractRepository(database.session_factory)
    snapshots = SQLAlchemySnapshotRepository(database.session_factory)
    secret_path = "/srv/private/customer-repository"
    forged_contract = f'{{"repository_path":"{secret_path}"}}'

    async with database.engine.begin() as connection:
        await connection.execute(
            text(
                "UPDATE audit_contracts SET canonical_contract_json=:payload "
                "WHERE contract_id=:contract_id"
            ),
            {"payload": forged_contract, "contract_id": record.contract_id},
        )
    for contract_or_audit_read in (
        contracts.get(scan.id),
        audits.get(scan.id),
        audits.list(),
    ):
        with pytest.raises(RepositoryIntegrityError) as raised:
            await contract_or_audit_read
        assert secret_path not in str(raised.value)
        assert forged_contract not in str(raised.value)

    async with database.engine.begin() as connection:
        await connection.execute(
            text(
                "UPDATE audit_contracts SET canonical_contract_json=:payload "
                "WHERE contract_id=:contract_id"
            ),
            {"payload": record.canonical_contract_json, "contract_id": record.contract_id},
        )
        await connection.execute(
            text("UPDATE source_snapshots SET tree_digest=:digest WHERE id=:snapshot_id"),
            {"digest": _digest("forged-tree"), "snapshot_id": snapshot.id},
        )
    for snapshot_read in (
        snapshots.get(snapshot.project_id, snapshot.id),
        snapshots.list(snapshot.project_id),
    ):
        with pytest.raises(RepositoryIntegrityError) as raised:
            await snapshot_read
        assert snapshot.content_storage_key not in str(raised.value)

    async with database.engine.begin() as connection:
        await connection.execute(
            text("UPDATE source_snapshots SET tree_digest=:digest WHERE id=:snapshot_id"),
            {"digest": snapshot.tree_digest, "snapshot_id": snapshot.id},
        )
        await connection.execute(
            text("UPDATE audit_scans SET current_phase='map_scope' WHERE id=:audit_id"),
            {"audit_id": scan.id},
        )
    for scan_read in (audits.get(scan.id), audits.list()):
        with pytest.raises(RepositoryIntegrityError):
            await scan_read
    await database.dispose()


def _published_scan(scan: AuditScan) -> AuditScan:
    current = scan.transition_to(
        AuditLifecycleStatus.QUEUED,
        at=NOW + timedelta(seconds=1),
    )
    current = current.transition_to(AuditLifecycleStatus.PREFLIGHTING)
    current = current.transition_to(AuditLifecycleStatus.SNAPSHOTTING)
    current = current.transition_to(AuditLifecycleStatus.RUNNING)
    for phase in (
        AuditPhase.DETERMINISTIC_PROBE,
        AuditPhase.THREAT_MODEL,
        AuditPhase.AGENT_HUNT,
        AuditPhase.RECONCILE,
        AuditPhase.PROVE,
        AuditPhase.COMPOSE_RISK,
        AuditPhase.COMPARE_BASELINE,
        AuditPhase.VALIDATE_CLOSURE,
    ):
        current = current.transition_phase_to(phase)
    current = current.transition_to(
        AuditLifecycleStatus.FINALIZING,
        terminal_outcome=AuditTerminalOutcome.COMPLETE,
    )
    current = current.transition_to(AuditLifecycleStatus.CLEANING)
    current = current.record_cleanup_convergence(
        cleanup_proof_digest=_digest("published-cleanup"),
        run_terminal_status=RunStatus.COMPLETED,
    )
    current = current.record_closure(AuditClosureStatus.COMPLETE_UNDER_DECLARED_SCOPE)
    current = current.transition_to(
        AuditLifecycleStatus.SEALING_CORE,
        at=NOW + timedelta(minutes=1),
    )
    current = current.record_core_seal(
        core_seal_root=_digest("published-core"),
        at=NOW + timedelta(minutes=2),
    )
    current = current.transition_to(AuditLifecycleStatus.REPORTING)
    current = current.transition_to(AuditLifecycleStatus.PACKAGING)
    current = current.record_distribution_revision(
        revision_id="distribution-1",
        at=NOW + timedelta(minutes=3),
    )
    return current.transition_to(AuditLifecycleStatus.COMPLETED)


async def test_aud506_distribution_facts_are_fenced_on_write_and_read(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'audit-publication-fence.db'}")
    await database.create_schema()
    await _create_engagement(database, "engagement-1")
    await SQLAlchemyAuditProjectRepository(database.session_factory).create(_project())
    await SQLAlchemySnapshotRepository(database.session_factory).create(_snapshot())
    await _create_run(database, "run-published")
    contract = _contract("audit-published")
    record = _contract_record(contract, sealed_at=NOW)
    draft = _scan(
        contract,
        record,
        run_id="run-published",
        snapshot_id="snapshot-1",
    )
    published = _published_scan(draft)
    audits = SQLAlchemyAuditRepository(database.session_factory)

    with pytest.raises(RepositoryConflictError, match="AUD-506"):
        await audits.create(published, record)
    async with database.engine.connect() as connection:
        assert (
            await connection.scalar(
                text("SELECT count(*) FROM audit_scans WHERE id=:audit_id"),
                {"audit_id": published.id},
            )
            == 0
        )

    _, _, persisted = await _create_audit(
        database,
        audit_id="audit-tampered-publication",
        run_id="run-tampered-publication",
    )
    async with database.engine.begin() as connection:
        await connection.execute(
            text(
                "UPDATE audit_scans SET publication_status='published', "
                "core_seal_root=:core, initial_distribution_revision_id='revision-1', "
                "latest_distribution_revision_id='revision-1', sealed_at=:sealed_at, "
                "publication_finished_at=:finished_at WHERE id=:audit_id"
            ),
            {
                "core": _digest("forged-core"),
                "sealed_at": NOW + timedelta(minutes=1),
                "finished_at": NOW + timedelta(minutes=2),
                "audit_id": persisted.id,
            },
        )
    with pytest.raises(RepositoryIntegrityError) as raised:
        await audits.get(persisted.id)
    assert raised.value.reason_code == "unsupported_publication_facts"
    await database.dispose()


async def test_scope_natural_key_replay_cannot_lower_the_persisted_risk_floor(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'scope-risk-floor.db'}")
    await database.create_schema()
    await _create_engagement(database, "engagement-1")
    await SQLAlchemyAuditProjectRepository(database.session_factory).create(_project())
    await SQLAlchemySnapshotRepository(database.session_factory).create(_snapshot())
    _, _, scan = await _create_audit(database)
    scopes = SQLAlchemyAuditScopeRepository(database.session_factory)
    high_risk = _replace(
        _scope(scan.id, "snapshot-1", scope_id="scope-high"),
        risk_tier=AuditRiskTier.HIGH,
    )
    stored, created = await scopes.create(high_risk)
    assert created is True
    lower_replay = _replace(
        high_risk,
        id="scope-lower-replay",
        risk_tier=AuditRiskTier.LOW,
        created_at=NOW + timedelta(seconds=1),
        updated_at=NOW + timedelta(seconds=1),
    )

    replayed, created = await scopes.create(lower_replay)

    assert created is False
    assert replayed == stored
    persisted = await scopes.get(scan.id, high_risk.id)
    assert persisted == stored
    assert persisted.value.risk_tier is AuditRiskTier.HIGH
    higher_replay = _replace(
        high_risk,
        id="scope-higher-replay",
        risk_tier=AuditRiskTier.CRITICAL,
    )
    changed_input_replay = _replace(
        high_risk,
        id="scope-changed-input-replay",
        required_analyses=("native_rules",),
    )
    for conflicting_replay in (higher_replay, changed_input_replay):
        with pytest.raises(RepositoryConflictError):
            await scopes.create(conflicting_replay)
    await database.dispose()


async def test_snapshot_natural_key_replay_ignores_generated_identity_only(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'snapshot-replay.db'}")
    await database.create_schema()
    await _create_engagement(database, "engagement-1")
    await SQLAlchemyAuditProjectRepository(database.session_factory).create(_project())
    snapshots = SQLAlchemySnapshotRepository(database.session_factory)
    original = _snapshot()
    await snapshots.create(original)
    generated_identity_replay = _replace(
        original,
        id="snapshot-replayed-id",
        created_at=NOW + timedelta(minutes=1),
        sealed_at=NOW + timedelta(minutes=1, seconds=1),
    )

    persisted, created = await snapshots.create(generated_identity_replay)

    assert created is False
    assert persisted == original
    materially_changed = _replace(
        generated_identity_replay,
        id="snapshot-conflicting-id",
        content_storage_key="cas/source/different-materialization",
    )
    with pytest.raises(RepositoryConflictError):
        await snapshots.create(materially_changed)
    await database.dispose()


async def test_audit_cas_rejects_cross_stage_jumps_and_merged_ordered_facts(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'audit-cas-steps.db'}")
    await database.create_schema()
    await _create_engagement(database, "engagement-1")
    await SQLAlchemyAuditProjectRepository(database.session_factory).create(_project())
    await SQLAlchemySnapshotRepository(database.session_factory).create(_snapshot())
    _, _, queued_scan = await _create_audit(database, queued=True)
    audits = SQLAlchemyAuditRepository(database.session_factory)
    current = await audits.get(queued_scan.id)
    assert current is not None

    two_lifecycle_steps = queued_scan.transition_to(
        AuditLifecycleStatus.PREFLIGHTING
    ).transition_to(AuditLifecycleStatus.SNAPSHOTTING)
    with pytest.raises(RepositoryConflictError):
        await audits.compare_and_set(current, two_lifecycle_steps)
    assert await audits.get(queued_scan.id) == current

    _, draft_record, draft = await _create_audit(
        database,
        audit_id="audit-merged-bind",
        run_id="run-merged-bind",
        snapshot_id=None,
    )
    contracts = SQLAlchemyAuditContractRepository(database.session_factory)
    stored_contract = await contracts.get(draft.id)
    assert stored_contract is not None
    await contracts.compare_and_set(
        stored_contract,
        draft_record.seal(at=NOW),
    )
    stored_draft = await audits.get(draft.id)
    assert stored_draft is not None
    bind_and_queue = draft.bind_snapshots(snapshot_id="snapshot-1").transition_to(
        AuditLifecycleStatus.QUEUED,
        at=NOW + timedelta(seconds=1),
    )
    with pytest.raises(RepositoryConflictError):
        await audits.compare_and_set(stored_draft, bind_and_queue)
    assert await audits.get(draft.id) == stored_draft

    for target in (
        AuditLifecycleStatus.PREFLIGHTING,
        AuditLifecycleStatus.SNAPSHOTTING,
        AuditLifecycleStatus.RUNNING,
    ):
        replacement = current.value.transition_to(target)
        current, changed = await audits.compare_and_set(current, replacement)
        assert changed is True
    two_phase_steps = current.value.transition_phase_to(
        AuditPhase.DETERMINISTIC_PROBE
    ).transition_phase_to(AuditPhase.THREAT_MODEL)
    with pytest.raises(RepositoryConflictError):
        await audits.compare_and_set(current, two_phase_steps)
    assert await audits.get(queued_scan.id) == current
    await database.dispose()


@pytest.mark.parametrize("binding", ["same_run", "missing", "cross_run", "mixed"])
async def test_phase_terminal_outputs_require_same_run_artifacts(
    tmp_path: Path,
    binding: str,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / f'phase-output-{binding}.db'}")
    await database.create_schema()
    await _create_engagement(database, "engagement-1")
    await SQLAlchemyAuditProjectRepository(database.session_factory).create(_project())
    await SQLAlchemySnapshotRepository(database.session_factory).create(_snapshot())
    _, _, scan = await _create_audit(database)
    phases = SQLAlchemyAuditPhaseRepository(database.session_factory)
    phase = _phase_run(scan.id)
    current, _ = await phases.create(phase)
    running = phase.transition_to(
        AuditPhaseRunStatus.RUNNING,
        at=NOW + timedelta(seconds=1),
    )
    current, _ = await phases.compare_and_set(current, running)
    output_ids: tuple[str, ...]
    if binding == "missing":
        output_ids = ("phase-output-missing",)
    elif binding == "mixed":
        output_ids = ("phase-output-missing", "phase-output-valid")
        await SQLAlchemyArtifactRepository(database.session_factory).create(
            _phase_output(
                phase,
                artifact_id="phase-output-valid",
                run_id=scan.run_id,
            )
        )
    else:
        artifact_run_id = scan.run_id
        if binding == "cross_run":
            artifact_run_id = "run-other"
            await _create_run(database, artifact_run_id)
        output_ids = ("phase-output",)
        artifact = _phase_output(
            phase,
            artifact_id=output_ids[0],
            run_id=artifact_run_id,
        )
        artifacts = SQLAlchemyArtifactRepository(database.session_factory)
        if binding == "cross_run":
            with pytest.raises(RepositoryConflictError):
                await artifacts.create(artifact)
            await _insert_corrupt_artifact_owner(database, artifact)
        else:
            await artifacts.create(artifact)
    completed = running.transition_to(
        AuditPhaseRunStatus.COMPLETED,
        at=NOW + timedelta(seconds=2),
        output_artifact_ids=output_ids,
        summary_counts=(AuditSummaryCount(key="outputs", count=len(output_ids)),),
    )

    if binding == "same_run":
        terminal, changed = await phases.compare_and_set(current, completed)
        assert changed is True
        assert terminal.value == completed
        assert await phases.get(scan.id, phase.id) == terminal
    else:
        with pytest.raises(RepositoryConflictError):
            await phases.compare_and_set(current, completed)
        assert await phases.get(scan.id, phase.id) == current
    await database.dispose()


@pytest.mark.parametrize("tamper", ["missing", "cross_run"])
async def test_raw_phase_output_binding_tamper_fails_all_reads_and_recovery(
    tmp_path: Path,
    tamper: str,
) -> None:
    database_path = tmp_path / f"phase-output-tamper-{tamper}.db"
    database = Database(f"sqlite+aiosqlite:///{database_path}")
    await database.create_schema()
    await _create_engagement(database, "engagement-1")
    await SQLAlchemyAuditProjectRepository(database.session_factory).create(_project())
    await SQLAlchemySnapshotRepository(database.session_factory).create(_snapshot())
    _, _, scan = await _create_audit(database)
    phases = SQLAlchemyAuditPhaseRepository(database.session_factory)
    phase = _phase_run(scan.id)
    current, _ = await phases.create(phase)
    running = phase.transition_to(
        AuditPhaseRunStatus.RUNNING,
        at=NOW + timedelta(seconds=1),
    )
    current, _ = await phases.compare_and_set(current, running)
    artifact = _phase_output(
        phase,
        artifact_id="phase-output",
        run_id=scan.run_id,
    )
    await SQLAlchemyArtifactRepository(database.session_factory).create(artifact)
    completed = running.transition_to(
        AuditPhaseRunStatus.COMPLETED,
        at=NOW + timedelta(seconds=2),
        output_artifact_ids=(artifact.id,),
    )
    terminal, _ = await phases.compare_and_set(current, completed)
    if tamper == "missing":
        statement = "DELETE FROM artifacts WHERE id=:artifact_id"
    else:
        await _create_run(database, "run-other")
        statement = (
            "UPDATE artifacts SET run_id='run-other', "
            "storage_key='runs/run-other/artifacts/phase-output/Phase output phase-output' "
            "WHERE id=:artifact_id"
        )
    async with database.engine.begin() as connection:
        await connection.execute(text(statement), {"artifact_id": artifact.id})

    failures = await _missing_integrity_failures(
        (
            ("get", lambda: phases.get(scan.id, phase.id)),
            ("list", lambda: phases.list(scan.id)),
            ("create_replay", lambda: phases.create(phase)),
            (
                "stale_or_exact_cas",
                lambda: phases.compare_and_set(terminal, terminal.value),
            ),
        )
    )
    await database.dispose()
    reopened = Database(f"sqlite+aiosqlite:///{database_path}")
    reopened_phases = SQLAlchemyAuditPhaseRepository(reopened.session_factory)
    failures.extend(
        await _missing_integrity_failures(
            (
                ("reopen_get", lambda: reopened_phases.get(scan.id, phase.id)),
                ("reopen_list", lambda: reopened_phases.list(scan.id)),
                ("reopen_create_replay", lambda: reopened_phases.create(phase)),
            )
        )
    )
    assert failures == []
    await reopened.dispose()


@pytest.mark.parametrize(
    ("column", "payload"),
    [
        ("output_artifact_ids_json", '["phase-output-active"]'),
        ("summary_counts_json", '[{"key":"active","count":1}]'),
    ],
)
async def test_active_phase_output_facts_are_db_rejected_and_mapper_fail_closed(
    tmp_path: Path,
    column: str,
    payload: str,
) -> None:
    database_path = tmp_path / f"active-phase-output-{column}.db"
    database = Database(f"sqlite+aiosqlite:///{database_path}")
    await database.create_schema()
    await _create_engagement(database, "engagement-1")
    await SQLAlchemyAuditProjectRepository(database.session_factory).create(_project())
    await SQLAlchemySnapshotRepository(database.session_factory).create(_snapshot())
    _, _, scan = await _create_audit(database)
    phase = _phase_run(scan.id)
    phases = SQLAlchemyAuditPhaseRepository(database.session_factory)
    await phases.create(phase)
    update_statement = text(
        f"UPDATE audit_phase_runs SET {column}=:payload WHERE id=:phase_id"
    )
    with pytest.raises(IntegrityError):
        async with database.engine.begin() as connection:
            await connection.execute(
                update_statement,
                {"payload": payload, "phase_id": phase.id},
            )
    async with database.engine.connect() as connection:
        await connection.exec_driver_sql("PRAGMA ignore_check_constraints=ON")
        try:
            await connection.execute(
                update_statement,
                {"payload": payload, "phase_id": phase.id},
            )
            await connection.commit()
        finally:
            await connection.exec_driver_sql("PRAGMA ignore_check_constraints=OFF")

    failures = await _missing_integrity_failures(
        (
            ("get", lambda: phases.get(scan.id, phase.id)),
            ("list", lambda: phases.list(scan.id)),
        )
    )
    await database.dispose()
    reopened = Database(f"sqlite+aiosqlite:///{database_path}")
    reopened_phases = SQLAlchemyAuditPhaseRepository(reopened.session_factory)
    failures.extend(
        await _missing_integrity_failures(
            (
                ("reopen_get", lambda: reopened_phases.get(scan.id, phase.id)),
                ("reopen_list", lambda: reopened_phases.list(scan.id)),
            )
        )
    )
    assert failures == []
    await reopened.dispose()


@pytest.mark.parametrize("failure", ["missing", "cross_run", "digest"])
async def test_work_creation_requires_a_same_run_digest_bound_coverage_plan(
    tmp_path: Path,
    failure: str,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / f'work-plan-create-{failure}.db'}")
    await database.create_schema()
    await _create_engagement(database, "engagement-1")
    await SQLAlchemyAuditProjectRepository(database.session_factory).create(_project())
    await SQLAlchemySnapshotRepository(database.session_factory).create(_snapshot())
    _, _, scan = await _create_audit(database)
    scope = _scope(scan.id, "snapshot-1")
    await SQLAlchemyAuditScopeRepository(database.session_factory).create(scope)
    work = _work(scan.id, scope.id)
    if failure != "missing":
        artifact_run_id = scan.run_id
        if failure == "cross_run":
            artifact_run_id = "run-other"
            await _create_run(database, artifact_run_id)
        artifact = _coverage_plan(work, run_id=artifact_run_id)
        if failure == "digest":
            artifact = _replace(artifact, sha256=_digest("wrong-coverage-plan"))
        artifacts = SQLAlchemyArtifactRepository(database.session_factory)
        if failure == "cross_run":
            with pytest.raises(RepositoryConflictError):
                await artifacts.create(artifact)
            await _insert_corrupt_artifact_owner(database, artifact)
        else:
            await artifacts.create(artifact)

    with pytest.raises(RepositoryConflictError):
        await SQLAlchemyAuditWorkRepository(database.session_factory).create(work)
    await database.dispose()


@pytest.mark.parametrize("tamper", ["missing", "cross_run", "digest"])
async def test_raw_coverage_plan_tampering_fails_work_get_list_and_reopen(
    tmp_path: Path,
    tamper: str,
) -> None:
    database_path = tmp_path / f"work-plan-tamper-{tamper}.db"
    database = Database(f"sqlite+aiosqlite:///{database_path}")
    await database.create_schema()
    await _create_engagement(database, "engagement-1")
    await SQLAlchemyAuditProjectRepository(database.session_factory).create(_project())
    await SQLAlchemySnapshotRepository(database.session_factory).create(_snapshot())
    _, _, scan = await _create_audit(database)
    scope = _scope(scan.id, "snapshot-1")
    await SQLAlchemyAuditScopeRepository(database.session_factory).create(scope)
    work = _work(scan.id, scope.id)
    artifact = await _create_coverage_plan(database, work, run_id=scan.run_id)
    works = SQLAlchemyAuditWorkRepository(database.session_factory)
    await works.create(work)
    if tamper == "missing":
        statement = "DELETE FROM artifacts WHERE id=:artifact_id"
        parameters: dict[str, object] = {"artifact_id": artifact.id}
    elif tamper == "cross_run":
        await _create_run(database, "run-other")
        statement = (
            "UPDATE artifacts SET run_id='run-other', "
            "storage_key='runs/run-other/artifacts/coverage-work-1/"
            "Coverage plan for work-1' WHERE id=:artifact_id"
        )
        parameters = {"artifact_id": artifact.id}
    else:
        statement = "UPDATE artifacts SET sha256=:digest WHERE id=:artifact_id"
        parameters = {"artifact_id": artifact.id, "digest": _digest("tampered-plan")}
    async with database.engine.begin() as connection:
        await connection.execute(text(statement), parameters)

    failures = await _missing_integrity_failures(
        (
            ("get", lambda: works.get(scan.id, work.id)),
            ("list", lambda: works.list(scan.id)),
        )
    )
    await database.dispose()
    reopened = Database(f"sqlite+aiosqlite:///{database_path}")
    reopened_works = SQLAlchemyAuditWorkRepository(reopened.session_factory)
    failures.extend(
        await _missing_integrity_failures(
            (
                ("reopen_get", lambda: reopened_works.get(scan.id, work.id)),
                ("reopen_list", lambda: reopened_works.list(scan.id)),
            )
        )
    )
    assert failures == []
    await reopened.dispose()


async def test_raw_orphan_contract_is_global_fail_closed_but_project_scoped_hidden(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "orphan-contract.db"
    database = Database(f"sqlite+aiosqlite:///{database_path}")
    await database.create_schema()
    await _create_engagement(database, "engagement-1")
    await SQLAlchemyAuditProjectRepository(database.session_factory).create(_project())
    orphan = _contract_record(_contract("audit-orphan"))
    await _insert_orphan_contract(database, orphan)
    audits = SQLAlchemyAuditRepository(database.session_factory)

    assert await audits.get(orphan.audit_id, project_id="project-1") is None
    assert list(await audits.list(project_id="project-1")) == []
    failures = await _missing_integrity_failures(
        (
            ("unscoped_get", lambda: audits.get(orphan.audit_id)),
            ("unscoped_list", audits.list),
        )
    )
    await database.dispose()

    reopened = Database(f"sqlite+aiosqlite:///{database_path}")
    reopened_audits = SQLAlchemyAuditRepository(reopened.session_factory)
    assert list(await reopened_audits.list(project_id="project-1")) == []
    failures.extend(
        await _missing_integrity_failures(
            (
                ("reopen_unscoped_get", lambda: reopened_audits.get(orphan.audit_id)),
                ("reopen_unscoped_list", reopened_audits.list),
            )
        )
    )
    assert failures == []
    await reopened.dispose()


async def test_orphan_contract_integrity_error_redacts_a_hostile_raw_identifier(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'orphan-id-redaction.db'}")
    await database.create_schema()
    orphan = _contract_record(_contract("audit-hostile-orphan"))
    await _insert_orphan_contract(database, orphan)
    hostile_id = "/srv/private/customer-repository/contract"
    async with database.engine.begin() as connection:
        await connection.execute(
            text(
                "UPDATE audit_contracts SET contract_id=:hostile_id "
                "WHERE contract_id=:contract_id"
            ),
            {"hostile_id": hostile_id, "contract_id": orphan.contract_id},
        )
    audits = SQLAlchemyAuditRepository(database.session_factory)

    for orphan_read in (audits.get(orphan.audit_id), audits.list()):
        with pytest.raises(RepositoryIntegrityError) as raised:
            await orphan_read
        assert hostile_id not in str(raised.value)
        assert raised.value.entity_id == "invalid-id"
    await database.dispose()


async def test_project_read_requires_its_engagement_root_owner(tmp_path: Path) -> None:
    database_path = tmp_path / "project-root-owner.db"
    database = Database(f"sqlite+aiosqlite:///{database_path}")
    await database.create_schema()
    await _create_engagement(database, "engagement-1")
    project = _project()
    await SQLAlchemyAuditProjectRepository(database.session_factory).create(project)
    await _raw_write_without_foreign_keys(
        database,
        "UPDATE audit_projects SET engagement_id='engagement-missing' WHERE id=:project_id",
        {"project_id": project.id},
    )
    projects = SQLAlchemyAuditProjectRepository(database.session_factory)
    failures = await _missing_integrity_failures(
        (
            ("get", lambda: projects.get(project.id)),
            (
                "identity",
                lambda: projects.get_by_identity(
                    project.repository_identity_digest,
                    engagement_id="engagement-missing",
                ),
            ),
            ("list", projects.list),
        )
    )
    await database.dispose()

    reopened = Database(f"sqlite+aiosqlite:///{database_path}")
    reopened_projects = SQLAlchemyAuditProjectRepository(reopened.session_factory)
    failures.extend(
        await _missing_integrity_failures(
            (
                ("reopen_get", lambda: reopened_projects.get(project.id)),
                ("reopen_list", reopened_projects.list),
            )
        )
    )
    assert failures == []
    await reopened.dispose()


async def test_audit_creation_rejects_run_bound_to_a_different_selected_node(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'selected-node-create.db'}")
    await database.create_schema()
    await _create_engagement(database, "engagement-1")
    await SQLAlchemyAuditProjectRepository(database.session_factory).create(_project())
    await SQLAlchemySnapshotRepository(database.session_factory).create(_snapshot())
    await _create_run(database, "run-wrong-node", node_id="other-node")
    contract = _contract("audit-wrong-node")
    record = _contract_record(contract)
    scan = _scan(
        contract,
        record,
        run_id="run-wrong-node",
        snapshot_id="snapshot-1",
    )

    with pytest.raises(RepositoryConflictError):
        await SQLAlchemyAuditRepository(database.session_factory).create(scan, record)

    async with database.engine.connect() as connection:
        assert (
            await connection.scalar(
                text("SELECT count(*) FROM audit_scans WHERE id=:audit_id"),
                {"audit_id": scan.id},
            )
            == 0
        )
        assert (
            await connection.scalar(
                text("SELECT count(*) FROM audit_contracts WHERE audit_id=:audit_id"),
                {"audit_id": scan.id},
            )
            == 0
        )
    await database.dispose()


async def test_audit_creation_rejects_other_run_projection_mismatches(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'run-projection-create.db'}")
    await database.create_schema()
    await _create_engagement(database, "engagement-1")
    await SQLAlchemyAuditProjectRepository(database.session_factory).create(_project())
    await _create_run(database, "run-model", model_profile="other-model-profile")
    await _create_run(
        database,
        "run-workflow",
        temporal_workflow_id="riftx-code-audit-other-audit",
    )
    await _create_run(database, "run-terminal")
    async with database.engine.begin() as connection:
        await connection.execute(
            text(
                "UPDATE runs SET status='failed', finished_at=:finished_at "
                "WHERE id='run-terminal'"
            ),
            {"finished_at": NOW},
        )
    audits = SQLAlchemyAuditRepository(database.session_factory)

    for audit_id, run_id in (
        ("audit-model", "run-model"),
        ("audit-workflow", "run-workflow"),
        ("audit-terminal", "run-terminal"),
    ):
        contract = _contract(audit_id)
        record = _contract_record(contract)
        scan = _scan(contract, record, run_id=run_id)
        with pytest.raises(RepositoryConflictError):
            await audits.create(scan, record)

    async with database.engine.connect() as connection:
        assert (await connection.scalar(text("SELECT count(*) FROM audit_scans"))) == 0
        assert (await connection.scalar(text("SELECT count(*) FROM audit_contracts"))) == 0
    await database.dispose()


async def test_raw_run_node_tamper_fails_audit_get_list_and_reopen(tmp_path: Path) -> None:
    database_path = tmp_path / "selected-node-read.db"
    database = Database(f"sqlite+aiosqlite:///{database_path}")
    await database.create_schema()
    await _create_engagement(database, "engagement-1")
    await SQLAlchemyAuditProjectRepository(database.session_factory).create(_project())
    await SQLAlchemySnapshotRepository(database.session_factory).create(_snapshot())
    _, _, scan = await _create_audit(database)
    await _raw_write_without_foreign_keys(
        database,
        "UPDATE runs SET node_id='other-node' WHERE id=:run_id",
        {"run_id": scan.run_id},
    )
    audits = SQLAlchemyAuditRepository(database.session_factory)
    failures = await _missing_integrity_failures(
        (
            ("get", lambda: audits.get(scan.id)),
            ("list", audits.list),
        )
    )
    await database.dispose()

    reopened = Database(f"sqlite+aiosqlite:///{database_path}")
    reopened_audits = SQLAlchemyAuditRepository(reopened.session_factory)
    failures.extend(
        await _missing_integrity_failures(
            (
                ("reopen_get", lambda: reopened_audits.get(scan.id)),
                ("reopen_list", reopened_audits.list),
            )
        )
    )
    assert failures == []
    await reopened.dispose()


@pytest.mark.parametrize(
    "relation",
    ["snapshot_id", "base_snapshot_id", "parent_audit_id", "baseline_audit_id"],
)
@pytest.mark.parametrize("target_kind", ["cross_project", "missing"])
async def test_raw_scan_relation_tamper_fails_get_list_and_reopen(
    tmp_path: Path,
    relation: str,
    target_kind: str,
) -> None:
    database_path = tmp_path / f"scan-{relation}-{target_kind}.db"
    database = Database(f"sqlite+aiosqlite:///{database_path}")
    await database.create_schema()
    await _create_engagement(database, "engagement-1")
    projects = SQLAlchemyAuditProjectRepository(database.session_factory)
    snapshots = SQLAlchemySnapshotRepository(database.session_factory)
    await projects.create(_project("project-1"))
    await projects.create(_project("project-2"))
    await snapshots.create(_snapshot("snapshot-head", project_id="project-1"))
    await snapshots.create(_snapshot("snapshot-base", project_id="project-1"))
    await snapshots.create(_snapshot("snapshot-foreign", project_id="project-2"))
    _, _, foreign_audit = await _create_audit(
        database,
        audit_id="audit-foreign",
        run_id="run-foreign",
        project_id="project-2",
        snapshot_id="snapshot-foreign",
    )
    _, _, related_audit = await _create_audit(
        database,
        audit_id="audit-related",
        run_id="run-related",
        project_id="project-1",
        snapshot_id="snapshot-head",
    )
    create_arguments: dict[str, object] = {
        "audit_id": "audit-target",
        "run_id": "run-target",
        "project_id": "project-1",
        "snapshot_id": "snapshot-head",
    }
    if relation == "base_snapshot_id":
        create_arguments.update(
            base_snapshot_id="snapshot-base",
            mode=AuditMode.DIFF,
        )
    elif relation == "parent_audit_id":
        create_arguments.update(
            parent_audit_id=related_audit.id,
            purpose=AuditPurpose.RETEST,
        )
    elif relation == "baseline_audit_id":
        create_arguments["baseline_audit_id"] = related_audit.id
    _, _, target_scan = await _create_audit(database, **create_arguments)  # type: ignore[arg-type]

    if relation in {"snapshot_id", "base_snapshot_id"}:
        cross_project_target = "snapshot-foreign"
    else:
        cross_project_target = foreign_audit.id
    tampered_target = (
        cross_project_target if target_kind == "cross_project" else f"missing-{relation}"
    )
    await _raw_write_without_foreign_keys(
        database,
        f"UPDATE audit_scans SET {relation}=:target WHERE id=:audit_id",
        {"target": tampered_target, "audit_id": target_scan.id},
    )
    audits = SQLAlchemyAuditRepository(database.session_factory)
    failures = await _missing_integrity_failures(
        (
            ("get", lambda: audits.get(target_scan.id)),
            ("list", lambda: audits.list(project_id="project-1")),
        )
    )
    await database.dispose()

    reopened = Database(f"sqlite+aiosqlite:///{database_path}")
    reopened_audits = SQLAlchemyAuditRepository(reopened.session_factory)
    failures.extend(
        await _missing_integrity_failures(
            (
                ("reopen_get", lambda: reopened_audits.get(target_scan.id)),
                (
                    "reopen_list",
                    lambda: reopened_audits.list(project_id="project-1"),
                ),
            )
        )
    )
    assert failures == []
    await reopened.dispose()


async def test_auto_commit_audit_cas_cannot_add_run_terminal_convergence(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'auto-cleanup-cas.db'}")
    await database.create_schema()
    await _create_engagement(database, "engagement-1")
    await SQLAlchemyAuditProjectRepository(database.session_factory).create(_project())
    await SQLAlchemySnapshotRepository(database.session_factory).create(_snapshot())
    _, _, scan = await _create_audit(database, queued=True)
    audits = SQLAlchemyAuditRepository(database.session_factory)
    cleaning = await _advance_to_failed_cleaning(audits, scan)
    async with database.engine.begin() as connection:
        await connection.execute(
            text("UPDATE runs SET status='failed', finished_at=:finished_at WHERE id=:run_id"),
            {"finished_at": NOW, "run_id": scan.run_id},
        )
    converged = cleaning.value.record_cleanup_convergence(
        cleanup_proof_digest=_digest("auto-cleanup-proof"),
        run_terminal_status=RunStatus.FAILED,
    )

    with pytest.raises(RepositoryConflictError):
        await audits.compare_and_set(cleaning, converged)

    async with database.engine.begin() as connection:
        await connection.execute(
            text("UPDATE runs SET status='created', finished_at=NULL WHERE id=:run_id"),
            {"run_id": scan.run_id},
        )
    assert await audits.get(scan.id) == cleaning
    await database.dispose()


async def test_session_bound_terminal_convergence_rolls_back_run_and_audit(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'cleanup-uow-rollback.db'}")
    await database.create_schema()
    await _create_engagement(database, "engagement-1")
    await SQLAlchemyAuditProjectRepository(database.session_factory).create(_project())
    await SQLAlchemySnapshotRepository(database.session_factory).create(_snapshot())
    _, _, scan = await _create_audit(database, queued=True)
    audits = SQLAlchemyAuditRepository(database.session_factory)
    cleaning = await _advance_to_failed_cleaning(audits, scan)
    converged = cleaning.value.record_cleanup_convergence(
        cleanup_proof_digest=_digest("rollback-cleanup-proof"),
        run_terminal_status=RunStatus.FAILED,
    )

    with pytest.raises(RuntimeError, match="force cleanup UoW rollback"):
        async with database.session_factory.begin() as session:
            await session.execute(
                text("UPDATE runs SET status='failed', finished_at=:finished_at WHERE id=:run_id"),
                {"finished_at": NOW, "run_id": scan.run_id},
            )
            stored, changed = await compare_and_set_audit_scan(
                session,
                cleaning,
                converged,
                allow_run_convergence=True,
            )
            assert stored.value == converged
            assert changed is True
            raise RuntimeError("force cleanup UoW rollback")

    assert await audits.get(scan.id) == cleaning
    async with database.engine.connect() as connection:
        row = (
            await connection.execute(
                text("SELECT status, finished_at FROM runs WHERE id=:run_id"),
                {"run_id": scan.run_id},
            )
        ).one()
    assert tuple(row) == (RunStatus.CREATED.value, None)
    await database.dispose()


async def test_terminal_publication_retry_is_an_allowed_single_audit_cas(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'publication-retry.db'}")
    await database.create_schema()
    await _create_engagement(database, "engagement-1")
    await SQLAlchemyAuditProjectRepository(database.session_factory).create(_project())
    await SQLAlchemySnapshotRepository(database.session_factory).create(_snapshot())
    _, _, scan = await _create_audit(database, queued=True)
    audits = SQLAlchemyAuditRepository(database.session_factory)
    current = await _advance_to_failed_cleaning(audits, scan)
    converged = current.value.record_cleanup_convergence(
        cleanup_proof_digest=_digest("retry-cleanup-proof"),
        run_terminal_status=RunStatus.FAILED,
    )
    async with database.session_factory.begin() as session:
        await session.execute(
            text("UPDATE runs SET status='failed', finished_at=:finished_at WHERE id=:run_id"),
            {"finished_at": NOW, "run_id": scan.run_id},
        )
        current, changed = await compare_and_set_audit_scan(
            session,
            current,
            converged,
            allow_run_convergence=True,
        )
        assert changed is True
    for replacement in (current.value.record_closure(AuditClosureStatus.FAILED),):
        current, changed = await audits.compare_and_set(current, replacement)
        assert changed is True
    sealing = current.value.transition_to(
        AuditLifecycleStatus.SEALING_CORE,
        at=NOW + timedelta(seconds=1),
    )
    current, _ = await audits.compare_and_set(current, sealing)
    publication_failed = current.value.record_publication_failure(
        AuditPublicationStatus.SEAL_FAILED
    )
    current, _ = await audits.compare_and_set(current, publication_failed)
    terminal = current.value.transition_to(AuditLifecycleStatus.FAILED)
    current, _ = await audits.compare_and_set(current, terminal)
    retry = current.value.begin_publication_retry()

    retried, changed = await audits.compare_and_set(current, retry)

    assert changed is True
    assert retried.state_version == current.state_version + 1
    assert retried.value.lifecycle_status is AuditLifecycleStatus.FAILED
    assert retried.value.publication_status is AuditPublicationStatus.SEALING_CORE
    assert retried.value.current_phase is AuditPhase.SEAL_CORE
    await database.dispose()


async def test_run_terminal_status_mismatch_fails_audit_get_list_and_reopen(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "run-terminal-mismatch.db"
    database = Database(f"sqlite+aiosqlite:///{database_path}")
    await database.create_schema()
    await _create_engagement(database, "engagement-1")
    await SQLAlchemyAuditProjectRepository(database.session_factory).create(_project())
    await SQLAlchemySnapshotRepository(database.session_factory).create(_snapshot())
    _, _, scan = await _create_audit(database, queued=True)
    audits = SQLAlchemyAuditRepository(database.session_factory)
    cleaning = await _advance_to_failed_cleaning(audits, scan)
    converged = cleaning.value.record_cleanup_convergence(
        cleanup_proof_digest=_digest("mismatch-cleanup-proof"),
        run_terminal_status=RunStatus.FAILED,
    )
    async with database.session_factory.begin() as session:
        await session.execute(
            text("UPDATE runs SET status='failed', finished_at=:finished_at WHERE id=:run_id"),
            {"finished_at": NOW, "run_id": scan.run_id},
        )
        stored, changed = await compare_and_set_audit_scan(
            session,
            cleaning,
            converged,
            allow_run_convergence=True,
        )
        assert stored.value == converged
        assert changed is True
    await _raw_write_without_foreign_keys(
        database,
        "UPDATE runs SET status='completed' WHERE id=:run_id",
        {"run_id": scan.run_id},
    )
    failures = await _missing_integrity_failures(
        (
            ("get", lambda: audits.get(scan.id)),
            ("list", audits.list),
        )
    )
    await database.dispose()

    reopened = Database(f"sqlite+aiosqlite:///{database_path}")
    reopened_audits = SQLAlchemyAuditRepository(reopened.session_factory)
    failures.extend(
        await _missing_integrity_failures(
            (
                ("reopen_get", lambda: reopened_audits.get(scan.id)),
                ("reopen_list", reopened_audits.list),
            )
        )
    )
    assert failures == []
    await reopened.dispose()
