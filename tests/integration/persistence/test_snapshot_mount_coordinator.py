from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest
from tests.integration.persistence.test_audit_repositories import (
    NOW,
    _create_audit,
    _create_engagement,
    _digest,
    _project,
    _snapshot,
)
from tests.integration.persistence.test_audit_static_effect_repository import (
    _EFFECT_NOW,
    _issue,
    _plan,
)
from tests.integration.persistence.test_snapshot_references import _reference

from riftx.audit import (
    PreparedSnapshotMount,
    SnapshotBlobMetadata,
    SnapshotCASDescriptor,
    SnapshotMountBackendError,
    SnapshotMountBackendState,
    SnapshotMountCoordinator,
    SnapshotMountError,
    SnapshotMountFailure,
    SnapshotMountInspection,
    SnapshotMountLeaseStatus,
    SnapshotMountPinStatus,
    SnapshotMountSource,
    SnapshotMountStopDisposition,
    SnapshotMountStopEvidence,
    SnapshotStoreError,
    SnapshotStoreFailure,
    snapshot_storage_key_digest,
)
from riftx.domain import Node, NodeStatus, RunnerPrincipal
from riftx.persistence import (
    Database,
    SQLAlchemyAuditProjectRepository,
    SQLAlchemyAuditStaticEffectAuthorityRepository,
    SQLAlchemyNodeRepository,
    SQLAlchemyRunnerCredentialRepository,
    SQLAlchemySnapshotMountSourceResolver,
    SQLAlchemySnapshotReferenceRepository,
    SQLAlchemySnapshotRepository,
)


class _DescriptorStore:
    def __init__(self, descriptor: SnapshotCASDescriptor) -> None:
        self.descriptor = descriptor
        self.describe_calls = 0

    def describe(self, binding, content_storage_key: str) -> SnapshotCASDescriptor:
        self.describe_calls += 1
        if not binding.accepts(self.descriptor):
            raise SnapshotStoreError(SnapshotStoreFailure.OWNER_MISMATCH)
        return self.descriptor

    def open_blob(self, *args, **kwargs):  # pragma: no cover - backend fixture never reads
        raise AssertionError("unexpected blob read")


class _StaticSourceResolver:
    def __init__(self, source: SnapshotMountSource) -> None:
        self.source = source
        self.calls = 0

    async def resolve(self, *, plan, lease) -> SnapshotMountSource:
        self.calls += 1
        SnapshotMountSource.require_authority(plan=plan, lease=lease)
        return self.source


