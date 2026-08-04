#!/usr/bin/env python3
"""Build the locked Snapshot mount image and run the real local-Linux gate."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
IMAGE_ROOT = ROOT / "packaging" / "audit" / "snapshot_mount"
DOCKERFILE = IMAGE_ROOT / "Dockerfile"
LOCK_FILE = IMAGE_ROOT / "image-lock.json"
QUALIFICATION_SCRIPT = ROOT / "scripts" / "qa" / "audit-snapshot-mount-qualification.py"
DOCKER_SOCKET = Path("/var/run/docker.sock")
REPORT_SCHEMA = "riftx.audit-snapshot-mount-release-qualification/v1"
REPORT_DIGEST_SCHEMA = "riftx.audit-snapshot-mount-release-qualification-report/v1"
LOCK_SCHEMA = "riftx.audit-snapshot-mount-image-lock/v1"
IMAGE_SCHEMA = "riftx.audit-snapshot-mount-image/v1"
QUALIFICATION_SCHEMA = "riftx.audit-snapshot-mount-real-linux-qualification/v1"
QUALIFICATION_REPORT_DIGEST_SCHEMA = "riftx.audit-snapshot-mount-qualification-report/v1"
QUALIFICATION_SCRIPT_DIGEST = "86e37fc3834b932418d056add4ef4f339085041f3a396d6927094ddc68bcf527"
QUALIFICATION_BACKEND_ID = "private_materialization"
_QUALIFICATION_REPORT_KEYS = frozenset(
    {
        "backend_digest",
        "backend_id",
        "checks",
        "evidence_digest",
        "failure_code",
        "failure_outcome_unknown",
        "generated_at",
        "host",
        "image_digest",
        "node_id",
        "proof",
        "ready",
        "schema_version",
    }
)
_QUALIFICATION_CHECK_KEYS = frozenset(
    {
        "availability",
        "cleanup_confirmed",
        "descriptor_bound_materialization",
        "non_root_kernel_mutation_denial",
        "post_stop_absent",
        "restart_inspection",
        "stop_affirmative",
    }
)
_QUALIFICATION_PROOF_DIGEST_KEYS = frozenset(
    {
        "availability_proof_digest",
        "descriptor_digest",
        "lease_digest",
        "mount_key_digest",
        "mount_proof_digest",
        "pin_digest",
        "plan_digest",
    }
)
_QUALIFICATION_PROOF_KEYS = _QUALIFICATION_PROOF_DIGEST_KEYS | {"file_count", "total_bytes"}
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_SAFE_IMAGE_NAME = re.compile(r"^[a-z0-9]+(?:[._/-][a-z0-9]+)*$")
_SAFE_IMAGE_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_MAX_DOCKER_OUTPUT = 8 * 1024 * 1024
_SMOKE_SCRIPT = r"""
import errno
import importlib.util
import json
import os
import platform
import sys

