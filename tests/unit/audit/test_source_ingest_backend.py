from __future__ import annotations

import hashlib
import io
import json
import os
import tarfile
from pathlib import Path

import pytest

from riftx.audit import source_ingest as backend_module
from riftx.audit.paths import open_authorized_source_repository
from riftx.audit.source_ingest import (
    SOURCE_INGEST_CAPSULE_RECORD_VERSION,
    DockerSourceIngestBackend,
    SourceIngestBackendAvailability,
    SourceIngestBackendError,
    SourceIngestCapsuleRecord,
    SourceIngestProbe,
    SourceMountObservation,
    _capsule_label,
    _container_name,
    _mount_observation_from_value,
    _read_worker_result_archive,
    _validate_prepared_container,
)
from riftx.audit.source_ingest_contract import (
    SourceIngestWorkerOutcome,
    SourceIngestWorkerRequest,
    SourceIngestWorkerResult,
)
from riftx.config import AuditConfig, AuditSourceIngestConfig
from riftx.domain.audit import AuditMode, SourceTargetKind


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _audit(root: Path) -> AuditConfig:
    return AuditConfig(
        enabled=True,
        source_roots=(root,),
        source_ingest=AuditSourceIngestConfig(image_digest="a" * 64),
    )


def _backend(tmp_path: Path) -> DockerSourceIngestBackend:
    root = tmp_path / "source-root"
    root.mkdir(exist_ok=True)
    return DockerSourceIngestBackend(audit=_audit(root), state_root=tmp_path / "state")


def _record(
    backend: DockerSourceIngestBackend,
    *,
    state: str,
    container_id: str | None = "c" * 64,
    prepare_proof_digest: str | None = None,
    process_identity_digest: str | None = None,
    observed_state: str | None = None,
    exit_code: int | None = None,
) -> SourceIngestCapsuleRecord:
    return SourceIngestCapsuleRecord(
        schema_version=SOURCE_INGEST_CAPSULE_RECORD_VERSION,
        capsule_id="capsule-1",
        container_name=_container_name("capsule-1"),
        container_id=container_id,
        request_digest=_digest("request"),
        source_root_identity_digest=_digest("source-root"),
        repository_descriptor_identity_digest=_digest("repository"),
        source_mount_identity_digest=_digest("source-mount"),
        backend_id="linux_container",
        image_digest="a" * 64,
        policy_digest=backend.policy_digest,
        capsule_user_id=os.geteuid(),
        lifecycle_state=state,
        prepare_proof_digest=prepare_proof_digest,
        process_identity_digest=process_identity_digest,
        observed_state=observed_state,
        exit_code=exit_code,
    )


def _mount_probe_record(
    backend: DockerSourceIngestBackend,
    *,
    state: str,
    container_id: str | None,
) -> backend_module.SourceMountProbeRecord:
    probe_id = "1" * 32
    container_name = backend_module._mount_probe_container_name(probe_id)
    container_label = backend_module._mount_probe_label(probe_id)
    host_identity = _digest("host-identity")
    host_proof = _digest("host-proof")
    owner_digest = backend_module._domain_digest(
        backend_module.SOURCE_INGEST_MOUNT_PROBE_OWNER_VERSION,
        {
            "backend_id": backend.audit.source_ingest.backend_id,
            "container_label": container_label,
            "container_name": container_name,
            "host_identity_digest": host_identity,
            "host_proof_digest": host_proof,
            "image_digest": "a" * 64,
            "policy_digest": backend.policy_digest,
            "probe_id": probe_id,
            "schema_version": backend_module.SOURCE_INGEST_MOUNT_PROBE_OWNER_VERSION,
        },
    )
    return backend_module.SourceMountProbeRecord(
        schema_version=backend_module.SOURCE_INGEST_MOUNT_PROBE_RECORD_VERSION,
        probe_id=probe_id,
        owner_digest=owner_digest,
        container_name=container_name,
        container_label=container_label,
        container_id=container_id,
        backend_id=backend.audit.source_ingest.backend_id,
        image_digest="a" * 64,
        policy_digest=backend.policy_digest,
        host_identity_digest=host_identity,
        host_proof_digest=host_proof,
        lifecycle_state=state,
        state_version=0,
        observed_state="fixture",
    )


def _write_record(
    backend: DockerSourceIngestBackend,
    tmp_path: Path,
    record: SourceIngestCapsuleRecord,
) -> None:
    capsule = tmp_path / "state" / "audit-preflight-capsules" / record.capsule_id
    capsule.mkdir(parents=True)
    backend._write_capsule_record(record)


def _request(source) -> SourceIngestWorkerRequest:
    return SourceIngestWorkerRequest(
        capsule_id="capsule-1",
        request_digest=_digest("request"),
        source_root_identity_digest=source.source_root_identity_digest,
        repository_descriptor_identity_digest=(source.repository_descriptor_identity_digest),
        expected_source_mount_identity_digest=_digest("source-mount"),
        target_kind=SourceTargetKind.WORKING_TREE,
        revision="HEAD",
        mode=AuditMode.STANDARD,
        include_untracked=True,
        max_files=100,
        max_repository_bytes=1024 * 1024,
        max_file_bytes=128 * 1024,
        max_git_output_bytes=1024 * 1024,
        command_timeout_seconds=10,
    )