class _FakePrivateMountBackend:
    def __init__(self, *, node_id: str, backend_id: str, backend_digest: str) -> None:
        self.node_id = node_id
        self.backend_id = backend_id
        self.backend_digest = backend_digest
        self.objects: dict[str, tuple[str, str]] = {}
        self.prepare_calls = 0
        self.inspect_calls = 0
        self.stop_calls = 0
        self.prepare_owner_drift = False
        self.inspect_owner_drift = False
        self.inspect_error: SnapshotMountBackendError | None = None
        self.stop_affirmative = True

    async def prepare(self, *, plan, lease, pin, source, prepared_at):
        self.prepare_calls += 1
        mount_key = f"snapshot-mount:v1:{_digest(f'mount:{lease.id}')}"
        mount_proof_digest = _digest(f"mount-proof:{lease.id}")
        self.objects[lease.id] = (mount_key, mount_proof_digest)
        prepared = PreparedSnapshotMount(
            lease_id=lease.id,
            lease_digest=lease.lease_digest,
            pin_id=pin.id,
            pin_digest=pin.pin_digest,
            plan_id=plan.id,
            plan_digest=plan.plan_digest,
            effect_execution_id=lease.effect_execution_id,
            node_id=self.node_id,
            backend_id=self.backend_id,
            backend_digest=self.backend_digest,
            principal=lease.target_runner_principal,
            mount_key=mount_key,
            mount_proof_digest=mount_proof_digest,
            descriptor_digest=source.descriptor.descriptor_digest,
            file_count=source.descriptor.file_count,
            total_bytes=source.descriptor.total_bytes,
            prepared_at=prepared_at,
        )
        if self.prepare_owner_drift:
            return replace(prepared, backend_digest=_digest("foreign-backend"))
        return prepared

    async def inspect(self, *, lease, pin, observed_at):
        self.inspect_calls += 1
        if self.inspect_error is not None:
            raise self.inspect_error
        mounted = self.objects.get(lease.id)
        inspection = SnapshotMountInspection(
            state=(
                SnapshotMountBackendState.ABSENT
                if mounted is None
                else SnapshotMountBackendState.ACTIVE
            ),
            lease_id=lease.id,
            lease_digest=lease.lease_digest,
            pin_id=pin.id,
            pin_digest=pin.pin_digest,
            node_id=self.node_id,
            backend_id=self.backend_id,
            backend_digest=self.backend_digest,
            principal=lease.target_runner_principal,
            observed_at=observed_at,
            mount_key=None if mounted is None else mounted[0],
            mount_proof_digest=None if mounted is None else mounted[1],
        )
        if self.inspect_owner_drift:
            return replace(inspection, backend_digest=_digest("foreign-backend"))
        return inspection

    async def stop(self, *, lease, pin, stopped_at):
        self.stop_calls += 1
        mounted = self.objects.get(lease.id)
        mount_key = (
            lease.mount_key
            or (mounted[0] if mounted is not None else None)
            or f"snapshot-mount:v1:{_digest(f'mount:{lease.id}')}"
        )
        evidence = SnapshotMountStopEvidence(
            lease_id=lease.id,
            lease_digest=lease.lease_digest,
            pin_id=pin.id,
            pin_digest=pin.pin_digest,
            node_id=self.node_id,
            backend_id=self.backend_id,
            backend_digest=self.backend_digest,
            principal=lease.target_runner_principal,
            mount_key=mount_key,
            stopped_at=stopped_at,
            active_fd_count=0,
            active_process_count=0,
            mount_namespace_unmounted=self.stop_affirmative,
            lease_revoked=True,
            pin_revoked=True,
            worker_path_inaccessible=self.stop_affirmative,
        )
        if evidence.affirmative:
            self.objects.pop(lease.id, None)
        return evidence


def _descriptor(plan) -> SnapshotCASDescriptor:
    return SnapshotCASDescriptor(
        project_id=plan.project_id,
        snapshot_digest=plan.snapshot_digest,
        manifest_digest=plan.manifest_digest,
        blobs=(
            SnapshotBlobMetadata(
                relative_path="a.py",
                blob_digest=_digest("blob-1"),
                size=2,
                mode=0o100644,
            ),
            SnapshotBlobMetadata(
                relative_path="b.py",
                blob_digest=_digest("blob-2"),
                size=3,
                mode=0o100644,
            ),
        ),
    )


async def _runtime(tmp_path: Path, name: str):
    database = Database(f"sqlite+aiosqlite:///{tmp_path / f'{name}.db'}")
    await database.create_schema()
    base_snapshot = _snapshot()
    descriptor = _descriptor(base_snapshot)
    snapshot_payload = base_snapshot.model_dump(mode="python")
    snapshot_payload.update(
        content_storage_key=descriptor.content_storage_key,
        file_count=descriptor.file_count,
        total_bytes=descriptor.total_bytes,
    )
    snapshot = type(base_snapshot).model_validate(snapshot_payload)
    credential = await _seed_snapshot(database, snapshot)
    authority = SQLAlchemyAuditStaticEffectAuthorityRepository(database.session_factory)
    base_plan = _plan()
    plan_payload = base_plan.model_dump(mode="python")
    plan_payload.update(
        content_storage_key_digest=snapshot_storage_key_digest(
            descriptor.content_storage_key,
            role="content",
        ),
        plan_digest="",
    )
    plan = type(base_plan).model_validate(plan_payload)
    await authority.create_plan(plan)
    issue, pin = _issue(plan, credential.principal)
    await authority.issue_mount(issue, pin)
    store = _DescriptorStore(descriptor)
    source = SnapshotMountSource.resolve(
        plan=plan,
        lease=issue.lease,
        content_storage_key=descriptor.content_storage_key,
        store=store,  # type: ignore[arg-type]
    )
    sources = _StaticSourceResolver(source)
    backend = _FakePrivateMountBackend(
        node_id=plan.node_id,
        backend_id=plan.backend_id,
        backend_digest=plan.backend_digest,
    )
    coordinator = SnapshotMountCoordinator(
        authority=authority,
        sources=sources,
        backend=backend,
    )
    return database, authority, credential, plan, issue, pin, sources, backend, coordinator