try:
    descriptor = os.open("/riftx-forbidden", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
except OSError as error:
    if error.errno not in {errno.EACCES, errno.EPERM, errno.EROFS}:
        raise SystemExit(91)
else:
    os.close(descriptor)
    raise SystemExit(92)

payload = {
    "gid": os.getgid(),
    "pip_absent": importlib.util.find_spec("pip") is None,
    "python_executable": sys.executable,
    "python_realpath": os.path.realpath(sys.executable),
    "python_version": platform.python_version(),
    "root_write_denied": True,
    "uid": os.getuid(),
}
sys.stdout.write(json.dumps(payload, sort_keys=True, separators=(",", ":")))
""".strip()


class _QualificationError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class _ImageLock:
    schema_version: str
    image_schema_version: str
    image_name: str
    image_version: str
    base_image: str
    base_index_digest: str
    python_version: str
    python_path: str
    runtime_uid: int
    runtime_gid: int
    source_date_epoch: int
    supported_platform_manifests: dict[str, str]

    @property
    def tag(self) -> str:
        return f"{self.image_name}:{self.image_version}"


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


def _require_digest(value: object, *, label: str) -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise _QualificationError(f"audit_snapshot_mount_image_{label}_invalid")
    return value


def _load_lock(path: Path = LOCK_FILE) -> _ImageLock:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise _QualificationError("audit_snapshot_mount_image_lock_unavailable") from error
    if not isinstance(value, dict) or set(value) != {
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
    }:
        raise _QualificationError("audit_snapshot_mount_image_lock_invalid")
    base_digest = _require_digest(value["base_index_digest"], label="base_digest")
    image_name = value["image_name"]
    image_version = value["image_version"]
    python_version = value["python_version"]
    python_path = value["python_path"]
    runtime_uid = value["runtime_uid"]
    runtime_gid = value["runtime_gid"]
    source_date_epoch = value["source_date_epoch"]
    platforms = value["supported_platform_manifests"]
    if (
        value["schema_version"] != LOCK_SCHEMA
        or value["image_schema_version"] != IMAGE_SCHEMA
        or not isinstance(image_name, str)
        or _SAFE_IMAGE_NAME.fullmatch(image_name) is None
        or not isinstance(image_version, str)
        or _SAFE_IMAGE_VERSION.fullmatch(image_version) is None
        or not isinstance(python_version, str)
        or re.fullmatch(r"3\.12\.[0-9]+", python_version) is None
        or python_path != "/usr/bin/python3"
        or runtime_uid != 65532
        or runtime_gid != 65532
        or not isinstance(source_date_epoch, int)
        or not 0 < source_date_epoch < 2**63
        or value["base_image"]
        != (f"docker.io/library/python:{python_version}-slim-bookworm@sha256:{base_digest}")
        or not isinstance(platforms, dict)
        or set(platforms) != {"linux/amd64", "linux/arm64"}
    ):
        raise _QualificationError("audit_snapshot_mount_image_lock_invalid")
    locked_platforms = {
        key: _require_digest(digest, label="platform_manifest") for key, digest in platforms.items()
    }
    return _ImageLock(
        schema_version=LOCK_SCHEMA,
        image_schema_version=IMAGE_SCHEMA,
        image_name=image_name,
        image_version=image_version,
        base_image=str(value["base_image"]),
        base_index_digest=base_digest,
        python_version=python_version,
        python_path=python_path,
        runtime_uid=runtime_uid,
        runtime_gid=runtime_gid,
        source_date_epoch=source_date_epoch,
        supported_platform_manifests=locked_platforms,
    )


def _verify_dockerfile(lock: _ImageLock) -> str:
    try:
        content = DOCKERFILE.read_bytes()
    except OSError as error:
        raise _QualificationError("audit_snapshot_mount_image_dockerfile_unavailable") from error
    try:
        text = content.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise _QualificationError("audit_snapshot_mount_image_dockerfile_invalid") from error
    required = (
        f"ARG SOURCE_DATE_EPOCH={lock.source_date_epoch}\n",
        f"FROM {lock.base_image} AS python-runtime\n",
        "FROM scratch\n",
        "COPY --from=python-runtime / /\n",
        "test ! -e /usr/bin/python3",
        "ln -s /usr/local/bin/python3 /usr/bin/python3",
        f"USER {lock.runtime_uid}:{lock.runtime_gid}\n",
        "WORKDIR /\n",
        f'ENTRYPOINT ["{lock.python_path}"]\n',
        'CMD ["-I", "-B", "-c", "import sys; sys.exit(0)"]\n',
    )
    if (
        not text.startswith("# syntax=docker/dockerfile:1.7\n")
        or any(fragment not in text for fragment in required)
        or any(fragment in text for fragment in ("apt-get ", "curl ", "wget ", "ADD "))
    ):
        raise _QualificationError("audit_snapshot_mount_image_dockerfile_invalid")
    return hashlib.sha256(content).hexdigest()


def _qualification_script_digest() -> str:
    try:
        content = QUALIFICATION_SCRIPT.read_bytes()
    except OSError as error:
        raise _QualificationError("audit_snapshot_mount_release_gate_unavailable") from error
    digest = hashlib.sha256(content).hexdigest()
    if not hmac.compare_digest(digest, QUALIFICATION_SCRIPT_DIGEST):
        raise _QualificationError("audit_snapshot_mount_release_gate_drifted")
    return digest


def _static_failure() -> str | None:
    if platform.system() != "Linux" or os.name != "posix":
        return "audit_snapshot_mount_release_linux_host_required"
    try:
        socket_value = DOCKER_SOCKET.stat(follow_symlinks=False)
    except OSError:
        return "audit_snapshot_mount_release_local_docker_socket_unavailable"
    if not stat.S_ISSOCK(socket_value.st_mode):
        return "audit_snapshot_mount_release_local_docker_socket_unavailable"
    return None


def _docker_path() -> str:
    value = shutil.which("docker", path="/usr/local/bin:/usr/bin:/bin")
    if value is None or not Path(value).is_absolute():
        raise _QualificationError("audit_snapshot_mount_release_docker_unavailable")
    return value


def _run(
    command: list[str],
    *,
    timeout_seconds: int,
    docker_config: Path,
    cwd: Path | str = "/",
) -> str:
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            env={
                "DOCKER_BUILDKIT": "1",
                "DOCKER_CONFIG": str(docker_config),
                "HOME": str(docker_config),
                "LANG": "C",
                "LC_ALL": "C",
                "PATH": "/usr/local/bin:/usr/bin:/bin",
            },
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise _QualificationError("audit_snapshot_mount_release_command_unavailable") from error
    if len(completed.stdout.encode()) + len(completed.stderr.encode()) > _MAX_DOCKER_OUTPUT:
        raise _QualificationError("audit_snapshot_mount_release_command_output_exceeded")
    if completed.returncode != 0:
        raise _QualificationError("audit_snapshot_mount_release_command_failed")
    return completed.stdout


def _docker(
    docker: str,
    docker_config: Path,
    *arguments: str,
    timeout_seconds: int,
) -> str:
    return _run(
        [docker, "--host", f"unix://{DOCKER_SOCKET}", *arguments],
        timeout_seconds=timeout_seconds,
        docker_config=docker_config,
    )


def _server_platform(docker: str, docker_config: Path, lock: _ImageLock) -> str:
    observed = _docker(
        docker,
        docker_config,
        "version",
        "--format",
        "{{.Server.Os}}/{{.Server.Arch}}",
        timeout_seconds=10,
    ).strip()
    if observed not in lock.supported_platform_manifests:
        raise _QualificationError("audit_snapshot_mount_release_platform_unsupported")
    return observed


def _build_image(docker: str, docker_config: Path, lock: _ImageLock) -> None:
    _docker(docker, docker_config, "pull", lock.base_image, timeout_seconds=300)
    _docker(
        docker,
        docker_config,
        "build",
        "--pull=false",
        "--no-cache",
        "--network=none",
        "--build-arg",
        f"SOURCE_DATE_EPOCH={lock.source_date_epoch}",
        "--tag",
        lock.tag,
        "--file",
        str(DOCKERFILE),
        str(IMAGE_ROOT),
        timeout_seconds=600,
    )


def _inspect_image(
    docker: str,
    docker_config: Path,
    lock: _ImageLock,
    server_platform: str,
) -> tuple[str, str]:
    payload = _docker(
        docker,
        docker_config,
        "image",
        "inspect",
        lock.tag,
        timeout_seconds=20,
    )
    try:
        values = json.loads(payload)
    except json.JSONDecodeError as error:
        raise _QualificationError("audit_snapshot_mount_release_image_inspect_invalid") from error
    if not isinstance(values, list) or len(values) != 1 or not isinstance(values[0], dict):
        raise _QualificationError("audit_snapshot_mount_release_image_inspect_invalid")
    value = values[0]
    config = value.get("Config")
    rootfs = value.get("RootFS")
    image_id = value.get("Id")
    labels = config.get("Labels") if isinstance(config, dict) else None
    expected_labels = {
        "io.riftx.audit.snapshot-mount.base-index-digest": f"sha256:{lock.base_index_digest}",
        "io.riftx.audit.snapshot-mount.image-schema": lock.image_schema_version,
        "org.opencontainers.image.description": (
            "Pinned standard-library-only runtime for private Snapshot materialization"
        ),
        "org.opencontainers.image.title": "RiftX Code Audit Snapshot Mount Runtime",
        "org.opencontainers.image.version": lock.image_version,
    }
    expected_architecture = server_platform.removeprefix("linux/")
    if (
        not isinstance(image_id, str)
        or not image_id.startswith("sha256:")
        or _DIGEST.fullmatch(image_id.removeprefix("sha256:")) is None
        or value.get("Os") != "linux"
        or value.get("Architecture") != expected_architecture
        or not isinstance(config, dict)
        or config.get("Env") not in (None, [])
        or config.get("User") != f"{lock.runtime_uid}:{lock.runtime_gid}"
        or config.get("WorkingDir") != "/"
        or config.get("Entrypoint") != [lock.python_path]
        or config.get("Cmd") != ["-I", "-B", "-c", "import sys; sys.exit(0)"]
        or labels != expected_labels
        or not isinstance(rootfs, dict)
        or rootfs.get("Type") != "layers"
        or not isinstance(rootfs.get("Layers"), list)
        or not rootfs["Layers"]
        or not isinstance(value.get("Size"), int)
        or not 0 < value["Size"] <= 512 * 1024 * 1024
    ):
        raise _QualificationError("audit_snapshot_mount_release_image_contract_invalid")
    proof = _domain_digest(
        "riftx.audit-snapshot-mount-image-config-proof/v1",
        {
            "architecture": value["Architecture"],
            "cmd": config["Cmd"],
            "dockerfile_digest": hashlib.sha256(DOCKERFILE.read_bytes()).hexdigest(),
            "entrypoint": config["Entrypoint"],
            "env": config["Env"],
            "image_id": image_id,
            "labels": labels,
            "os": value["Os"],
            "rootfs_layers": rootfs["Layers"],
            "size": value["Size"],
            "user": config["User"],
            "working_dir": config["WorkingDir"],
        },
    )
    return image_id.removeprefix("sha256:"), proof


def _smoke_image(
    docker: str,
    docker_config: Path,
    lock: _ImageLock,
    image_digest: str,
) -> str:
    payload = _docker(
        docker,
        docker_config,
        "run",
        "--rm",
        "--pull",
        "never",
        "--network",
        "none",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges=true",
        "--user",
        f"{lock.runtime_uid}:{lock.runtime_gid}",
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
        f"sha256:{image_digest}",
        "-I",
        "-B",
        "-c",
        _SMOKE_SCRIPT,
        timeout_seconds=60,
    )
    try:
        value = json.loads(payload)
    except json.JSONDecodeError as error:
        raise _QualificationError("audit_snapshot_mount_release_image_smoke_invalid") from error
    if value != {
        "gid": lock.runtime_gid,
        "pip_absent": True,
        "python_executable": lock.python_path,
        "python_realpath": "/usr/local/bin/python3.12",
        "python_version": lock.python_version,
        "root_write_denied": True,
        "uid": lock.runtime_uid,
    }:
        raise _QualificationError("audit_snapshot_mount_release_image_smoke_invalid")
    return _domain_digest("riftx.audit-snapshot-mount-image-smoke-proof/v1", value)


def _validate_mount_qualification(
    report: object,
    *,
    image_digest: str,
) -> dict[str, Any]:
    if not isinstance(report, dict) or set(report) != _QUALIFICATION_REPORT_KEYS:
        raise _QualificationError("audit_snapshot_mount_release_gate_invalid")
    checks = report.get("checks")
    host = report.get("host")
    proof = report.get("proof")
    evidence_digest = report.get("evidence_digest")
    if (
        report.get("schema_version") != QUALIFICATION_SCHEMA
        or report.get("ready") is not True
        or report.get("image_digest") != image_digest
        or report.get("node_id") != "local"
        or report.get("failure_code") is not None
        or report.get("failure_outcome_unknown") not in (None, False)
        or report.get("backend_id") != QUALIFICATION_BACKEND_ID
        or not isinstance(report.get("backend_digest"), str)
        or _DIGEST.fullmatch(report["backend_digest"]) is None
        or not isinstance(report.get("generated_at"), str)
        or not isinstance(host, dict)
        or set(host) != {"machine", "release", "system"}
        or host.get("system") != "Linux"
        or not all(isinstance(value, str) and value for value in host.values())
        or not isinstance(proof, dict)
        or set(proof) != _QUALIFICATION_PROOF_KEYS
        or any(
            not isinstance(proof.get(key), str) or _DIGEST.fullmatch(proof[key]) is None
            for key in _QUALIFICATION_PROOF_DIGEST_KEYS
        )
        or type(proof.get("file_count")) is not int
        or proof["file_count"] <= 0
        or type(proof.get("total_bytes")) is not int
        or proof["total_bytes"] <= 0
        or not isinstance(checks, dict)
        or set(checks) != _QUALIFICATION_CHECK_KEYS
        or any(value is not True for value in checks.values())
        or not isinstance(evidence_digest, str)
        or _DIGEST.fullmatch(evidence_digest) is None
    ):
        raise _QualificationError("audit_snapshot_mount_release_gate_invalid")
    payload = dict(report)
    payload.pop("evidence_digest")
    expected_digest = _domain_digest(QUALIFICATION_REPORT_DIGEST_SCHEMA, payload)
    if not hmac.compare_digest(evidence_digest, expected_digest):
        raise _QualificationError("audit_snapshot_mount_release_gate_invalid")
    return report


def _run_mount_qualification(
    image_digest: str,
    *,
    qualification_script_digest: str,
) -> dict[str, Any]:
    if not hmac.compare_digest(_qualification_script_digest(), qualification_script_digest):
        raise _QualificationError("audit_snapshot_mount_release_gate_drifted")
    try:
        completed = subprocess.run(
            [
                sys.executable,
                "-I",
                str(QUALIFICATION_SCRIPT),
                "--image-digest",
                image_digest,
            ],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
            timeout=300,
            env={
                "HOME": "/nonexistent",
                "LANG": "C",
                "LC_ALL": "C",
                "PATH": "/usr/local/bin:/usr/bin:/bin",
            },
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise _QualificationError("audit_snapshot_mount_release_gate_unavailable") from error
    if len(completed.stdout.encode()) + len(completed.stderr.encode()) > _MAX_DOCKER_OUTPUT:
        raise _QualificationError("audit_snapshot_mount_release_gate_output_exceeded")
    try:
        report = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise _QualificationError("audit_snapshot_mount_release_gate_invalid") from error
    if completed.returncode != 0 or completed.stderr:
        raise _QualificationError("audit_snapshot_mount_release_gate_failed")
    if not hmac.compare_digest(_qualification_script_digest(), qualification_script_digest):
        raise _QualificationError("audit_snapshot_mount_release_gate_drifted")
    return _validate_mount_qualification(report, image_digest=image_digest)


def _base_report(
    lock: _ImageLock,
    dockerfile_digest: str,
    qualification_script_digest: str,
) -> dict[str, Any]:
    return {
        "schema_version": REPORT_SCHEMA,
        "ready": False,
        "generated_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
        "host": {
            "machine": platform.machine(),
            "release": platform.release(),
            "system": platform.system(),
        },
        "image_lock": {
            "base_image": lock.base_image,
            "base_index_digest": lock.base_index_digest,
            "dockerfile_digest": dockerfile_digest,
            "image_schema_version": lock.image_schema_version,
            "image_tag": lock.tag,
            "python_version": lock.python_version,
            "source_date_epoch": lock.source_date_epoch,
            "supported_platform_manifests": lock.supported_platform_manifests,
        },
        "qualification_gate": {
            "report_schema_version": QUALIFICATION_SCHEMA,
            "script_digest": qualification_script_digest,
        },
        "checks": {
            "linux_host": False,
            "local_linux_docker": False,
            "locked_image_built": False,
            "image_contract": False,
            "non_root_read_only_smoke": False,
            "snapshot_mount_qualification": False,
        },
        "proof": {},
        "qualification": None,
        "failure_code": None,
    }


def _finish_report(report: dict[str, Any]) -> dict[str, Any]:
    payload = dict(report)
    payload.pop("evidence_digest", None)
    report["evidence_digest"] = _domain_digest(REPORT_DIGEST_SCHEMA, payload)
    return report


def _qualify_release() -> dict[str, Any]:
    try:
        lock = _load_lock()
        dockerfile_digest = _verify_dockerfile(lock)
        qualification_script_digest = _qualification_script_digest()
    except _QualificationError as error:
        report = {
            "schema_version": REPORT_SCHEMA,
            "ready": False,
            "generated_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
            "failure_code": error.code,
        }
        return _finish_report(report)
    report = _base_report(lock, dockerfile_digest, qualification_script_digest)
    checks = report["checks"]
    proof = report["proof"]
    assert isinstance(checks, dict) and isinstance(proof, dict)
    static_failure = _static_failure()
    if static_failure is not None:
        report["failure_code"] = static_failure
        return _finish_report(report)
    checks["linux_host"] = True
    try:
        with tempfile.TemporaryDirectory(
            prefix="riftx-snapshot-mount-docker-",
            dir="/tmp",
        ) as docker_config_value:
            docker_config = Path(docker_config_value)
            docker = _docker_path()
            server_platform = _server_platform(docker, docker_config, lock)
            checks["local_linux_docker"] = True
            proof["server_platform"] = server_platform
            proof["platform_manifest_digest"] = lock.supported_platform_manifests[server_platform]
            _build_image(docker, docker_config, lock)
            checks["locked_image_built"] = True
            image_digest, config_proof = _inspect_image(
                docker,
                docker_config,
                lock,
                server_platform,
            )
            checks["image_contract"] = True
            proof["image_digest"] = image_digest
            proof["image_config_proof_digest"] = config_proof
            proof["image_smoke_proof_digest"] = _smoke_image(
                docker,
                docker_config,
                lock,
                image_digest,
            )
            checks["non_root_read_only_smoke"] = True
            qualification = _run_mount_qualification(
                image_digest,
                qualification_script_digest=qualification_script_digest,
            )
            report["qualification"] = qualification
            checks["snapshot_mount_qualification"] = True
    except _QualificationError as error:
        report["failure_code"] = error.code
    report["ready"] = all(checks.values()) and report["failure_code"] is None
    return _finish_report(report)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--evidence",
        type=Path,
        help="Create this new combined JSON evidence file; existing files are never overwritten",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    arguments = parser.parse_args(argv)
    if arguments.evidence is not None and arguments.evidence.exists():
        parser.error("--evidence target already exists")
    report = _qualify_release()
    rendered = json.dumps(report, indent=2, ensure_ascii=False) + "\n"
    if arguments.evidence is not None:
        with arguments.evidence.open("x", encoding="utf-8") as stream:
            stream.write(rendered)
    print(rendered, end="")
    return 0 if report["ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
