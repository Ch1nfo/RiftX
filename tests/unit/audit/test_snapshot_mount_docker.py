from __future__ import annotations

import copy
import hashlib
import io
import json
import tarfile
from datetime import timedelta

import pytest
from tests.integration.persistence.test_audit_static_effect_repository import (
    _EFFECT_NOW,
    _plan,
)

import riftx.audit.snapshot_mount_docker as backend_module
from riftx.audit import (
    AuditStaticEffectLimits,
    DockerSnapshotMountAvailability,
    DockerSnapshotMountBackend,
    SnapshotBlobMetadata,
    SnapshotBlobObjectType,
    SnapshotCASDescriptor,
    SnapshotMountBackendError,
    SnapshotMountBackendState,
    SnapshotMountFailure,
    SnapshotMountLease,
    SnapshotMountPin,
    SnapshotMountSource,
    snapshot_storage_key_digest,
)
from riftx.domain import RunnerPrincipal


def _digest(value: bytes | str) -> str:
    content = value.encode() if isinstance(value, str) else value
    return hashlib.sha256(content).hexdigest()


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
            raise AssertionError("fixture blob was not fully consumed")

    def close(self) -> None:
        self._content.close()


class _MemoryStore:
    def __init__(self, descriptor: SnapshotCASDescriptor, content: dict[str, bytes]) -> None:
        self.descriptor = descriptor
        self.content = content

    def describe(self, binding, content_storage_key: str) -> SnapshotCASDescriptor:
        assert binding.accepts(self.descriptor)
        assert content_storage_key == self.descriptor.content_storage_key
        return self.descriptor

    def open_blob(
        self,
        binding,
        content_storage_key: str,
        relative_path: str,
        expected_blob_digest: str,
        *,
        max_bytes: int,
    ) -> _MemoryReader:
        assert binding.accepts(self.descriptor)
        assert content_storage_key == self.descriptor.content_storage_key
        content = self.content[relative_path]
        assert _digest(content) == expected_blob_digest
        assert len(content) <= max_bytes
        return _MemoryReader(content)


