"""Runner-only production SourceIngest capsule backend.

The Control Plane must not import or instantiate this module.  It owns Docker
processes and descriptor-bound source mounts, while Git itself remains confined
to the standalone capsule worker.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import platform
import secrets
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from riftx.config import AuditConfig, audit_source_ingest_policy_digest
from riftx.runner._durable_file import atomic_write_json, locked_file

from .paths import AuthorizedSourceRepository, SourcePathAuthorizationError
from .source_ingest_contract import (
    SourceIngestWorkerRequest,
    SourceIngestWorkerResult,
)

SOURCE_INGEST_BACKEND_COMPONENT_VERSION = "riftx.audit-source-ingest-docker/v1"
SOURCE_INGEST_PREPARE_PROOF_VERSION = "riftx.audit-source-ingest-prepare-proof/v1"
SOURCE_INGEST_PROCESS_IDENTITY_VERSION = "riftx.audit-source-ingest-process-identity/v1"
SOURCE_INGEST_NEVER_CREATED_PROOF_VERSION = "riftx.audit-source-ingest-never-created-proof/v1"
SOURCE_INGEST_CAPSULE_RECORD_VERSION = "riftx.audit-source-ingest-capsule-record/v1"
SOURCE_INGEST_MOUNT_IDENTITY_VERSION = "riftx.audit-source-mount-identity/v1"
SOURCE_INGEST_MOUNT_PROOF_VERSION = "riftx.audit-source-mount-proof/v1"
SOURCE_INGEST_MOUNT_PROBE_RECORD_VERSION = "riftx.audit-source-mount-probe-record/v1"
SOURCE_INGEST_MOUNT_PROBE_OWNER_VERSION = "riftx.audit-source-mount-probe-owner/v1"
SOURCE_INGEST_MOUNT_PROBE_DESTRUCTION_PROOF_VERSION = (
    "riftx.audit-source-mount-probe-destruction-proof/v1"
)
_DOCKER_SOCKET = Path("/var/run/docker.sock")
_DOCKER_OUTPUT_LIMIT = 1024 * 1024
_CONTAINER_ID_LENGTH = 64
_CAPSULE_RECORD_NAME = "capsule.json"
_MOUNT_PROBE_RECORD_NAME = "audit-preflight-mount-probe.json"
_CAPSULE_STATES = frozenset(
    {
        "create_intent",
        "created",
        "prepared",
        "start_requested",
        "running",
        "terminal",
        "stop_requested",
        "stop_observed",
        "outcome_unknown",
        "cleanup_authorized",
        "cleanup_complete",
    }
)
_ACTIVE_CONTAINER_STATES = frozenset({"running", "paused", "restarting"})
_TERMINAL_CONTAINER_STATES = frozenset({"exited", "dead"})
_MOUNT_PROBE_STATES = frozenset(
    {
        "create_intent",
        "created",
        "start_requested",
        "terminal",
        "cleanup_requested",
        "cleanup_complete",
        "outcome_unknown",
    }
)
_SUPPORTED_LOCAL_SOURCE_FILESYSTEMS = frozenset(
    {"bcachefs", "btrfs", "ext2", "ext3", "ext4", "f2fs", "tmpfs", "xfs", "zfs"}
)
_SOURCE_MOUNT_PROBE_SCRIPT = r"""
import json
import os

value = os.stat("/source", follow_symlinks=False)
device = f"{os.major(value.st_dev)}:{os.minor(value.st_dev)}"
selected = None
with open("/proc/self/mountinfo", "r", encoding="utf-8") as stream:
    for line in stream:
        fields = line.rstrip("\n").split(" ")
        separator = fields.index("-")
        mount_point = fields[4]
        escapes = (("\\040", " "), ("\\011", "\t"), ("\\012", "\n"), ("\\134", "\\"))
        for escaped, decoded in escapes:
            mount_point = mount_point.replace(escaped, decoded)
        covers_source = "/source" == mount_point or "/source".startswith(
            mount_point.rstrip("/") + "/"
        )
        if fields[2] == device and covers_source:
            candidate = (len(mount_point), int(fields[0]), fields[separator + 1])
            if selected is None or candidate[0] > selected[0]:
                selected = candidate
if selected is None:
    raise SystemExit(72)