async def test_source_resolution_binds_plan_lease_locator_and_descriptor() -> None:
    base = _plan()
    descriptor = _descriptor(base)
    payload = base.model_dump(mode="python")
    payload.update(
        content_storage_key_digest=snapshot_storage_key_digest(
            descriptor.content_storage_key,
            role="content",
        ),
        plan_digest="",
    )
    plan = type(base).model_validate(payload)
    issue, _ = _issue(plan, _principal())
    store = _DescriptorStore(descriptor)

    source = SnapshotMountSource.resolve(
        plan=plan,
        lease=issue.lease,
        content_storage_key=descriptor.content_storage_key,
        store=store,  # type: ignore[arg-type]
    )

    assert source.descriptor == descriptor
    assert store.describe_calls == 1
    drifted = replace(descriptor, blobs=descriptor.blobs[:1])
    with pytest.raises(SnapshotMountError) as mismatch:
        SnapshotMountSource.resolve(
            plan=plan,
            lease=issue.lease,
            content_storage_key=descriptor.content_storage_key,
            store=_DescriptorStore(drifted),  # type: ignore[arg-type]
        )
    assert mismatch.value.failure is SnapshotMountFailure.SOURCE_INTEGRITY


def _principal():
    return RunnerPrincipal(instance_id="runner-instance-1", epoch=1)


async def test_activate_authenticates_before_source_or_backend_io(tmp_path: Path) -> None:
    runtime = await _runtime(tmp_path, "auth-first")
    database, _, credential, _, issue, _, sources, backend, coordinator = runtime
    foreign = credential.principal.model_copy(update={"epoch": credential.principal.epoch + 1})
    attempts = (
        {"nonce": "x" * 43, "principal": credential.principal},
        {"nonce": issue.nonce, "principal": foreign},
    )
    for values in attempts:
        with pytest.raises(SnapshotMountError) as rejected:
            await coordinator.activate(
                lease_id=issue.lease.id,
                observed_at=_EFFECT_NOW + timedelta(seconds=1),
                node_id="analysis-node",
                **values,
            )
        assert rejected.value.failure is SnapshotMountFailure.AUTHENTICATION_FAILED
    with pytest.raises(SnapshotMountError) as cross_node:
        await coordinator.activate(
            lease_id=issue.lease.id,
            nonce=issue.nonce,
            principal=credential.principal,
            node_id="other-node",
            observed_at=_EFFECT_NOW + timedelta(seconds=1),
        )
    assert cross_node.value.failure is SnapshotMountFailure.CROSS_NODE_NOT_SUPPORTED
    assert sources.calls == 0
    assert backend.prepare_calls == 0
    await database.dispose()


async def test_activate_and_terminal_stop_are_exactly_replayable(tmp_path: Path) -> None:
    runtime = await _runtime(tmp_path, "activation-replay")
    database, authority, credential, _, issue, _, sources, backend, coordinator = runtime
    activated_at = _EFFECT_NOW + timedelta(seconds=1)

    active_lease, active_pin, created = await coordinator.activate(
        lease_id=issue.lease.id,
        nonce=issue.nonce,
        principal=credential.principal,
        node_id="analysis-node",
        observed_at=activated_at,
    )
    replayed_lease, replayed_pin, replayed = await coordinator.activate(
        lease_id=issue.lease.id,
        nonce=issue.nonce,
        principal=credential.principal,
        node_id="analysis-node",
        observed_at=activated_at,
    )
    assert created is True and replayed is False
    assert (replayed_lease, replayed_pin) == (active_lease, active_pin)
    assert sources.calls == 1 and backend.prepare_calls == 1

    stopped = await coordinator.stop(
        lease_id=issue.lease.id,
        requested_at=_EFFECT_NOW + timedelta(seconds=2),
    )
    replayed_stop = await coordinator.stop(
        lease_id=issue.lease.id,
        requested_at=_EFFECT_NOW + timedelta(seconds=3),
    )
    assert stopped[0].status is SnapshotMountLeaseStatus.REVOKED
    assert stopped[1].status is SnapshotMountPinStatus.REVOKED
    assert stopped[2].disposition is SnapshotMountStopDisposition.REVOKED
    assert stopped[3] is True
    assert replayed_stop == (*stopped[:3], False)
    assert await authority.get_stop_proof(issue.lease.id) == stopped[2]
    assert backend.stop_calls == 1
    await database.dispose()


