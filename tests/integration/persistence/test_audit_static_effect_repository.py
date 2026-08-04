from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import text
from tests.integration.persistence.test_audit_repositories import (
    NOW,
    _create_audit,
    _create_engagement,
    _digest,
    _project,
    _snapshot,
)
from tests.integration.persistence.test_snapshot_references import _reference

from riftx.application.errors import RepositoryConflictError, RepositoryIntegrityError
from riftx.audit import (
    AuditStaticEffectLimits,
    AuditStaticEffectPlan,
    AuditStaticOperationFamily,
    AuditStaticReadOnlyMount,
    SnapshotMountLease,
    SnapshotMountPin,
    SnapshotMountStopDisposition,
    SnapshotMountStopProof,
    snapshot_storage_key_digest,
)
from riftx.domain import Node, NodeStatus
from riftx.persistence import (
    Database,
    SQLAlchemyAuditProjectRepository,
    SQLAlchemyAuditStaticEffectAuthorityRepository,
    SQLAlchemyNodeRepository,
    SQLAlchemyRunnerCredentialRepository,
    SQLAlchemySnapshotReferenceRepository,
    SQLAlchemySnapshotRepository,
)

_EFFECT_NOW = NOW + timedelta(seconds=10)


def _plan(
    *, node_id: str = "analysis-node", plan_id: str = "static-plan-1"
) -> AuditStaticEffectPlan:
    snapshot = _snapshot()
    return AuditStaticEffectPlan(
        id=plan_id,
        project_id=snapshot.project_id,
        audit_id="audit-1",
        run_id="run-1",
        snapshot_id=snapshot.id,
        snapshot_digest=snapshot.snapshot_digest,
        manifest_digest=snapshot.manifest_digest,
        operation_family=AuditStaticOperationFamily.SNAPSHOT_MOUNT,
        node_id=node_id,
        backend_digest=_digest("private-materialization-backend"),
        image_digest=_digest("static-effect-image"),
        policy_digest=_digest("static-effect-policy"),
        content_storage_key_digest=snapshot_storage_key_digest(
            snapshot.content_storage_key,
            role="content",
        ),
        manifest_storage_key_digest=snapshot_storage_key_digest(
            snapshot.manifest_storage_key,
            role="manifest",
        ),
        read_only_mounts=(
            AuditStaticReadOnlyMount(
                snapshot_id=snapshot.id,
                snapshot_digest=snapshot.snapshot_digest,
                manifest_digest=snapshot.manifest_digest,
            ),
        ),
        unique_output_root_digest=_digest("static-output-root"),
        clean_env_digest=_digest("static-clean-env"),
        limits=AuditStaticEffectLimits(
            cpu_millis=1000,
            memory_bytes=1024 * 1024,
            pids=8,
            wall_seconds=60,
            disk_bytes=8192,
            file_count=64,
            input_bytes=snapshot.total_bytes,
            output_bytes=1024,
        ),
        input_manifest_digest=snapshot.manifest_digest,
        output_contract_digest=_digest("static-output-contract"),
        policy_version="policy-v1",
        created_at=_EFFECT_NOW,
    )


async def _seed(database: Database):
    node = Node(
        id="analysis-node",
        name="Analysis Node",
        platform="linux",
        architecture="x86_64",
        status=NodeStatus.ONLINE,
        created_at=NOW,
        updated_at=NOW,
    )
    await SQLAlchemyNodeRepository(database.session_factory).create(node)
    credential = await SQLAlchemyRunnerCredentialRepository(database.session_factory).issue(
        node.id,
        token_hash=_digest("runner-token"),
        token_prefix="runner",
        issued_at=NOW + timedelta(seconds=1),
        instance_id="runner-instance-1",
    )
    await _create_engagement(database, "engagement-1")
    await SQLAlchemyAuditProjectRepository(database.session_factory).create(_project())
    snapshot = _snapshot()
    await SQLAlchemySnapshotRepository(database.session_factory).create(snapshot)
    await _create_audit(database)
    await SQLAlchemySnapshotReferenceRepository(database.session_factory).add(_reference())
    return credential


