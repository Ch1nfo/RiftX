from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from riftx.audit.static_effect import (
    AuditStaticEffectLimits,
    AuditStaticEffectPlan,
    AuditStaticOperationFamily,
    AuditStaticReadOnlyMount,
    SnapshotMountLease,
    SnapshotMountLeaseStatus,
    SnapshotMountPin,
    SnapshotMountPinStatus,
    SnapshotMountStopDisposition,
    SnapshotMountStopProof,
)
from riftx.domain.runner import RunnerPrincipal

_NOW = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _plan(
    *,
    operation_family: AuditStaticOperationFamily = AuditStaticOperationFamily.SNAPSHOT_MOUNT,
) -> AuditStaticEffectPlan:
    snapshot_digest = _digest("snapshot")
    manifest_digest = _digest("manifest")
    return AuditStaticEffectPlan(
        id="static-plan-1",
        project_id="project-1",
        audit_id="audit-1",
        run_id="run-1",
        snapshot_id="snapshot-1",
        snapshot_digest=snapshot_digest,
        manifest_digest=manifest_digest,
        operation_family=operation_family,
        backend_digest=_digest("backend"),
        image_digest=_digest("image"),
        policy_digest=_digest("policy"),
        content_storage_key_digest=_digest("content-key"),
        manifest_storage_key_digest=_digest("manifest-key"),
        read_only_mounts=(
            AuditStaticReadOnlyMount(
                snapshot_id="snapshot-1",
                snapshot_digest=snapshot_digest,
                manifest_digest=manifest_digest,
            ),
        ),
        unique_output_root_digest=_digest("output-root"),
        clean_env_digest=_digest("clean-env"),
        limits=AuditStaticEffectLimits(
            cpu_millis=1000,
            memory_bytes=1024 * 1024,
            pids=8,
            wall_seconds=60,
            disk_bytes=4096,
            file_count=32,
            input_bytes=2048,
            output_bytes=1024,
        ),
        input_manifest_digest=manifest_digest,
        output_contract_digest=_digest("output-contract"),
        policy_version="policy-v1",
        created_at=_NOW,
    )


def _issued() -> tuple[AuditStaticEffectPlan, str, SnapshotMountLease, SnapshotMountPin]:
    plan = _plan()
    issue = SnapshotMountLease.issue(
        plan=plan,
        effect_execution_id="effect-1",
        target_runner_principal=RunnerPrincipal(instance_id="runner-1", epoch=7),
        allowed_blob_digests=tuple(sorted((_digest("blob-a"), _digest("blob-b")))),
        max_bytes=2048,
        expires_at=_NOW + timedelta(minutes=5),
        mount_policy_digest=_digest("mount-policy"),
        lease_id="lease-1",
        created_at=_NOW,
    )
    return plan, issue.nonce, issue.lease, SnapshotMountPin.for_lease(
        issue.lease,
        pin_id="pin-1",
    )


def _active() -> tuple[str, SnapshotMountLease, SnapshotMountPin]:
    _, nonce, lease, pin = _issued()
    active_lease = lease.activate(
        mount_key=f"snapshot-mount:v1:{_digest('mount-key')}",
        mount_proof_digest=_digest("mount-proof"),
        activated_at=_NOW + timedelta(seconds=1),
    )
    return nonce, active_lease, pin.activate(active_lease)


def test_static_plan_digest_is_canonical_and_rejects_tampering() -> None:
    plan = _plan()

    assert plan.plan_digest == _plan().plan_digest
    tampered = plan.model_dump(mode="python")
    tampered["backend_digest"] = _digest("other-backend")

    with pytest.raises(ValidationError, match="plan digest does not match"):
        AuditStaticEffectPlan.model_validate(tampered)


