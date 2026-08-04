#!/usr/bin/env python3
"""Qualify the private Snapshot mount backend on a real local-Linux Docker host."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import io
import json
import platform
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from riftx.audit import (  # noqa: E402
    AuditStaticEffectLimits,
    AuditStaticEffectPlan,
    AuditStaticOperationFamily,
    AuditStaticReadOnlyMount,
    DockerSnapshotMountBackend,
    SnapshotBlobMetadata,
    SnapshotBlobObjectType,
    SnapshotCASDescriptor,
    SnapshotMountBackendError,
    SnapshotMountBackendState,
    SnapshotMountLease,
    SnapshotMountPin,
    SnapshotMountSource,
    snapshot_mount_key_digest,
    snapshot_storage_key_digest,
)
from riftx.domain import RunnerPrincipal  # noqa: E402

QUALIFICATION_SCHEMA_VERSION = "riftx.audit-snapshot-mount-real-linux-qualification/v1"
_REPORT_DIGEST_VERSION = "riftx.audit-snapshot-mount-qualification-report/v1"


def _digest(value: bytes | str) -> str:
    content = value.encode("utf-8") if isinstance(value, str) else value
    return hashlib.sha256(content).hexdigest()


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _domain_digest(domain: str, value: object) -> str:
    return hashlib.sha256(
        domain.encode("ascii") + b"\0" + _canonical_json(value).encode("utf-8")
    ).hexdigest()


def _sha256_digest(value: str) -> str:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise argparse.ArgumentTypeError(
            "image digest must be 64 lowercase hexadecimal characters without sha256:"
        )
    return value


class _MemoryReader:
    def __init__(self, content: bytes) -> None:
        self._content = io.BytesIO(content)
        self.size = len(content)

    @property
    def closed(self) -> bool:
        return self._content.closed

    def read(self, max_bytes: int) -> bytes:
        return self._content.read(max_bytes)

    def verify_complete(self) -> None:
        if self._content.tell() != self.size:
            raise ValueError("qualification blob was not fully consumed")

    def close(self) -> None:
        self._content.close()


class _MemoryStore:
    def __init__(self, descriptor: SnapshotCASDescriptor, content: dict[str, bytes]) -> None:
        self._descriptor = descriptor
        self._content = content

    def describe(self, binding: Any, content_storage_key: str) -> SnapshotCASDescriptor:
        if content_storage_key != self._descriptor.content_storage_key or not binding.accepts(
            self._descriptor
        ):
            raise ValueError("qualification descriptor owner differs")
        return self._descriptor

    def open_blob(
        self,
        binding: Any,
        content_storage_key: str,
        relative_path: str,
        expected_blob_digest: str,
        *,
        max_bytes: int,
    ) -> _MemoryReader:
        if content_storage_key != self._descriptor.content_storage_key or not binding.accepts(
            self._descriptor
        ):
            raise ValueError("qualification blob owner differs")
        content = self._content[relative_path]
        if _digest(content) != expected_blob_digest or len(content) > max_bytes:
            raise ValueError("qualification blob integrity differs")
        return _MemoryReader(content)


def _build_authority(
    backend: DockerSnapshotMountBackend,
    *,
    observed_at: datetime,
) -> tuple[
    AuditStaticEffectPlan,
    SnapshotMountLease,
    SnapshotMountPin,
    SnapshotMountSource,
]:
    content = {
        "bin/check.sh": b"#!/bin/sh\nexit 0\n",
        "main-link": b"bin/check.sh",
        "src/main.py": b"print('riftx snapshot qualification')\n",
    }
    snapshot_digest = _digest("riftx-snapshot-mount-qualification-snapshot")
    manifest_digest = _digest("riftx-snapshot-mount-qualification-manifest")
    descriptor = SnapshotCASDescriptor(
        project_id="snapshot-mount-qualification-project",
        snapshot_digest=snapshot_digest,
        manifest_digest=manifest_digest,
        blobs=(
            SnapshotBlobMetadata(
                relative_path="bin/check.sh",
                blob_digest=_digest(content["bin/check.sh"]),
                size=len(content["bin/check.sh"]),
                mode=0o100755,
            ),
            SnapshotBlobMetadata(
                relative_path="main-link",
                blob_digest=_digest(content["main-link"]),
                size=len(content["main-link"]),
                mode=0o120000,
                object_type=SnapshotBlobObjectType.SYMLINK,
            ),
            SnapshotBlobMetadata(
                relative_path="src/main.py",
                blob_digest=_digest(content["src/main.py"]),
                size=len(content["src/main.py"]),
                mode=0o100644,
            ),
        ),
    )
    plan = AuditStaticEffectPlan(
        id="snapshot-mount-qualification-plan",
        project_id=descriptor.project_id,
        audit_id="snapshot-mount-qualification-audit",
        run_id="snapshot-mount-qualification-run",
        snapshot_id="snapshot-mount-qualification-snapshot",
        snapshot_digest=snapshot_digest,
        manifest_digest=manifest_digest,
        operation_family=AuditStaticOperationFamily.SNAPSHOT_MOUNT,
        node_id=backend.node_id,
        backend_digest=backend.backend_digest,
        image_digest=backend.image_digest,
        policy_digest=_digest("riftx-snapshot-mount-qualification-policy"),
        content_storage_key_digest=snapshot_storage_key_digest(
            descriptor.content_storage_key,
            role="content",
        ),
        manifest_storage_key_digest=snapshot_storage_key_digest(
            f"snapshot-manifest:v1:{manifest_digest}",
            role="manifest",
        ),
        read_only_mounts=(
            AuditStaticReadOnlyMount(
                snapshot_id="snapshot-mount-qualification-snapshot",
                snapshot_digest=snapshot_digest,
                manifest_digest=manifest_digest,
            ),
        ),
        unique_output_root_digest=_digest("snapshot-mount-qualification-output"),
        clean_env_digest=_digest("snapshot-mount-qualification-clean-env"),
        limits=AuditStaticEffectLimits(
            cpu_millis=1_000,
            memory_bytes=64 * 1024 * 1024,
            pids=8,
            wall_seconds=60,
            disk_bytes=1024 * 1024,
            file_count=16,
            input_bytes=descriptor.total_bytes,
            output_bytes=0,
        ),
        input_manifest_digest=manifest_digest,
        output_contract_digest=_digest("snapshot-mount-qualification-contract"),
        policy_version="qualification-v1",
        created_at=observed_at,
    )
    principal = RunnerPrincipal(
        instance_id="snapshot-mount-qualification-runner",
        epoch=1,
    )
    issue = SnapshotMountLease.issue(
        plan=plan,
        effect_execution_id="snapshot-mount-qualification-effect",
        target_runner_principal=principal,
        allowed_blob_digests=tuple(sorted({metadata.blob_digest for metadata in descriptor.blobs})),
        max_bytes=descriptor.total_bytes,
        expires_at=observed_at + timedelta(minutes=5),
        mount_policy_digest=_digest("snapshot-mount-qualification-mount-policy"),
        lease_id="snapshot-mount-qualification-lease",
        created_at=observed_at,
    )
    pin = SnapshotMountPin.for_lease(
        issue.lease,
        pin_id="snapshot-mount-qualification-pin",
    )
    source = SnapshotMountSource.resolve(
        plan=plan,
        lease=issue.lease,
        content_storage_key=descriptor.content_storage_key,
        store=_MemoryStore(descriptor, content),  # type: ignore[arg-type]
    )
    return plan, issue.lease, pin, source


def _base_report(
    backend: DockerSnapshotMountBackend,
    *,
    generated_at: datetime,
) -> dict[str, Any]:
    return {
        "schema_version": QUALIFICATION_SCHEMA_VERSION,
        "ready": False,
        "generated_at": generated_at.isoformat(),
        "host": {
            "machine": platform.machine(),
            "release": platform.release(),
            "system": platform.system(),
        },
        "backend_id": backend.backend_id,
        "backend_digest": backend.backend_digest,
        "image_digest": backend.image_digest,
        "node_id": backend.node_id,
        "checks": {
            "availability": False,
            "descriptor_bound_materialization": False,
            "non_root_kernel_mutation_denial": False,
            "restart_inspection": False,
            "stop_affirmative": False,
            "post_stop_absent": False,
            "cleanup_confirmed": False,
        },
        "proof": {},
        "failure_code": None,
        "failure_outcome_unknown": None,
    }


def _finish_report(report: dict[str, Any]) -> dict[str, Any]:
    payload = dict(report)
    payload.pop("evidence_digest", None)
    report["evidence_digest"] = _domain_digest(_REPORT_DIGEST_VERSION, payload)
    return report


async def _run_qualification(arguments: argparse.Namespace) -> dict[str, Any]:
    generated_at = datetime.now(UTC).replace(microsecond=0)
    backend = DockerSnapshotMountBackend(
        node_id=arguments.node_id,
        image_digest=arguments.image_digest,
    )
    report = _base_report(backend, generated_at=generated_at)
    checks = report["checks"]
    proof = report["proof"]
    assert isinstance(checks, dict) and isinstance(proof, dict)

    availability = await backend.probe_availability()
    proof["availability_proof_digest"] = availability.qualification_proof_digest
    if not availability.available:
        report["failure_code"] = availability.reason_code
        return _finish_report(report)
    checks["availability"] = True

    plan, current_lease, current_pin, source = _build_authority(
        backend,
        observed_at=generated_at,
    )
    proof.update(
        {
            "descriptor_digest": source.descriptor.descriptor_digest,
            "file_count": source.descriptor.file_count,
            "lease_digest": current_lease.lease_digest,
            "pin_digest": current_pin.pin_digest,
            "plan_digest": plan.plan_digest,
            "total_bytes": source.descriptor.total_bytes,
        }
    )
    cleanup_backend = backend
    cleanup_confirmed = False
    try:
        prepared_at = generated_at + timedelta(seconds=1)
        prepared = await backend.prepare(
            plan=plan,
            lease=current_lease,
            pin=current_pin,
            source=source,
            prepared_at=prepared_at,
        )
        if not prepared.matches(
            current_lease,
            current_pin,
            source,
            observed_at=prepared_at,
        ):
            raise RuntimeError("prepared qualification mount owner differs")
        checks["descriptor_bound_materialization"] = True
        checks["non_root_kernel_mutation_denial"] = True
        proof["mount_key_digest"] = snapshot_mount_key_digest(prepared.mount_key)
        proof["mount_proof_digest"] = prepared.mount_proof_digest

        current_lease = current_lease.activate(
            mount_key=prepared.mount_key,
            mount_proof_digest=prepared.mount_proof_digest,
            activated_at=prepared_at,
        )
        current_pin = current_pin.activate(current_lease)
        restarted = DockerSnapshotMountBackend(
            node_id=arguments.node_id,
            image_digest=arguments.image_digest,
        )
        cleanup_backend = restarted
        inspected_at = generated_at + timedelta(seconds=2)
        inspection = await restarted.inspect(
            plan=plan,
            lease=current_lease,
            pin=current_pin,
            observed_at=inspected_at,
        )
        if inspection.state is not SnapshotMountBackendState.ACTIVE or not inspection.matches(
            current_lease,
            current_pin,
            observed_at=inspected_at,
        ):
            raise RuntimeError("restarted qualification inspection differs")
        checks["restart_inspection"] = True

        stopped_at = generated_at + timedelta(seconds=3)
        current_lease = current_lease.begin_stop(
            expired=False,
            requested_at=stopped_at,
        )
        current_pin = current_pin.begin_revocation(current_lease)
        evidence = await restarted.stop(
            plan=plan,
            lease=current_lease,
            pin=current_pin,
            stopped_at=stopped_at,
        )
        if not evidence.affirmative or not evidence.matches(current_lease, current_pin):
            raise RuntimeError("qualification stop evidence is not affirmative")
        checks["stop_affirmative"] = True
        cleanup_confirmed = True
        checks["cleanup_confirmed"] = True

        absent_at = generated_at + timedelta(seconds=4)
        absent = await restarted.inspect(
            plan=plan,
            lease=current_lease,
            pin=current_pin,
            observed_at=absent_at,
        )
        if absent.state is not SnapshotMountBackendState.ABSENT:
            raise RuntimeError("qualification mount remains after stop")
        checks["post_stop_absent"] = True
    except SnapshotMountBackendError as error:
        report["failure_code"] = error.failure.value
        report["failure_outcome_unknown"] = error.outcome_unknown
    except Exception:
        report["failure_code"] = "audit_snapshot_mount_qualification_failed"
        report["failure_outcome_unknown"] = True
    finally:
        if not cleanup_confirmed:
            cleanup_at = datetime.now(UTC).replace(microsecond=0)
            if cleanup_at < current_lease.updated_at:
                cleanup_at = current_lease.updated_at
            try:
                cleanup = await cleanup_backend.stop(
                    plan=plan,
                    lease=current_lease,
                    pin=current_pin,
                    stopped_at=cleanup_at,
                )
            except Exception:
                report["failure_code"] = "audit_snapshot_mount_qualification_cleanup_unproven"
                report["failure_outcome_unknown"] = True
            else:
                cleanup_confirmed = cleanup.affirmative and cleanup.matches(
                    current_lease,
                    current_pin,
                )
                checks["cleanup_confirmed"] = cleanup_confirmed
                if not cleanup_confirmed:
                    report["failure_code"] = "audit_snapshot_mount_qualification_cleanup_unproven"
                    report["failure_outcome_unknown"] = True

    report["ready"] = all(checks.values()) and report["failure_code"] is None
    return _finish_report(report)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--image-digest",
        required=True,
        type=_sha256_digest,
        help="Locally present pinned Linux image SHA-256 digest, without the sha256: prefix",
    )
    parser.add_argument(
        "--node-id",
        default="local",
        help="Same-node authority identifier (default: local)",
    )
    parser.add_argument(
        "--evidence",
        type=Path,
        help="Create this new JSON evidence file; existing files are never overwritten",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    arguments = parser.parse_args(argv)
    if arguments.evidence is not None and arguments.evidence.exists():
        parser.error("--evidence target already exists")
    report = asyncio.run(_run_qualification(arguments))
    rendered = json.dumps(report, indent=2, ensure_ascii=False) + "\n"
    if arguments.evidence is not None:
        with arguments.evidence.open("x", encoding="utf-8") as stream:
            stream.write(rendered)
    print(rendered, end="")
    return 0 if report["ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