def _issue(plan: AuditStaticEffectPlan, principal):
    issue = SnapshotMountLease.issue(
        plan=plan,
        effect_execution_id="static-effect-1",
        target_runner_principal=principal,
        allowed_blob_digests=tuple(sorted((_digest("blob-1"), _digest("blob-2")))),
        max_bytes=4096,
        expires_at=_EFFECT_NOW + timedelta(minutes=5),
        mount_policy_digest=_digest("mount-policy"),
        lease_id="mount-lease-1",
        created_at=_EFFECT_NOW,
    )
    return issue, SnapshotMountPin.for_lease(issue.lease, pin_id="mount-pin-1")


async def test_plan_and_mount_authority_round_trip_and_exact_replay(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'static-authority.db'}")
    await database.create_schema()
    credential = await _seed(database)
    repository = SQLAlchemyAuditStaticEffectAuthorityRepository(database.session_factory)
    plan = _plan()

    assert await repository.create_plan(plan) == (plan, True)
    assert await repository.create_plan(plan) == (plan, False)
    assert await repository.get_plan(plan.id) == plan

    issue, pin = _issue(plan, credential.principal)
    assert await repository.issue_mount(issue, pin) == (issue, pin, True)
    assert await repository.issue_mount(issue, pin) == (issue, pin, False)
    assert await repository.get_mount(issue.lease.id) == (issue.lease, pin)
    assert await repository.list_reconcilable(node_id="analysis-node") == ((issue.lease, pin),)
    await SQLAlchemyRunnerCredentialRepository(database.session_factory).issue(
        "analysis-node",
        token_hash=_digest("rotated-runner-token"),
        token_prefix="rotated",
        issued_at=_EFFECT_NOW + timedelta(seconds=1),
        instance_id="runner-instance-2",
    )
    assert await repository.get_mount(issue.lease.id) == (issue.lease, pin)
    await database.dispose()


async def test_plan_rejects_audit_run_snapshot_node_and_storage_drift(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'static-plan-drift.db'}")
    await database.create_schema()
    await _seed(database)
    await SQLAlchemyNodeRepository(database.session_factory).create(
        Node(
            id="other-node",
            name="Other Node",
            platform="linux",
            architecture="x86_64",
            created_at=NOW,
            updated_at=NOW,
        )
    )
    repository = SQLAlchemyAuditStaticEffectAuthorityRepository(database.session_factory)

    base = _plan()
    foreign_snapshot_mount = base.read_only_mounts[0].model_copy(
        update={"snapshot_id": "foreign-snapshot"}
    )
    for updates in (
        {"node_id": "other-node"},
        {"run_id": "foreign-run"},
        {
            "snapshot_id": "foreign-snapshot",
            "read_only_mounts": (foreign_snapshot_mount,),
        },
        {"content_storage_key_digest": _digest("foreign-storage")},
    ):
        payload = {**base.model_dump(mode="python"), **updates, "plan_digest": ""}
        with pytest.raises(RepositoryConflictError):
            await repository.create_plan(AuditStaticEffectPlan.model_validate(payload))
    await database.dispose()


