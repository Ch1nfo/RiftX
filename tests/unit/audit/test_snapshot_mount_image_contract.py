from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
IMAGE_ROOT = ROOT / "packaging" / "audit" / "snapshot_mount"
DOCKERFILE = IMAGE_ROOT / "Dockerfile"
LOCK_FILE = IMAGE_ROOT / "image-lock.json"
_DIGEST = re.compile(r"^[0-9a-f]{64}$")


def _lock() -> dict[str, object]:
    value = json.loads(LOCK_FILE.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def test_snapshot_mount_image_lock_is_strict_and_digest_pinned() -> None:
    lock = _lock()

    assert set(lock) == {
        "base_image",
        "base_index_digest",
        "image_name",
        "image_schema_version",
        "image_version",
        "python_path",
        "python_version",
        "runtime_gid",
        "runtime_uid",
        "schema_version",
        "source_date_epoch",
        "supported_platform_manifests",
    }
    assert lock["schema_version"] == "riftx.audit-snapshot-mount-image-lock/v1"
    assert lock["image_schema_version"] == "riftx.audit-snapshot-mount-image/v1"
    assert lock["image_name"] == "riftx/audit-snapshot-mount"
    assert lock["image_version"] == "3.12.13-riftx1"
    assert lock["python_version"] == "3.12.13"
    assert lock["python_path"] == "/usr/bin/python3"
    assert lock["runtime_uid"] == 65532
    assert lock["runtime_gid"] == 65532
    assert lock["source_date_epoch"] == 1785801600

    base_digest = lock["base_index_digest"]
    assert isinstance(base_digest, str) and _DIGEST.fullmatch(base_digest)
    assert lock["base_image"] == (
        "docker.io/library/python:3.12.13-slim-bookworm@sha256:" + base_digest
    )
    platforms = lock["supported_platform_manifests"]
    assert isinstance(platforms, dict)
    assert set(platforms) == {"linux/amd64", "linux/arm64"}
    assert all(isinstance(value, str) and _DIGEST.fullmatch(value) for value in platforms.values())


def test_snapshot_mount_dockerfile_matches_the_locked_runtime_contract() -> None:
    lock = _lock()
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")

    assert dockerfile.startswith("# syntax=docker/dockerfile:1.7\n")
    assert "ARG SOURCE_DATE_EPOCH=1785801600\n" in dockerfile
    assert f"FROM {lock['base_image']} AS python-runtime\n" in dockerfile
    assert "FROM scratch\n" in dockerfile
    assert "COPY --from=python-runtime / /\n" in dockerfile
    assert "test ! -e /usr/bin/python3" in dockerfile
    assert "ln -s /usr/local/bin/python3 /usr/bin/python3" in dockerfile
    assert "USER 65532:65532\n" in dockerfile
    assert "WORKDIR /\n" in dockerfile
    assert 'ENTRYPOINT ["/usr/bin/python3"]\n' in dockerfile
    assert 'CMD ["-I", "-B", "-c", "import sys; sys.exit(0)"]\n' in dockerfile
    assert "pip-*.dist-info" in dockerfile
    assert "setuptools-*.dist-info" in dockerfile
    assert "wheel-*.dist-info" in dockerfile
    assert "curl " not in dockerfile
    assert "wget " not in dockerfile
    assert "apt-get " not in dockerfile
    assert "ADD " not in dockerfile
    assert "COPY ." not in dockerfile

    digest = hashlib.sha256(dockerfile.encode("utf-8")).hexdigest()
    assert _DIGEST.fullmatch(digest)
