from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts" / "qa" / "audit-snapshot-mount-release-qualification.py"


def _module():
    specification = importlib.util.spec_from_file_location(
        "snapshot_mount_release_qualification",
        SCRIPT,
    )
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    return module


def test_release_qualification_loads_the_exact_image_contract() -> None:
    module = _module()

    lock = module._load_lock()
    dockerfile_digest = module._verify_dockerfile(lock)

    assert lock.tag == "riftx/audit-snapshot-mount:3.12.13-riftx1"
    assert lock.base_index_digest == (
        "d50fb7611f86d04a3b0471b46d7557818d88983fc3136726336b2a4c657aa30b"
    )
    assert lock.supported_platform_manifests == {
        "linux/amd64": "72d3d75f2639ab82b34b29390ad3d6e0827c775befee94edda8e9976818f488d",
        "linux/arm64": "c18c7a910432dde3311fc54d02e5d5220f3ebe26fec43ff15745982863dd7b3b",
    }
    assert len(dockerfile_digest) == hashlib.sha256().digest_size * 2


def test_release_qualification_is_fail_closed_off_real_linux() -> None:
    completed = subprocess.run(
        [sys.executable, str(SCRIPT)],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert completed.returncode == 1
    report = json.loads(completed.stdout)
    assert report["schema_version"] == ("riftx.audit-snapshot-mount-release-qualification/v1")
    assert report["ready"] is False
    assert report["checks"]["linux_host"] is False
    assert report["failure_code"] == "audit_snapshot_mount_release_linux_host_required"
    assert len(report["evidence_digest"]) == hashlib.sha256().digest_size * 2
    assert completed.stderr == ""


def test_locked_image_inspection_rejects_default_environment_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    lock = module._load_lock()
    image = {
        "Id": "sha256:" + "a" * 64,
        "Os": "linux",
        "Architecture": "amd64",
        "Size": 128 * 1024 * 1024,
        "Config": {
            "Cmd": ["-I", "-B", "-c", "import sys; sys.exit(0)"],
            "Entrypoint": ["/usr/bin/python3"],
            "Env": ["UNEXPECTED=value"],
            "Labels": {
                "io.riftx.audit.snapshot-mount.base-index-digest": (
                    "sha256:" + lock.base_index_digest
                ),
                "io.riftx.audit.snapshot-mount.image-schema": lock.image_schema_version,
                "org.opencontainers.image.description": (
                    "Pinned standard-library-only runtime for private Snapshot materialization"
                ),
                "org.opencontainers.image.title": ("RiftX Code Audit Snapshot Mount Runtime"),
                "org.opencontainers.image.version": lock.image_version,
            },
            "User": "65532:65532",
            "WorkingDir": "/",
        },
        "RootFS": {"Type": "layers", "Layers": ["sha256:" + "b" * 64]},
    }
    monkeypatch.setattr(module, "_docker", lambda *args, **kwargs: json.dumps([image]))

    with pytest.raises(
        module._QualificationError,
        match="audit_snapshot_mount_release_image_contract_invalid",
    ):
        module._inspect_image(
            "/usr/bin/docker",
            Path("/tmp/docker-config"),
            lock,
            "linux/amd64",
        )


def test_release_qualification_composes_build_smoke_and_mount_proofs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    image_digest = "a" * 64
    monkeypatch.setattr(module, "_static_failure", lambda: None)
    monkeypatch.setattr(module, "_docker_path", lambda: "/usr/bin/docker")
    monkeypatch.setattr(
        module,
        "_server_platform",
        lambda docker, docker_config, lock: "linux/amd64",
    )
    monkeypatch.setattr(module, "_build_image", lambda *args: None)
    monkeypatch.setattr(
        module,
        "_inspect_image",
        lambda *args: (image_digest, "b" * 64),
    )
    monkeypatch.setattr(module, "_smoke_image", lambda *args: "c" * 64)
    monkeypatch.setattr(
        module,
        "_run_mount_qualification",
        lambda digest: {"ready": True, "image_digest": digest},
    )

    report = module._qualify_release()

    assert report["ready"] is True
    assert all(report["checks"].values())
    assert report["proof"]["image_digest"] == image_digest
    assert report["proof"]["image_config_proof_digest"] == "b" * 64
    assert report["proof"]["image_smoke_proof_digest"] == "c" * 64
    assert report["qualification"] == {"ready": True, "image_digest": image_digest}
    assert len(report["evidence_digest"]) == hashlib.sha256().digest_size * 2