def _inspect(
    *,
    repository_fd_path: Path,
    input_fd_path: Path,
    worker_fd_path: Path,
    user: str,
) -> dict[str, object]:
    return {
        "Id": "c" * 64,
        "Image": "sha256:" + "a" * 64,
        "HostConfig": {
            "NetworkMode": "none",
            "ReadonlyRootfs": True,
            "Privileged": False,
            "PidsLimit": 32,
            "Memory": 512 * 1024 * 1024,
            "MemorySwap": 512 * 1024 * 1024,
            "NanoCpus": 1_000_000_000,
            "CapDrop": ["ALL"],
            "CapAdd": None,
            "SecurityOpt": ["no-new-privileges=true"],
            "LogConfig": {"Type": "none", "Config": {}},
            "Tmpfs": {
                "/tmp": "rw,noexec,nosuid,nodev,size=67108864",
                "/output": "rw,noexec,nosuid,nodev,mode=1777,size=1048576",
            },
            "Ulimits": [
                {"Name": "nofile", "Soft": 256, "Hard": 256},
                {"Name": "nproc", "Soft": 32, "Hard": 32},
            ],
            "Devices": [],
            "DeviceRequests": None,
            "PortBindings": {},
            "Links": None,
            "Dns": [],
            "ExtraHosts": [],
            "VolumesFrom": None,
        },
        "Config": {
            "User": user,
            "WorkingDir": "/",
            "Entrypoint": ["/usr/bin/python3"],
            "Cmd": [
                "-I",
                "-B",
                "/opt/riftx/preflight.py",
                "/input/request.json",
                "/output/result.json",
            ],
            "OpenStdin": False,
            "Tty": False,
            "Env": [
                "HOME=/nonexistent",
                "LANG=C",
                "LC_ALL=C",
                "PATH=/usr/local/bin:/usr/bin:/bin",
            ],
            "Labels": {"riftx.audit-preflight.capsule": _capsule_label("capsule-1")},
        },
        "Mounts": [
            {
                "Type": "bind",
                "Source": str(repository_fd_path),
                "Destination": "/source",
                "RW": False,
            },
            {
                "Type": "bind",
                "Source": str(input_fd_path),
                "Destination": "/input/request.json",
                "RW": False,
            },
            {
                "Type": "bind",
                "Source": str(worker_fd_path),
                "Destination": "/opt/riftx/preflight.py",
                "RW": False,
            },
        ],
    }