def test_static_plan_is_private_read_only_and_exactly_one_snapshot() -> None:
    plan = _plan()
    payload = plan.model_dump(mode="json")

    assert payload["node_id"] == "local"
    assert payload["backend_id"] == "private_materialization"
    assert payload["network"] == "none"
    assert payload["read_only_mounts"][0]["read_only"] is True
    assert "content_storage_key" not in payload
    assert "manifest_storage_key" not in payload

    for override in (
        {"backend_id": "shared_path"},
        {"read_only_mounts": ()},
        {"read_only_mounts": plan.read_only_mounts * 2},
    ):
        with pytest.raises(ValidationError):
            AuditStaticEffectPlan.model_validate(
                {**plan.model_dump(mode="python"), **override, "plan_digest": ""}
            )


def test_static_plan_rejects_snapshot_or_manifest_drift() -> None:
    plan = _plan()
    mount = plan.read_only_mounts[0]

    with pytest.raises(ValidationError, match="does not match its Snapshot"):
        AuditStaticEffectPlan.model_validate(
            {
                **plan.model_dump(mode="python"),
                "read_only_mounts": (
                    mount.model_copy(update={"snapshot_digest": _digest("foreign")}),
                ),
                "plan_digest": "",
            }
        )
    with pytest.raises(ValidationError, match="input Manifest digest differs"):
        AuditStaticEffectPlan.model_validate(
            {
                **plan.model_dump(mode="python"),
                "input_manifest_digest": _digest("foreign"),
                "plan_digest": "",
            }
        )


def test_mount_lease_issue_binds_plan_runner_and_nonce() -> None:
    plan, nonce, lease, pin = _issued()

    assert lease.plan_id == plan.id
    assert lease.plan_digest == plan.plan_digest
    assert pin.lease_digest == lease.lease_digest
    assert pin.target_runner_principal == lease.target_runner_principal
    assert not lease.accepts(
        nonce=nonce,
        principal=lease.target_runner_principal,
        node_id="local",
        observed_at=_NOW,
    )

    with pytest.raises(ValueError, match="snapshot_mount plan"):
        SnapshotMountLease.issue(
            plan=_plan(operation_family=AuditStaticOperationFamily.SNAPSHOT_MATERIALIZE),
            effect_execution_id="effect-1",
            target_runner_principal=lease.target_runner_principal,
            allowed_blob_digests=(_digest("blob"),),
            max_bytes=1,
            expires_at=_NOW + timedelta(minutes=1),
            mount_policy_digest=_digest("mount-policy"),
            created_at=_NOW,
        )


def test_active_lease_rejects_foreign_nonce_principal_node_and_expiry() -> None:
    nonce, lease, _ = _active()

    assert lease.accepts(
        nonce=nonce,
        principal=lease.target_runner_principal,
        node_id="local",
        observed_at=_NOW + timedelta(seconds=2),
    )
    assert not lease.accepts(
        nonce="invalid",
        principal=lease.target_runner_principal,
        node_id="local",
        observed_at=_NOW + timedelta(seconds=2),
    )
    assert not lease.accepts(
        nonce=nonce,
        principal=RunnerPrincipal(instance_id="runner-2", epoch=7),
        node_id="local",
        observed_at=_NOW + timedelta(seconds=2),
    )
    assert not lease.accepts(
        nonce=nonce,
        principal=lease.target_runner_principal,
        node_id="remote",
        observed_at=_NOW + timedelta(seconds=2),
    )
    assert not lease.accepts(
        nonce=nonce,
        principal=lease.target_runner_principal,
        node_id="local",
        observed_at=lease.expires_at,
    )


