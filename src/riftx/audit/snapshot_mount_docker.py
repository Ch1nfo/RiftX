"""Qualified local-Linux Docker backend for private Snapshot materialization."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import io
import json
import os
import platform
import re
import shutil
import stat
import tarfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any

from .snapshot import SnapshotBlobObjectType
from .snapshot_mount import (
    PreparedSnapshotMount,
    SnapshotMountBackendError,
    SnapshotMountBackendState,
    SnapshotMountFailure,
    SnapshotMountInspection,
    SnapshotMountSource,
    SnapshotMountStopEvidence,
)
from .static_effect import (
    SNAPSHOT_MOUNT_BACKEND_ID,
    AuditStaticEffectPlan,
    SnapshotMountLease,
    SnapshotMountPin,
)

DOCKER_SNAPSHOT_MOUNT_COMPONENT_VERSION = "riftx.audit-snapshot-mount-docker/v1"
DOCKER_SNAPSHOT_MOUNT_QUALIFICATION_VERSION = (
    "riftx.audit-snapshot-mount-docker-qualification/v1"
)
DOCKER_SNAPSHOT_MOUNT_OWNER_VERSION = "riftx.audit-snapshot-mount-owner/v1"
DOCKER_SNAPSHOT_MOUNT_PROOF_VERSION = "riftx.audit-snapshot-mount-proof/v1"
DOCKER_SNAPSHOT_MOUNT_TREE_VERSION = "riftx.audit-snapshot-mount-tree/v1"
DOCKER_SNAPSHOT_MOUNT_ALLOWLIST_VERSION = "riftx.audit-snapshot-mount-allowlist/v1"

_DOCKER_SOCKET = Path("/var/run/docker.sock")
_DOCKER_OUTPUT_LIMIT = 1024 * 1024
_CONTAINER_ID_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_DIGEST_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_SAFE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@+~\-]{0,127}$")
_CONTAINER_USER = "65532:65532"
_PROOF_PATH = "/workspace/.riftx-snapshot-mount-proof.json"
_SOURCE_PATH = "/workspace/src"
_HOLDER_SCRIPT = "import signal; signal.pause()"
_PROBE_SCRIPT = r"""
import errno
import hashlib
import json
import os
import stat
import sys

schema = sys.argv[1]
expected = sys.argv[2]
root = "/workspace/src"
root_stat = os.lstat(root)
if not stat.S_ISDIR(root_stat.st_mode):
    raise SystemExit(71)
if root_stat.st_uid != 0 or root_stat.st_gid != 0 or stat.S_IMODE(root_stat.st_mode) != 0o555:
    raise SystemExit(72)

records = []
total_bytes = 0
first_regular = None

def walk(directory, prefix=""):
    global first_regular, total_bytes
    with os.scandir(directory) as entries:
        ordered = sorted(entries, key=lambda entry: entry.name)
    for entry in ordered:
        relative = entry.name if not prefix else prefix + "/" + entry.name
        value = entry.stat(follow_symlinks=False)
        if value.st_uid != 0 or value.st_gid != 0:
            raise SystemExit(73)
        mode = stat.S_IMODE(value.st_mode)
        if stat.S_ISDIR(value.st_mode):
            if mode != 0o555:
                raise SystemExit(74)
            records.append({
                "blob_digest": None,
                "mode": mode,
                "object_type": "directory",
                "relative_path": relative,
                "size": 0,
            })
            walk(entry.path, relative)
            continue
        if stat.S_ISREG(value.st_mode):
            if mode not in (0o444, 0o555):
                raise SystemExit(75)
            digest = hashlib.sha256()
            size = 0
            with open(entry.path, "rb", buffering=0) as stream:
                while True:
                    chunk = stream.read(65536)
                    if not chunk:
                        break
                    digest.update(chunk)
                    size += len(chunk)
            if size != value.st_size:
                raise SystemExit(76)
            total_bytes += size
            if first_regular is None:
                first_regular = entry.path
            records.append({
                "blob_digest": digest.hexdigest(),
                "mode": mode,
                "object_type": "regular_file",
                "relative_path": relative,
                "size": size,
            })
            continue
        if stat.S_ISLNK(value.st_mode):
            target = os.readlink(entry.path).encode("utf-8", errors="strict")
            total_bytes += len(target)
            records.append({
                "blob_digest": hashlib.sha256(target).hexdigest(),
                "mode": mode,
                "object_type": "symlink",
                "relative_path": relative,
                "size": len(target),
            })
            continue
        raise SystemExit(77)

walk(root)

denied_errnos = {errno.EACCES, errno.EPERM, errno.EROFS}
denied_operations = 0

def require_denied(operation, exit_code):
    global denied_operations
    try:
        operation()
    except OSError as error:
        if error.errno not in denied_errnos:
            raise SystemExit(exit_code)
        denied_operations += 1
        return
    raise SystemExit(exit_code)