def _result_archive(
    result: SourceIngestWorkerResult,
    *,
    mode: int = 0o600,
    owner_uid: int | None = None,
    name: str = "result.json",
) -> bytes:
    content = result.model_dump_json().encode()
    payload = io.BytesIO()
    info = tarfile.TarInfo(name)
    info.size = len(content)
    info.mode = mode
    info.uid = os.geteuid() if owner_uid is None else owner_uid
    info.gid = os.getegid()
    with tarfile.open(fileobj=payload, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        archive.addfile(info, io.BytesIO(content))
    minimum_size = 512 + ((len(content) + 511) // 512) * 512 + 1024
    return payload.getvalue()[:minimum_size]


def _probe_archive(value: dict[str, object], *, owner_uid: int) -> bytes:
    content = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("ascii")
    payload = io.BytesIO()
    info = tarfile.TarInfo("identity.json")
    info.size = len(content)
    info.mode = 0o600
    info.uid = owner_uid
    info.gid = os.getegid()
    with tarfile.open(fileobj=payload, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        archive.addfile(info, io.BytesIO(content))
    minimum_size = 512 + ((len(content) + 511) // 512) * 512 + 1024
    return payload.getvalue()[:minimum_size]


async def test_non_linux_host_is_fail_closed_before_docker_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _backend(tmp_path)
    calls = 0

    async def docker(*_args: str, **_kwargs: object) -> bytes:
        nonlocal calls
        calls += 1
        return b""

    monkeypatch.setattr("riftx.audit.source_ingest.platform.system", lambda: "Darwin")
    monkeypatch.setattr(backend, "_docker", docker)

    availability = await backend.probe_availability()

    assert availability.available is False
    assert availability.reason_code == "audit_source_ingest_linux_host_required"
    assert calls == 0


async def test_availability_requires_descriptor_mount_round_trip(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _backend(tmp_path)
    monkeypatch.setattr(backend, "_static_unavailability_reason", lambda: None)
    monkeypatch.setattr(
        "riftx.audit.source_ingest._regular_file_digest",
        lambda *_args, **_kwargs: _digest("worker"),
    )

    async def docker(*arguments: str, **_kwargs: object) -> bytes:
        assert arguments == ("version", "--format", "{{.Server.Os}}:{{.Server.Arch}}")
        return b"linux:x86_64\n"

    async def inspect_image() -> dict[str, object]:
        return {"Id": "sha256:" + "a" * 64, "Os": "linux"}

    round_trip_calls = 0

    async def round_trip() -> str:
        nonlocal round_trip_calls
        round_trip_calls += 1
        return _digest("mount-round-trip")

    monkeypatch.setattr(backend, "_docker", docker)
    monkeypatch.setattr(backend, "_inspect_image", inspect_image)
    monkeypatch.setattr(backend, "_probe_descriptor_mount_round_trip", round_trip)

    availability = await backend.probe_availability()

    assert availability.available is True
    assert round_trip_calls == 1
    assert availability.component_digest is not None


async def test_descriptor_mount_round_trip_compares_worker_inode_and_cleans_up(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _backend(tmp_path)
    observed_value = {
        "filesystem_type": "ext4",
        "mount_id": 91,
        "st_dev": 2049,
        "st_ino": 501,
    }
    worker_observation = _mount_observation_from_value(observed_value)
    monkeypatch.setattr(
        "riftx.audit.source_ingest._source_mount_observation",
        lambda *_args, **_kwargs: SourceMountObservation(
            identity_digest=worker_observation.identity_digest,
            proof_digest=_digest("host-mount-proof"),
            filesystem_type="ext4",
            mount_id=17,
        ),
    )
    monkeypatch.setattr(
        "riftx.audit.source_ingest._require_fd_path_identity",
        lambda *_args, **_kwargs: None,
    )
    calls: list[tuple[str, ...]] = []
    removed = False

    async def docker(*arguments: str, **_kwargs: object) -> bytes:
        nonlocal removed
        calls.append(arguments)
        if arguments[0] == "create":
            return ("c" * 64 + "\n").encode("ascii")
        if arguments[0] == "wait":
            return b"0\n"
        if arguments[0] == "cp":
            return _probe_archive(observed_value, owner_uid=os.geteuid())
        if arguments[:2] == ("rm", "--force"):
            removed = True
        return b""

    async def inspect(_container_id: str) -> dict[str, object]:
        if removed:
            raise SourceIngestBackendError("audit_source_ingest_container_not_found")
        create = calls[0]
        descriptor_mount = next(
            create[index + 1] for index, value in enumerate(create) if value == "--mount"
        )
        descriptor_path = descriptor_mount.removeprefix("type=bind,src=").split(",", 1)[0]
        name = create[create.index("--name") + 1]
        label_value = create[create.index("--label") + 1].split("=", 1)[1]
        user = create[create.index("--user") + 1]
        return {
            "Id": "c" * 64,
            "Name": f"/{name}",
            "Image": "sha256:" + "a" * 64,
            "HostConfig": {
                "NetworkMode": "none",
                "ReadonlyRootfs": True,
                "Privileged": False,
                "AutoRemove": False,
                "RestartPolicy": {"Name": "no", "MaximumRetryCount": 0},
                "PidsLimit": 8,
                "Memory": 64 * 1024 * 1024,
                "MemorySwap": 64 * 1024 * 1024,
                "NanoCpus": 250_000_000,
                "CapDrop": ["ALL"],
                "CapAdd": None,
                "SecurityOpt": ["no-new-privileges=true"],
                "LogConfig": {"Type": "none", "Config": {}},
                "Tmpfs": {"/output": "rw,noexec,nosuid,nodev,mode=1777,size=1048576"},
            },
            "Config": {
                "User": user,
                "WorkingDir": "/",
                "Entrypoint": ["/usr/bin/python3"],
                "Cmd": ["-I", "-B", "-c", backend_module._SOURCE_MOUNT_PROBE_SCRIPT],
                "OpenStdin": False,
                "Tty": False,
                "Env": [
                    "HOME=/nonexistent",
                    "LANG=C",
                    "LC_ALL=C",
                    "PATH=/usr/local/bin:/usr/bin:/bin",
                ],
                "Labels": {"riftx.audit-preflight.mount-probe": label_value},
            },
            "Mounts": [
                {
                    "Type": "bind",
                    "Source": descriptor_path,
                    "Destination": "/source",
                    "RW": False,
                }
            ],
        }

    monkeypatch.setattr(backend, "_docker", docker)
    monkeypatch.setattr(backend, "_inspect", inspect)

    digest = await backend._probe_descriptor_mount_round_trip()

    assert len(digest) == 64
    assert any(call[:2] == ("rm", "--force") for call in calls)
    probe_record = backend_module._read_mount_probe_record(
        backend.state_root / backend_module._MOUNT_PROBE_RECORD_NAME
    )
    assert probe_record.lifecycle_state == "cleanup_complete"
    assert probe_record.destruction_proof_digest is not None


async def test_mount_probe_create_response_loss_is_durably_recovered(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _backend(tmp_path)
    observation = SourceMountObservation(
        identity_digest=_digest("source-mount"),
        proof_digest=_digest("source-mount-proof"),
        filesystem_type="ext4",
        mount_id=17,
    )
    monkeypatch.setattr(
        "riftx.audit.source_ingest._source_mount_observation",
        lambda *_args, **_kwargs: observation,
    )
    monkeypatch.setattr(
        "riftx.audit.source_ingest._require_fd_path_identity",
        lambda *_args, **_kwargs: None,
    )
    created = False
    removed = False
    container_name = ""
    container_label = ""

    async def docker(*arguments: str, **_kwargs: object) -> bytes:
        nonlocal created, removed, container_name, container_label
        if arguments[0] == "create":
            container_name = arguments[arguments.index("--name") + 1]
            container_label = arguments[arguments.index("--label") + 1].split("=", 1)[1]
            created = True
            raise SourceIngestBackendError("audit_source_ingest_docker_command_failed")
        if arguments[:2] == ("rm", "--force"):
            removed = True
            return b""
        raise AssertionError(arguments)

    async def inspect(_locator: str) -> dict[str, object]:
        if not created or removed:
            raise SourceIngestBackendError("audit_source_ingest_container_not_found")
        return {
            "Id": "c" * 64,
            "Name": f"/{container_name}",
            "Image": "sha256:" + "a" * 64,
            "Config": {"Labels": {"riftx.audit-preflight.mount-probe": container_label}},
        }

    monkeypatch.setattr(backend, "_docker", docker)
    monkeypatch.setattr(backend, "_inspect", inspect)

    with pytest.raises(SourceIngestBackendError) as error:
        await backend._probe_descriptor_mount_round_trip()

    assert error.value.code == "audit_source_ingest_docker_command_failed"
    assert removed is True
    record = backend_module._read_mount_probe_record(
        backend.state_root / backend_module._MOUNT_PROBE_RECORD_NAME
    )
    assert record.container_id == "c" * 64
    assert record.lifecycle_state == "cleanup_complete"
    assert record.destruction_proof_digest is not None


async def test_mount_probe_late_create_stays_unknown_until_container_is_recovered(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _backend(tmp_path)
    observation = SourceMountObservation(
        identity_digest=_digest("source-mount"),
        proof_digest=_digest("source-mount-proof"),
        filesystem_type="ext4",
        mount_id=17,
    )
    monkeypatch.setattr(
        "riftx.audit.source_ingest._source_mount_observation",
        lambda *_args, **_kwargs: observation,
    )
    monkeypatch.setattr(
        "riftx.audit.source_ingest._require_fd_path_identity",
        lambda *_args, **_kwargs: None,
    )
    container_visible = False
    removed = False
    container_name = ""
    container_label = ""
    docker_calls: list[tuple[str, ...]] = []

    async def docker(*arguments: str, **_kwargs: object) -> bytes:
        nonlocal removed, container_name, container_label
        docker_calls.append(arguments)
        if arguments[0] == "create":
            container_name = arguments[arguments.index("--name") + 1]
            container_label = arguments[arguments.index("--label") + 1].split("=", 1)[1]
            raise SourceIngestBackendError("audit_source_ingest_docker_command_failed")
        if arguments[:2] == ("rm", "--force"):
            assert arguments[2] == "c" * 64
            removed = True
            return b""
        raise AssertionError(arguments)

    async def inspect(locator: str) -> dict[str, object]:
        if not container_visible or removed:
            raise SourceIngestBackendError("audit_source_ingest_container_not_found")
        assert locator == container_name
        return {
            "Id": "c" * 64,
            "Name": f"/{container_name}",
            "Image": "sha256:" + "a" * 64,
            "Config": {"Labels": {"riftx.audit-preflight.mount-probe": container_label}},
        }

    monkeypatch.setattr(backend, "_docker", docker)
    monkeypatch.setattr(backend, "_inspect", inspect)

    with pytest.raises(SourceIngestBackendError) as error:
        await backend._probe_descriptor_mount_round_trip()
    assert error.value.code == "audit_source_ingest_mount_probe_create_outcome_unknown"

    record_path = backend.state_root / backend_module._MOUNT_PROBE_RECORD_NAME
    uncertain = backend_module._read_mount_probe_record(record_path)
    assert uncertain.lifecycle_state == "outcome_unknown"
    assert uncertain.container_id is None
    assert uncertain.observed_state == "create_outcome_unknown"
    assert uncertain.destruction_proof_digest is None
    assert all(call[:2] != ("rm", "--force") for call in docker_calls)

    with pytest.raises(SourceIngestBackendError) as repeated_error:
        await backend.reconcile_mount_probe()
    assert repeated_error.value.code == "audit_source_ingest_mount_probe_create_outcome_unknown"
    repeated = backend_module._read_mount_probe_record(record_path)
    assert repeated == uncertain

    container_visible = True
    proof = await backend.reconcile_mount_probe()

    completed = backend_module._read_mount_probe_record(record_path)
    assert proof == completed.destruction_proof_digest
    assert completed.lifecycle_state == "cleanup_complete"
    assert completed.container_id == "c" * 64
    assert completed.observed_state == "confirmed_absent"
    assert completed.destruction_proof_digest is not None
    assert any(call[:2] == ("rm", "--force") for call in docker_calls)


async def test_mount_probe_cleanup_failure_is_retried_from_durable_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _backend(tmp_path)
    record_path = backend.state_root / backend_module._MOUNT_PROBE_RECORD_NAME
    record_path.parent.mkdir(parents=True)
    record = _mount_probe_record(
        backend,
        state="start_requested",
        container_id="c" * 64,
    )
    backend_module._write_new_mount_probe_record(record_path, record)
    allow_removal = False
    removed = False

    async def docker(*arguments: str, **_kwargs: object) -> bytes:
        nonlocal removed
        assert arguments[:2] == ("rm", "--force")
        if not allow_removal:
            raise SourceIngestBackendError("audit_source_ingest_docker_command_failed")
        removed = True
        return b""

    async def inspect(_locator: str) -> dict[str, object]:
        if removed:
            raise SourceIngestBackendError("audit_source_ingest_container_not_found")
        return {
            "Id": "c" * 64,
            "Name": f"/{record.container_name}",
            "Image": "sha256:" + record.image_digest,
            "Config": {"Labels": {"riftx.audit-preflight.mount-probe": record.container_label}},
        }

    monkeypatch.setattr(backend, "_docker", docker)
    monkeypatch.setattr(backend, "_inspect", inspect)

    with pytest.raises(SourceIngestBackendError) as error:
        await backend.reconcile_mount_probe()
    assert error.value.code == "audit_source_ingest_docker_command_failed"
    uncertain = backend_module._read_mount_probe_record(record_path)
    assert uncertain.lifecycle_state == "outcome_unknown"
    assert uncertain.destruction_proof_digest is None

    allow_removal = True
    proof = await backend.reconcile_mount_probe()

    completed = backend_module._read_mount_probe_record(record_path)
    assert proof == completed.destruction_proof_digest
    assert completed.lifecycle_state == "cleanup_complete"
    assert completed.state_version > uncertain.state_version


async def test_inspect_failure_requires_independent_absence_proof(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _backend(tmp_path)

    async def docker(*arguments: str, **_kwargs: object) -> bytes:
        if arguments[0] == "inspect":
            raise SourceIngestBackendError("audit_source_ingest_docker_command_failed")
        if arguments[:2] == ("container", "ls"):
            raise SourceIngestBackendError("audit_source_ingest_inspect_unavailable")
        raise AssertionError(arguments)

    monkeypatch.setattr(backend, "_docker", docker)

    with pytest.raises(SourceIngestBackendError) as error:
        await backend._inspect("c" * 64)

    assert error.value.code == "audit_source_ingest_inspect_unavailable"


@pytest.mark.parametrize(
    "locator",
    ("c" * 64, "riftx-preflight-probe-" + "1" * 32),
)
async def test_inspect_failure_accepts_successful_empty_container_listing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    locator: str,
) -> None:
    backend = _backend(tmp_path)

    async def docker(*arguments: str, **_kwargs: object) -> bytes:
        if arguments[0] == "inspect":
            raise SourceIngestBackendError("audit_source_ingest_docker_command_failed")
        if arguments[:2] == ("container", "ls"):
            return b""
        raise AssertionError(arguments)

    monkeypatch.setattr(backend, "_docker", docker)

    with pytest.raises(SourceIngestBackendError) as error:
        await backend._inspect(locator)

    assert error.value.code == "audit_source_ingest_container_not_found"


def test_capsule_record_is_durable_bounded_and_rejects_symlink_replacement(
    tmp_path: Path,
) -> None:
    backend = _backend(tmp_path)
    record = _record(backend, state="created", observed_state="created")
    _write_record(backend, tmp_path, record)

    assert backend.get_capsule_record("capsule-1") == record
    assert backend.list_capsule_records() == (record,)

    state_path = tmp_path / "state" / "audit-preflight-capsules" / "capsule-1" / "capsule.json"
    state_path.unlink()
    state_path.symlink_to(tmp_path / "outside.json")
    with pytest.raises(SourceIngestBackendError) as error:
        backend.get_capsule_record("capsule-1")
    assert error.value.code == "audit_source_ingest_capsule_state_invalid"


async def test_created_but_never_started_capsule_is_affirmatively_stopped_without_removal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _backend(tmp_path)
    _write_record(
        backend,
        tmp_path,
        _record(backend, state="created", observed_state="created"),
    )
    docker_calls: list[tuple[str, ...]] = []

    async def probe(_container_id: str) -> SourceIngestProbe:
        return SourceIngestProbe(
            True,
            False,
            False,
            _digest("created-process"),
            "created",
        )

    async def docker(*arguments: str, **_kwargs: object) -> bytes:
        docker_calls.append(arguments)
        return b""

    monkeypatch.setattr(backend, "probe_container", probe)
    monkeypatch.setattr(backend, "_docker", docker)

    evidence = await backend.stop_capsule("capsule-1")

    assert evidence.stopped is True
    assert evidence.observed_state == "created_not_started"
    assert docker_calls == []
    assert backend.get_capsule_record("capsule-1").lifecycle_state == "stop_observed"


async def test_missing_container_never_becomes_a_fake_stop_proof(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _backend(tmp_path)
    _write_record(
        backend,
        tmp_path,
        _record(
            backend,
            state="prepared",
            prepare_proof_digest=_digest("prepare"),
            observed_state="created",
        ),
    )

    async def probe(_container_id: str) -> SourceIngestProbe:
        return SourceIngestProbe(False, False, False, None, "not_found")

    monkeypatch.setattr(backend, "probe_container", probe)

    evidence = await backend.stop_capsule("capsule-1")

    assert evidence.stopped is False
    assert evidence.observed_state == "not_found_unproven"
    assert backend.get_capsule_record("capsule-1").lifecycle_state == "outcome_unknown"


async def test_uncertain_create_recovers_exact_named_container_for_stop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _backend(tmp_path)
    _write_record(
        backend,
        tmp_path,
        _record(
            backend,
            state="create_intent",
            container_id=None,
        ),
    )
    inspect = _inspect(
        repository_fd_path=Path("/proc/4321/fd/10"),
        input_fd_path=Path("/proc/4321/fd/11"),
        worker_fd_path=Path("/proc/4321/fd/12"),
        user=f"{os.geteuid()}:{os.getegid()}",
    )
    inspect["Name"] = f"/{_container_name('capsule-1')}"
    inspect["State"] = {
        "Status": "created",
        "Pid": 0,
        "StartedAt": "0001-01-01T00:00:00Z",
        "FinishedAt": "0001-01-01T00:00:00Z",
    }

    async def inspect_container(locator: str) -> dict[str, object]:
        assert locator in {_container_name("capsule-1"), "c" * 64}
        return inspect

    monkeypatch.setattr(backend, "_inspect", inspect_container)

    probe = await backend.recover_create_intent("capsule-1")

    assert probe.exists is True
    assert probe.observed_state == "created"
    record = backend.get_capsule_record("capsule-1")
    assert record is not None
    assert record.container_id == "c" * 64
    assert record.lifecycle_state == "created"
    assert record.observed_state == "recovered_create_created"
    assert record.process_identity_digest == probe.process_identity_digest

    stop = await backend.stop_capsule("capsule-1")
    assert stop.stopped is True
    assert stop.observed_state == "created_not_started"
    assert backend.get_capsule_record("capsule-1").lifecycle_state == "stop_observed"


async def test_uncertain_create_missing_name_remains_unproven(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _backend(tmp_path)
    _write_record(
        backend,
        tmp_path,
        _record(
            backend,
            state="create_intent",
            container_id=None,
        ),
    )

    async def inspect_container(_locator: str) -> dict[str, object]:
        raise SourceIngestBackendError("audit_source_ingest_container_not_found")

    monkeypatch.setattr(backend, "_inspect", inspect_container)

    probe = await backend.recover_create_intent("capsule-1")

    assert probe.exists is False
    assert probe.observed_state == "create_not_found_unproven"
    record = backend.get_capsule_record("capsule-1")
    assert record is not None
    assert record.container_id is None
    assert record.lifecycle_state == "outcome_unknown"


async def test_ambiguous_start_never_uses_created_as_stop_proof(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _backend(tmp_path)
    _write_record(
        backend,
        tmp_path,
        _record(
            backend,
            state="start_requested",
            prepare_proof_digest=_digest("prepare"),
            observed_state="created",
        ),
    )
    probes = iter(
        (
            SourceIngestProbe(
                True,
                False,
                False,
                _digest("created-process"),
                "created",
            ),
            SourceIngestProbe(
                True,
                False,
                False,
                _digest("created-process"),
                "created",
            ),
            SourceIngestProbe(
                True,
                True,
                False,
                _digest("running-process"),
                "running",
            ),
            SourceIngestProbe(
                True,
                False,
                True,
                _digest("terminal-process"),
                "exited",
            ),
        )
    )
    docker_calls: list[tuple[str, ...]] = []

    async def probe(_container_id: str) -> SourceIngestProbe:
        return next(probes)

    async def docker(*arguments: str, **_kwargs: object) -> bytes:
        docker_calls.append(arguments)
        return b""

    monkeypatch.setattr(backend, "probe_container", probe)
    monkeypatch.setattr(backend, "_docker", docker)

    first = await backend.stop_capsule("capsule-1")
    assert first.stopped is False
    assert first.observed_state == "start_outcome_unknown_created"
    uncertain = backend.get_capsule_record("capsule-1")
    assert uncertain is not None
    assert uncertain.lifecycle_state == "outcome_unknown"
    assert docker_calls == []

    second = await backend.stop_capsule("capsule-1")
    assert second.stopped is False
    assert second.observed_state == "start_outcome_unknown_created"
    assert backend.get_capsule_record("capsule-1") == uncertain
    assert docker_calls == []

    settled = await backend.stop_capsule("capsule-1")
    assert settled.stopped is True
    assert settled.observed_state == "exited"
    assert [call[0] for call in docker_calls] == ["stop"]
    assert backend.get_capsule_record("capsule-1").lifecycle_state == "stop_observed"


async def test_created_stop_proof_loses_to_concurrent_start_intent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _backend(tmp_path)
    _write_record(
        backend,
        tmp_path,
        _record(
            backend,
            state="prepared",
            prepare_proof_digest=_digest("prepare"),
            observed_state="created",
        ),
    )

    async def probe(_container_id: str) -> SourceIngestProbe:
        backend._transition_capsule_record(
            "capsule-1",
            expected_states={"prepared"},
            lifecycle_state="start_requested",
        )
        return SourceIngestProbe(
            True,
            False,
            False,
            _digest("created-process"),
            "created",
        )

    monkeypatch.setattr(backend, "probe_container", probe)

    evidence = await backend.stop_capsule("capsule-1")

    assert evidence.stopped is False
    assert evidence.observed_state == "created_stop_unproven"
    record = backend.get_capsule_record("capsule-1")
    assert record is not None
    assert record.lifecycle_state == "start_requested"


async def test_running_stop_is_journaled_before_docker_and_cleanup_waits_for_ack(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _backend(tmp_path)
    _write_record(
        backend,
        tmp_path,
        _record(
            backend,
            state="running",
            prepare_proof_digest=_digest("prepare"),
            process_identity_digest=_digest("running-process"),
            observed_state="running",
        ),
    )
    probes = iter(
        (
            SourceIngestProbe(
                True,
                True,
                False,
                _digest("running-process"),
                "running",
            ),
            SourceIngestProbe(
                True,
                False,
                True,
                _digest("terminal-process"),
                "exited",
            ),
            SourceIngestProbe(
                True,
                False,
                True,
                _digest("terminal-process"),
                "exited",
            ),
            SourceIngestProbe(
                False,
                False,
                False,
                None,
                "not_found",
            ),
        )
    )
    docker_calls: list[tuple[str, ...]] = []

    async def probe(_container_id: str) -> SourceIngestProbe:
        return next(probes)

    async def docker(*arguments: str, **_kwargs: object) -> bytes:
        if arguments[0] == "stop":
            assert backend.get_capsule_record("capsule-1").lifecycle_state == "stop_requested"
        docker_calls.append(arguments)
        return b""

    monkeypatch.setattr(backend, "probe_container", probe)
    monkeypatch.setattr(backend, "_docker", docker)

    evidence = await backend.stop_capsule("capsule-1")
    assert evidence.stopped is True
    assert backend.get_capsule_record("capsule-1").lifecycle_state == "stop_observed"

    with pytest.raises(SourceIngestBackendError) as error:
        await backend.cleanup_capsule(
            "capsule-1",
            terminal_proof_persisted=False,
        )
    assert error.value.code == "audit_source_ingest_cleanup_requires_persisted_proof"
    assert all(call[0] != "rm" for call in docker_calls)

    await backend.cleanup_capsule("capsule-1", terminal_proof_persisted=True)
    assert docker_calls[-1][0] == "rm"
    assert backend.get_capsule_record("capsule-1").lifecycle_state == "cleanup_complete"


async def test_start_and_wait_are_split_and_terminal_wait_is_restart_replayable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _backend(tmp_path)
    _write_record(
        backend,
        tmp_path,
        _record(
            backend,
            state="prepared",
            prepare_proof_digest=_digest("prepare"),
            observed_state="created",
        ),
    )
    worker_result = SourceIngestWorkerResult(
        outcome=SourceIngestWorkerOutcome.FAILED,
        safe_error_code="audit_fixture_failed",
        request_digest=_digest("request"),
        source_root_identity_digest=_digest("source-root"),
        repository_descriptor_identity_digest=_digest("repository"),
    )
    calls: list[str] = []
    inspect_count = 0

    async def docker(*arguments: str, **_kwargs: object) -> bytes:
        calls.append(arguments[0])
        if arguments[0] == "wait":
            return b"0\n"
        if arguments[0] == "cp":
            return _result_archive(worker_result)
        return b""

    async def inspect(_container_id: str) -> dict[str, object]:
        nonlocal inspect_count
        inspect_count += 1
        running = inspect_count == 1
        return {
            "Id": "c" * 64,
            "State": {
                "Status": "running" if running else "exited",
                "Pid": 1234 if running else 0,
                "StartedAt": "2026-08-04T00:00:00Z",
                "FinishedAt": "" if running else "2026-08-04T00:00:01Z",
            },
        }

    monkeypatch.setattr(backend, "_docker", docker)
    monkeypatch.setattr(backend, "_inspect", inspect)

    start = await backend.start_capsule("capsule-1")
    assert start.observed_state == "running"
    assert calls == ["start"]
    assert backend.get_capsule_record("capsule-1").lifecycle_state == "running"

    result = await backend.wait_capsule("capsule-1")
    assert result.worker_result == worker_result
    assert result.process_identity_digest == start.process_identity_digest
    assert calls == ["start", "wait", "cp"]
    assert backend.get_capsule_record("capsule-1").lifecycle_state == "terminal"

    replay = await backend.wait_capsule("capsule-1")
    assert replay.worker_result == worker_result
    assert calls == ["start", "wait", "cp", "cp"]


def test_prepared_container_validation_rejects_extra_env_volume_and_resource_drift(
    tmp_path: Path,
) -> None:
    repository_fd_path = Path("/proc/1/fd/10")
    input_fd_path = Path("/proc/1/fd/11")
    worker_fd_path = Path("/proc/1/fd/12")
    inspect = _inspect(
        repository_fd_path=repository_fd_path,
        input_fd_path=input_fd_path,
        worker_fd_path=worker_fd_path,
        user="501:20",
    )
    arguments = {
        "container_id": "c" * 64,
        "image_reference": "sha256:" + "a" * 64,
        "repository_fd_path": repository_fd_path,
        "input_fd_path": input_fd_path,
        "worker_fd_path": worker_fd_path,
        "user": "501:20",
        "capsule_label": _capsule_label("capsule-1"),
        "max_memory_mib": 512,
        "max_pids": 32,
        "max_output_bytes": 1_048_576,
    }

    _validate_prepared_container(inspect, **arguments)

    for mutate in (
        lambda value: value["Config"]["Env"].append("HTTPS_PROXY=https://host"),
        lambda value: value["Mounts"].append(
            {
                "Type": "volume",
                "Source": "unexpected",
                "Destination": "/unexpected",
                "RW": True,
            }
        ),
        lambda value: value["HostConfig"].__setitem__("Memory", 0),
    ):
        candidate = _inspect(
            repository_fd_path=repository_fd_path,
            input_fd_path=input_fd_path,
            worker_fd_path=worker_fd_path,
            user="501:20",
        )
        mutate(candidate)
        with pytest.raises(SourceIngestBackendError) as error:
            _validate_prepared_container(candidate, **arguments)
        assert error.value.code == "audit_source_ingest_prepare_proof_invalid"


def test_worker_result_archive_requires_exact_private_regular_member() -> None:
    result = SourceIngestWorkerResult(
        outcome=SourceIngestWorkerOutcome.FAILED,
        safe_error_code="audit_fixture_failed",
        request_digest=_digest("request"),
        source_root_identity_digest=_digest("source-root"),
        repository_descriptor_identity_digest=_digest("repository"),
    )
    archive = _result_archive(result)

    assert (
        _read_worker_result_archive(
            archive,
            maximum_bytes=262_144,
            expected_owner_uid=os.geteuid(),
        )
        == result
    )

    for invalid in (
        _result_archive(result, mode=0o644),
        _result_archive(result, owner_uid=os.geteuid() + 1),
        _result_archive(result, name="../result.json"),
    ):
        with pytest.raises(SourceIngestBackendError) as error:
            _read_worker_result_archive(
                invalid,
                maximum_bytes=262_144,
                expected_owner_uid=os.geteuid(),
            )
        assert error.value.code == "audit_source_ingest_result_archive_invalid"


async def test_prepare_failure_after_docker_create_keeps_recoverable_locator(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _backend(tmp_path)
    root = tmp_path / "source-root"
    repository = root / "repository"
    repository.mkdir()
    source = open_authorized_source_repository(repository, allowed_roots=(root,))
    request = _request(source)
    create_arguments: tuple[str, ...] = ()

    async def availability() -> SourceIngestBackendAvailability:
        return SourceIngestBackendAvailability(
            True,
            None,
            component_digest=_digest("component"),
            worker_digest=hashlib.sha256(backend.worker_path.read_bytes()).hexdigest(),
        )

    async def docker(*arguments: str, **_kwargs: object) -> bytes:
        nonlocal create_arguments
        create_arguments = arguments
        return ("c" * 64 + "\n").encode()

    async def inspect(_container_id: str) -> dict[str, object]:
        return {}

    monkeypatch.setattr(backend, "probe_availability", availability)
    monkeypatch.setattr(
        backend,
        "observe_source_mount",
        lambda _source: SourceMountObservation(
            identity_digest=_digest("source-mount"),
            proof_digest=_digest("source-mount-proof"),
            filesystem_type="ext4",
            mount_id=42,
        ),
    )
    monkeypatch.setattr(backend, "_docker", docker)
    monkeypatch.setattr(backend, "_inspect", inspect)
    monkeypatch.setattr(
        "riftx.audit.source_ingest._require_fd_path_identity",
        lambda *_args, **_kwargs: None,
    )

    with pytest.raises(SourceIngestBackendError) as error:
        await backend.prepare(source=source, capsule_id="capsule-1", request=request)

    assert error.value.code == "audit_source_ingest_prepare_proof_invalid"
    assert error.value.container_id == "c" * 64
    assert source.closed is True
    assert "--pull" in create_arguments
    assert create_arguments[create_arguments.index("--pull") + 1] == "never"
    assert create_arguments[create_arguments.index("--name") + 1] == _container_name("capsule-1")
    record = backend.get_capsule_record("capsule-1")
    assert record is not None
    assert record.lifecycle_state == "created"
    assert record.container_id == "c" * 64