def test_revocation_requires_validated_stop_proof_and_preserves_identity_digests() -> None:
    _, active_lease, active_pin = _active()
    pending_lease = active_lease.begin_stop(
        expired=False,
        requested_at=_NOW + timedelta(seconds=2),
    )
    pending_pin = active_pin.begin_revocation(pending_lease)
    proof = SnapshotMountStopProof.for_pending_stop(
        lease=pending_lease,
        pin=pending_pin,
        disposition=SnapshotMountStopDisposition.REVOKED,
        stopped_at=_NOW + timedelta(seconds=3),
        proof_id="proof-1",
    )
    stopped_lease = pending_lease.finish_stop(proof, pin=pending_pin)
    stopped_pin = pending_pin.revoke(stopped_lease)

    assert stopped_lease.status is SnapshotMountLeaseStatus.REVOKED
    assert stopped_pin.status is SnapshotMountPinStatus.REVOKED
    assert stopped_lease.lease_digest == active_lease.lease_digest
    assert stopped_pin.pin_digest == active_pin.pin_digest
    assert stopped_lease.mount_key == active_lease.mount_key
    assert stopped_lease.stop_proof_digest == proof.proof_digest


def test_expiry_cannot_be_mislabeled_as_revocation() -> None:
    _, active_lease, _ = _active()

    with pytest.raises(ValueError, match="differs from lease expiry"):
        active_lease.begin_stop(expired=False, requested_at=active_lease.expires_at)
    with pytest.raises(ValueError, match="differs from lease expiry"):
        active_lease.begin_stop(expired=True, requested_at=_NOW + timedelta(seconds=2))


def test_stop_proof_rejects_owner_drift_and_nonzero_or_accessible_state() -> None:
    _, active_lease, active_pin = _active()
    pending_lease = active_lease.begin_stop(
        expired=False,
        requested_at=_NOW + timedelta(seconds=2),
    )
    pending_pin = active_pin.begin_revocation(pending_lease)
    proof = SnapshotMountStopProof.for_pending_stop(
        lease=pending_lease,
        pin=pending_pin,
        disposition=SnapshotMountStopDisposition.REVOKED,
        stopped_at=_NOW + timedelta(seconds=3),
    )

    for override in (
        {"audit_id": "foreign-audit"},
        {"run_id": "foreign-run"},
        {"snapshot_digest": _digest("foreign-snapshot")},
        {"backend_digest": _digest("foreign-backend")},
    ):
        drifted = SnapshotMountStopProof.model_validate(
            {**proof.model_dump(mode="python"), **override, "proof_digest": ""}
        )
        with pytest.raises(ValueError, match="owner binding differs"):
            pending_lease.finish_stop(drifted, pin=pending_pin)

    for override in (
        {"active_fd_count": 1},
        {"active_process_count": 1},
        {"mount_namespace_unmounted": False},
        {"lease_revoked": False},
        {"pin_revoked": False},
        {"worker_path_inaccessible": False},
    ):
        with pytest.raises(ValidationError):
            SnapshotMountStopProof.model_validate(
                {**proof.model_dump(mode="python"), **override, "proof_digest": ""}
            )


def test_internal_transitions_revalidate_complete_lifecycle_shape() -> None:
    _, _, issued, _ = _issued()

    with pytest.raises(ValidationError, match="active Snapshot mount lease lacks mount proof"):
        SnapshotMountLease.model_validate(
            {
                **issued.model_dump(mode="python"),
                "status": SnapshotMountLeaseStatus.ACTIVE,
                "state_version": 2,
                "updated_at": _NOW + timedelta(seconds=1),
            }
        )
    unknown = issued.mark_outcome_unknown(
        failure_code="backend_state_unavailable",
        observed_at=_NOW + timedelta(seconds=1),
    )
    assert unknown.status is SnapshotMountLeaseStatus.OUTCOME_UNKNOWN
    assert unknown.state_version == 2


def test_mount_authority_schema_has_no_absolute_locator_field() -> None:
    forbidden = {"path", "absolute_path", "locator", "content_storage_key", "manifest_storage_key"}

    for model in (
        AuditStaticEffectPlan,
        SnapshotMountLease,
        SnapshotMountPin,
        SnapshotMountStopProof,
    ):
        assert forbidden.isdisjoint(model.model_fields)