def create_forbidden():
    descriptor = os.open(
        "/workspace/src/.riftx-forbidden-create",
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    os.close(descriptor)

require_denied(create_forbidden, 79)
require_denied(lambda: os.chmod(root, 0o755), 80)
if first_regular is not None:
    def open_for_write():
        descriptor = os.open(first_regular, os.O_WRONLY)
        os.close(descriptor)

    require_denied(open_for_write, 81)
    require_denied(lambda: os.chmod(first_regular, 0o644), 82)
    require_denied(
        lambda: os.rename(first_regular, "/workspace/src/.riftx-forbidden-rename"),
        83,
    )
    require_denied(lambda: os.unlink(first_regular), 84)

records.sort(key=lambda item: (item["relative_path"], item["object_type"]))
canonical = json.dumps(
    records,
    sort_keys=True,
    separators=(",", ":"),
    ensure_ascii=False,
).encode("utf-8")
observed = hashlib.sha256(schema.encode("ascii") + b"\0" + canonical).hexdigest()
if observed != expected:
    raise SystemExit(78)
with open("/workspace/.riftx-snapshot-mount-proof.json", "rb", buffering=0) as stream:
    proof = json.loads(stream.read(1048576))
payload = {
    "file_count": sum(1 for item in records if item["object_type"] != "directory"),
    "mutation_denial_count": denied_operations,
    "total_bytes": total_bytes,
    "tree_proof_digest": observed,
    "mount_proof_digest": proof.get("mount_proof_digest"),
}
sys.stdout.write(json.dumps(payload, sort_keys=True, separators=(",", ":")))
""".strip()
_QUALIFICATION_SCRIPT = r"""
import os
import sys

with open("/workspace/probe.txt", "rb", buffering=0) as stream:
    if stream.read() != b"riftx-snapshot-mount-probe\n":
        raise SystemExit(81)
try:
    descriptor = os.open("/workspace/forbidden", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
except PermissionError:
    sys.stdout.write("qualified")
else:
    os.close(descriptor)
    raise SystemExit(82)
""".strip()


class _DockerError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class DockerSnapshotMountAvailability:
    available: bool
    reason_code: str | None
    component_digest: str
    qualification_proof_digest: str | None = None


@dataclass(frozen=True, slots=True)
class _MaterializedTreeArchive:
    content: bytes
    descriptor_digest: str
    tree_proof_digest: str
    file_count: int
    total_bytes: int


class DockerSnapshotMountBackend:
    """One rootless-reader, container-private tmpfs per effect execution."""

    backend_id = SNAPSHOT_MOUNT_BACKEND_ID

    def __init__(
        self,
        *,
        node_id: str,
        image_digest: str,
        docker_path: str | None = None,
        docker_socket: Path = _DOCKER_SOCKET,
    ) -> None:
        if _SAFE_ID_PATTERN.fullmatch(node_id) is None:
            raise ValueError("Docker Snapshot mount node_id is invalid")
        if _DIGEST_PATTERN.fullmatch(image_digest) is None:
            raise ValueError("Docker Snapshot mount image digest is invalid")
        resolved_docker = docker_path or shutil.which(
            "docker",
            path="/usr/local/bin:/usr/bin:/bin",
        )
        if resolved_docker is not None and not Path(resolved_docker).is_absolute():
            raise ValueError("Docker executable path must be absolute")
        if not docker_socket.is_absolute():
            raise ValueError("Docker socket path must be absolute")
        self.node_id = node_id
        self.image_digest = image_digest
        self.docker_path = resolved_docker
        self.docker_socket = docker_socket
        self.backend_digest = _domain_digest(
            DOCKER_SNAPSHOT_MOUNT_COMPONENT_VERSION,
            {
                "backend_id": self.backend_id,
                "container_user": _CONTAINER_USER,
                "image_digest": image_digest,
                "materialization": "container_private_tmpfs",
                "network": "none",
                "rootfs": "read_only",
                "schema_version": DOCKER_SNAPSHOT_MOUNT_COMPONENT_VERSION,
                "source_tree": "root_owned_read_only",
                "source_mutation_probe": "non_root_kernel_denial",
            },
        )

    async def probe_availability(self) -> DockerSnapshotMountAvailability:
        reason = self._static_unavailability_reason()
        if reason is not None:
            return DockerSnapshotMountAvailability(False, reason, self.backend_digest)
        try:
            server = (
                await self._docker(
                    "version",
                    "--format",
                    "{{.Server.Os}}:{{.Server.Arch}}",
                    timeout_seconds=5,
                )
            ).decode("ascii", errors="strict").strip()
            if not server.startswith("linux:"):
                return DockerSnapshotMountAvailability(
                    False,
                    "audit_snapshot_mount_linux_daemon_required",
                    self.backend_digest,
                )
            image = await self._inspect_image()
            round_trip = await self._qualification_round_trip()
            proof = _domain_digest(
                DOCKER_SNAPSHOT_MOUNT_QUALIFICATION_VERSION,
                {
                    "backend_digest": self.backend_digest,
                    "docker_server": server,
                    "image_id": image["Id"],
                    "round_trip_digest": round_trip,
                    "schema_version": DOCKER_SNAPSHOT_MOUNT_QUALIFICATION_VERSION,
                },
            )
        except (_DockerError, UnicodeDecodeError, ValueError):
            return DockerSnapshotMountAvailability(
                False,
                "audit_snapshot_mount_backend_unavailable",
                self.backend_digest,
            )
        return DockerSnapshotMountAvailability(
            True,
            None,
            self.backend_digest,
            qualification_proof_digest=proof,
        )

    async def prepare(
        self,
        *,
        plan: AuditStaticEffectPlan,
        lease: SnapshotMountLease,
        pin: SnapshotMountPin,
        source: SnapshotMountSource,
        prepared_at: datetime,
    ) -> PreparedSnapshotMount:
        self._require_authority(plan, lease, pin)
        if not source.accepts(plan=plan, lease=lease):
            raise SnapshotMountBackendError(
                SnapshotMountFailure.SOURCE_INTEGRITY,
                outcome_unknown=False,
            )
        try:
            archive = self._build_source_archive(source, lease=lease)
        except ValueError as exc:
            raise SnapshotMountBackendError(
                SnapshotMountFailure.SOURCE_INTEGRITY,
                outcome_unknown=False,
            ) from exc
        availability = await self.probe_availability()
        if not availability.available:
            raise SnapshotMountBackendError(
                SnapshotMountFailure.BACKEND_UNAVAILABLE,
                outcome_unknown=False,
            )
        owner_digest = _owner_digest(plan, lease, pin)
        container_name = _container_name(owner_digest)
        try:
            inspect = await self._inspect_optional(container_name)
        except _DockerError as exc:
            raise SnapshotMountBackendError(
                SnapshotMountFailure.BACKEND_STATE_UNKNOWN,
                outcome_unknown=True,
            ) from exc
        created_now = False
        if inspect is None:
            arguments = self._create_arguments(
                plan=plan,
                lease=lease,
                owner_digest=owner_digest,
                container_name=container_name,
            )
            try:
                container_id = _require_container_id(
                    (await self._docker(*arguments, timeout_seconds=20))
                    .decode("ascii", errors="strict")
                    .strip()
                )
            except (_DockerError, UnicodeDecodeError, ValueError) as exc:
                try:
                    inspect = await self._inspect_optional(container_name)
                except _DockerError:
                    raise SnapshotMountBackendError(
                        SnapshotMountFailure.BACKEND_STATE_UNKNOWN,
                        outcome_unknown=True,
                    ) from exc
                if inspect is None:
                    raise SnapshotMountBackendError(
                        SnapshotMountFailure.BACKEND_UNAVAILABLE,
                        outcome_unknown=False,
                    ) from exc
            else:
                created_now = True
                inspect = await self._inspect_optional(container_id)
                if inspect is None:
                    raise SnapshotMountBackendError(
                        SnapshotMountFailure.BACKEND_STATE_UNKNOWN,
                        outcome_unknown=True,
                    )
        assert inspect is not None
        container_id = self._validate_container(
            inspect,
            plan=plan,
            lease=lease,
            pin=pin,
            owner_digest=owner_digest,
            require_running=False,
        )
        state = _container_state(inspect)
        if not created_now:
            if state not in {"created", "running"}:
                raise SnapshotMountBackendError(
                    SnapshotMountFailure.BACKEND_STATE_UNKNOWN,
                    outcome_unknown=True,
                )
            try:
                return await self._await_existing_prepared(
                    plan=plan,
                    lease=lease,
                    pin=pin,
                    source=source,
                    owner_digest=owner_digest,
                    container_id=container_id,
                    observed_at=prepared_at,
                )
            except SnapshotMountBackendError:
                raise
            except Exception as exc:
                raise SnapshotMountBackendError(
                    SnapshotMountFailure.BACKEND_STATE_UNKNOWN,
                    outcome_unknown=True,
                ) from exc
        try:
            await self._docker("start", container_id, timeout_seconds=20)
            running = await self._inspect_required(container_id)
            self._validate_container(
                running,
                plan=plan,
                lease=lease,
                pin=pin,
                owner_digest=owner_digest,
                require_running=True,
            )
            await self._docker_with_input(
                archive.content,
                "cp",
                "-",
                f"{container_id}:/workspace",
                timeout_seconds=60,
            )
            mount_key = _mount_key(owner_digest, container_id)
            proof_document = _mount_proof_document(
                plan=plan,
                lease=lease,
                pin=pin,
                owner_digest=owner_digest,
                container_id=container_id,
                mount_key=mount_key,
                archive=archive,
                prepared_at=prepared_at,
            )
            await self._docker_with_input(
                _single_file_archive(
                    ".riftx-snapshot-mount-proof.json",
                    _canonical_json(proof_document).encode("utf-8"),
                    mode=0o444,
                ),
                "cp",
                "-",
                f"{container_id}:/workspace",
                timeout_seconds=20,
            )
            observed = await self._probe_materialization(
                container_id,
                expected_tree_digest=archive.tree_proof_digest,
            )
            if (
                observed.get("file_count") != archive.file_count
                or observed.get("mutation_denial_count")
                != (
                    6
                    if any(
                        blob.object_type is SnapshotBlobObjectType.REGULAR_FILE
                        for blob in source.descriptor.blobs
                    )
                    else 2
                )
                or observed.get("total_bytes") != archive.total_bytes
                or observed.get("tree_proof_digest") != archive.tree_proof_digest
                or observed.get("mount_proof_digest")
                != proof_document["mount_proof_digest"]
            ):
                raise ValueError("Snapshot materialization probe differs")
        except SnapshotMountBackendError:
            raise
        except Exception as exc:
            raise SnapshotMountBackendError(
                SnapshotMountFailure.BACKEND_STATE_UNKNOWN,
                outcome_unknown=True,
            ) from exc
        return PreparedSnapshotMount(
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
            mount_proof_digest=str(proof_document["mount_proof_digest"]),
            descriptor_digest=source.descriptor.descriptor_digest,
            file_count=archive.file_count,
            total_bytes=archive.total_bytes,
            prepared_at=prepared_at,
        )

    async def inspect(
        self,
        *,
        plan: AuditStaticEffectPlan,
        lease: SnapshotMountLease,
        pin: SnapshotMountPin,
        observed_at: datetime,
    ) -> SnapshotMountInspection:
        self._require_authority(plan, lease, pin)
        owner_digest = _owner_digest(plan, lease, pin)
        try:
            inspect = await self._inspect_optional(_container_name(owner_digest))
        except _DockerError as exc:
            raise SnapshotMountBackendError(
                SnapshotMountFailure.BACKEND_STATE_UNKNOWN,
                outcome_unknown=True,
            ) from exc
        if inspect is None:
            return self._inspection(
                SnapshotMountBackendState.ABSENT,
                lease=lease,
                pin=pin,
                observed_at=observed_at,
            )
        try:
            container_id = self._validate_container(
                inspect,
                plan=plan,
                lease=lease,
                pin=pin,
                owner_digest=owner_digest,
                require_running=False,
            )
            if _container_state(inspect) != "running":
                return self._inspection(
                    SnapshotMountBackendState.UNKNOWN,
                    lease=lease,
                    pin=pin,
                    observed_at=observed_at,
                )
            proof = await self._read_mount_proof(container_id)
            mount_key, proof_digest = _validate_mount_proof_document(
                proof,
                plan=plan,
                lease=lease,
                pin=pin,
                owner_digest=owner_digest,
                container_id=container_id,
            )
        except SnapshotMountBackendError:
            raise
        except Exception as exc:
            raise SnapshotMountBackendError(
                SnapshotMountFailure.BACKEND_STATE_UNKNOWN,
                outcome_unknown=True,
            ) from exc
        return self._inspection(
            SnapshotMountBackendState.ACTIVE,
            lease=lease,
            pin=pin,
            observed_at=observed_at,
            mount_key=mount_key,
            mount_proof_digest=proof_digest,
        )

    async def stop(
        self,
        *,
        plan: AuditStaticEffectPlan,
        lease: SnapshotMountLease,
        pin: SnapshotMountPin,
        stopped_at: datetime,
    ) -> SnapshotMountStopEvidence:
        self._require_authority(plan, lease, pin)
        owner_digest = _owner_digest(plan, lease, pin)
        container_name = _container_name(owner_digest)
        try:
            inspect = await self._inspect_optional(container_name)
            if inspect is None:
                return self._stop_evidence(
                    lease=lease,
                    pin=pin,
                    mount_key=(lease.mount_key or _absent_mount_key(owner_digest)),
                    stopped_at=stopped_at,
                )
            container_id = self._validate_container(
                inspect,
                plan=plan,
                lease=lease,
                pin=pin,
                owner_digest=owner_digest,
                require_running=False,
            )
            mount_key = _mount_key(owner_digest, container_id)
            if lease.mount_key is not None and not hmac.compare_digest(
                lease.mount_key,
                mount_key,
            ):
                raise ValueError("Snapshot mount key differs")
            if _container_state(inspect) in {"running", "paused", "restarting"}:
                try:
                    await self._docker("stop", "--time", "10", container_id, timeout_seconds=20)
                except _DockerError:
                    remaining = await self._inspect_optional(container_id)
                    if remaining is not None and _container_state(remaining) in {
                        "running",
                        "paused",
                        "restarting",
                    }:
                        raise
            try:
                await self._docker("rm", "--force", container_id, timeout_seconds=20)
            except _DockerError:
                if await self._inspect_optional(container_id) is not None:
                    raise
            if (
                await self._inspect_optional(container_id) is not None
                or await self._inspect_optional(container_name) is not None
            ):
                raise ValueError("Snapshot mount container removal is unproven")
        except SnapshotMountBackendError:
            raise
        except Exception as exc:
            raise SnapshotMountBackendError(
                SnapshotMountFailure.STOP_UNCONFIRMED,
                outcome_unknown=True,
            ) from exc
        return self._stop_evidence(
            lease=lease,
            pin=pin,
            mount_key=mount_key,
            stopped_at=stopped_at,
        )

    def _require_authority(
        self,
        plan: AuditStaticEffectPlan,
        lease: SnapshotMountLease,
        pin: SnapshotMountPin,
    ) -> None:
        SnapshotMountSource.require_authority(plan=plan, lease=lease)
        try:
            pin._require_lease_binding(lease)
        except ValueError as exc:
            raise SnapshotMountBackendError(
                SnapshotMountFailure.OWNER_MISMATCH,
                outcome_unknown=False,
            ) from exc
        if (
            plan.image_digest != self.image_digest
            or lease.target_node_id != self.node_id
            or lease.backend_id != self.backend_id
            or lease.backend_digest != self.backend_digest
        ):
            raise SnapshotMountBackendError(
                SnapshotMountFailure.OWNER_MISMATCH,
                outcome_unknown=False,
            )

    def _create_arguments(
        self,
        *,
        plan: AuditStaticEffectPlan,
        lease: SnapshotMountLease,
        owner_digest: str,
        container_name: str,
    ) -> tuple[str, ...]:
        return (
            "create",
            "--pull",
            "never",
            "--name",
            container_name,
            "--label",
            f"riftx.audit.snapshot-mount.owner={owner_digest}",
            "--label",
            f"riftx.audit.snapshot-mount.backend={self.backend_digest}",
            "--label",
            f"riftx.audit.snapshot-mount.lease={lease.lease_digest}",
            "--network",
            "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges=true",
            "--user",
            _CONTAINER_USER,
            "--pids-limit",
            str(plan.limits.pids),
            "--memory",
            f"{plan.limits.memory_bytes}b",
            "--memory-swap",
            f"{plan.limits.memory_bytes}b",
            "--cpus",
            "1.0",
            "--ulimit",
            "nofile=256:256",
            "--log-driver",
            "none",
            "--workdir",
            "/",
            "--tmpfs",
            "/workspace:rw,noexec,nosuid,nodev,mode=0755,"
            f"size={plan.limits.disk_bytes}",
            "--env",
            "HOME=/nonexistent",
            "--env",
            "LANG=C",
            "--env",
            "LC_ALL=C",
            "--env",
            "PATH=/usr/local/bin:/usr/bin:/bin",
            "--env",
            "PYTHONDONTWRITEBYTECODE=1",
            "--entrypoint",
            "/usr/bin/python3",
            self._image_reference(),
            "-I",
            "-B",
            "-c",
            _HOLDER_SCRIPT,
        )

    def _validate_container(
        self,
        inspect: dict[str, Any],
        *,
        plan: AuditStaticEffectPlan,
        lease: SnapshotMountLease,
        pin: SnapshotMountPin,
        owner_digest: str,
        require_running: bool,
    ) -> str:
        del pin
        container_id = _require_container_id(inspect.get("Id"))
        if inspect.get("Image") != self._image_reference():
            raise SnapshotMountBackendError(
                SnapshotMountFailure.OWNER_MISMATCH,
                outcome_unknown=True,
            )
        config = inspect.get("Config")
        host = inspect.get("HostConfig")
        name = inspect.get("Name")
        if not isinstance(config, dict) or not isinstance(host, dict):
            raise ValueError("Docker Snapshot mount inspect shape is invalid")
        labels = config.get("Labels")
        expected_env = {
            "HOME=/nonexistent",
            "LANG=C",
            "LC_ALL=C",
            "PATH=/usr/local/bin:/usr/bin:/bin",
            "PYTHONDONTWRITEBYTECODE=1",
        }
        if (
            name != f"/{_container_name(owner_digest)}"
            or config.get("Image") != self._image_reference()
            or config.get("User") != _CONTAINER_USER
            or config.get("WorkingDir") != "/"
            or config.get("Entrypoint") != ["/usr/bin/python3"]
            or config.get("Cmd") != ["-I", "-B", "-c", _HOLDER_SCRIPT]
            or set(config.get("Env") or ()) != expected_env
            or not isinstance(labels, dict)
            or labels.get("riftx.audit.snapshot-mount.owner") != owner_digest
            or labels.get("riftx.audit.snapshot-mount.backend") != self.backend_digest
            or labels.get("riftx.audit.snapshot-mount.lease") != lease.lease_digest
            or host.get("NetworkMode") != "none"
            or host.get("ReadonlyRootfs") is not True
            or host.get("Privileged") is not False
            or set(host.get("CapDrop") or ()) != {"ALL"}
            or set(host.get("SecurityOpt") or ())
            not in ({"no-new-privileges=true"}, {"no-new-privileges"})
            or host.get("PidsLimit") != plan.limits.pids
            or host.get("Memory") != plan.limits.memory_bytes
            or host.get("MemorySwap") != plan.limits.memory_bytes
            or host.get("Binds") not in (None, [])
            or (host.get("LogConfig") or {}).get("Type") != "none"
        ):
            raise SnapshotMountBackendError(
                SnapshotMountFailure.OWNER_MISMATCH,
                outcome_unknown=True,
            )
        tmpfs = host.get("Tmpfs")
        if not isinstance(tmpfs, dict) or set(tmpfs) != {"/workspace"}:
            raise SnapshotMountBackendError(
                SnapshotMountFailure.OWNER_MISMATCH,
                outcome_unknown=True,
            )
        options = set(str(tmpfs["/workspace"]).split(","))
        if options != {
            "rw",
            "noexec",
            "nosuid",
            "nodev",
            "mode=0755",
            f"size={plan.limits.disk_bytes}",
        }:
            raise SnapshotMountBackendError(
                SnapshotMountFailure.OWNER_MISMATCH,
                outcome_unknown=True,
            )
        if require_running and _container_state(inspect) != "running":
            raise SnapshotMountBackendError(
                SnapshotMountFailure.BACKEND_STATE_UNKNOWN,
                outcome_unknown=True,
            )
        return container_id

    async def _prepared_from_existing(
        self,
        *,
        plan: AuditStaticEffectPlan,
        lease: SnapshotMountLease,
        pin: SnapshotMountPin,
        source: SnapshotMountSource,
        owner_digest: str,
        container_id: str,
        observed_at: datetime,
    ) -> PreparedSnapshotMount:
        proof = await self._read_mount_proof(container_id)
        mount_key, proof_digest = _validate_mount_proof_document(
            proof,
            plan=plan,
            lease=lease,
            pin=pin,
            owner_digest=owner_digest,
            container_id=container_id,
        )
        if (
            proof.get("descriptor_digest") != source.descriptor.descriptor_digest
            or proof.get("file_count") != source.descriptor.file_count
            or proof.get("total_bytes") != source.descriptor.total_bytes
        ):
            raise SnapshotMountBackendError(
                SnapshotMountFailure.SOURCE_INTEGRITY,
                outcome_unknown=True,
            )
        prepared_at = _parse_datetime(proof.get("prepared_at"))
        if prepared_at > observed_at:
            raise SnapshotMountBackendError(
                SnapshotMountFailure.BACKEND_STATE_UNKNOWN,
                outcome_unknown=True,
            )
        return PreparedSnapshotMount(
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
            mount_proof_digest=proof_digest,
            descriptor_digest=source.descriptor.descriptor_digest,
            file_count=source.descriptor.file_count,
            total_bytes=source.descriptor.total_bytes,
            prepared_at=prepared_at,
        )

    async def _await_existing_prepared(
        self,
        *,
        plan: AuditStaticEffectPlan,
        lease: SnapshotMountLease,
        pin: SnapshotMountPin,
        source: SnapshotMountSource,
        owner_digest: str,
        container_id: str,
        observed_at: datetime,
    ) -> PreparedSnapshotMount:
        for attempt in range(20):
            inspect = await self._inspect_required(container_id)
            self._validate_container(
                inspect,
                plan=plan,
                lease=lease,
                pin=pin,
                owner_digest=owner_digest,
                require_running=False,
            )
            state = _container_state(inspect)
            if state not in {"created", "running"}:
                raise SnapshotMountBackendError(
                    SnapshotMountFailure.BACKEND_STATE_UNKNOWN,
                    outcome_unknown=True,
                )
            if state == "running":
                try:
                    return await self._prepared_from_existing(
                        plan=plan,
                        lease=lease,
                        pin=pin,
                        source=source,
                        owner_digest=owner_digest,
                        container_id=container_id,
                        observed_at=observed_at,
                    )
                except _DockerError:
                    pass
            if attempt != 19:
                await asyncio.sleep(0.1)
        raise SnapshotMountBackendError(
            SnapshotMountFailure.BACKEND_STATE_UNKNOWN,
            outcome_unknown=True,
        )

    def _build_source_archive(
        self,
        source: SnapshotMountSource,
        *,
        lease: SnapshotMountLease,
    ) -> _MaterializedTreeArchive:
        records: list[dict[str, object]] = []
        directories = {""}
        for metadata in source.descriptor.blobs:
            parent = PurePosixPath(metadata.relative_path).parent
            while str(parent) != ".":
                directories.add(parent.as_posix())
                parent = parent.parent
        for directory in sorted(directories):
            if directory:
                records.append(_tree_record(directory, "directory", 0o555, 0, None))
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode="w", format=tarfile.PAX_FORMAT) as archive:
            archive.addfile(_tar_directory("src"))
            for directory in sorted(value for value in directories if value):
                archive.addfile(_tar_directory(f"src/{directory}"))
            remaining = lease.max_bytes
            ordered_blobs = tuple(
                blob
                for object_type in (
                    SnapshotBlobObjectType.REGULAR_FILE,
                    SnapshotBlobObjectType.SYMLINK,
                )
                for blob in source.descriptor.blobs
                if blob.object_type is object_type
            )
            for metadata in ordered_blobs:
                content = source.read_blob(metadata, max_bytes=remaining)
                if len(content) != metadata.size:
                    raise ValueError("Snapshot blob size differs during materialization")
                remaining -= len(content)
                name = f"src/{metadata.relative_path}"
                if metadata.object_type is SnapshotBlobObjectType.REGULAR_FILE:
                    mode = 0o555 if metadata.mode & 0o111 else 0o444
                    info = _tar_regular(name, len(content), mode=mode)
                    archive.addfile(info, io.BytesIO(content))
                    records.append(
                        _tree_record(
                            metadata.relative_path,
                            "regular_file",
                            mode,
                            len(content),
                            metadata.blob_digest,
                        )
                    )
                else:
                    target = _safe_symlink_target(metadata.relative_path, content)
                    archive.addfile(_tar_symlink(name, target))
                    records.append(
                        _tree_record(
                            metadata.relative_path,
                            "symlink",
                            0o777,
                            len(content),
                            metadata.blob_digest,
                        )
                    )
        records.sort(key=lambda value: (str(value["relative_path"]), str(value["object_type"])))
        tree_proof = _domain_digest(DOCKER_SNAPSHOT_MOUNT_TREE_VERSION, records)
        content = output.getvalue()
        maximum_archive_bytes = (
            source.descriptor.total_bytes
            + (source.descriptor.file_count + len(directories)) * 4096
            + 1024 * 1024
        )
        if len(content) > maximum_archive_bytes:
            raise ValueError("Snapshot materialization archive exceeds its bound")
        return _MaterializedTreeArchive(
            content=content,
            descriptor_digest=source.descriptor.descriptor_digest,
            tree_proof_digest=tree_proof,
            file_count=source.descriptor.file_count,
            total_bytes=source.descriptor.total_bytes,
        )

    async def _probe_materialization(
        self,
        container_id: str,
        *,
        expected_tree_digest: str,
    ) -> dict[str, object]:
        payload = await self._docker(
            "exec",
            container_id,
            "/usr/bin/python3",
            "-I",
            "-B",
            "-c",
            _PROBE_SCRIPT,
            DOCKER_SNAPSHOT_MOUNT_TREE_VERSION,
            expected_tree_digest,
            timeout_seconds=60,
        )
        value = json.loads(payload)
        if not isinstance(value, dict):
            raise ValueError("Snapshot materialization probe is invalid")
        return value

    async def _read_mount_proof(self, container_id: str) -> dict[str, object]:
        payload = await self._docker(
            "exec",
            container_id,
            "/usr/bin/python3",
            "-I",
            "-B",
            "-c",
            "import os,sys; fd=os.open(sys.argv[1],os.O_RDONLY|os.O_NOFOLLOW); "
            "data=os.read(fd,1048576); os.close(fd); sys.stdout.buffer.write(data)",
            _PROOF_PATH,
            timeout_seconds=20,
        )
        value = json.loads(payload)
        if not isinstance(value, dict):
            raise ValueError("Snapshot mount proof document is invalid")
        return value

    def _inspection(
        self,
        state: SnapshotMountBackendState,
        *,
        lease: SnapshotMountLease,
        pin: SnapshotMountPin,
        observed_at: datetime,
        mount_key: str | None = None,
        mount_proof_digest: str | None = None,
    ) -> SnapshotMountInspection:
        return SnapshotMountInspection(
            state=state,
            lease_id=lease.id,
            lease_digest=lease.lease_digest,
            pin_id=pin.id,
            pin_digest=pin.pin_digest,
            node_id=self.node_id,
            backend_id=self.backend_id,
            backend_digest=self.backend_digest,
            principal=lease.target_runner_principal,
            observed_at=observed_at,
            mount_key=mount_key,
            mount_proof_digest=mount_proof_digest,
        )

    def _stop_evidence(
        self,
        *,
        lease: SnapshotMountLease,
        pin: SnapshotMountPin,
        mount_key: str,
        stopped_at: datetime,
    ) -> SnapshotMountStopEvidence:
        return SnapshotMountStopEvidence(
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
            mount_namespace_unmounted=True,
            lease_revoked=True,
            pin_revoked=True,
            worker_path_inaccessible=True,
        )

    def _static_unavailability_reason(self) -> str | None:
        if platform.system() != "Linux" or os.name != "posix":
            return "audit_snapshot_mount_linux_host_required"
        if self.docker_path is None:
            return "audit_snapshot_mount_docker_unavailable"
        try:
            value = self.docker_socket.stat(follow_symlinks=False)
        except OSError:
            return "audit_snapshot_mount_local_docker_socket_unavailable"
        if not stat.S_ISSOCK(value.st_mode):
            return "audit_snapshot_mount_local_docker_socket_unavailable"
        return None

    async def _inspect_image(self) -> dict[str, Any]:
        payload = await self._docker(
            "image",
            "inspect",
            self._image_reference(),
            timeout_seconds=10,
        )
        value = json.loads(payload)
        if not isinstance(value, list) or len(value) != 1 or not isinstance(value[0], dict):
            raise ValueError("Docker Snapshot mount image inspect is invalid")
        image = value[0]
        if image.get("Id") != self._image_reference() or image.get("Os") != "linux":
            raise ValueError("Docker Snapshot mount image identity differs")
        return image

    async def _qualification_round_trip(self) -> str:
        owner = hashlib.sha256(os.urandom(32)).hexdigest()
        name = f"riftx-audit-snapshot-probe-{owner[:24]}"
        container_id: str | None = None
        try:
            container_id = _require_container_id(
                (
                    await self._docker(
                        "create",
                        "--pull",
                        "never",
                        "--name",
                        name,
                        "--label",
                        f"riftx.audit.snapshot-mount.probe={owner}",
                        "--network",
                        "none",
                        "--read-only",
                        "--cap-drop",
                        "ALL",
                        "--security-opt",
                        "no-new-privileges=true",
                        "--user",
                        _CONTAINER_USER,
                        "--pids-limit",
                        "8",
                        "--memory",
                        "64m",
                        "--memory-swap",
                        "64m",
                        "--log-driver",
                        "none",
                        "--workdir",
                        "/",
                        "--tmpfs",
                        "/workspace:rw,noexec,nosuid,nodev,mode=0755,size=1048576",
                        "--env",
                        "HOME=/nonexistent",
                        "--env",
                        "LANG=C",
                        "--env",
                        "LC_ALL=C",
                        "--env",
                        "PATH=/usr/local/bin:/usr/bin:/bin",
                        "--env",
                        "PYTHONDONTWRITEBYTECODE=1",
                        "--entrypoint",
                        "/usr/bin/python3",
                        self._image_reference(),
                        "-I",
                        "-B",
                        "-c",
                        _HOLDER_SCRIPT,
                        timeout_seconds=20,
                    )
                )
                .decode("ascii", errors="strict")
                .strip()
            )
            await self._docker("start", container_id, timeout_seconds=20)
            await self._docker_with_input(
                _single_file_archive(
                    "probe.txt",
                    b"riftx-snapshot-mount-probe\n",
                    mode=0o444,
                ),
                "cp",
                "-",
                f"{container_id}:/workspace",
                timeout_seconds=20,
            )
            observed = (
                await self._docker(
                    "exec",
                    container_id,
                    "/usr/bin/python3",
                    "-I",
                    "-B",
                    "-c",
                    _QUALIFICATION_SCRIPT,
                    timeout_seconds=20,
                )
            ).decode("ascii", errors="strict")
            if observed != "qualified":
                raise ValueError("Docker Snapshot mount qualification probe differs")
            return _domain_digest(
                DOCKER_SNAPSHOT_MOUNT_QUALIFICATION_VERSION,
                {
                    "container_id_digest": hashlib.sha256(container_id.encode()).hexdigest(),
                    "owner_digest": owner,
                    "result": observed,
                    "schema_version": DOCKER_SNAPSHOT_MOUNT_QUALIFICATION_VERSION,
                },
            )
        finally:
            locator = container_id or name
            existing = await self._inspect_optional(locator)
            if existing is not None:
                config = existing.get("Config")
                labels = config.get("Labels") if isinstance(config, dict) else None
                if (
                    existing.get("Name") != f"/{name}"
                    or not isinstance(labels, dict)
                    or labels.get("riftx.audit.snapshot-mount.probe") != owner
                ):
                    raise _DockerError("Docker qualification owner binding differs")
                cleanup_id = _require_container_id(existing.get("Id"))
                try:
                    await self._docker("rm", "--force", cleanup_id, timeout_seconds=20)
                except _DockerError:
                    if await self._inspect_optional(cleanup_id) is not None:
                        raise
                if await self._inspect_optional(name) is not None:
                    raise _DockerError("Docker qualification cleanup is unproven")

    async def _inspect_required(self, locator: str) -> dict[str, Any]:
        value = await self._inspect_optional(locator)
        if value is None:
            raise _DockerError("Docker Snapshot mount container is absent")
        return value

    async def _inspect_optional(self, locator: str) -> dict[str, Any] | None:
        try:
            payload = await self._docker("inspect", locator, timeout_seconds=10)
        except _DockerError:
            if await self._container_is_absent(locator):
                return None
            raise
        value = json.loads(payload)
        if not isinstance(value, list) or len(value) != 1 or not isinstance(value[0], dict):
            raise _DockerError("Docker Snapshot mount inspect is invalid")
        return value[0]

    async def _container_is_absent(self, locator: str) -> bool:
        is_identifier = _CONTAINER_ID_PATTERN.fullmatch(locator) is not None
        filter_value = f"id={locator}" if is_identifier else f"name=^/{locator}$"
        payload = await self._docker(
            "container",
            "ls",
            "--all",
            "--no-trunc",
            "--filter",
            filter_value,
            "--format",
            "{{.ID}}\t{{.Names}}",
            timeout_seconds=10,
        )
        lines = payload.decode("ascii", errors="strict").splitlines()
        if not lines:
            return True
        for line in lines:
            identifier, separator, names = line.partition("\t")
            if not separator or _CONTAINER_ID_PATTERN.fullmatch(identifier) is None or not names:
                raise _DockerError("Docker Snapshot mount listing is invalid")
            if is_identifier and identifier != locator:
                raise _DockerError("Docker Snapshot mount listing identity differs")
            if not is_identifier and locator not in names.split(","):
                raise _DockerError("Docker Snapshot mount listing name differs")
        return False

    async def _docker(
        self,
        *arguments: str,
        timeout_seconds: int,
    ) -> bytes:
        return await self._run_docker(None, arguments, timeout_seconds=timeout_seconds)

    async def _docker_with_input(
        self,
        content: bytes,
        *arguments: str,
        timeout_seconds: int,
    ) -> bytes:
        return await self._run_docker(content, arguments, timeout_seconds=timeout_seconds)

    async def _run_docker(
        self,
        content: bytes | None,
        arguments: tuple[str, ...],
        *,
        timeout_seconds: int,
    ) -> bytes:
        if self.docker_path is None:
            raise _DockerError("Docker is unavailable")
        if any(not value or "\x00" in value for value in arguments):
            raise _DockerError("Docker argument is invalid")
        try:
            process = await asyncio.create_subprocess_exec(
                self.docker_path,
                "--host",
                f"unix://{self.docker_socket}",
                *arguments,
                cwd="/",
                env={
                    "DOCKER_API_VERSION": "1.45",
                    "HOME": "/nonexistent",
                    "LANG": "C",
                    "LC_ALL": "C",
                    "PATH": "/usr/local/bin:/usr/bin:/bin",
                },
                stdin=(
                    asyncio.subprocess.PIPE
                    if content is not None
                    else asyncio.subprocess.DEVNULL
                ),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                close_fds=True,
                start_new_session=True,
            )
        except OSError as exc:
            raise _DockerError("Docker command could not start") from exc
        try:
            stdout, stderr = await asyncio.wait_for(
                _communicate_bounded(
                    process,
                    content,
                    maximum_bytes=_DOCKER_OUTPUT_LIMIT,
                ),
                timeout=timeout_seconds,
            )
        except TimeoutError as exc:
            process.kill()
            await process.wait()
            raise _DockerError("Docker command timed out") from exc
        except _DockerError:
            if process.returncode is None:
                process.kill()
                await process.wait()
            raise
        if len(stdout) + len(stderr) > _DOCKER_OUTPUT_LIMIT:
            raise _DockerError("Docker command output exceeds its bound")
        if process.returncode != 0:
            raise _DockerError("Docker command failed")
        return stdout

    def _image_reference(self) -> str:
        return f"sha256:{self.image_digest}"


def _owner_digest(
    plan: AuditStaticEffectPlan,
    lease: SnapshotMountLease,
    pin: SnapshotMountPin,
) -> str:
    return _domain_digest(
        DOCKER_SNAPSHOT_MOUNT_OWNER_VERSION,
        {
            "audit_id": lease.audit_id,
            "backend_digest": lease.backend_digest,
            "effect_execution_id": lease.effect_execution_id,
            "image_digest": plan.image_digest,
            "lease_digest": lease.lease_digest,
            "lease_id": lease.id,
            "manifest_digest": lease.manifest_digest,
            "node_id": lease.target_node_id,
            "pin_digest": pin.pin_digest,
            "pin_id": pin.id,
            "plan_digest": plan.plan_digest,
            "plan_id": plan.id,
            "principal": lease.target_runner_principal.model_dump(mode="json"),
            "project_id": lease.project_id,
            "run_id": lease.run_id,
            "schema_version": DOCKER_SNAPSHOT_MOUNT_OWNER_VERSION,
            "snapshot_digest": lease.snapshot_digest,
            "snapshot_id": lease.snapshot_id,
        },
    )


def _mount_proof_document(
    *,
    plan: AuditStaticEffectPlan,
    lease: SnapshotMountLease,
    pin: SnapshotMountPin,
    owner_digest: str,
    container_id: str,
    mount_key: str,
    archive: _MaterializedTreeArchive,
    prepared_at: datetime,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "allowed_blob_digests_digest": _domain_digest(
            DOCKER_SNAPSHOT_MOUNT_ALLOWLIST_VERSION,
            list(lease.allowed_blob_digests),
        ),
        "backend_digest": lease.backend_digest,
        "container_id_digest": hashlib.sha256(container_id.encode()).hexdigest(),
        "descriptor_digest": archive.descriptor_digest,
        "file_count": archive.file_count,
        "image_digest": plan.image_digest,
        "lease_digest": lease.lease_digest,
        "lease_id": lease.id,
        "manifest_digest": lease.manifest_digest,
        "mount_key": mount_key,
        "node_id": lease.target_node_id,
        "owner_digest": owner_digest,
        "pin_digest": pin.pin_digest,
        "pin_id": pin.id,
        "plan_digest": plan.plan_digest,
        "plan_id": plan.id,
        "prepared_at": prepared_at.isoformat(),
        "schema_version": DOCKER_SNAPSHOT_MOUNT_PROOF_VERSION,
        "snapshot_digest": lease.snapshot_digest,
        "total_bytes": archive.total_bytes,
        "tree_proof_digest": archive.tree_proof_digest,
    }
    payload["mount_proof_digest"] = _domain_digest(
        DOCKER_SNAPSHOT_MOUNT_PROOF_VERSION,
        payload,
    )
    return payload


def _validate_mount_proof_document(
    value: dict[str, object],
    *,
    plan: AuditStaticEffectPlan,
    lease: SnapshotMountLease,
    pin: SnapshotMountPin,
    owner_digest: str,
    container_id: str,
) -> tuple[str, str]:
    proof_digest = value.get("mount_proof_digest")
    payload = dict(value)
    payload.pop("mount_proof_digest", None)
    if not isinstance(proof_digest, str) or not hmac.compare_digest(
        proof_digest,
        _domain_digest(DOCKER_SNAPSHOT_MOUNT_PROOF_VERSION, payload),
    ):
        raise ValueError("Snapshot mount proof digest differs")
    mount_key = value.get("mount_key")
    expected = {
        "allowed_blob_digests_digest": _domain_digest(
            DOCKER_SNAPSHOT_MOUNT_ALLOWLIST_VERSION,
            list(lease.allowed_blob_digests),
        ),
        "backend_digest": lease.backend_digest,
        "container_id_digest": hashlib.sha256(container_id.encode()).hexdigest(),
        "image_digest": plan.image_digest,
        "lease_digest": lease.lease_digest,
        "lease_id": lease.id,
        "manifest_digest": lease.manifest_digest,
        "mount_key": _mount_key(owner_digest, container_id),
        "node_id": lease.target_node_id,
        "owner_digest": owner_digest,
        "pin_digest": pin.pin_digest,
        "pin_id": pin.id,
        "plan_digest": plan.plan_digest,
        "plan_id": plan.id,
        "schema_version": DOCKER_SNAPSHOT_MOUNT_PROOF_VERSION,
        "snapshot_digest": lease.snapshot_digest,
    }
    if any(value.get(key) != expected_value for key, expected_value in expected.items()):
        raise ValueError("Snapshot mount proof owner binding differs")
    if (
        not isinstance(mount_key, str)
        or value.get("file_count") is None
        or type(value.get("file_count")) is not int
        or not 0 <= int(value["file_count"]) <= plan.limits.file_count
        or type(value.get("total_bytes")) is not int
        or not 0 <= int(value["total_bytes"]) <= lease.max_bytes
        or not isinstance(value.get("tree_proof_digest"), str)
        or _DIGEST_PATTERN.fullmatch(str(value["tree_proof_digest"])) is None
        or not isinstance(value.get("descriptor_digest"), str)
        or _DIGEST_PATTERN.fullmatch(str(value["descriptor_digest"])) is None
    ):
        raise ValueError("Snapshot mount proof limits are invalid")
    _parse_datetime(value.get("prepared_at"))
    return mount_key, proof_digest


def _container_name(owner_digest: str) -> str:
    return f"riftx-audit-snapshot-{owner_digest[:40]}"


def _mount_key(owner_digest: str, container_id: str) -> str:
    digest = _domain_digest(
        "riftx.snapshot-mount-key/docker/v1",
        {"container_id": container_id, "owner_digest": owner_digest},
    )
    return f"snapshot-mount:v1:{digest}"


def _absent_mount_key(owner_digest: str) -> str:
    digest = _domain_digest(
        "riftx.snapshot-mount-key/absent/v1",
        {"owner_digest": owner_digest},
    )
    return f"snapshot-mount:v1:{digest}"


def _container_state(inspect: dict[str, Any]) -> str:
    state = inspect.get("State")
    if not isinstance(state, dict) or not isinstance(state.get("Status"), str):
        raise ValueError("Docker Snapshot mount state is invalid")
    return str(state["Status"])


def _require_container_id(value: object) -> str:
    if not isinstance(value, str) or _CONTAINER_ID_PATTERN.fullmatch(value) is None:
        raise ValueError("Docker Snapshot mount container ID is invalid")
    return value


def _tree_record(
    relative_path: str,
    object_type: str,
    mode: int,
    size: int,
    blob_digest: str | None,
) -> dict[str, object]:
    return {
        "blob_digest": blob_digest,
        "mode": mode,
        "object_type": object_type,
        "relative_path": relative_path,
        "size": size,
    }


def _tar_directory(name: str) -> tarfile.TarInfo:
    value = tarfile.TarInfo(name)
    value.type = tarfile.DIRTYPE
    value.mode = 0o555
    value.uid = value.gid = 0
    value.uname = value.gname = ""
    value.mtime = 0
    return value


def _tar_regular(name: str, size: int, *, mode: int) -> tarfile.TarInfo:
    value = tarfile.TarInfo(name)
    value.type = tarfile.REGTYPE
    value.size = size
    value.mode = mode
    value.uid = value.gid = 0
    value.uname = value.gname = ""
    value.mtime = 0
    return value


def _tar_symlink(name: str, target: str) -> tarfile.TarInfo:
    value = tarfile.TarInfo(name)
    value.type = tarfile.SYMTYPE
    value.linkname = target
    value.mode = 0o777
    value.uid = value.gid = 0
    value.uname = value.gname = ""
    value.mtime = 0
    return value


def _single_file_archive(name: str, content: bytes, *, mode: int) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w", format=tarfile.PAX_FORMAT) as archive:
        archive.addfile(_tar_regular(name, len(content), mode=mode), io.BytesIO(content))
    return output.getvalue()


def _safe_symlink_target(relative_path: str, content: bytes) -> str:
    target = content.decode("utf-8", errors="strict")
    if not target or "\x00" in target or target.startswith("/"):
        raise ValueError("Snapshot symlink target is unsafe")
    parts = list(PurePosixPath(relative_path).parent.parts)
    for part in PurePosixPath(target).parts:
        if part in {"", "."}:
            continue
        if part == "..":
            if not parts:
                raise ValueError("Snapshot symlink escapes its source root")
            parts.pop()
        else:
            parts.append(part)
    return target


def _parse_datetime(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("Snapshot mount proof timestamp is invalid")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Snapshot mount proof timestamp is naive")
    return parsed


async def _communicate_bounded(
    process: asyncio.subprocess.Process,
    content: bytes | None,
    *,
    maximum_bytes: int,
) -> tuple[bytes, bytes]:
    if process.stdout is None or process.stderr is None:
        raise _DockerError("Docker command pipes are unavailable")

    async def read_stream(stream: asyncio.StreamReader) -> bytes:
        chunks: list[bytes] = []
        observed = 0
        while True:
            chunk = await stream.read(64 * 1024)
            if not chunk:
                return b"".join(chunks)
            observed += len(chunk)
            if observed > maximum_bytes:
                process.kill()
                raise _DockerError("Docker command output exceeds its bound")
            chunks.append(chunk)

    async def write_input() -> None:
        if content is None:
            return
        if process.stdin is None:
            raise _DockerError("Docker command stdin is unavailable")
        try:
            process.stdin.write(content)
            await process.stdin.drain()
            process.stdin.close()
            await process.stdin.wait_closed()
        except (BrokenPipeError, ConnectionResetError) as exc:
            raise _DockerError("Docker command rejected its input") from exc

    writer, stdout, stderr = await asyncio.gather(
        write_input(),
        read_stream(process.stdout),
        read_stream(process.stderr),
    )
    del writer
    await process.wait()
    return stdout, stderr


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


__all__ = [
    "DOCKER_SNAPSHOT_MOUNT_COMPONENT_VERSION",
    "DOCKER_SNAPSHOT_MOUNT_QUALIFICATION_VERSION",
    "DockerSnapshotMountAvailability",
    "DockerSnapshotMountBackend",
]