async def test_prepare_owner_drift_is_cleaned_and_marked_unknown(tmp_path: Path) -> None:
    runtime = await _runtime(tmp_path, "prepare-drift")
    database, authority, credential, _, issue, _, _, backend, coordinator = runtime
    backend.prepare_owner_drift = True

    with pytest.raises(SnapshotMountError) as rejected:
        await coordinator.activate(
            lease_id=issue.lease.id,
            nonce=issue.nonce,
            principal=credential.principal,
            node_id="analysis-node",
            observed_at=_EFFECT_NOW + timedelta(seconds=1),
        )
    assert rejected.value.failure is SnapshotMountFailure.OWNER_MISMATCH
    lease, pin = (await authority.get_mount(issue.lease.id)) or (None, None)
    assert lease is not None and lease.status is SnapshotMountLeaseStatus.OUTCOME_UNKNOWN
    assert pin is not None and pin.status is SnapshotMountPinStatus.PENDING
    assert issue.lease.id not in backend.objects
    await database.dispose()


async def test_unconfirmed_stop_preserves_pin_and_marks_lease_unknown(tmp_path: Path) -> None:
    runtime = await _runtime(tmp_path, "stop-unconfirmed")
    database, authority, credential, _, issue, _, _, backend, coordinator = runtime
    await coordinator.activate(
        lease_id=issue.lease.id,
        nonce=issue.nonce,
        principal=credential.principal,
        node_id="analysis-node",
        observed_at=_EFFECT_NOW + timedelta(seconds=1),
    )
    backend.stop_affirmative = False

    with pytest.raises(SnapshotMountError) as rejected:
        await coordinator.stop(
            lease_id=issue.lease.id,
            requested_at=_EFFECT_NOW + timedelta(seconds=2),
        )
    assert rejected.value.failure is SnapshotMountFailure.STOP_UNCONFIRMED
    lease, pin = (await authority.get_mount(issue.lease.id)) or (None, None)
    assert lease is not None and lease.status is SnapshotMountLeaseStatus.OUTCOME_UNKNOWN
    assert pin is not None and pin.status is SnapshotMountPinStatus.REVOCATION_PENDING
    assert issue.lease.id in backend.objects
    await database.dispose()