payload = {
    "filesystem_type": selected[2],
    "mount_id": selected[1],
    "st_dev": int(value.st_dev),
    "st_ino": int(value.st_ino),
}
descriptor = os.open("/output/identity.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
try:
    os.write(descriptor, json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("ascii"))
finally:
    os.close(descriptor)
""".strip()


class SourceIngestBackendError(RuntimeError):
    """Path-free backend error with a stable safe code."""

    def __init__(
        self,
        code: str,
        *,
        capsule_id: str | None = None,
        container_id: str | None = None,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.capsule_id = capsule_id
        self.container_id = container_id


@dataclass(frozen=True, slots=True)
class SourceIngestBackendAvailability:
    available: bool
    reason_code: str | None
    component_digest: str | None = None
    worker_digest: str | None = None


@dataclass(frozen=True, slots=True)
class SourceIngestExecutionResult:
    worker_result: SourceIngestWorkerResult
    prepare_proof_digest: str
    process_identity_digest: str
    container_id: str
    exit_code: int


@dataclass(frozen=True, slots=True)
class SourceIngestStartEvidence:
    process_identity_digest: str
    observed_state: str


@dataclass(frozen=True, slots=True)
class SourceIngestStopEvidence:
    stopped: bool
    process_identity_digest: str | None
    observed_state: str


@dataclass(frozen=True, slots=True)
class SourceIngestProbe:
    exists: bool
    running: bool
    terminal: bool
    process_identity_digest: str | None
    observed_state: str


@dataclass(frozen=True, slots=True)
class SourceMountObservation:
    identity_digest: str
    proof_digest: str
    filesystem_type: str
    mount_id: int


@dataclass(frozen=True, slots=True)
class SourceMountProbeRecord:
    schema_version: str
    probe_id: str
    owner_digest: str
    container_name: str
    container_label: str
    container_id: str | None
    backend_id: str
    image_digest: str
    policy_digest: str
    host_identity_digest: str
    host_proof_digest: str
    lifecycle_state: str
    state_version: int
    worker_identity_digest: str | None = None
    worker_proof_digest: str | None = None
    observed_state: str | None = None
    destruction_proof_digest: str | None = None

    def canonical_value(self) -> dict[str, object]:
        return {
            "backend_id": self.backend_id,
            "container_id": self.container_id,
            "container_label": self.container_label,
            "container_name": self.container_name,
            "destruction_proof_digest": self.destruction_proof_digest,
            "host_identity_digest": self.host_identity_digest,
            "host_proof_digest": self.host_proof_digest,
            "image_digest": self.image_digest,
            "lifecycle_state": self.lifecycle_state,
            "observed_state": self.observed_state,
            "owner_digest": self.owner_digest,
            "policy_digest": self.policy_digest,
            "probe_id": self.probe_id,
            "schema_version": self.schema_version,
            "state_version": self.state_version,
            "worker_identity_digest": self.worker_identity_digest,
            "worker_proof_digest": self.worker_proof_digest,
        }


@dataclass(frozen=True, slots=True)
class SourceIngestCapsuleRecord:
    schema_version: str
    capsule_id: str
    container_name: str
    container_id: str | None
    request_digest: str
    source_root_identity_digest: str
    repository_descriptor_identity_digest: str
    source_mount_identity_digest: str
    backend_id: str
    image_digest: str
    policy_digest: str
    capsule_user_id: int
    lifecycle_state: str
    prepare_proof_digest: str | None = None
    process_identity_digest: str | None = None
    observed_state: str | None = None
    exit_code: int | None = None

    def canonical_value(self) -> dict[str, object]:
        return {
            "backend_id": self.backend_id,
            "capsule_user_id": self.capsule_user_id,
            "capsule_id": self.capsule_id,
            "container_id": self.container_id,
            "container_name": self.container_name,
            "exit_code": self.exit_code,
            "image_digest": self.image_digest,
            "lifecycle_state": self.lifecycle_state,
            "observed_state": self.observed_state,
            "policy_digest": self.policy_digest,
            "prepare_proof_digest": self.prepare_proof_digest,
            "process_identity_digest": self.process_identity_digest,
            "repository_descriptor_identity_digest": (self.repository_descriptor_identity_digest),
            "request_digest": self.request_digest,
            "schema_version": self.schema_version,
            "source_mount_identity_digest": self.source_mount_identity_digest,
            "source_root_identity_digest": self.source_root_identity_digest,
        }


class PreparedSourceIngestCapsule:
    """One Docker capsule that retains the authorized repository descriptor."""

    def __init__(
        self,
        *,
        backend: DockerSourceIngestBackend,
        source: AuthorizedSourceRepository,
        capsule_id: str,
        container_id: str,
        capsule_dir: Path,
        prepare_proof_digest: str,
        request_digest: str,
        source_root_identity_digest: str,
        repository_descriptor_identity_digest: str,
        source_mount_identity_digest: str,
        output_owner_uid: int,
        retained_file_descriptors: tuple[int, ...],
    ) -> None:
        self._backend = backend
        self._source = source
        self.capsule_id = capsule_id
        self.container_id = container_id
        self.capsule_dir = capsule_dir
        self.prepare_proof_digest = prepare_proof_digest
        self.request_digest = request_digest
        self.source_root_identity_digest = source_root_identity_digest
        self.repository_descriptor_identity_digest = repository_descriptor_identity_digest
        self.source_mount_identity_digest = source_mount_identity_digest
        self.output_owner_uid = output_owner_uid
        self._retained_file_descriptors = retained_file_descriptors
        self._process_identity_digest: str | None = None
        self._closed = False

    @property
    def process_identity_digest(self) -> str | None:
        return self._process_identity_digest

    async def run(self) -> SourceIngestExecutionResult:
        await self.start()
        return await self.wait()

    async def start(self) -> SourceIngestStartEvidence:
        self._require_open()
        self._source.verify_unchanged()
        evidence = await self._backend.start_capsule(self.capsule_id)
        self._process_identity_digest = evidence.process_identity_digest
        self._source.verify_unchanged()
        return evidence

    async def wait(self) -> SourceIngestExecutionResult:
        self._require_open()
        self._source.verify_unchanged()
        result = await self._backend.wait_capsule(self.capsule_id)
        self._process_identity_digest = result.process_identity_digest
        self._source.verify_unchanged()
        current_mount = self._backend.observe_source_mount(self._source)
        if not secrets.compare_digest(
            current_mount.identity_digest,
            self.source_mount_identity_digest,
        ) or (
            result.worker_result.source_mount_identity_digest is not None
            and not secrets.compare_digest(
                result.worker_result.source_mount_identity_digest,
                current_mount.identity_digest,
            )
        ):
            raise SourceIngestBackendError("audit_source_ingest_mount_identity_changed")
        return result

    async def stop(self) -> SourceIngestStopEvidence:
        self._require_open()
        evidence = await self._backend.stop_capsule(self.capsule_id)
        self._process_identity_digest = (
            evidence.process_identity_digest or self._process_identity_digest
        )
        return evidence

    async def cleanup(self, *, terminal_proof_persisted: bool = False) -> None:
        if self._closed:
            return
        await self._backend.cleanup_capsule(
            self.capsule_id,
            terminal_proof_persisted=terminal_proof_persisted,
        )
        self._source.close()
        for descriptor in self._retained_file_descriptors:
            _close_descriptor(descriptor)
        self._retained_file_descriptors = ()
        self._closed = True

    def _require_open(self) -> None:
        if self._closed:
            raise SourceIngestBackendError("audit_source_ingest_capsule_closed")


class DockerSourceIngestBackend:
    """Pinned, local-Linux Docker implementation of SourceIngest."""

    def __init__(self, *, audit: AuditConfig, state_root: Path) -> None:
        self.audit = audit
        self.state_root = state_root.expanduser().resolve()
        self.policy_digest = audit_source_ingest_policy_digest(audit.source_ingest)
        self.worker_path = Path(__file__).resolve().parents[1] / "audit_worker" / "preflight.py"
        self.docker_path = shutil.which("docker", path="/usr/local/bin:/usr/bin:/bin")
        self._mount_probe_lock = asyncio.Lock()

    def observe_source_mount(
        self,
        source: AuthorizedSourceRepository,
    ) -> SourceMountObservation:
        source.verify_unchanged()
        return _source_mount_observation(
            source.repository_fd,
            canonical_path=source.canonical_repository,
        )

    async def _probe_descriptor_mount_round_trip(self) -> str:
        """Prove descriptor binding and durably prove probe destruction."""

        async with self._mount_probe_lock:
            record_path = self.state_root / _MOUNT_PROBE_RECORD_NAME
            with locked_file(record_path):
                await self._reconcile_mount_probe_locked(record_path)
                source_root = self.audit.source_roots[0]
                descriptor = -1
                try:
                    descriptor = os.open(
                        source_root,
                        os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_DIRECTORY,
                    )
                    host_observation = _source_mount_observation(
                        descriptor,
                        canonical_path=str(source_root),
                    )
                    descriptor_path = Path(f"/proc/{os.getpid()}/fd/{descriptor}")
                    _require_fd_path_identity(
                        descriptor_path,
                        descriptor,
                        expected_kind="directory",
                    )
                    probe_id = secrets.token_hex(16)
                    container_name = _mount_probe_container_name(probe_id)
                    container_label = _mount_probe_label(probe_id)
                    image_digest = self.audit.source_ingest.image_digest
                    if image_digest is None:
                        raise SourceIngestBackendError("audit_source_ingest_image_unconfigured")
                    owner_digest = _domain_digest(
                        SOURCE_INGEST_MOUNT_PROBE_OWNER_VERSION,
                        {
                            "backend_id": self.audit.source_ingest.backend_id,
                            "container_label": container_label,
                            "container_name": container_name,
                            "host_identity_digest": host_observation.identity_digest,
                            "host_proof_digest": host_observation.proof_digest,
                            "image_digest": image_digest,
                            "policy_digest": self.policy_digest,
                            "probe_id": probe_id,
                            "schema_version": SOURCE_INGEST_MOUNT_PROBE_OWNER_VERSION,
                        },
                    )
                    record = SourceMountProbeRecord(
                        schema_version=SOURCE_INGEST_MOUNT_PROBE_RECORD_VERSION,
                        probe_id=probe_id,
                        owner_digest=owner_digest,
                        container_name=container_name,
                        container_label=container_label,
                        container_id=None,
                        backend_id=self.audit.source_ingest.backend_id,
                        image_digest=image_digest,
                        policy_digest=self.policy_digest,
                        host_identity_digest=host_observation.identity_digest,
                        host_proof_digest=host_observation.proof_digest,
                        lifecycle_state="create_intent",
                        state_version=0,
                    )
                    _write_new_mount_probe_record(record_path, record)
                    user_id, group_id = _capsule_user()
                    container_id = _parse_container_id(
                        await self._docker(
                            "create",
                            "--pull",
                            "never",
                            "--name",
                            container_name,
                            "--label",
                            f"riftx.audit-preflight.mount-probe={container_label}",
                            "--network",
                            "none",
                            "--read-only",
                            "--cap-drop",
                            "ALL",
                            "--security-opt",
                            "no-new-privileges=true",
                            "--user",
                            f"{user_id}:{group_id}",
                            "--pids-limit",
                            "8",
                            "--memory",
                            "64m",
                            "--memory-swap",
                            "64m",
                            "--cpus",
                            "0.25",
                            "--log-driver",
                            "none",
                            "--workdir",
                            "/",
                            "--tmpfs",
                            "/output:rw,noexec,nosuid,nodev,mode=1777,size=1048576",
                            "--mount",
                            f"type=bind,src={descriptor_path},dst=/source,readonly,bind-nonrecursive",
                            "--env",
                            "HOME=/nonexistent",
                            "--env",
                            "LANG=C",
                            "--env",
                            "LC_ALL=C",
                            "--env",
                            "PATH=/usr/local/bin:/usr/bin:/bin",
                            "--entrypoint",
                            "/usr/bin/python3",
                            self._image_reference(),
                            "-I",
                            "-B",
                            "-c",
                            _SOURCE_MOUNT_PROBE_SCRIPT,
                            timeout_seconds=20,
                        )
                    )
                    record = _transition_mount_probe_record(
                        record_path,
                        record,
                        expected_states={"create_intent"},
                        lifecycle_state="created",
                        container_id=container_id,
                        observed_state="created",
                    )
                    inspect = await self._inspect(container_id)
                    _validate_mount_probe_container(
                        inspect,
                        container_id=container_id,
                        container_name=container_name,
                        image_reference=self._image_reference(),
                        descriptor_path=descriptor_path,
                        user=f"{user_id}:{group_id}",
                        label=container_label,
                    )
                    record = _transition_mount_probe_record(
                        record_path,
                        record,
                        expected_states={"created"},
                        lifecycle_state="start_requested",
                    )
                    await self._docker("start", container_id, timeout_seconds=10)
                    wait_output = (
                        (await self._docker("wait", container_id, timeout_seconds=15))
                        .decode("ascii", errors="strict")
                        .strip()
                    )
                    if wait_output != "0":
                        raise SourceIngestBackendError("audit_source_ingest_mount_probe_failed")
                    archive = await self._docker(
                        "cp",
                        f"{container_id}:/output/identity.json",
                        "-",
                        timeout_seconds=10,
                    )
                    observed = _read_mount_probe_archive(
                        archive,
                        expected_owner_uid=user_id,
                    )
                    worker_observation = _mount_observation_from_value(observed)
                    if not secrets.compare_digest(
                        worker_observation.identity_digest,
                        host_observation.identity_digest,
                    ):
                        raise SourceIngestBackendError(
                            "audit_source_ingest_mount_identity_mismatch"
                        )
                    record = _transition_mount_probe_record(
                        record_path,
                        record,
                        expected_states={"start_requested"},
                        lifecycle_state="terminal",
                        worker_identity_digest=worker_observation.identity_digest,
                        worker_proof_digest=worker_observation.proof_digest,
                        observed_state="exited",
                    )
                    destruction_proof = await self._destroy_mount_probe_locked(
                        record_path,
                        record,
                    )
                    return _domain_digest(
                        "riftx.audit-source-mount-round-trip/v1",
                        {
                            "destruction_proof_digest": destruction_proof,
                            "host_proof_digest": host_observation.proof_digest,
                            "schema_version": "riftx.audit-source-mount-round-trip/v1",
                            "worker_proof_digest": worker_observation.proof_digest,
                        },
                    )
                except (OSError, UnicodeDecodeError) as exc:
                    raise SourceIngestBackendError(
                        "audit_source_ingest_mount_probe_failed"
                    ) from exc
                finally:
                    _close_descriptor(descriptor)
                    current = _read_optional_mount_probe_record(record_path)
                    if current is not None and current.lifecycle_state != "cleanup_complete":
                        await self._destroy_mount_probe_locked(record_path, current)

    async def reconcile_mount_probe(self) -> str | None:
        """Destroy a probe left by a crash before advertising readiness."""

        async with self._mount_probe_lock:
            record_path = self.state_root / _MOUNT_PROBE_RECORD_NAME
            with locked_file(record_path):
                return await self._reconcile_mount_probe_locked(record_path)

    async def _reconcile_mount_probe_locked(self, record_path: Path) -> str | None:
        record = _read_optional_mount_probe_record(record_path)
        if record is None:
            return None
        if record.lifecycle_state == "cleanup_complete":
            return record.destruction_proof_digest
        return await self._destroy_mount_probe_locked(record_path, record)

    async def _destroy_mount_probe_locked(
        self,
        record_path: Path,
        record: SourceMountProbeRecord,
    ) -> str:
        locator = record.container_id or record.container_name
        try:
            inspect = await self._inspect(locator)
        except SourceIngestBackendError as exc:
            if exc.code != "audit_source_ingest_container_not_found":
                raise
            if record.container_id is None:
                if record.lifecycle_state == "create_intent":
                    _transition_mount_probe_record(
                        record_path,
                        record,
                        expected_states={"create_intent"},
                        lifecycle_state="outcome_unknown",
                        observed_state="create_outcome_unknown",
                    )
                raise SourceIngestBackendError(
                    "audit_source_ingest_mount_probe_create_outcome_unknown"
                ) from exc
            proof = _mount_probe_destruction_proof(
                record,
                container_id=record.container_id,
            )
            completed = _transition_mount_probe_record(
                record_path,
                record,
                expected_states=_MOUNT_PROBE_STATES - {"cleanup_complete"},
                lifecycle_state="cleanup_complete",
                observed_state="confirmed_absent",
                destruction_proof_digest=proof,
            )
            return completed.destruction_proof_digest or proof

        container_id = _validate_owned_mount_probe_container(inspect, record=record)
        record = _transition_mount_probe_record(
            record_path,
            record,
            expected_states=_MOUNT_PROBE_STATES - {"cleanup_complete"},
            lifecycle_state="cleanup_requested",
            container_id=container_id,
            observed_state="owned_container_found",
        )
        removal_error: SourceIngestBackendError | None = None
        try:
            await self._docker("rm", "--force", container_id, timeout_seconds=10)
        except SourceIngestBackendError as exc:
            removal_error = exc
        try:
            remaining = await self._inspect(container_id)
        except SourceIngestBackendError as exc:
            if exc.code != "audit_source_ingest_container_not_found":
                raise
            proof = _mount_probe_destruction_proof(
                record,
                container_id=container_id,
            )
            completed = _transition_mount_probe_record(
                record_path,
                record,
                expected_states={"cleanup_requested", "outcome_unknown"},
                lifecycle_state="cleanup_complete",
                observed_state="confirmed_absent",
                destruction_proof_digest=proof,
            )
            return completed.destruction_proof_digest or proof
        _validate_owned_mount_probe_container(remaining, record=record)
        _transition_mount_probe_record(
            record_path,
            record,
            expected_states={"cleanup_requested", "outcome_unknown"},
            lifecycle_state="outcome_unknown",
            observed_state="cleanup_outcome_unknown",
        )
        if removal_error is not None:
            raise removal_error
        raise SourceIngestBackendError("audit_source_ingest_mount_probe_cleanup_unproven")

    async def probe_availability(self) -> SourceIngestBackendAvailability:
        try:
            await self.reconcile_mount_probe()
        except SourceIngestBackendError:
            return SourceIngestBackendAvailability(
                False,
                "audit_source_ingest_mount_probe_recovery_failed",
            )
        reason = self._static_unavailability_reason()
        if reason is not None:
            return SourceIngestBackendAvailability(False, reason)
        assert self.docker_path is not None
        try:
            worker_digest = _regular_file_digest(self.worker_path, maximum_bytes=2 * 1024 * 1024)
            output = await self._docker(
                "version",
                "--format",
                "{{.Server.Os}}:{{.Server.Arch}}",
                timeout_seconds=5,
            )
            server = output.decode("ascii", errors="strict").strip()
            if not server.startswith("linux:"):
                return SourceIngestBackendAvailability(
                    False,
                    "audit_source_ingest_linux_daemon_required",
                )
            image = await self._inspect_image()
            mount_round_trip_digest = await self._probe_descriptor_mount_round_trip()
            component_digest = _domain_digest(
                SOURCE_INGEST_BACKEND_COMPONENT_VERSION,
                {
                    "backend": self.audit.source_ingest.backend_id,
                    "docker_server": server,
                    "image_id": image["Id"],
                    "mount_round_trip_digest": mount_round_trip_digest,
                    "policy_digest": self.policy_digest,
                    "runtime": self.audit.source_ingest.runtime,
                    "schema_version": SOURCE_INGEST_BACKEND_COMPONENT_VERSION,
                    "worker_digest": worker_digest,
                },
            )
        except (SourceIngestBackendError, UnicodeDecodeError, ValueError):
            return SourceIngestBackendAvailability(
                False,
                "audit_sandbox_unavailable",
            )
        return SourceIngestBackendAvailability(
            True,
            None,
            component_digest=component_digest,
            worker_digest=worker_digest,
        )

    async def prepare(
        self,
        *,
        source: AuthorizedSourceRepository,
        capsule_id: str,
        request: SourceIngestWorkerRequest,
    ) -> PreparedSourceIngestCapsule:
        availability = await self.probe_availability()
        if not availability.available:
            source.close()
            raise SourceIngestBackendError(availability.reason_code or "audit_sandbox_unavailable")
        if request.capsule_id != capsule_id:
            source.close()
            raise SourceIngestBackendError("audit_source_ingest_capsule_binding_mismatch")
        if (
            request.source_root_identity_digest != source.source_root_identity_digest
            or request.repository_descriptor_identity_digest
            != source.repository_descriptor_identity_digest
        ):
            source.close()
            raise SourceIngestBackendError("audit_source_ingest_descriptor_binding_mismatch")
        try:
            source.verify_unchanged()
            mount_observation = self.observe_source_mount(source)
        except SourcePathAuthorizationError as exc:
            source.close()
            raise SourceIngestBackendError(exc.failure.value) from exc
        except SourceIngestBackendError:
            source.close()
            raise
        if not secrets.compare_digest(
            request.expected_source_mount_identity_digest,
            mount_observation.identity_digest,
        ):
            source.close()
            raise SourceIngestBackendError("audit_source_ingest_mount_identity_changed")

        capsule_dir = self._capsule_directory(capsule_id)
        input_dir = capsule_dir / "input"
        container_id: str | None = None
        input_descriptor = -1
        worker_descriptor = -1
        try:
            _create_capsule_directories(capsule_dir, input_dir)
            input_path = input_dir / "request.json"
            _write_new_file(
                input_path,
                request.model_dump_json().encode("utf-8"),
                mode=0o444,
            )
            input_descriptor, _ = _open_regular_file_descriptor(
                input_path,
                maximum_bytes=128 * 1024,
            )
            repository_fd_path = Path(f"/proc/{os.getpid()}/fd/{source.repository_fd}")
            _require_fd_path_identity(
                repository_fd_path,
                source.repository_fd,
                expected_kind="directory",
            )
            worker_digest = availability.worker_digest
            if worker_digest is None:
                raise SourceIngestBackendError("audit_sandbox_unavailable")
            worker_descriptor, opened_worker_digest = _open_regular_file_descriptor(
                self.worker_path,
                maximum_bytes=2 * 1024 * 1024,
            )
            if opened_worker_digest != worker_digest:
                raise SourceIngestBackendError("audit_source_ingest_worker_changed")
            input_fd_path = Path(f"/proc/{os.getpid()}/fd/{input_descriptor}")
            worker_fd_path = Path(f"/proc/{os.getpid()}/fd/{worker_descriptor}")
            _require_fd_path_identity(
                input_fd_path,
                input_descriptor,
                expected_kind="regular",
            )
            _require_fd_path_identity(
                worker_fd_path,
                worker_descriptor,
                expected_kind="regular",
            )
            user_id, group_id = _capsule_user()
            image_reference = self._image_reference()
            mount_source = str(repository_fd_path)
            container_name = _container_name(capsule_id)
            self._write_capsule_record(
                SourceIngestCapsuleRecord(
                    schema_version=SOURCE_INGEST_CAPSULE_RECORD_VERSION,
                    capsule_id=capsule_id,
                    container_name=container_name,
                    container_id=None,
                    request_digest=request.request_digest,
                    source_root_identity_digest=request.source_root_identity_digest,
                    repository_descriptor_identity_digest=(
                        request.repository_descriptor_identity_digest
                    ),
                    source_mount_identity_digest=mount_observation.identity_digest,
                    backend_id=self.audit.source_ingest.backend_id,
                    image_digest=self.audit.source_ingest.image_digest or "",
                    policy_digest=self.policy_digest,
                    capsule_user_id=user_id,
                    lifecycle_state="create_intent",
                )
            )
            container_id = _parse_container_id(
                await self._docker(
                    "create",
                    "--pull",
                    "never",
                    "--name",
                    container_name,
                    "--label",
                    f"riftx.audit-preflight.capsule={_capsule_label(capsule_id)}",
                    "--network",
                    "none",
                    "--read-only",
                    "--cap-drop",
                    "ALL",
                    "--security-opt",
                    "no-new-privileges=true",
                    "--user",
                    f"{user_id}:{group_id}",
                    "--pids-limit",
                    str(self.audit.source_ingest.max_pids),
                    "--memory",
                    f"{self.audit.source_ingest.max_memory_mib}m",
                    "--memory-swap",
                    f"{self.audit.source_ingest.max_memory_mib}m",
                    "--cpus",
                    "1.0",
                    "--ulimit",
                    "nofile=256:256",
                    "--ulimit",
                    f"nproc={self.audit.source_ingest.max_pids}:"
                    f"{self.audit.source_ingest.max_pids}",
                    "--log-driver",
                    "none",
                    "--workdir",
                    "/",
                    "--tmpfs",
                    "/tmp:rw,noexec,nosuid,nodev,size=67108864",
                    "--tmpfs",
                    "/output:rw,noexec,nosuid,nodev,mode=1777,"
                    f"size={self.audit.source_ingest.max_output_bytes}",
                    "--mount",
                    f"type=bind,src={mount_source},dst=/source,readonly,bind-nonrecursive",
                    "--mount",
                    f"type=bind,src={input_fd_path},dst=/input/request.json,readonly",
                    "--mount",
                    f"type=bind,src={worker_fd_path},dst=/opt/riftx/preflight.py,readonly",
                    "--env",
                    "HOME=/nonexistent",
                    "--env",
                    "LANG=C",
                    "--env",
                    "LC_ALL=C",
                    "--env",
                    "PATH=/usr/local/bin:/usr/bin:/bin",
                    "--entrypoint",
                    "/usr/bin/python3",
                    image_reference,
                    "-I",
                    "-B",
                    "/opt/riftx/preflight.py",
                    "/input/request.json",
                    "/output/result.json",
                    timeout_seconds=20,
                )
            )
            self._transition_capsule_record(
                capsule_id,
                expected_states={"create_intent"},
                lifecycle_state="created",
                container_id=container_id,
                observed_state="created",
            )
            source.verify_unchanged()
            inspect = await self._inspect(container_id)
            _validate_prepared_container(
                inspect,
                container_id=container_id,
                image_reference=image_reference,
                repository_fd_path=repository_fd_path,
                input_fd_path=input_fd_path,
                worker_fd_path=worker_fd_path,
                user=f"{user_id}:{group_id}",
                capsule_label=_capsule_label(capsule_id),
                max_memory_mib=self.audit.source_ingest.max_memory_mib,
                max_pids=self.audit.source_ingest.max_pids,
                max_output_bytes=self.audit.source_ingest.max_output_bytes,
            )
            prepare_proof_digest = _domain_digest(
                SOURCE_INGEST_PREPARE_PROOF_VERSION,
                {
                    "backend_id": self.audit.source_ingest.backend_id,
                    "capsule_id": capsule_id,
                    "container_id_digest": hashlib.sha256(container_id.encode()).hexdigest(),
                    "image_digest": self.audit.source_ingest.image_digest,
                    "network": "none",
                    "output": "bounded_tmpfs",
                    "output_limit_bytes": self.audit.source_ingest.max_output_bytes,
                    "policy_digest": self.policy_digest,
                    "repository_descriptor_identity_digest": (
                        source.repository_descriptor_identity_digest
                    ),
                    "source_mount_identity_digest": mount_observation.identity_digest,
                    "source_mount_proof_digest": mount_observation.proof_digest,
                    "request_digest": request.request_digest,
                    "rootfs": "read_only",
                    "schema_version": SOURCE_INGEST_PREPARE_PROOF_VERSION,
                    "source_mount": "descriptor_bound_read_only",
                    "worker_digest": worker_digest,
                },
            )
            self._transition_capsule_record(
                capsule_id,
                expected_states={"created"},
                lifecycle_state="prepared",
                prepare_proof_digest=prepare_proof_digest,
                observed_state="created",
            )
            return PreparedSourceIngestCapsule(
                backend=self,
                source=source,
                capsule_id=capsule_id,
                container_id=container_id,
                capsule_dir=capsule_dir,
                prepare_proof_digest=prepare_proof_digest,
                request_digest=request.request_digest,
                source_root_identity_digest=request.source_root_identity_digest,
                repository_descriptor_identity_digest=(
                    request.repository_descriptor_identity_digest
                ),
                source_mount_identity_digest=mount_observation.identity_digest,
                output_owner_uid=user_id,
                retained_file_descriptors=(input_descriptor, worker_descriptor),
            )
        except SourceIngestBackendError as exc:
            _close_descriptor(input_descriptor)
            _close_descriptor(worker_descriptor)
            source.close()
            if container_id is not None and exc.container_id is None:
                raise SourceIngestBackendError(
                    exc.code,
                    capsule_id=capsule_id,
                    container_id=container_id,
                ) from exc
            raise
        except Exception as exc:
            _close_descriptor(input_descriptor)
            _close_descriptor(worker_descriptor)
            source.close()
            raise SourceIngestBackendError(
                "audit_source_ingest_prepare_failed",
                capsule_id=capsule_id,
                container_id=container_id,
            ) from exc

    def get_capsule_record(self, capsule_id: str) -> SourceIngestCapsuleRecord | None:
        path = self._capsule_directory(capsule_id) / _CAPSULE_RECORD_NAME
        try:
            return _read_capsule_record(path)
        except FileNotFoundError:
            return None

    def list_capsule_records(self) -> tuple[SourceIngestCapsuleRecord, ...]:
        root = self.state_root / "audit-preflight-capsules"
        try:
            entries = tuple(root.iterdir())
        except FileNotFoundError:
            return ()
        except OSError as exc:
            raise SourceIngestBackendError("audit_source_ingest_capsule_state_unavailable") from exc
        records: list[SourceIngestCapsuleRecord] = []
        for entry in sorted(entries, key=lambda value: value.name):
            try:
                value = entry.lstat()
            except OSError as exc:
                raise SourceIngestBackendError(
                    "audit_source_ingest_capsule_state_unavailable"
                ) from exc
            if not stat.S_ISDIR(value.st_mode) or stat.S_ISLNK(value.st_mode):
                raise SourceIngestBackendError("audit_source_ingest_capsule_state_invalid")
            _validate_capsule_id(entry.name)
            records.append(_read_capsule_record(entry / _CAPSULE_RECORD_NAME))
        return tuple(records)

    async def recover_create_intent(self, capsule_id: str) -> SourceIngestProbe:
        """Adopt a container created after an uncertain ``docker create`` reply.

        The deterministic name and high-entropy capsule label let recovery find
        the exact container without guessing a locator. A missing name is never
        promoted to ``never_created``: Docker may have created and an external
        actor may have removed it, so absence remains outcome-unknown.
        """

        record = self.get_capsule_record(capsule_id)
        if record is None:
            raise SourceIngestBackendError("audit_source_ingest_capsule_state_missing")
        if record.container_id is not None:
            return await self.probe_container(record.container_id)
        if record.lifecycle_state not in {"create_intent", "outcome_unknown"}:
            raise SourceIngestBackendError("audit_source_ingest_capsule_state_conflict")
        try:
            inspect = await self._inspect(record.container_name)
        except SourceIngestBackendError as exc:
            if exc.code != "audit_source_ingest_container_not_found":
                raise
            self._transition_capsule_record(
                capsule_id,
                expected_states={"create_intent", "outcome_unknown"},
                lifecycle_state="outcome_unknown",
                observed_state="create_not_found_unproven",
            )
            return SourceIngestProbe(
                False,
                False,
                False,
                None,
                "create_not_found_unproven",
            )

        container_id = _validate_recovered_create_intent_container(
            inspect,
            record=record,
            image_reference=self._image_reference(),
            max_memory_mib=self.audit.source_ingest.max_memory_mib,
            max_pids=self.audit.source_ingest.max_pids,
            max_output_bytes=self.audit.source_ingest.max_output_bytes,
        )
        observed_state = _container_state(inspect)
        process_identity_digest = self._process_identity_digest(inspect)
        self._transition_capsule_record(
            capsule_id,
            expected_states={"create_intent", "outcome_unknown"},
            lifecycle_state=("created" if observed_state == "created" else "outcome_unknown"),
            container_id=container_id,
            process_identity_digest=process_identity_digest,
            observed_state=f"recovered_create_{observed_state}",
        )
        return SourceIngestProbe(
            True,
            observed_state in _ACTIVE_CONTAINER_STATES,
            observed_state in _TERMINAL_CONTAINER_STATES,
            process_identity_digest,
            observed_state,
        )

    async def start_capsule(self, capsule_id: str) -> SourceIngestStartEvidence:
        record = self.get_capsule_record(capsule_id)
        if record is None or record.container_id is None:
            raise SourceIngestBackendError("audit_source_ingest_capsule_state_missing")
        self._transition_capsule_record(
            capsule_id,
            expected_states={"prepared"},
            lifecycle_state="start_requested",
        )
        await self._docker("start", record.container_id, timeout_seconds=15)
        inspect = await self._inspect(record.container_id)
        process_identity_digest = self._process_identity_digest(inspect)
        observed_state = _container_state(inspect)
        self._transition_capsule_record(
            capsule_id,
            expected_states={"start_requested"},
            lifecycle_state=(
                "running"
                if observed_state in _ACTIVE_CONTAINER_STATES | _TERMINAL_CONTAINER_STATES
                else "outcome_unknown"
            ),
            process_identity_digest=process_identity_digest,
            observed_state=observed_state,
        )
        if observed_state not in _ACTIVE_CONTAINER_STATES | _TERMINAL_CONTAINER_STATES:
            raise SourceIngestBackendError("audit_source_ingest_started_state_invalid")
        return SourceIngestStartEvidence(
            process_identity_digest=process_identity_digest,
            observed_state=observed_state,
        )

    async def wait_capsule(self, capsule_id: str) -> SourceIngestExecutionResult:
        record = self.get_capsule_record(capsule_id)
        if record is None or record.container_id is None or record.prepare_proof_digest is None:
            raise SourceIngestBackendError("audit_source_ingest_capsule_state_missing")
        if record.lifecycle_state == "terminal":
            probe = await self.probe_container(record.container_id)
            if not probe.exists or not probe.terminal:
                raise SourceIngestBackendError("audit_source_ingest_terminal_state_unavailable")
            if record.exit_code is None or record.process_identity_digest is None:
                raise SourceIngestBackendError("audit_source_ingest_capsule_state_invalid")
            exit_code = record.exit_code
            process_identity_digest = record.process_identity_digest
        else:
            if record.lifecycle_state not in {"running", "start_requested", "outcome_unknown"}:
                raise SourceIngestBackendError("audit_source_ingest_capsule_state_conflict")
            try:
                wait_output = await self._docker(
                    "wait",
                    record.container_id,
                    timeout_seconds=self.audit.source_ingest.max_wall_seconds,
                    failure_code="audit_source_ingest_wall_timeout",
                )
            except SourceIngestBackendError as exc:
                if exc.code == "audit_source_ingest_wall_timeout":
                    await self.stop_capsule(capsule_id)
                raise
            exit_code = _parse_exit_code(wait_output)
            inspect = await self._inspect(record.container_id)
            state = _container_state(inspect)
            if state not in _TERMINAL_CONTAINER_STATES:
                raise SourceIngestBackendError("audit_source_ingest_terminal_state_invalid")
            process_identity_digest = self._process_identity_digest(inspect)
            self._transition_capsule_record(
                capsule_id,
                expected_states={"running", "start_requested", "outcome_unknown"},
                lifecycle_state="terminal",
                process_identity_digest=process_identity_digest,
                observed_state=state,
                exit_code=exit_code,
            )
        if exit_code != 0:
            raise SourceIngestBackendError("audit_source_ingest_worker_exit_nonzero")
        result_archive = await self._docker(
            "cp",
            f"{record.container_id}:/output/result.json",
            "-",
            timeout_seconds=10,
            failure_code="audit_source_ingest_result_unavailable",
        )
        result = _read_worker_result_archive(
            result_archive,
            maximum_bytes=self.audit.source_ingest.max_result_bytes,
            expected_owner_uid=record.capsule_user_id,
        )
        if (
            result.request_digest != record.request_digest
            or result.source_root_identity_digest != record.source_root_identity_digest
            or result.repository_descriptor_identity_digest
            != record.repository_descriptor_identity_digest
            or (
                result.source_mount_identity_digest is not None
                and not secrets.compare_digest(
                    result.source_mount_identity_digest,
                    record.source_mount_identity_digest,
                )
            )
        ):
            raise SourceIngestBackendError("audit_source_ingest_result_binding_mismatch")
        return SourceIngestExecutionResult(
            worker_result=result,
            prepare_proof_digest=record.prepare_proof_digest,
            process_identity_digest=process_identity_digest,
            container_id=record.container_id,
            exit_code=exit_code,
        )

    async def stop_capsule(self, capsule_id: str) -> SourceIngestStopEvidence:
        record = self.get_capsule_record(capsule_id)
        if record is None:
            raise SourceIngestBackendError("audit_source_ingest_capsule_state_missing")
        if record.container_id is None:
            self._transition_capsule_record(
                capsule_id,
                expected_states={"create_intent", "outcome_unknown"},
                lifecycle_state="outcome_unknown",
                observed_state="create_outcome_unknown",
            )
            return SourceIngestStopEvidence(False, None, "create_outcome_unknown")

        probe = await self.probe_container(record.container_id)
        if not probe.exists:
            self._transition_capsule_record(
                capsule_id,
                expected_states={
                    "created",
                    "prepared",
                    "start_requested",
                    "running",
                    "terminal",
                    "stop_requested",
                    "outcome_unknown",
                },
                lifecycle_state="outcome_unknown",
                observed_state="not_found_unproven",
            )
            return SourceIngestStopEvidence(
                False,
                record.process_identity_digest,
                "not_found_unproven",
            )

        if probe.observed_state == "created":
            if record.lifecycle_state in {"start_requested", "stop_requested"}:
                try:
                    updated = self._transition_capsule_record(
                        capsule_id,
                        expected_states={record.lifecycle_state},
                        lifecycle_state="outcome_unknown",
                        process_identity_digest=probe.process_identity_digest,
                        observed_state="start_outcome_unknown_created",
                    )
                except SourceIngestBackendError as exc:
                    if exc.code != "audit_source_ingest_capsule_state_conflict":
                        raise
                    return SourceIngestStopEvidence(
                        False,
                        probe.process_identity_digest,
                        "created_stop_unproven",
                    )
                return SourceIngestStopEvidence(
                    False,
                    updated.process_identity_digest,
                    "start_outcome_unknown_created",
                )
            if record.lifecycle_state == "outcome_unknown":
                return SourceIngestStopEvidence(
                    False,
                    probe.process_identity_digest,
                    "start_outcome_unknown_created",
                )
            if record.lifecycle_state not in {"created", "prepared"}:
                return SourceIngestStopEvidence(
                    False,
                    probe.process_identity_digest,
                    "created_stop_unproven",
                )
            try:
                updated = self._transition_capsule_record(
                    capsule_id,
                    expected_states={record.lifecycle_state},
                    lifecycle_state="stop_observed",
                    process_identity_digest=probe.process_identity_digest,
                    observed_state="created_not_started",
                )
            except SourceIngestBackendError as exc:
                if exc.code != "audit_source_ingest_capsule_state_conflict":
                    raise
                return SourceIngestStopEvidence(
                    False,
                    probe.process_identity_digest,
                    "created_stop_unproven",
                )
            return SourceIngestStopEvidence(
                True,
                updated.process_identity_digest,
                "created_not_started",
            )

        if probe.terminal:
            updated = self._transition_capsule_record(
                capsule_id,
                expected_states={
                    "start_requested",
                    "running",
                    "terminal",
                    "stop_requested",
                    "outcome_unknown",
                },
                lifecycle_state="stop_observed",
                process_identity_digest=probe.process_identity_digest,
                observed_state=probe.observed_state,
            )
            return SourceIngestStopEvidence(
                True,
                updated.process_identity_digest,
                probe.observed_state,
            )

        if not probe.running:
            self._transition_capsule_record(
                capsule_id,
                expected_states={
                    "start_requested",
                    "running",
                    "stop_requested",
                    "outcome_unknown",
                },
                lifecycle_state="outcome_unknown",
                process_identity_digest=probe.process_identity_digest,
                observed_state=probe.observed_state,
            )
            return SourceIngestStopEvidence(
                False,
                probe.process_identity_digest,
                probe.observed_state,
            )

        self._transition_capsule_record(
            capsule_id,
            expected_states={
                "start_requested",
                "running",
                "stop_requested",
                "outcome_unknown",
            },
            lifecycle_state="stop_requested",
            process_identity_digest=probe.process_identity_digest,
            observed_state=probe.observed_state,
        )
        try:
            await self._docker(
                "stop",
                "--time",
                "2",
                record.container_id,
                timeout_seconds=5,
            )
        except SourceIngestBackendError:
            await self._docker(
                "kill",
                "--signal",
                "KILL",
                record.container_id,
                timeout_seconds=5,
            )
        final = await self.probe_container(record.container_id)
        if not final.exists or not final.terminal:
            self._transition_capsule_record(
                capsule_id,
                expected_states={"stop_requested"},
                lifecycle_state="outcome_unknown",
                process_identity_digest=(
                    final.process_identity_digest or probe.process_identity_digest
                ),
                observed_state=(final.observed_state if final.exists else "not_found_unproven"),
            )
            return SourceIngestStopEvidence(
                False,
                final.process_identity_digest or probe.process_identity_digest,
                final.observed_state if final.exists else "not_found_unproven",
            )
        updated = self._transition_capsule_record(
            capsule_id,
            expected_states={"stop_requested"},
            lifecycle_state="stop_observed",
            process_identity_digest=final.process_identity_digest,
            observed_state=final.observed_state,
        )
        return SourceIngestStopEvidence(
            True,
            updated.process_identity_digest,
            final.observed_state,
        )

    async def cleanup_capsule(
        self,
        capsule_id: str,
        *,
        terminal_proof_persisted: bool,
    ) -> None:
        if not terminal_proof_persisted:
            raise SourceIngestBackendError("audit_source_ingest_cleanup_requires_persisted_proof")
        record = self.get_capsule_record(capsule_id)
        if record is None:
            raise SourceIngestBackendError("audit_source_ingest_capsule_state_missing")
        if record.lifecycle_state == "cleanup_complete":
            return
        if record.container_id is None:
            raise SourceIngestBackendError("audit_source_ingest_cleanup_outcome_unknown")
        container_id = record.container_id
        if record.lifecycle_state != "cleanup_authorized":
            record = self._transition_capsule_record(
                capsule_id,
                expected_states={"terminal", "stop_observed"},
                lifecycle_state="cleanup_authorized",
            )
        probe = await self.probe_container(container_id)
        if probe.exists and probe.observed_state not in {
            "created",
            *_TERMINAL_CONTAINER_STATES,
        }:
            raise SourceIngestBackendError("audit_source_ingest_cleanup_requires_stop")
        if probe.exists:
            await self._docker(
                "rm",
                "--force",
                container_id,
                timeout_seconds=10,
            )
            remaining = await self.probe_container(container_id)
            if remaining.exists:
                raise SourceIngestBackendError("audit_source_ingest_cleanup_unproven")
        self._transition_capsule_record(
            capsule_id,
            expected_states={"cleanup_authorized"},
            lifecycle_state="cleanup_complete",
            observed_state="removed_after_persisted_proof",
        )

    def _capsule_directory(self, capsule_id: str) -> Path:
        _validate_capsule_id(capsule_id)
        return self.state_root / "audit-preflight-capsules" / capsule_id

    def _write_capsule_record(self, record: SourceIngestCapsuleRecord) -> None:
        _validate_capsule_record(record)
        path = self._capsule_directory(record.capsule_id) / _CAPSULE_RECORD_NAME
        with locked_file(path):
            if path.exists():
                raise SourceIngestBackendError("audit_source_ingest_capsule_state_conflict")
            atomic_write_json(path, record.canonical_value())

    def _transition_capsule_record(
        self,
        capsule_id: str,
        *,
        expected_states: set[str],
        lifecycle_state: str,
        container_id: str | None = None,
        prepare_proof_digest: str | None = None,
        process_identity_digest: str | None = None,
        observed_state: str | None = None,
        exit_code: int | None = None,
    ) -> SourceIngestCapsuleRecord:
        if lifecycle_state not in _CAPSULE_STATES or not expected_states <= _CAPSULE_STATES:
            raise SourceIngestBackendError("audit_source_ingest_capsule_state_invalid")
        path = self._capsule_directory(capsule_id) / _CAPSULE_RECORD_NAME
        with locked_file(path):
            try:
                current = _read_capsule_record(path)
            except FileNotFoundError as exc:
                raise SourceIngestBackendError("audit_source_ingest_capsule_state_missing") from exc
            updates = {
                "container_id": container_id,
                "prepare_proof_digest": prepare_proof_digest,
                "process_identity_digest": process_identity_digest,
                "observed_state": observed_state,
                "exit_code": exit_code,
            }
            if current.lifecycle_state == lifecycle_state:
                values = current.canonical_value()
                changed = False
                for field_name, value in updates.items():
                    if value is None:
                        continue
                    existing = getattr(current, field_name)
                    if existing is not None and existing != value:
                        raise SourceIngestBackendError("audit_source_ingest_capsule_state_conflict")
                    if existing is None:
                        values[field_name] = value
                        changed = True
                if not changed:
                    return current
                updated = _capsule_record_from_value(values)
                atomic_write_json(path, updated.canonical_value())
                return updated
            if current.lifecycle_state not in expected_states:
                raise SourceIngestBackendError("audit_source_ingest_capsule_state_conflict")
            values = current.canonical_value()
            values["lifecycle_state"] = lifecycle_state
            for field_name, value in updates.items():
                if value is not None:
                    existing = values[field_name]
                    if (
                        field_name in {"container_id", "prepare_proof_digest", "exit_code"}
                        and existing is not None
                        and existing != value
                    ):
                        raise SourceIngestBackendError("audit_source_ingest_capsule_state_conflict")
                    values[field_name] = value
            updated = _capsule_record_from_value(values)
            atomic_write_json(path, updated.canonical_value())
            return updated

    async def probe_container(self, container_id: str) -> SourceIngestProbe:
        try:
            inspect = await self._inspect(container_id)
        except SourceIngestBackendError as exc:
            if exc.code == "audit_source_ingest_container_not_found":
                return SourceIngestProbe(False, False, False, None, "not_found")
            raise
        state = _container_state(inspect)
        return SourceIngestProbe(
            exists=True,
            running=state in _ACTIVE_CONTAINER_STATES,
            terminal=state in _TERMINAL_CONTAINER_STATES,
            process_identity_digest=self._process_identity_digest(inspect),
            observed_state=state,
        )

    def never_created_proof_digest(
        self,
        *,
        capsule_id: str,
        effect_owner_digest: str,
        lease_envelope_digest: str,
    ) -> str:
        return _domain_digest(
            SOURCE_INGEST_NEVER_CREATED_PROOF_VERSION,
            {
                "capsule_id": capsule_id,
                "effect_owner_digest": effect_owner_digest,
                "lease_envelope_digest": lease_envelope_digest,
                "policy_digest": self.policy_digest,
                "schema_version": SOURCE_INGEST_NEVER_CREATED_PROOF_VERSION,
            },
        )

    def _static_unavailability_reason(self) -> str | None:
        if platform.system() != "Linux" or os.name != "posix":
            return "audit_source_ingest_linux_host_required"
        if self.audit.source_ingest.image_digest is None:
            return "audit_source_ingest_image_unconfigured"
        if not self.audit.source_roots:
            return "audit_source_roots_empty"
        if self.docker_path is None:
            return "audit_source_ingest_docker_unavailable"
        try:
            socket_stat = _DOCKER_SOCKET.stat(follow_symlinks=False)
        except OSError:
            return "audit_source_ingest_local_docker_socket_unavailable"
        if not stat.S_ISSOCK(socket_stat.st_mode):
            return "audit_source_ingest_local_docker_socket_unavailable"
        return None

    async def _inspect_image(self) -> dict[str, Any]:
        payload = await self._docker(
            "image",
            "inspect",
            self._image_reference(),
            timeout_seconds=10,
        )
        try:
            values = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise SourceIngestBackendError("audit_source_ingest_image_invalid") from exc
        if not isinstance(values, list) or len(values) != 1 or not isinstance(values[0], dict):
            raise SourceIngestBackendError("audit_source_ingest_image_invalid")
        value = values[0]
        expected = self._image_reference()
        if value.get("Id") != expected or value.get("Os") != "linux":
            raise SourceIngestBackendError("audit_source_ingest_image_invalid")
        return value

    async def _inspect(self, container_id: str) -> dict[str, Any]:
        try:
            payload = await self._docker(
                "inspect",
                container_id,
                timeout_seconds=10,
            )
        except SourceIngestBackendError as exc:
            if exc.code == "audit_source_ingest_docker_command_failed":
                if await self._container_is_absent(container_id):
                    raise SourceIngestBackendError(
                        "audit_source_ingest_container_not_found"
                    ) from exc
                raise SourceIngestBackendError("audit_source_ingest_inspect_unavailable") from exc
            raise
        try:
            values = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise SourceIngestBackendError("audit_source_ingest_inspect_invalid") from exc
        if not isinstance(values, list) or len(values) != 1 or not isinstance(values[0], dict):
            raise SourceIngestBackendError("audit_source_ingest_inspect_invalid")
        return values[0]

    async def _container_is_absent(self, locator: str) -> bool:
        is_container_id = _is_container_id(locator)
        if not is_container_id:
            _validate_container_name(locator)
        filter_value = f"id={locator}" if is_container_id else f"name=^/{locator}$"
        output = await self._docker(
            "container",
            "ls",
            "--all",
            "--no-trunc",
            "--filter",
            filter_value,
            "--format",
            "{{.ID}}\t{{.Names}}",
            timeout_seconds=10,
            failure_code="audit_source_ingest_inspect_unavailable",
        )
        try:
            lines = output.decode("ascii", errors="strict").splitlines()
        except UnicodeDecodeError as exc:
            raise SourceIngestBackendError("audit_source_ingest_inspect_invalid") from exc
        if not lines:
            return True
        for line in lines:
            identifier, separator, names = line.partition("\t")
            if (
                not separator
                or not _is_container_id(identifier)
                or not names
                or any(name != locator and not is_container_id for name in names.split(","))
            ):
                raise SourceIngestBackendError("audit_source_ingest_inspect_invalid")
            if is_container_id and identifier != locator:
                raise SourceIngestBackendError("audit_source_ingest_inspect_invalid")
        return False

    def _process_identity_digest(self, inspect: dict[str, Any]) -> str:
        state = inspect.get("State")
        if not isinstance(state, dict):
            raise SourceIngestBackendError("audit_source_ingest_inspect_invalid")
        container_id = _require_container_id(inspect.get("Id"))
        pid = state.get("Pid")
        if type(pid) is not int or pid < 0:
            raise SourceIngestBackendError("audit_source_ingest_inspect_invalid")
        started_at = state.get("StartedAt")
        finished_at = state.get("FinishedAt")
        if not isinstance(started_at, str) or not isinstance(finished_at, str):
            raise SourceIngestBackendError("audit_source_ingest_inspect_invalid")
        return _domain_digest(
            SOURCE_INGEST_PROCESS_IDENTITY_VERSION,
            {
                "container_id": container_id,
                "image_digest": self.audit.source_ingest.image_digest,
                "schema_version": SOURCE_INGEST_PROCESS_IDENTITY_VERSION,
                "started_at": started_at,
            },
        )

    async def _docker(
        self,
        *arguments: str,
        timeout_seconds: int,
        failure_code: str = "audit_source_ingest_docker_command_failed",
    ) -> bytes:
        if self.docker_path is None:
            raise SourceIngestBackendError("audit_source_ingest_docker_unavailable")
        if any(not argument or "\x00" in argument for argument in arguments):
            raise SourceIngestBackendError("audit_source_ingest_docker_argument_invalid")
        argv = (
            self.docker_path,
            "--host",
            f"unix://{_DOCKER_SOCKET}",
            *arguments,
        )
        environment = {
            "DOCKER_API_VERSION": "1.45",
            "HOME": "/nonexistent",
            "LANG": "C",
            "LC_ALL": "C",
            "PATH": "/usr/local/bin:/usr/bin:/bin",
        }
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                cwd="/",
                env=environment,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                close_fds=True,
                start_new_session=True,
            )
        except OSError as exc:
            raise SourceIngestBackendError("audit_source_ingest_docker_spawn_failed") from exc
        try:
            stdout, stderr = await asyncio.wait_for(
                _read_process_bounded(process, maximum_bytes=_DOCKER_OUTPUT_LIMIT),
                timeout=timeout_seconds,
            )
        except TimeoutError as exc:
            process.kill()
            await process.wait()
            raise SourceIngestBackendError(failure_code) from exc
        if process.returncode != 0:
            del stderr
            raise SourceIngestBackendError(failure_code)
        return stdout

    def _image_reference(self) -> str:
        digest = self.audit.source_ingest.image_digest
        if digest is None:
            raise SourceIngestBackendError("audit_source_ingest_image_unconfigured")
        return f"sha256:{digest}"


async def _read_process_bounded(
    process: asyncio.subprocess.Process,
    *,
    maximum_bytes: int,
) -> tuple[bytes, bytes]:
    if process.stdout is None or process.stderr is None:
        process.kill()
        await process.wait()
        raise SourceIngestBackendError("audit_source_ingest_docker_pipe_unavailable")
    total = 0
    lock = asyncio.Lock()

    async def read(stream: asyncio.StreamReader) -> bytes:
        nonlocal total
        result = bytearray()
        while True:
            chunk = await stream.read(64 * 1024)
            if not chunk:
                return bytes(result)
            async with lock:
                total += len(chunk)
                if total > maximum_bytes:
                    process.kill()
                    raise SourceIngestBackendError(
                        "audit_source_ingest_docker_output_limit_exceeded"
                    )
            result.extend(chunk)

    try:
        stdout, stderr = await asyncio.gather(read(process.stdout), read(process.stderr))
        await process.wait()
        return stdout, stderr
    except Exception:
        if process.returncode is None:
            process.kill()
        await process.wait()
        raise


def _validate_capsule_id(value: str) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 128
        or not value[0].isalnum()
        or any(
            not (character.isascii() and (character.isalnum() or character in "._:@+~-"))
            for character in value
        )
    ):
        raise SourceIngestBackendError("audit_source_ingest_capsule_id_invalid")
    return value


def _container_name(capsule_id: str) -> str:
    _validate_capsule_id(capsule_id)
    identity = hashlib.sha256(capsule_id.encode("ascii")).hexdigest()[:40]
    return f"riftx-preflight-{identity}"


def _capsule_label(capsule_id: str) -> str:
    _validate_capsule_id(capsule_id)
    return hashlib.sha256(
        b"riftx.audit-source-ingest-capsule-label/v1\0" + capsule_id.encode("ascii")
    ).hexdigest()


def _mount_probe_container_name(probe_id: str) -> str:
    _validate_mount_probe_id(probe_id)
    return f"riftx-preflight-probe-{probe_id}"


def _mount_probe_label(probe_id: str) -> str:
    _validate_mount_probe_id(probe_id)
    return hashlib.sha256(
        b"riftx.audit-source-mount-probe-label/v1\0" + probe_id.encode("ascii")
    ).hexdigest()


def _validate_mount_probe_id(value: str) -> str:
    if len(value) != 32 or any(character not in "0123456789abcdef" for character in value):
        raise SourceIngestBackendError("audit_source_ingest_mount_probe_state_invalid")
    return value


def _read_optional_mount_probe_record(path: Path) -> SourceMountProbeRecord | None:
    try:
        return _read_mount_probe_record(path)
    except FileNotFoundError:
        return None


def _read_mount_probe_record(path: Path) -> SourceMountProbeRecord:
    descriptor = -1
    try:
        value = path.lstat()
        if (
            not stat.S_ISREG(value.st_mode)
            or stat.S_ISLNK(value.st_mode)
            or value.st_nlink != 1
            or value.st_size <= 0
            or value.st_size > 16 * 1024
        ):
            raise SourceIngestBackendError("audit_source_ingest_mount_probe_state_invalid")
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        opened = os.fstat(descriptor)
        if _stat_identity(opened) != _stat_identity(value):
            raise SourceIngestBackendError("audit_source_ingest_mount_probe_state_changed")
        raw = bytearray()
        while True:
            chunk = os.read(descriptor, min(4096, 16 * 1024 + 1 - len(raw)))
            if not chunk:
                break
            raw.extend(chunk)
            if len(raw) > 16 * 1024:
                raise SourceIngestBackendError("audit_source_ingest_mount_probe_state_invalid")
        completed = os.fstat(descriptor)
        if _stat_identity(completed) != _stat_identity(opened):
            raise SourceIngestBackendError("audit_source_ingest_mount_probe_state_changed")
        parsed = json.loads(bytes(raw))
    except FileNotFoundError:
        raise
    except SourceIngestBackendError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SourceIngestBackendError("audit_source_ingest_mount_probe_state_invalid") from exc
    finally:
        _close_descriptor(descriptor)
    return _mount_probe_record_from_value(parsed)


def _mount_probe_record_from_value(value: object) -> SourceMountProbeRecord:
    expected_keys = {
        "backend_id",
        "container_id",
        "container_label",
        "container_name",
        "destruction_proof_digest",
        "host_identity_digest",
        "host_proof_digest",
        "image_digest",
        "lifecycle_state",
        "observed_state",
        "owner_digest",
        "policy_digest",
        "probe_id",
        "schema_version",
        "state_version",
        "worker_identity_digest",
        "worker_proof_digest",
    }
    if not isinstance(value, dict) or set(value) != expected_keys:
        raise SourceIngestBackendError("audit_source_ingest_mount_probe_state_invalid")
    state_version = value["state_version"]
    if type(state_version) is not int or not 0 <= state_version <= 2**63 - 1:
        raise SourceIngestBackendError("audit_source_ingest_mount_probe_state_invalid")
    record = SourceMountProbeRecord(
        schema_version=_require_string(value["schema_version"], maximum=128),
        probe_id=_require_string(value["probe_id"], maximum=32),
        owner_digest=_require_string(value["owner_digest"], maximum=64),
        container_name=_require_string(value["container_name"], maximum=128),
        container_label=_require_string(value["container_label"], maximum=64),
        container_id=_optional_string(value["container_id"], maximum=64),
        backend_id=_require_string(value["backend_id"], maximum=128),
        image_digest=_require_string(value["image_digest"], maximum=64),
        policy_digest=_require_string(value["policy_digest"], maximum=64),
        host_identity_digest=_require_string(value["host_identity_digest"], maximum=64),
        host_proof_digest=_require_string(value["host_proof_digest"], maximum=64),
        lifecycle_state=_require_string(value["lifecycle_state"], maximum=64),
        state_version=state_version,
        worker_identity_digest=_optional_string(value["worker_identity_digest"], maximum=64),
        worker_proof_digest=_optional_string(value["worker_proof_digest"], maximum=64),
        observed_state=_optional_string(value["observed_state"], maximum=64),
        destruction_proof_digest=_optional_string(value["destruction_proof_digest"], maximum=64),
    )
    _validate_mount_probe_record(record)
    return record


def _validate_mount_probe_record(record: SourceMountProbeRecord) -> None:
    if record.schema_version != SOURCE_INGEST_MOUNT_PROBE_RECORD_VERSION:
        raise SourceIngestBackendError("audit_source_ingest_mount_probe_state_invalid")
    _validate_mount_probe_id(record.probe_id)
    if (
        record.container_name != _mount_probe_container_name(record.probe_id)
        or record.container_label != _mount_probe_label(record.probe_id)
        or record.lifecycle_state not in _MOUNT_PROBE_STATES
    ):
        raise SourceIngestBackendError("audit_source_ingest_mount_probe_state_invalid")
    for digest in (
        record.owner_digest,
        record.container_label,
        record.image_digest,
        record.policy_digest,
        record.host_identity_digest,
        record.host_proof_digest,
        record.worker_identity_digest,
        record.worker_proof_digest,
        record.destruction_proof_digest,
    ):
        if digest is not None:
            _require_digest(digest)
    if record.container_id is not None:
        _require_container_id(record.container_id)
    expected_owner = _domain_digest(
        SOURCE_INGEST_MOUNT_PROBE_OWNER_VERSION,
        {
            "backend_id": record.backend_id,
            "container_label": record.container_label,
            "container_name": record.container_name,
            "host_identity_digest": record.host_identity_digest,
            "host_proof_digest": record.host_proof_digest,
            "image_digest": record.image_digest,
            "policy_digest": record.policy_digest,
            "probe_id": record.probe_id,
            "schema_version": SOURCE_INGEST_MOUNT_PROBE_OWNER_VERSION,
        },
    )
    if not secrets.compare_digest(record.owner_digest, expected_owner):
        raise SourceIngestBackendError("audit_source_ingest_mount_probe_state_invalid")
    has_worker_identity = record.worker_identity_digest is not None
    if has_worker_identity != (record.worker_proof_digest is not None):
        raise SourceIngestBackendError("audit_source_ingest_mount_probe_state_invalid")
    if record.lifecycle_state == "create_intent":
        if record.container_id is not None:
            raise SourceIngestBackendError("audit_source_ingest_mount_probe_state_invalid")
    elif record.container_id is None and record.lifecycle_state != "outcome_unknown":
        raise SourceIngestBackendError("audit_source_ingest_mount_probe_state_invalid")
    if record.lifecycle_state == "terminal" and not has_worker_identity:
        raise SourceIngestBackendError("audit_source_ingest_mount_probe_state_invalid")
    if record.lifecycle_state == "cleanup_complete":
        if (
            record.destruction_proof_digest is None
            or record.observed_state != "confirmed_absent"
            or not secrets.compare_digest(
                record.destruction_proof_digest,
                _expected_mount_probe_destruction_proof(record),
            )
        ):
            raise SourceIngestBackendError("audit_source_ingest_mount_probe_state_invalid")
    elif record.destruction_proof_digest is not None:
        raise SourceIngestBackendError("audit_source_ingest_mount_probe_state_invalid")


def _write_new_mount_probe_record(path: Path, record: SourceMountProbeRecord) -> None:
    _validate_mount_probe_record(record)
    current = _read_optional_mount_probe_record(path)
    if current is not None and current.lifecycle_state != "cleanup_complete":
        raise SourceIngestBackendError("audit_source_ingest_mount_probe_state_conflict")
    atomic_write_json(path, record.canonical_value())


def _transition_mount_probe_record(
    path: Path,
    previous: SourceMountProbeRecord,
    *,
    expected_states: set[str] | frozenset[str],
    lifecycle_state: str,
    container_id: str | None = None,
    worker_identity_digest: str | None = None,
    worker_proof_digest: str | None = None,
    observed_state: str | None = None,
    destruction_proof_digest: str | None = None,
) -> SourceMountProbeRecord:
    if lifecycle_state not in _MOUNT_PROBE_STATES or not set(expected_states) <= set(
        _MOUNT_PROBE_STATES
    ):
        raise SourceIngestBackendError("audit_source_ingest_mount_probe_state_invalid")
    current = _read_mount_probe_record(path)
    if current != previous or current.lifecycle_state not in expected_states:
        raise SourceIngestBackendError("audit_source_ingest_mount_probe_state_conflict")
    values = current.canonical_value()
    values["lifecycle_state"] = lifecycle_state
    values["state_version"] = current.state_version + 1
    updates = {
        "container_id": container_id,
        "destruction_proof_digest": destruction_proof_digest,
        "observed_state": observed_state,
        "worker_identity_digest": worker_identity_digest,
        "worker_proof_digest": worker_proof_digest,
    }
    for field_name, updated_value in updates.items():
        if updated_value is None:
            continue
        existing = values[field_name]
        if field_name in {
            "container_id",
            "destruction_proof_digest",
            "worker_identity_digest",
            "worker_proof_digest",
        } and existing not in {None, updated_value}:
            raise SourceIngestBackendError("audit_source_ingest_mount_probe_state_conflict")
        values[field_name] = updated_value
    updated = _mount_probe_record_from_value(values)
    atomic_write_json(path, updated.canonical_value())
    return updated


def _mount_probe_destruction_proof(
    record: SourceMountProbeRecord,
    *,
    container_id: str | None,
) -> str:
    return _domain_digest(
        SOURCE_INGEST_MOUNT_PROBE_DESTRUCTION_PROOF_VERSION,
        {
            "container_id_digest": (
                hashlib.sha256(container_id.encode("ascii")).hexdigest()
                if container_id is not None
                else None
            ),
            "container_label": record.container_label,
            "container_name": record.container_name,
            "disposition": "confirmed_absent",
            "owner_digest": record.owner_digest,
            "probe_id": record.probe_id,
            "schema_version": SOURCE_INGEST_MOUNT_PROBE_DESTRUCTION_PROOF_VERSION,
            "state_version": record.state_version + 1,
        },
    )


def _expected_mount_probe_destruction_proof(record: SourceMountProbeRecord) -> str:
    return _domain_digest(
        SOURCE_INGEST_MOUNT_PROBE_DESTRUCTION_PROOF_VERSION,
        {
            "container_id_digest": (
                hashlib.sha256(record.container_id.encode("ascii")).hexdigest()
                if record.container_id is not None
                else None
            ),
            "container_label": record.container_label,
            "container_name": record.container_name,
            "disposition": "confirmed_absent",
            "owner_digest": record.owner_digest,
            "probe_id": record.probe_id,
            "schema_version": SOURCE_INGEST_MOUNT_PROBE_DESTRUCTION_PROOF_VERSION,
            "state_version": record.state_version,
        },
    )


def _validate_owned_mount_probe_container(
    inspect: dict[str, Any],
    *,
    record: SourceMountProbeRecord,
) -> str:
    container_id = _require_container_id(inspect.get("Id"))
    config = inspect.get("Config")
    if (
        (record.container_id is not None and container_id != record.container_id)
        or inspect.get("Name") != f"/{record.container_name}"
        or inspect.get("Image") != f"sha256:{record.image_digest}"
        or not isinstance(config, dict)
        or not isinstance(config.get("Labels"), dict)
        or config["Labels"].get("riftx.audit-preflight.mount-probe") != record.container_label
    ):
        raise SourceIngestBackendError("audit_source_ingest_mount_probe_owner_mismatch")
    return container_id


def _read_capsule_record(path: Path) -> SourceIngestCapsuleRecord:
    descriptor = -1
    try:
        value = path.lstat()
        if (
            not stat.S_ISREG(value.st_mode)
            or stat.S_ISLNK(value.st_mode)
            or value.st_size <= 0
            or value.st_size > 16 * 1024
        ):
            raise SourceIngestBackendError("audit_source_ingest_capsule_state_invalid")
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        opened = os.fstat(descriptor)
        if _stat_identity(opened) != _stat_identity(value):
            raise SourceIngestBackendError("audit_source_ingest_capsule_state_changed")
        raw = bytearray()
        while True:
            chunk = os.read(descriptor, min(4096, 16 * 1024 + 1 - len(raw)))
            if not chunk:
                break
            raw.extend(chunk)
            if len(raw) > 16 * 1024:
                raise SourceIngestBackendError("audit_source_ingest_capsule_state_invalid")
        completed = os.fstat(descriptor)
        if _stat_identity(completed) != _stat_identity(opened):
            raise SourceIngestBackendError("audit_source_ingest_capsule_state_changed")
        parsed = json.loads(bytes(raw))
    except FileNotFoundError:
        raise
    except SourceIngestBackendError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SourceIngestBackendError("audit_source_ingest_capsule_state_invalid") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    return _capsule_record_from_value(parsed)


def _capsule_record_from_value(value: object) -> SourceIngestCapsuleRecord:
    expected_keys = {
        "backend_id",
        "capsule_id",
        "capsule_user_id",
        "container_id",
        "container_name",
        "exit_code",
        "image_digest",
        "lifecycle_state",
        "observed_state",
        "policy_digest",
        "prepare_proof_digest",
        "process_identity_digest",
        "repository_descriptor_identity_digest",
        "request_digest",
        "schema_version",
        "source_mount_identity_digest",
        "source_root_identity_digest",
    }
    if not isinstance(value, dict) or set(value) != expected_keys:
        raise SourceIngestBackendError("audit_source_ingest_capsule_state_invalid")
    exit_code = value["exit_code"]
    if exit_code is not None and (type(exit_code) is not int or not 0 <= exit_code <= 255):
        raise SourceIngestBackendError("audit_source_ingest_capsule_state_invalid")
    capsule_user_id = value["capsule_user_id"]
    if type(capsule_user_id) is not int or not 0 <= capsule_user_id <= 2**32 - 1:
        raise SourceIngestBackendError("audit_source_ingest_capsule_state_invalid")
    record = SourceIngestCapsuleRecord(
        schema_version=_require_string(value["schema_version"], maximum=128),
        capsule_id=_require_string(value["capsule_id"], maximum=128),
        container_name=_require_string(value["container_name"], maximum=128),
        container_id=_optional_string(value["container_id"], maximum=64),
        request_digest=_require_string(value["request_digest"], maximum=64),
        source_root_identity_digest=_require_string(
            value["source_root_identity_digest"], maximum=64
        ),
        repository_descriptor_identity_digest=_require_string(
            value["repository_descriptor_identity_digest"], maximum=64
        ),
        source_mount_identity_digest=_require_string(
            value["source_mount_identity_digest"], maximum=64
        ),
        backend_id=_require_string(value["backend_id"], maximum=128),
        image_digest=_require_string(value["image_digest"], maximum=64),
        policy_digest=_require_string(value["policy_digest"], maximum=64),
        capsule_user_id=capsule_user_id,
        lifecycle_state=_require_string(value["lifecycle_state"], maximum=64),
        prepare_proof_digest=_optional_string(value["prepare_proof_digest"], maximum=64),
        process_identity_digest=_optional_string(value["process_identity_digest"], maximum=64),
        observed_state=_optional_string(value["observed_state"], maximum=64),
        exit_code=exit_code,
    )
    _validate_capsule_record(record)
    return record


def _validate_capsule_record(record: SourceIngestCapsuleRecord) -> None:
    if record.schema_version != SOURCE_INGEST_CAPSULE_RECORD_VERSION:
        raise SourceIngestBackendError("audit_source_ingest_capsule_state_invalid")
    _validate_capsule_id(record.capsule_id)
    if record.container_name != _container_name(record.capsule_id):
        raise SourceIngestBackendError("audit_source_ingest_capsule_state_invalid")
    if record.container_id is not None:
        _require_container_id(record.container_id)
    for digest in (
        record.request_digest,
        record.source_root_identity_digest,
        record.repository_descriptor_identity_digest,
        record.source_mount_identity_digest,
        record.image_digest,
        record.policy_digest,
        record.prepare_proof_digest,
        record.process_identity_digest,
    ):
        if digest is not None:
            _require_digest(digest)
    if record.lifecycle_state not in _CAPSULE_STATES:
        raise SourceIngestBackendError("audit_source_ingest_capsule_state_invalid")
    if record.lifecycle_state == "create_intent":
        if record.container_id is not None:
            raise SourceIngestBackendError("audit_source_ingest_capsule_state_invalid")
    elif record.container_id is None:
        if record.lifecycle_state != "outcome_unknown":
            raise SourceIngestBackendError("audit_source_ingest_capsule_state_invalid")
    if record.lifecycle_state in {"prepared", "start_requested", "running", "terminal"} and (
        record.prepare_proof_digest is None
    ):
        raise SourceIngestBackendError("audit_source_ingest_capsule_state_invalid")
    if record.lifecycle_state in {"running", "terminal", "stop_observed"} and (
        record.process_identity_digest is None
    ):
        raise SourceIngestBackendError("audit_source_ingest_capsule_state_invalid")
    if record.lifecycle_state == "terminal" and record.exit_code is None:
        raise SourceIngestBackendError("audit_source_ingest_capsule_state_invalid")
    if record.exit_code is not None and record.lifecycle_state not in {
        "terminal",
        "stop_observed",
        "outcome_unknown",
        "cleanup_authorized",
        "cleanup_complete",
    }:
        raise SourceIngestBackendError("audit_source_ingest_capsule_state_invalid")


def _require_string(value: object, *, maximum: int) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= maximum
        or "\x00" in value
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
    ):
        raise SourceIngestBackendError("audit_source_ingest_capsule_state_invalid")
    return value


def _optional_string(value: object, *, maximum: int) -> str | None:
    if value is None:
        return None
    return _require_string(value, maximum=maximum)


def _require_digest(value: str) -> str:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise SourceIngestBackendError("audit_source_ingest_capsule_state_invalid")
    return value


def _stat_identity(value: os.stat_result) -> tuple[int, ...]:
    return (
        int(value.st_dev),
        int(value.st_ino),
        int(value.st_mode),
        int(value.st_uid),
        int(value.st_gid),
        int(value.st_size),
        int(value.st_mtime_ns),
        int(value.st_ctime_ns),
    )


def _create_capsule_directories(*paths: Path) -> None:
    for path in paths:
        try:
            path.mkdir(mode=0o700, parents=True, exist_ok=False)
        except FileExistsError as exc:
            raise SourceIngestBackendError("audit_source_ingest_capsule_state_conflict") from exc
        except OSError as exc:
            raise SourceIngestBackendError("audit_source_ingest_capsule_state_unavailable") from exc


def _capsule_user() -> tuple[int, int]:
    user_id = os.geteuid()
    group_id = os.getegid()
    if user_id != 0:
        return user_id, group_id
    return 65534, 65534


def _write_new_file(path: Path, content: bytes, *, mode: int) -> None:
    descriptor = -1
    try:
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            mode,
        )
        offset = 0
        while offset < len(content):
            offset += os.write(descriptor, content[offset:])
        os.fsync(descriptor)
    except OSError as exc:
        raise SourceIngestBackendError("audit_source_ingest_capsule_state_unavailable") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _read_worker_result_archive(
    archive: bytes,
    *,
    maximum_bytes: int,
    expected_owner_uid: int,
) -> SourceIngestWorkerResult:
    try:
        if len(archive) < 1536 or len(archive) % 512 != 0 or len(archive) > maximum_bytes + 4096:
            raise SourceIngestBackendError("audit_source_ingest_result_archive_invalid")
        header = archive[:512]
        if not any(header):
            raise SourceIngestBackendError("audit_source_ingest_result_archive_invalid")
        stored_checksum = _parse_tar_octal(header[148:156])
        checksum_header = bytearray(header)
        checksum_header[148:156] = b"        "
        if sum(checksum_header) != stored_checksum:
            raise SourceIngestBackendError("audit_source_ingest_result_archive_invalid")
        name = header[:100].split(b"\0", 1)[0]
        prefix = header[345:500].split(b"\0", 1)[0]
        mode = _parse_tar_octal(header[100:108])
        owner_uid = _parse_tar_octal(header[108:116])
        size = _parse_tar_octal(header[124:136])
        type_flag = header[156:157]
        if (
            name != b"result.json"
            or prefix
            or mode != 0o600
            or owner_uid != expected_owner_uid
            or size <= 0
            or size > maximum_bytes
            or type_flag not in {b"0", b"\0"}
            or header[157:257].strip(b"\0")
            or header[257:263] not in {b"ustar\0", b"ustar "}
        ):
            raise SourceIngestBackendError("audit_source_ingest_result_archive_invalid")
        content_end = 512 + size
        padded_end = 512 + ((size + 511) // 512) * 512
        if padded_end + 1024 > len(archive):
            raise SourceIngestBackendError("audit_source_ingest_result_archive_invalid")
        if any(archive[content_end:padded_end]) or any(archive[padded_end:]):
            raise SourceIngestBackendError("audit_source_ingest_result_archive_invalid")
        return SourceIngestWorkerResult.model_validate_json(archive[512:content_end])
    except SourceIngestBackendError:
        raise
    except (UnicodeDecodeError, ValidationError, ValueError) as exc:
        raise SourceIngestBackendError("audit_source_ingest_result_invalid") from exc


def _read_mount_probe_archive(
    archive: bytes,
    *,
    expected_owner_uid: int,
) -> object:
    try:
        maximum_bytes = 4096
        if len(archive) < 1536 or len(archive) % 512 != 0 or len(archive) > 8192:
            raise SourceIngestBackendError("audit_source_ingest_mount_probe_invalid")
        header = archive[:512]
        if not any(header):
            raise SourceIngestBackendError("audit_source_ingest_mount_probe_invalid")
        stored_checksum = _parse_tar_octal(header[148:156])
        checksum_header = bytearray(header)
        checksum_header[148:156] = b"        "
        name = header[:100].split(b"\0", 1)[0]
        prefix = header[345:500].split(b"\0", 1)[0]
        mode = _parse_tar_octal(header[100:108])
        owner_uid = _parse_tar_octal(header[108:116])
        size = _parse_tar_octal(header[124:136])
        type_flag = header[156:157]
        if (
            sum(checksum_header) != stored_checksum
            or name != b"identity.json"
            or prefix
            or mode != 0o600
            or owner_uid != expected_owner_uid
            or size <= 0
            or size > maximum_bytes
            or type_flag not in {b"0", b"\0"}
            or header[157:257].strip(b"\0")
            or header[257:263] not in {b"ustar\0", b"ustar "}
        ):
            raise SourceIngestBackendError("audit_source_ingest_mount_probe_invalid")
        content_end = 512 + size
        padded_end = 512 + ((size + 511) // 512) * 512
        if padded_end + 1024 > len(archive):
            raise SourceIngestBackendError("audit_source_ingest_mount_probe_invalid")
        if any(archive[content_end:padded_end]) or any(archive[padded_end:]):
            raise SourceIngestBackendError("audit_source_ingest_mount_probe_invalid")
        return json.loads(archive[512:content_end])
    except SourceIngestBackendError as exc:
        if exc.code == "audit_source_ingest_mount_probe_invalid":
            raise
        raise SourceIngestBackendError("audit_source_ingest_mount_probe_invalid") from exc
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise SourceIngestBackendError("audit_source_ingest_mount_probe_invalid") from exc


def _mount_observation_from_value(value: object) -> SourceMountObservation:
    if not isinstance(value, dict) or set(value) != {
        "filesystem_type",
        "mount_id",
        "st_dev",
        "st_ino",
    }:
        raise SourceIngestBackendError("audit_source_ingest_mount_probe_invalid")
    filesystem_type = value["filesystem_type"]
    mount_id = value["mount_id"]
    st_dev = value["st_dev"]
    st_ino = value["st_ino"]
    if (
        not isinstance(filesystem_type, str)
        or filesystem_type not in _SUPPORTED_LOCAL_SOURCE_FILESYSTEMS
        or type(mount_id) is not int
        or mount_id <= 0
        or type(st_dev) is not int
        or st_dev < 0
        or type(st_ino) is not int
        or st_ino <= 0
    ):
        raise SourceIngestBackendError("audit_source_ingest_mount_probe_invalid")
    identity_value = {
        "filesystem_type": filesystem_type,
        "schema_version": SOURCE_INGEST_MOUNT_IDENTITY_VERSION,
        "st_dev": st_dev,
        "st_ino": st_ino,
    }
    return SourceMountObservation(
        identity_digest=_domain_digest(
            SOURCE_INGEST_MOUNT_IDENTITY_VERSION,
            identity_value,
        ),
        proof_digest=_domain_digest(
            SOURCE_INGEST_MOUNT_PROOF_VERSION,
            {
                **identity_value,
                "mount_id": mount_id,
                "schema_version": SOURCE_INGEST_MOUNT_PROOF_VERSION,
            },
        ),
        filesystem_type=filesystem_type,
        mount_id=mount_id,
    )


def _parse_tar_octal(value: bytes) -> int:
    stripped = value.rstrip(b"\0 ").lstrip(b" ")
    if not stripped or any(character not in b"01234567" for character in stripped):
        raise SourceIngestBackendError("audit_source_ingest_result_archive_invalid")
    return int(stripped, 8)


def _require_fd_path_identity(
    path: Path,
    descriptor: int,
    *,
    expected_kind: str,
) -> None:
    try:
        expected = os.fstat(descriptor)
        actual = path.stat()
    except OSError as exc:
        raise SourceIngestBackendError("audit_source_ingest_descriptor_unavailable") from exc
    if expected_kind == "directory":
        kind_matches = stat.S_ISDIR(actual.st_mode)
    elif expected_kind == "regular":
        kind_matches = stat.S_ISREG(actual.st_mode)
    else:  # pragma: no cover - all callers use a closed internal enum
        raise SourceIngestBackendError("audit_source_ingest_descriptor_kind_invalid")
    if expected.st_dev != actual.st_dev or expected.st_ino != actual.st_ino or not kind_matches:
        raise SourceIngestBackendError("audit_source_ingest_descriptor_binding_mismatch")


def _validate_mount_probe_container(
    inspect: dict[str, Any],
    *,
    container_id: str,
    container_name: str,
    image_reference: str,
    descriptor_path: Path,
    user: str,
    label: str,
) -> None:
    host_config = inspect.get("HostConfig")
    config = inspect.get("Config")
    mounts = inspect.get("Mounts")
    if (
        inspect.get("Id") != container_id
        or inspect.get("Name") != f"/{container_name}"
        or inspect.get("Image") != image_reference
        or not isinstance(host_config, dict)
        or not isinstance(config, dict)
        or not isinstance(mounts, list)
    ):
        raise SourceIngestBackendError("audit_source_ingest_mount_probe_invalid")
    restart_policy = host_config.get("RestartPolicy")
    if (
        host_config.get("NetworkMode") != "none"
        or host_config.get("ReadonlyRootfs") is not True
        or host_config.get("Privileged") is not False
        or host_config.get("AutoRemove") is not False
        or not isinstance(restart_policy, dict)
        or restart_policy.get("Name") not in {"", "no"}
        or restart_policy.get("MaximumRetryCount") not in {None, 0}
        or host_config.get("PidsLimit") != 8
        or host_config.get("Memory") != 64 * 1024 * 1024
        or host_config.get("MemorySwap") != 64 * 1024 * 1024
        or host_config.get("NanoCpus") != 250_000_000
        or set(str(value).upper() for value in host_config.get("CapDrop") or ()) != {"ALL"}
        or host_config.get("CapAdd") not in (None, [])
        or set(host_config.get("SecurityOpt") or ())
        not in ({"no-new-privileges=true"}, {"no-new-privileges"})
        or host_config.get("LogConfig")
        not in (
            {"Type": "none", "Config": {}},
            {"Type": "none", "Config": None},
        )
        or not isinstance(host_config.get("Tmpfs"), dict)
        or set(host_config["Tmpfs"]) != {"/output"}
        or _mount_option_set(host_config["Tmpfs"]["/output"])
        != {"rw", "noexec", "nosuid", "nodev", "mode=1777", "size=1048576"}
    ):
        raise SourceIngestBackendError("audit_source_ingest_mount_probe_invalid")
    if (
        config.get("User") != user
        or config.get("WorkingDir") != "/"
        or config.get("Entrypoint") != ["/usr/bin/python3"]
        or config.get("Cmd") != ["-I", "-B", "-c", _SOURCE_MOUNT_PROBE_SCRIPT]
        or config.get("OpenStdin") is not False
        or config.get("Tty") is not False
        or _environment_map(config.get("Env"))
        != {
            "HOME": "/nonexistent",
            "LANG": "C",
            "LC_ALL": "C",
            "PATH": "/usr/local/bin:/usr/bin:/bin",
        }
        or not isinstance(config.get("Labels"), dict)
        or config["Labels"].get("riftx.audit-preflight.mount-probe") != label
    ):
        raise SourceIngestBackendError("audit_source_ingest_mount_probe_invalid")
    if len(mounts) != 1:
        raise SourceIngestBackendError("audit_source_ingest_mount_probe_invalid")
    mount = mounts[0]
    if (
        not isinstance(mount, dict)
        or mount.get("Type") != "bind"
        or mount.get("Source") != str(descriptor_path)
        or mount.get("Destination") != "/source"
        or mount.get("RW") is not False
    ):
        raise SourceIngestBackendError("audit_source_ingest_mount_probe_invalid")


def _validate_recovered_create_intent_container(
    inspect: dict[str, Any],
    *,
    record: SourceIngestCapsuleRecord,
    image_reference: str,
    max_memory_mib: int,
    max_pids: int,
    max_output_bytes: int,
) -> str:
    """Validate enough immutable create facts to stop, but never resume, an orphan."""

    container_id = _require_container_id(inspect.get("Id"))
    if inspect.get("Name") != f"/{record.container_name}":
        raise SourceIngestBackendError("audit_source_ingest_recovered_container_invalid")
    config = inspect.get("Config")
    mounts = inspect.get("Mounts")
    if not isinstance(config, dict) or not isinstance(mounts, list):
        raise SourceIngestBackendError("audit_source_ingest_recovered_container_invalid")
    user = config.get("User")
    if not isinstance(user, str):
        raise SourceIngestBackendError("audit_source_ingest_recovered_container_invalid")
    user_parts = user.split(":", 1)
    if (
        len(user_parts) != 2
        or not all(part.isascii() and part.isdigit() for part in user_parts)
        or int(user_parts[0]) != record.capsule_user_id
    ):
        raise SourceIngestBackendError("audit_source_ingest_recovered_container_invalid")

    sources: dict[str, Path] = {}
    for item in mounts:
        if not isinstance(item, dict):
            raise SourceIngestBackendError("audit_source_ingest_recovered_container_invalid")
        destination = item.get("Destination")
        source = item.get("Source")
        if not isinstance(destination, str) or not isinstance(source, str):
            raise SourceIngestBackendError("audit_source_ingest_recovered_container_invalid")
        source_path = Path(source)
        parts = source_path.parts
        if (
            len(parts) != 5
            or parts[0] != "/"
            or parts[1] != "proc"
            or not parts[2].isascii()
            or not parts[2].isdigit()
            or parts[3] != "fd"
            or not parts[4].isascii()
            or not parts[4].isdigit()
            or int(parts[2]) <= 0
            or int(parts[4]) < 0
            or destination in sources
        ):
            raise SourceIngestBackendError("audit_source_ingest_recovered_container_invalid")
        sources[destination] = source_path
    if set(sources) != {
        "/source",
        "/input/request.json",
        "/opt/riftx/preflight.py",
    }:
        raise SourceIngestBackendError("audit_source_ingest_recovered_container_invalid")
    try:
        _validate_prepared_container(
            inspect,
            container_id=container_id,
            image_reference=image_reference,
            repository_fd_path=sources["/source"],
            input_fd_path=sources["/input/request.json"],
            worker_fd_path=sources["/opt/riftx/preflight.py"],
            user=user,
            capsule_label=_capsule_label(record.capsule_id),
            max_memory_mib=max_memory_mib,
            max_pids=max_pids,
            max_output_bytes=max_output_bytes,
        )
    except SourceIngestBackendError as exc:
        raise SourceIngestBackendError("audit_source_ingest_recovered_container_invalid") from exc
    return container_id


def _validate_prepared_container(
    inspect: dict[str, Any],
    *,
    container_id: str,
    image_reference: str,
    repository_fd_path: Path,
    input_fd_path: Path,
    worker_fd_path: Path,
    user: str,
    capsule_label: str,
    max_memory_mib: int,
    max_pids: int,
    max_output_bytes: int,
) -> None:
    if inspect.get("Id") != container_id or inspect.get("Image") != image_reference:
        raise SourceIngestBackendError("audit_source_ingest_prepare_proof_invalid")
    host_config = inspect.get("HostConfig")
    config = inspect.get("Config")
    mounts = inspect.get("Mounts")
    if not isinstance(host_config, dict) or not isinstance(config, dict):
        raise SourceIngestBackendError("audit_source_ingest_prepare_proof_invalid")
    security_options = set(host_config.get("SecurityOpt") or ())
    log_config = host_config.get("LogConfig")
    tmpfs = host_config.get("Tmpfs")
    ulimits = host_config.get("Ulimits")
    if (
        host_config.get("NetworkMode") != "none"
        or host_config.get("ReadonlyRootfs") is not True
        or host_config.get("Privileged") is not False
        or host_config.get("PidsLimit") != max_pids
        or host_config.get("Memory") != max_memory_mib * 1024 * 1024
        or host_config.get("MemorySwap") != max_memory_mib * 1024 * 1024
        or host_config.get("NanoCpus") != 1_000_000_000
        or set(str(value).upper() for value in host_config.get("CapDrop") or ()) != {"ALL"}
        or host_config.get("CapAdd") not in (None, [])
        or security_options not in ({"no-new-privileges=true"}, {"no-new-privileges"})
        or not isinstance(log_config, dict)
        or log_config.get("Type") != "none"
        or log_config.get("Config") not in (None, {})
        or not isinstance(tmpfs, dict)
        or set(tmpfs) != {"/tmp", "/output"}
        or _mount_option_set(tmpfs["/tmp"]) != {"rw", "noexec", "nosuid", "nodev", "size=67108864"}
        or _mount_option_set(tmpfs["/output"])
        != {
            "rw",
            "noexec",
            "nosuid",
            "nodev",
            "mode=1777",
            f"size={max_output_bytes}",
        }
        or _ulimit_set(ulimits)
        != {
            ("nofile", 256, 256),
            ("nproc", max_pids, max_pids),
        }
        or host_config.get("Devices") not in (None, [])
        or host_config.get("DeviceRequests") not in (None, [])
        or host_config.get("PortBindings") not in (None, {})
        or host_config.get("Links") not in (None, [])
        or host_config.get("Dns") not in (None, [])
        or host_config.get("ExtraHosts") not in (None, [])
        or host_config.get("VolumesFrom") not in (None, [])
    ):
        raise SourceIngestBackendError("audit_source_ingest_prepare_proof_invalid")
    expected_environment = {
        "HOME": "/nonexistent",
        "LANG": "C",
        "LC_ALL": "C",
        "PATH": "/usr/local/bin:/usr/bin:/bin",
    }
    labels = config.get("Labels")
    if (
        config.get("User") != user
        or config.get("WorkingDir") != "/"
        or config.get("Entrypoint") != ["/usr/bin/python3"]
        or config.get("Cmd")
        != [
            "-I",
            "-B",
            "/opt/riftx/preflight.py",
            "/input/request.json",
            "/output/result.json",
        ]
        or config.get("OpenStdin") is not False
        or config.get("Tty") is not False
        or _environment_map(config.get("Env")) != expected_environment
        or not isinstance(labels, dict)
        or labels.get("riftx.audit-preflight.capsule") != capsule_label
    ):
        raise SourceIngestBackendError("audit_source_ingest_prepare_proof_invalid")
    if not isinstance(mounts, list):
        raise SourceIngestBackendError("audit_source_ingest_prepare_proof_invalid")
    expected = {
        (str(repository_fd_path), "/source", False),
        (str(input_fd_path), "/input/request.json", False),
        (str(worker_fd_path), "/opt/riftx/preflight.py", False),
    }
    if len(mounts) != len(expected) or any(
        not isinstance(item, dict) or item.get("Type") != "bind" for item in mounts
    ):
        raise SourceIngestBackendError("audit_source_ingest_prepare_proof_invalid")
    observed = {
        (str(item.get("Source")), str(item.get("Destination")), bool(item.get("RW")))
        for item in mounts
        if isinstance(item, dict)
    }
    if observed != expected:
        raise SourceIngestBackendError("audit_source_ingest_prepare_proof_invalid")


def _mount_option_set(value: object) -> set[str]:
    if not isinstance(value, str):
        return set()
    return {item for item in value.split(",") if item}


def _ulimit_set(value: object) -> set[tuple[str, int, int]]:
    if not isinstance(value, list):
        return set()
    result: set[tuple[str, int, int]] = set()
    for item in value:
        if not isinstance(item, dict):
            return set()
        name = item.get("Name")
        soft = item.get("Soft")
        hard = item.get("Hard")
        if not isinstance(name, str) or type(soft) is not int or type(hard) is not int:
            return set()
        result.add((name, soft, hard))
    return result


def _environment_map(value: object) -> dict[str, str]:
    if not isinstance(value, list):
        return {}
    result: dict[str, str] = {}
    for item in value:
        if not isinstance(item, str) or "=" not in item:
            return {}
        name, content = item.split("=", 1)
        if not name or name in result:
            return {}
        result[name] = content
    return result


def _container_state(inspect: dict[str, Any]) -> str:
    state = inspect.get("State")
    if not isinstance(state, dict):
        raise SourceIngestBackendError("audit_source_ingest_inspect_invalid")
    value = state.get("Status")
    if value not in {"created", "running", "paused", "restarting", "removing", "exited", "dead"}:
        raise SourceIngestBackendError("audit_source_ingest_inspect_invalid")
    return str(value)


def _parse_exit_code(value: bytes) -> int:
    try:
        code = int(value.decode("ascii", errors="strict").strip())
    except (UnicodeDecodeError, ValueError) as exc:
        raise SourceIngestBackendError("audit_source_ingest_exit_code_invalid") from exc
    if not 0 <= code <= 255:
        raise SourceIngestBackendError("audit_source_ingest_exit_code_invalid")
    return code


def _parse_container_id(value: bytes) -> str:
    try:
        decoded = value.decode("ascii", errors="strict").strip()
    except UnicodeDecodeError as exc:
        raise SourceIngestBackendError("audit_source_ingest_container_id_invalid") from exc
    return _require_container_id(decoded)


def _require_container_id(value: object) -> str:
    if (
        not isinstance(value, str)
        or len(value) != _CONTAINER_ID_LENGTH
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise SourceIngestBackendError("audit_source_ingest_container_id_invalid")
    return value


def _is_container_id(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == _CONTAINER_ID_LENGTH
        and all(character in "0123456789abcdef" for character in value)
    )


def _validate_container_name(value: str) -> str:
    if (
        not 1 <= len(value) <= 128
        or not value[0].isalnum()
        or any(
            not (character.isascii() and (character.isalnum() or character in "_.-"))
            for character in value
        )
    ):
        raise SourceIngestBackendError("audit_source_ingest_container_name_invalid")
    return value


def _regular_file_digest(path: Path, *, maximum_bytes: int) -> str:
    descriptor, digest = _open_regular_file_descriptor(
        path,
        maximum_bytes=maximum_bytes,
    )
    _close_descriptor(descriptor)
    return digest


def _open_regular_file_descriptor(
    path: Path,
    *,
    maximum_bytes: int,
) -> tuple[int, str]:
    descriptor = -1
    try:
        value = path.lstat()
        if not stat.S_ISREG(value.st_mode) or stat.S_ISLNK(value.st_mode) or value.st_nlink != 1:
            raise SourceIngestBackendError("audit_source_ingest_worker_invalid")
        if value.st_size <= 0 or value.st_size > maximum_bytes:
            raise SourceIngestBackendError("audit_source_ingest_worker_invalid")
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        opened = os.fstat(descriptor)
        if _stat_identity(opened) != _stat_identity(value):
            raise SourceIngestBackendError("audit_source_ingest_worker_changed")
        digest = hashlib.sha256()
        total = 0
        while True:
            chunk = os.read(descriptor, 64 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > maximum_bytes:
                raise SourceIngestBackendError("audit_source_ingest_worker_invalid")
            digest.update(chunk)
        completed = os.fstat(descriptor)
        if total != value.st_size or _stat_identity(completed) != _stat_identity(opened):
            raise SourceIngestBackendError("audit_source_ingest_worker_changed")
        return descriptor, digest.hexdigest()
    except SourceIngestBackendError:
        _close_descriptor(descriptor)
        raise
    except OSError as exc:
        _close_descriptor(descriptor)
        raise SourceIngestBackendError("audit_source_ingest_worker_invalid") from exc
    except BaseException:
        _close_descriptor(descriptor)
        raise


def _close_descriptor(descriptor: int) -> None:
    if descriptor < 0:
        return
    try:
        os.close(descriptor)
    except OSError:
        pass


def _source_mount_observation(
    descriptor: int,
    *,
    canonical_path: str,
) -> SourceMountObservation:
    if platform.system().lower() != "linux":
        raise SourceIngestBackendError("audit_source_ingest_linux_host_required")
    if not canonical_path.startswith("/") or "\x00" in canonical_path:
        raise SourceIngestBackendError("audit_source_ingest_mount_identity_unavailable")
    try:
        descriptor_stat = os.fstat(descriptor)
        raw = Path("/proc/self/mountinfo").read_bytes()
    except OSError as exc:
        raise SourceIngestBackendError("audit_source_ingest_mount_identity_unavailable") from exc
    if not stat.S_ISDIR(descriptor_stat.st_mode) or len(raw) > 4 * 1024 * 1024:
        raise SourceIngestBackendError("audit_source_ingest_mount_identity_unavailable")

    selected: tuple[int, str, str] | None = None
    try:
        text = raw.decode("utf-8", errors="strict")
        expected_device = f"{os.major(descriptor_stat.st_dev)}:{os.minor(descriptor_stat.st_dev)}"
        for line in text.splitlines():
            fields = line.split(" ")
            separator = fields.index("-")
            if separator < 6 or separator + 2 >= len(fields) or fields[2] != expected_device:
                continue
            mount_id = int(fields[0])
            mount_point = _unescape_mountinfo_path(fields[4])
            filesystem_type = fields[separator + 1]
            if canonical_path != mount_point and not canonical_path.startswith(
                mount_point.rstrip("/") + "/"
            ):
                continue
            if selected is None or len(mount_point) > len(selected[1]):
                selected = (mount_id, mount_point, filesystem_type)
    except (UnicodeDecodeError, ValueError):
        raise SourceIngestBackendError("audit_source_ingest_mount_identity_unavailable") from None
    if selected is None:
        raise SourceIngestBackendError("audit_source_ingest_mount_identity_unavailable")
    mount_id, _mount_point, filesystem_type = selected
    if filesystem_type not in _SUPPORTED_LOCAL_SOURCE_FILESYSTEMS:
        raise SourceIngestBackendError("audit_source_ingest_filesystem_unsupported")
    identity_value = {
        "filesystem_type": filesystem_type,
        "schema_version": SOURCE_INGEST_MOUNT_IDENTITY_VERSION,
        "st_dev": int(descriptor_stat.st_dev),
        "st_ino": int(descriptor_stat.st_ino),
    }
    identity_digest = _domain_digest(
        SOURCE_INGEST_MOUNT_IDENTITY_VERSION,
        identity_value,
    )
    return SourceMountObservation(
        identity_digest=identity_digest,
        proof_digest=_domain_digest(
            SOURCE_INGEST_MOUNT_PROOF_VERSION,
            {
                **identity_value,
                "mount_id": mount_id,
                "schema_version": SOURCE_INGEST_MOUNT_PROOF_VERSION,
            },
        ),
        filesystem_type=filesystem_type,
        mount_id=mount_id,
    )


def _unescape_mountinfo_path(value: str) -> str:
    result = value
    for escaped, decoded in (
        ("\\040", " "),
        ("\\011", "\t"),
        ("\\012", "\n"),
        ("\\134", "\\"),
    ):
        result = result.replace(escaped, decoded)
    if not result.startswith("/") or "\x00" in result:
        raise ValueError("invalid mountinfo path")
    return result


def _domain_digest(domain: str, value: object) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")
    return hashlib.sha256(domain.encode("ascii") + b"\0" + encoded).hexdigest()


__all__ = [
    "SOURCE_INGEST_BACKEND_COMPONENT_VERSION",
    "SOURCE_INGEST_CAPSULE_RECORD_VERSION",
    "SOURCE_INGEST_NEVER_CREATED_PROOF_VERSION",
    "SOURCE_INGEST_MOUNT_IDENTITY_VERSION",
    "SOURCE_INGEST_MOUNT_PROBE_DESTRUCTION_PROOF_VERSION",
    "SOURCE_INGEST_MOUNT_PROBE_OWNER_VERSION",
    "SOURCE_INGEST_MOUNT_PROBE_RECORD_VERSION",
    "SOURCE_INGEST_MOUNT_PROOF_VERSION",
    "SOURCE_INGEST_PREPARE_PROOF_VERSION",
    "SOURCE_INGEST_PROCESS_IDENTITY_VERSION",
    "DockerSourceIngestBackend",
    "PreparedSourceIngestCapsule",
    "SourceIngestBackendAvailability",
    "SourceIngestBackendError",
    "SourceIngestCapsuleRecord",
    "SourceIngestExecutionResult",
    "SourceIngestProbe",
    "SourceIngestStartEvidence",
    "SourceIngestStopEvidence",
    "SourceMountObservation",
    "SourceMountProbeRecord",
]