async def test_mount_lifecycle_cas_and_stop_proof_are_atomic(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'static-lifecycle.db'}")
    await database.create_schema()
    credential = await _seed(database)
    repository = SQLAlchemyAuditStaticEffectAuthorityRepository(database.session_factory)
    plan = _plan()
    await repository.create_plan(plan)
    issue, pending_pin = _issue(plan, credential.principal)
    await repository.issue_mount(issue, pending_pin)

    active_lease = issue.lease.activate(
        mount_key=f"snapshot-mount:v1:{_digest('mount-key')}",
        mount_proof_digest=_digest("mount-proof"),
        activated_at=_EFFECT_NOW + timedelta(seconds=1),
    )
    active_pin = pending_pin.activate(active_lease)
    assert await repository.compare_and_set_mount(
        previous_lease=issue.lease,
        updated_lease=active_lease,
        previous_pin=pending_pin,
        updated_pin=active_pin,
    ) == (active_lease, active_pin, True)

    pending_lease = active_lease.begin_stop(
        expired=False,
        requested_at=_EFFECT_NOW + timedelta(seconds=2),
    )
    revoking_pin = active_pin.begin_revocation(pending_lease)
    await repository.compare_and_set_mount(
        previous_lease=active_lease,
        updated_lease=pending_lease,
        previous_pin=active_pin,
        updated_pin=revoking_pin,
    )
    proof = SnapshotMountStopProof.for_pending_stop(
        lease=pending_lease,
        pin=revoking_pin,
        disposition=SnapshotMountStopDisposition.REVOKED,
        stopped_at=_EFFECT_NOW + timedelta(seconds=3),
        proof_id="mount-stop-proof-1",
    )
    stopped_lease = pending_lease.finish_stop(proof, pin=revoking_pin)
    stopped_pin = revoking_pin.revoke(stopped_lease)

    assert await repository.record_stop(
        previous_lease=pending_lease,
        stopped_lease=stopped_lease,
        previous_pin=revoking_pin,
        stopped_pin=stopped_pin,
        proof=proof,
    ) == (stopped_lease, stopped_pin, proof, True)
    assert await repository.record_stop(
        previous_lease=pending_lease,
        stopped_lease=stopped_lease,
        previous_pin=revoking_pin,
        stopped_pin=stopped_pin,
        proof=proof,
    ) == (stopped_lease, stopped_pin, proof, False)
    assert await repository.get_mount(issue.lease.id) == (stopped_lease, stopped_pin)
    assert await repository.list_reconcilable(node_id="analysis-node") == ()
    await database.dispose()


async def test_mount_rejects_stale_cas_foreign_principal_and_corrupt_rows(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'static-fail-closed.db'}")
    await database.create_schema()
    credential = await _seed(database)
    repository = SQLAlchemyAuditStaticEffectAuthorityRepository(database.session_factory)
    plan = _plan()
    await repository.create_plan(plan)
    issue, pin = _issue(plan, credential.principal)
    await repository.issue_mount(issue, pin)
    active_lease = issue.lease.activate(
        mount_key=f"snapshot-mount:v1:{_digest('mount-key')}",
        mount_proof_digest=_digest("mount-proof"),
        activated_at=_EFFECT_NOW + timedelta(seconds=1),
    )
    active_pin = pin.activate(active_lease)
    await repository.compare_and_set_mount(
        previous_lease=issue.lease,
        updated_lease=active_lease,
        previous_pin=pin,
        updated_pin=active_pin,
    )
    stale_unknown = issue.lease.mark_outcome_unknown(
        failure_code="stale_observer",
        observed_at=_EFFECT_NOW + timedelta(seconds=2),
    )
    with pytest.raises(RepositoryConflictError):
        await repository.compare_and_set_mount(
            previous_lease=issue.lease,
            updated_lease=stale_unknown,
            previous_pin=pin,
            updated_pin=pin,
        )

    foreign_issue = SnapshotMountLease.issue(
        plan=plan,
        effect_execution_id="foreign-effect",
        target_runner_principal=credential.principal.model_copy(
            update={"epoch": credential.principal.epoch + 1}
        ),
        allowed_blob_digests=(_digest("blob-1"),),
        max_bytes=4096,
        expires_at=_EFFECT_NOW + timedelta(minutes=5),
        mount_policy_digest=_digest("mount-policy"),
        lease_id="foreign-lease",
        created_at=_EFFECT_NOW,
    )
    foreign_pin = SnapshotMountPin.for_lease(foreign_issue.lease, pin_id="foreign-pin")
    with pytest.raises(RepositoryConflictError, match="principal"):
        await repository.issue_mount(foreign_issue, foreign_pin)

    async with database.session_factory() as session, session.begin():
        await session.execute(
            text(
                "UPDATE snapshot_mount_leases SET canonical_json = "
                "replace(canonical_json, 'static-effect-1', 'static-effect-X') "
                "WHERE id = 'mount-lease-1'"
            )
        )
    with pytest.raises(RepositoryIntegrityError):
        await repository.get_mount(issue.lease.id)
    await database.dispose()