async def test_restart_reconciliation_retains_old_generation_and_expires_mount(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(tmp_path, "restart-expiry")
    database, authority, credential, plan, issue, _, sources, backend, coordinator = runtime
    await coordinator.activate(
        lease_id=issue.lease.id,
        nonce=issue.nonce,
        principal=credential.principal,
        node_id=plan.node_id,
        observed_at=_EFFECT_NOW + timedelta(seconds=1),
    )
    await SQLAlchemyRunnerCredentialRepository(database.session_factory).issue(
        plan.node_id,
        token_hash=_digest("rotated-token"),
        token_prefix="rotated",
        issued_at=_EFFECT_NOW + timedelta(seconds=2),
        instance_id="runner-instance-2",
    )
    restarted = SnapshotMountCoordinator(
        authority=authority,
        sources=sources,
        backend=backend,
    )

    retained = await restarted.reconcile(
        node_id=plan.node_id,
        observed_at=_EFFECT_NOW + timedelta(minutes=1),
    )
    assert retained.retained_active == 1
    expired = await restarted.reconcile(
        node_id=plan.node_id,
        observed_at=issue.lease.expires_at,
    )
    assert expired.stopped == 1
    lease, pin = (await authority.get_mount(issue.lease.id)) or (None, None)
    assert lease is not None and lease.status is SnapshotMountLeaseStatus.EXPIRED
    assert pin is not None and pin.status is SnapshotMountPinStatus.REVOKED
    await database.dispose()


@pytest.mark.parametrize("scenario", ["issued_orphan", "active_missing", "active_drift"])
async def test_reconciliation_marks_missing_or_orphaned_backend_state_unknown(
    tmp_path: Path,
    scenario: str,
) -> None:
    runtime = await _runtime(tmp_path, f"reconcile-{scenario}")
    database, authority, credential, plan, issue, _, _, backend, coordinator = runtime
    if scenario.startswith("active_"):
        await coordinator.activate(
            lease_id=issue.lease.id,
            nonce=issue.nonce,
            principal=credential.principal,
            node_id=plan.node_id,
            observed_at=_EFFECT_NOW + timedelta(seconds=1),
        )
        if scenario == "active_missing":
            backend.objects.clear()
        else:
            backend.inspect_owner_drift = True
    else:
        backend.objects[issue.lease.id] = (
            f"snapshot-mount:v1:{_digest('orphan')}",
            _digest("orphan-proof"),
        )

    result = await coordinator.reconcile(
        node_id=plan.node_id,
        observed_at=_EFFECT_NOW + timedelta(seconds=2),
    )
    lease, pin = (await authority.get_mount(issue.lease.id)) or (None, None)
    assert result.outcome_unknown == 1
    assert lease is not None and lease.status is SnapshotMountLeaseStatus.OUTCOME_UNKNOWN
    assert pin is not None
    assert pin.status is (
        SnapshotMountPinStatus.ACTIVE
        if scenario.startswith("active_")
        else SnapshotMountPinStatus.PENDING
    )
    assert (issue.lease.id in backend.objects) is (scenario == "active_drift")
    await database.dispose()


async def test_reconciliation_persists_backend_inspection_failure_and_rejects_cross_node(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(tmp_path, "inspection-failure")
    database, authority, _, plan, issue, _, _, backend, coordinator = runtime
    backend.inspect_error = SnapshotMountBackendError(
        SnapshotMountFailure.BACKEND_UNAVAILABLE,
        outcome_unknown=False,
    )

    with pytest.raises(SnapshotMountError) as cross_node:
        await coordinator.reconcile(
            node_id="other-node",
            observed_at=_EFFECT_NOW + timedelta(seconds=1),
        )
    assert cross_node.value.failure is SnapshotMountFailure.CROSS_NODE_NOT_SUPPORTED

    result = await coordinator.reconcile(
        node_id=plan.node_id,
        observed_at=_EFFECT_NOW + timedelta(seconds=1),
    )
    lease, pin = (await authority.get_mount(issue.lease.id)) or (None, None)
    assert result.outcome_unknown == 1
    assert lease is not None and lease.status is SnapshotMountLeaseStatus.OUTCOME_UNKNOWN
    assert pin is not None and pin.status is SnapshotMountPinStatus.PENDING
    await database.dispose()


async def _seed_snapshot(database: Database, snapshot) -> object:
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
    await SQLAlchemySnapshotRepository(database.session_factory).create(snapshot)
    await _create_audit(database)
    await SQLAlchemySnapshotReferenceRepository(database.session_factory).add(_reference())
    return credential


async def test_sql_source_resolver_uses_only_authoritative_snapshot_locator(
    tmp_path: Path,
) -> None:
    base_snapshot = _snapshot()
    descriptor = SnapshotCASDescriptor(
        project_id=base_snapshot.project_id,
        snapshot_digest=base_snapshot.snapshot_digest,
        manifest_digest=base_snapshot.manifest_digest,
        blobs=(
            SnapshotBlobMetadata(
                relative_path="source.py",
                blob_digest=_digest("blob-1"),
                size=4,
                mode=0o100644,
            ),
            SnapshotBlobMetadata(
                relative_path="source_test.py",
                blob_digest=_digest("blob-2"),
                size=5,
                mode=0o100644,
            ),
        ),
    )
    snapshot_payload = base_snapshot.model_dump(mode="python")
    snapshot_payload.update(
        content_storage_key=descriptor.content_storage_key,
        file_count=descriptor.file_count,
        total_bytes=descriptor.total_bytes,
    )
    snapshot = type(base_snapshot).model_validate(snapshot_payload)
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'source-resolver.db'}")
    await database.create_schema()
    credential = await _seed_snapshot(database, snapshot)
    base_plan = _plan()
    plan_payload = base_plan.model_dump(mode="python")
    plan_payload.update(
        content_storage_key_digest=snapshot_storage_key_digest(
            descriptor.content_storage_key,
            role="content",
        ),
        plan_digest="",
    )
    plan = type(base_plan).model_validate(plan_payload)
    issue, _ = _issue(plan, credential.principal)
    store = _DescriptorStore(descriptor)
    resolver = SQLAlchemySnapshotMountSourceResolver(
        database.session_factory,
        store,  # type: ignore[arg-type]
    )

    source = await resolver.resolve(plan=plan, lease=issue.lease)

    assert source.content_storage_key == descriptor.content_storage_key
    assert source.descriptor == descriptor
    assert store.describe_calls == 1
    await database.dispose()