class _FakeDockerSnapshotMountBackend(DockerSnapshotMountBackend):
    def __init__(self, *, daemon: dict[str, object] | None = None) -> None:
        super().__init__(
            node_id="analysis-node",
            image_digest=_digest("snapshot-mount-image"),
            docker_path="/usr/bin/docker",
        )
        self.daemon = daemon if daemon is not None else {
            "inspect": None,
            "calls": [],
            "archives": [],
            "mutation_denial_count": 6,
            "proof": None,
        }

    async def probe_availability(self) -> DockerSnapshotMountAvailability:
        return DockerSnapshotMountAvailability(
            True,
            None,
            self.backend_digest,
            qualification_proof_digest=_digest("qualification"),
        )

    async def _inspect_optional(self, locator: str):
        value = self.daemon["inspect"]
        if value is None:
            return None
        assert isinstance(value, dict)
        if locator not in {
            value["Id"],
            str(value["Name"]).removeprefix("/"),
        }:
            return None
        return copy.deepcopy(value)

    async def _docker(self, *arguments: str, timeout_seconds: int) -> bytes:
        del timeout_seconds
        calls = self.daemon["calls"]
        assert isinstance(calls, list)
        calls.append(arguments)
        command = arguments[0]
        if command == "create":
            self.daemon["inspect"] = _inspect_from_create(arguments, self.image_digest)
            return ("a" * 64 + "\n").encode("ascii")
        if command == "start":
            _set_state(self.daemon, "running")
            return ("a" * 64 + "\n").encode("ascii")
        if command == "exec":
            proof = self.daemon["proof"]
            assert isinstance(proof, dict)
            if backend_module._PROBE_SCRIPT in arguments:
                return json.dumps(
                    {
                        "file_count": proof["file_count"],
                        "mount_proof_digest": proof["mount_proof_digest"],
                        "mutation_denial_count": self.daemon[
                            "mutation_denial_count"
                        ],
                        "total_bytes": proof["total_bytes"],
                        "tree_proof_digest": proof["tree_proof_digest"],
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            return json.dumps(proof, sort_keys=True, separators=(",", ":")).encode("utf-8")
        if command == "stop":
            _set_state(self.daemon, "exited")
            return b""
        if command == "rm":
            self.daemon["inspect"] = None
            return b""
        raise AssertionError(f"unexpected Docker command: {arguments!r}")

    async def _docker_with_input(
        self,
        content: bytes,
        *arguments: str,
        timeout_seconds: int,
    ) -> bytes:
        del timeout_seconds
        calls = self.daemon["calls"]
        archives = self.daemon["archives"]
        assert isinstance(calls, list) and isinstance(archives, list)
        calls.append(arguments)
        archives.append(content)
        with tarfile.open(fileobj=io.BytesIO(content), mode="r:") as archive:
            proof = (
                archive.extractfile(".riftx-snapshot-mount-proof.json")
                if ".riftx-snapshot-mount-proof.json" in archive.getnames()
                else None
            )
            if proof is not None:
                self.daemon["proof"] = json.loads(proof.read())
        return b""


def _set_state(daemon: dict[str, object], state: str) -> None:
    inspect = daemon["inspect"]
    assert isinstance(inspect, dict)
    inspect["State"] = {"Status": state}


def _option(arguments: tuple[str, ...], name: str) -> str:
    return arguments[arguments.index(name) + 1]


def _options(arguments: tuple[str, ...], name: str) -> list[str]:
    return [arguments[index + 1] for index, value in enumerate(arguments) if value == name]


def _inspect_from_create(arguments: tuple[str, ...], image_digest: str) -> dict[str, object]:
    entrypoint_index = arguments.index("--entrypoint")
    image_reference = arguments[entrypoint_index + 2]
    labels = dict(value.split("=", 1) for value in _options(arguments, "--label"))
    tmpfs = _option(arguments, "--tmpfs")
    destination, options = tmpfs.split(":", 1)
    memory = int(_option(arguments, "--memory").removesuffix("b"))
    return {
        "Id": "a" * 64,
        "Image": image_reference,
        "Name": f"/{_option(arguments, '--name')}",
        "State": {"Status": "created"},
        "Config": {
            "Cmd": list(arguments[entrypoint_index + 3 :]),
            "Entrypoint": [_option(arguments, "--entrypoint")],
            "Env": _options(arguments, "--env"),
            "Image": f"sha256:{image_digest}",
            "Labels": labels,
            "User": _option(arguments, "--user"),
            "WorkingDir": _option(arguments, "--workdir"),
        },
        "HostConfig": {
            "Binds": None,
            "CapDrop": _options(arguments, "--cap-drop"),
            "LogConfig": {"Type": _option(arguments, "--log-driver")},
            "Memory": memory,
            "MemorySwap": int(_option(arguments, "--memory-swap").removesuffix("b")),
            "NetworkMode": _option(arguments, "--network"),
            "PidsLimit": int(_option(arguments, "--pids-limit")),
            "Privileged": False,
            "ReadonlyRootfs": "--read-only" in arguments,
            "SecurityOpt": ["no-new-privileges=true"],
            "Tmpfs": {destination: options},
        },
    }


def _authority(backend: DockerSnapshotMountBackend, *, unsafe_link: bool = False):
    base = _plan()
    contents = {
        "bin/check.sh": b"#!/bin/sh\nexit 0\n",
        "main-link": (b"../../etc/passwd" if unsafe_link else b"bin/check.sh"),
        "src/main.py": b"print('safe')\n",
    }
    descriptor = SnapshotCASDescriptor(
        project_id=base.project_id,
        snapshot_digest=base.snapshot_digest,
        manifest_digest=base.manifest_digest,
        blobs=(
            SnapshotBlobMetadata(
                relative_path="bin/check.sh",
                blob_digest=_digest(contents["bin/check.sh"]),
                size=len(contents["bin/check.sh"]),
                mode=0o100755,
            ),
            SnapshotBlobMetadata(
                relative_path="main-link",
                blob_digest=_digest(contents["main-link"]),
                size=len(contents["main-link"]),
                mode=0o120000,
                object_type=SnapshotBlobObjectType.SYMLINK,
            ),
            SnapshotBlobMetadata(
                relative_path="src/main.py",
                blob_digest=_digest(contents["src/main.py"]),
                size=len(contents["src/main.py"]),
                mode=0o100644,
            ),
        ),
    )
    limits = AuditStaticEffectLimits.model_validate(
        {
            **base.limits.model_dump(mode="python"),
            "disk_bytes": 1024 * 1024,
            "input_bytes": descriptor.total_bytes,
            "memory_bytes": 64 * 1024 * 1024,
        }
    )
    payload = base.model_dump(mode="python")
    payload.update(
        backend_digest=backend.backend_digest,
        content_storage_key_digest=snapshot_storage_key_digest(
            descriptor.content_storage_key,
            role="content",
        ),
        image_digest=backend.image_digest,
        limits=limits,
        plan_digest="",
    )
    plan = type(base).model_validate(payload)
    principal = RunnerPrincipal(instance_id="runner-instance-1", epoch=1)
    issue = SnapshotMountLease.issue(
        plan=plan,
        effect_execution_id="static-effect-1",
        target_runner_principal=principal,
        allowed_blob_digests=tuple(sorted({blob.blob_digest for blob in descriptor.blobs})),
        max_bytes=descriptor.total_bytes,
        expires_at=_EFFECT_NOW + timedelta(minutes=5),
        mount_policy_digest=_digest("mount-policy"),
        lease_id="mount-lease-1",
        created_at=_EFFECT_NOW,
    )
    pin = SnapshotMountPin.for_lease(issue.lease, pin_id="mount-pin-1")
    store = _MemoryStore(descriptor, contents)
    source = SnapshotMountSource.resolve(
        plan=plan,
        lease=issue.lease,
        content_storage_key=descriptor.content_storage_key,
        store=store,  # type: ignore[arg-type]
    )
    return plan, issue, pin, source


async def test_non_linux_backend_is_explicitly_unavailable(monkeypatch) -> None:
    backend = DockerSnapshotMountBackend(
        node_id="analysis-node",
        image_digest=_digest("image"),
        docker_path="/usr/bin/docker",
    )
    monkeypatch.setattr(backend_module.platform, "system", lambda: "Darwin")

    availability = await backend.probe_availability()

    assert availability.available is False
    assert availability.reason_code == "audit_snapshot_mount_linux_host_required"
    assert availability.component_digest == backend.backend_digest


async def test_availability_binds_linux_daemon_image_and_private_tmpfs_probe(
    monkeypatch,
) -> None:
    backend = DockerSnapshotMountBackend(
        node_id="analysis-node",
        image_digest=_digest("image"),
        docker_path="/usr/bin/docker",
    )

    async def docker(*arguments: str, timeout_seconds: int) -> bytes:
        del timeout_seconds
        assert arguments == ("version", "--format", "{{.Server.Os}}:{{.Server.Arch}}")
        return b"linux:amd64\n"

    async def inspect_image():
        return {"Id": f"sha256:{backend.image_digest}", "Os": "linux"}

    async def round_trip() -> str:
        return _digest("private-tmpfs-round-trip")

    monkeypatch.setattr(backend, "_static_unavailability_reason", lambda: None)
    monkeypatch.setattr(backend, "_docker", docker)
    monkeypatch.setattr(backend, "_inspect_image", inspect_image)
    monkeypatch.setattr(backend, "_qualification_round_trip", round_trip)

    availability = await backend.probe_availability()

    assert availability.available is True
    assert availability.reason_code is None
    assert availability.component_digest == backend.backend_digest
    assert availability.qualification_proof_digest is not None


async def test_prepare_materializes_private_root_owned_read_only_tree() -> None:
    backend = _FakeDockerSnapshotMountBackend()
    plan, issue, pin, source = _authority(backend)

    prepared = await backend.prepare(
        plan=plan,
        lease=issue.lease,
        pin=pin,
        source=source,
        prepared_at=_EFFECT_NOW + timedelta(seconds=1),
    )

    assert prepared.matches(
        issue.lease,
        pin,
        source,
        observed_at=_EFFECT_NOW + timedelta(seconds=1),
    )
    calls = backend.daemon["calls"]
    archives = backend.daemon["archives"]
    assert isinstance(calls, list) and isinstance(archives, list)
    create = next(call for call in calls if call[0] == "create")
    assert _option(create, "--network") == "none"
    assert "--read-only" in create
    assert _options(create, "--cap-drop") == ["ALL"]
    assert _option(create, "--security-opt") == "no-new-privileges=true"
    assert _option(create, "--user") == backend_module._CONTAINER_USER
    assert _options(create, "--mount") == []
    assert _option(create, "--tmpfs").startswith("/workspace:rw,noexec,nosuid,nodev")

    with tarfile.open(fileobj=io.BytesIO(archives[0]), mode="r:") as archive:
        members = archive.getmembers()
        names = [member.name for member in members]
        assert names == [
            "src",
            "src/bin",
            "src/src",
            "src/bin/check.sh",
            "src/src/main.py",
            "src/main-link",
        ]
        assert all(member.uid == 0 and member.gid == 0 for member in members)
        assert archive.getmember("src").mode == 0o555
        assert archive.getmember("src/bin/check.sh").mode == 0o555
        assert archive.getmember("src/src/main.py").mode == 0o444
        assert archive.getmember("src/main-link").issym()
        assert archive.getmember("src/main-link").linkname == "bin/check.sh"
    assert b"/Users/" not in archives[0]


async def test_prepare_requires_non_root_kernel_mutation_denials() -> None:
    backend = _FakeDockerSnapshotMountBackend()
    plan, issue, pin, source = _authority(backend)
    backend.daemon["mutation_denial_count"] = 5

    with pytest.raises(SnapshotMountBackendError) as rejected:
        await backend.prepare(
            plan=plan,
            lease=issue.lease,
            pin=pin,
            source=source,
            prepared_at=_EFFECT_NOW + timedelta(seconds=1),
        )

    assert rejected.value.failure is SnapshotMountFailure.BACKEND_STATE_UNKNOWN
    assert rejected.value.outcome_unknown is True


async def test_restart_replays_existing_proof_then_stop_proves_absence() -> None:
    backend = _FakeDockerSnapshotMountBackend()
    plan, issue, pin, source = _authority(backend)
    original = await backend.prepare(
        plan=plan,
        lease=issue.lease,
        pin=pin,
        source=source,
        prepared_at=_EFFECT_NOW + timedelta(seconds=1),
    )
    restarted = _FakeDockerSnapshotMountBackend(daemon=backend.daemon)

    replayed = await restarted.prepare(
        plan=plan,
        lease=issue.lease,
        pin=pin,
        source=source,
        prepared_at=_EFFECT_NOW + timedelta(seconds=2),
    )
    inspection = await restarted.inspect(
        plan=plan,
        lease=issue.lease,
        pin=pin,
        observed_at=_EFFECT_NOW + timedelta(seconds=2),
    )
    evidence = await restarted.stop(
        plan=plan,
        lease=issue.lease,
        pin=pin,
        stopped_at=_EFFECT_NOW + timedelta(seconds=3),
    )
    absent = await restarted.inspect(
        plan=plan,
        lease=issue.lease,
        pin=pin,
        observed_at=_EFFECT_NOW + timedelta(seconds=4),
    )

    assert replayed.mount_key == original.mount_key
    assert replayed.mount_proof_digest == original.mount_proof_digest
    assert replayed.prepared_at == original.prepared_at
    assert inspection.state is SnapshotMountBackendState.ACTIVE
    assert evidence.affirmative is True
    assert absent.state is SnapshotMountBackendState.ABSENT
    calls = restarted.daemon["calls"]
    assert isinstance(calls, list)
    assert sum(call[0] == "create" for call in calls) == 1
    assert any(call[:2] == ("rm", "--force") for call in calls)


async def test_inspection_rejects_container_owner_or_security_drift() -> None:
    backend = _FakeDockerSnapshotMountBackend()
    plan, issue, pin, source = _authority(backend)
    await backend.prepare(
        plan=plan,
        lease=issue.lease,
        pin=pin,
        source=source,
        prepared_at=_EFFECT_NOW + timedelta(seconds=1),
    )
    inspect = backend.daemon["inspect"]
    assert isinstance(inspect, dict) and isinstance(inspect["Config"], dict)
    inspect["Config"]["User"] = "0:0"

    with pytest.raises(SnapshotMountBackendError) as rejected:
        await backend.inspect(
            plan=plan,
            lease=issue.lease,
            pin=pin,
            observed_at=_EFFECT_NOW + timedelta(seconds=2),
        )

    assert rejected.value.failure is SnapshotMountFailure.OWNER_MISMATCH
    assert rejected.value.outcome_unknown is True


async def test_inspection_rejects_tampered_materialization_proof() -> None:
    backend = _FakeDockerSnapshotMountBackend()
    plan, issue, pin, source = _authority(backend)
    await backend.prepare(
        plan=plan,
        lease=issue.lease,
        pin=pin,
        source=source,
        prepared_at=_EFFECT_NOW + timedelta(seconds=1),
    )
    proof = backend.daemon["proof"]
    assert isinstance(proof, dict)
    proof["mount_proof_digest"] = _digest("tampered-proof")

    with pytest.raises(SnapshotMountBackendError) as rejected:
        await backend.inspect(
            plan=plan,
            lease=issue.lease,
            pin=pin,
            observed_at=_EFFECT_NOW + timedelta(seconds=2),
        )

    assert rejected.value.failure is SnapshotMountFailure.BACKEND_STATE_UNKNOWN
    assert rejected.value.outcome_unknown is True


async def test_escaping_symlink_rejects_before_any_docker_effect() -> None:
    backend = _FakeDockerSnapshotMountBackend()
    plan, issue, pin, source = _authority(backend, unsafe_link=True)

    with pytest.raises(SnapshotMountBackendError) as rejected:
        await backend.prepare(
            plan=plan,
            lease=issue.lease,
            pin=pin,
            source=source,
            prepared_at=_EFFECT_NOW + timedelta(seconds=1),
        )

    assert rejected.value.failure is SnapshotMountFailure.SOURCE_INTEGRITY
    assert rejected.value.outcome_unknown is False
    assert backend.daemon["calls"] == []


def test_backend_digest_binds_image_and_private_materialization_policy() -> None:
    first = DockerSnapshotMountBackend(
        node_id="analysis-node",
        image_digest=_digest("image-1"),
        docker_path="/usr/bin/docker",
    )
    replay = DockerSnapshotMountBackend(
        node_id="analysis-node",
        image_digest=_digest("image-1"),
        docker_path="/usr/bin/docker",
    )
    second = DockerSnapshotMountBackend(
        node_id="analysis-node",
        image_digest=_digest("image-2"),
        docker_path="/usr/bin/docker",
    )

    assert first.backend_digest == replay.backend_digest
    assert first.backend_digest != second.backend_digest
