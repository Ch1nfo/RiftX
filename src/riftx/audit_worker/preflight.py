"""Standalone, standard-library-only Git metadata worker for SourceIngest.

This file is mounted read-only into the pinned capsule image and executed with
``python3 -I -B``.  It intentionally does not import the RiftX package: the
Runner validates the bounded JSON result against the authoritative domain model.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import selectors
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, cast

REQUEST_SCHEMA = "riftx.audit-source-ingest-worker-request/v1"
RESULT_SCHEMA = "riftx.audit-source-ingest-worker-result/v1"
WORKER_COMPONENT_SCHEMA = "riftx.audit-source-ingest-worker/v1"
REPOSITORY_IDENTITY_SCHEMA = "riftx.audit-git-repository-identity/v1"
CONTENT_IDENTITY_SCHEMA = "riftx.audit-git-content-identity/v1"
GIT_PROOF_SCHEMA = "riftx.audit-safe-git-proof/v1"
SOURCE_MOUNT_IDENTITY_SCHEMA = "riftx.audit-source-mount-identity/v1"
SOURCE_MOUNT_PROOF_SCHEMA = "riftx.audit-source-mount-proof/v1"
SOURCE_ROOT = Path("/source")
MOUNTINFO_PATH = Path("/proc/self/mountinfo")
MAX_INPUT_BYTES = 128 * 1024
MAX_CONFIG_BYTES = 1024 * 1024
MAX_ADMIN_SNAPSHOT_BYTES = 16 * 1024 * 1024
MAX_ADMIN_SNAPSHOT_FILES = 65_536
MAX_OBJECT_STORE_BYTES = 2_147_483_648
MAX_OBJECT_STORE_FILES = 200_000
MAX_MOUNTINFO_BYTES = 4 * 1024 * 1024
MAX_SAFE_CODES = 128

_ALLOWED_LOCAL_FILESYSTEM_TYPES = frozenset(
    {"bcachefs", "btrfs", "ext2", "ext3", "ext4", "f2fs", "tmpfs", "xfs", "zfs"}
)

_DANGEROUS_CONFIG_PREFIXES = (
    "credential.",
    "filter.",
    "http.",
    "https.",
    "include.",
    "includeif.",
    "remote.",
    "submodule.",
    "url.",
)
_DANGEROUS_CONFIG_KEYS = {
    "core.askpass",
    "core.excludesfile",
    "core.fsmonitor",
    "core.hookspath",
    "core.sshcommand",
    "diff.external",
    "interactive.difffilter",
    "protocol.allow",
}
_ALLOWED_SUBCOMMANDS = {
    "ls-files",
    "ls-tree",
    "merge-base",
    "rev-parse",
    "status",
}

_SAFE_BOOLEAN_CONFIG_KEYS = (
    "core.filemode",
    "core.ignorecase",
    "core.precomposeunicode",
    "core.symlinks",
)
_PSEUDO_REF_FILES = (
    "AUTO_MERGE",
    "BISECT_HEAD",
    "CHERRY_PICK_HEAD",
    "FETCH_HEAD",
    "MERGE_HEAD",
    "ORIG_HEAD",
    "REBASE_HEAD",
    "REVERT_HEAD",
)

_LANGUAGE_BY_SUFFIX = {
    ".bash": "shell",
    ".c": "c",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".cs": "csharp",
    ".css": "css",
    ".cxx": "cpp",
    ".go": "go",
    ".h": "c",
    ".hpp": "cpp",
    ".html": "html",
    ".java": "java",
    ".js": "javascript",
    ".json": "json",
    ".jsx": "javascript",
    ".kt": "kotlin",
    ".kts": "kotlin",
    ".md": "markdown",
    ".php": "php",
    ".proto": "protobuf",
    ".py": "python",
    ".rb": "ruby",
    ".rs": "rust",
    ".scala": "scala",
    ".sh": "shell",
    ".sql": "sql",
    ".swift": "swift",
    ".toml": "toml",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".vue": "vue",
    ".xml": "xml",
    ".yaml": "yaml",
    ".yml": "yaml",
}


class SafeWorkerError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class WorkerRejected(SafeWorkerError):
    pass


class WorkerFailed(SafeWorkerError):
    pass


@dataclass(frozen=True, slots=True)
class GitStructure:
    git_dir: Path
    common_dir: Path
    git_dir_relative: str
    common_dir_relative: str
    warnings: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SourceMountEvidence:
    identity_digest: str
    proof_digest: str


@dataclass(frozen=True, slots=True)
class GitSnapshot:
    git_dir: Path
    inherited_descriptors: tuple[int, ...]


@dataclass(slots=True)
class SnapshotBudget:
    files: int = 0
    bytes: int = 0

    def add(self, size: int) -> None:
        self.files += 1
        self.bytes += size
        if self.files > MAX_ADMIN_SNAPSHOT_FILES or self.bytes > MAX_ADMIN_SNAPSHOT_BYTES:
            raise WorkerRejected("audit_git_administrative_limit_exceeded")


@dataclass(slots=True)
class ObjectStoreBudget:
    files: int = 0
    bytes: int = 0

    def add(self, size: int) -> None:
        self.files += 1
        self.bytes += size
        if self.files > MAX_OBJECT_STORE_FILES or self.bytes > MAX_OBJECT_STORE_BYTES:
            raise WorkerRejected("audit_git_object_store_limit_exceeded")


@dataclass(slots=True)
class Inventory:
    identity: hashlib._Hash
    file_count: int = 0
    total_bytes: int = 0
    max_file_bytes: int = 0
    languages: dict[str, list[int]] | None = None
    warnings: set[str] | None = None

    def __post_init__(self) -> None:
        self.languages = defaultdict(lambda: [0, 0])
        self.warnings = set()

    def add(self, *, path: bytes, size: int, content_digest: str, kind: str) -> None:
        path_digest = hashlib.sha256(path).hexdigest()
        _hash_record(
            self.identity,
            {
                "content_digest": content_digest,
                "kind": kind,
                "path_digest": path_digest,
                "size": size,
            },
        )
        self.file_count += 1
        self.total_bytes += size
        self.max_file_bytes = max(self.max_file_bytes, size)
        assert self.languages is not None
        language = _language_for_path(path)
        values = self.languages[language]
        values[0] += 1
        values[1] += size


class SafeGitAdapter:
    def __init__(
        self,
        *,
        timeout_seconds: int,
        maximum_output_bytes: int,
        source_descriptor: int,
    ) -> None:
        executable = shutil.which("git", path="/usr/local/bin:/usr/bin:/bin")
        if executable is None or not os.path.isabs(executable):
            raise WorkerFailed("audit_git_unavailable")
        try:
            executable_stat = os.stat(executable, follow_symlinks=True)
        except OSError as exc:
            raise WorkerFailed("audit_git_unavailable") from exc
        if not stat.S_ISREG(executable_stat.st_mode):
            raise WorkerFailed("audit_git_unavailable")
        self.executable = executable
        self.timeout_seconds = timeout_seconds
        self.maximum_output_bytes = maximum_output_bytes
        self.source_descriptor = source_descriptor
        self._repository_base: tuple[str, ...] | None = None
        self._repository_descriptors: tuple[int, ...] = ()
        self.environment = {
            "GIT_ASKPASS": "/bin/false",
            "GIT_ATTR_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_SYSTEM": "/dev/null",
            "GIT_EXTERNAL_DIFF": "/bin/false",
            "GIT_LFS_SKIP_SMUDGE": "1",
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_NO_LAZY_FETCH": "1",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_PAGER": "cat",
            "GIT_PROTOCOL_FROM_USER": "0",
            "GIT_SSH_COMMAND": "/bin/false",
            "GIT_TERMINAL_PROMPT": "0",
            "HOME": "/nonexistent",
            "LANG": "C",
            "LC_ALL": "C",
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "SSH_ASKPASS": "/bin/false",
        }

    def bind_repository(self, snapshot: GitSnapshot) -> None:
        work_tree = _descriptor_path(self.source_descriptor, fallback=SOURCE_ROOT)
        self._repository_descriptors = tuple(
            sorted({self.source_descriptor, *snapshot.inherited_descriptors})
        )
        self._repository_base = (
            self.executable,
            "--no-pager",
            "--no-optional-locks",
            "-c",
            "advice.detachedHead=false",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "credential.helper=",
            "-c",
            "diff.external=",
            "-c",
            "interactive.diffFilter=",
            "-c",
            "protocol.file.allow=never",
            "-c",
            "protocol.ext.allow=never",
            f"--git-dir={snapshot.git_dir}",
            f"--work-tree={work_tree}",
        )

    def version(self) -> str:
        output = self._run_raw((self.executable, "--version"), maximum_bytes=1024)
        value = output.decode("ascii", errors="strict").strip()
        if not value.startswith("git version ") or len(value) > 128:
            raise WorkerFailed("audit_git_version_invalid")
        return value.removeprefix("git version ")

    def run(self, subcommand: str, *arguments: str) -> bytes:
        if subcommand not in _ALLOWED_SUBCOMMANDS:
            raise WorkerFailed("audit_git_command_not_allowed")
        if any(
            not isinstance(value, str)
            or not value
            or "\x00" in value
            or "\n" in value
            or "\r" in value
            for value in arguments
        ):
            raise WorkerFailed("audit_git_argument_invalid")
        if self._repository_base is None:
            raise WorkerFailed("audit_git_repository_unbound")
        return self._run_raw(
            (*self._repository_base, subcommand, *arguments),
            pass_fds=self._repository_descriptors,
        )

    def read_blob(self, object_id: str, *, expected_size: int) -> bytes:
        if (
            not isinstance(object_id, str)
            or len(object_id) not in {40, 64}
            or any(character not in "0123456789abcdef" for character in object_id)
            or not isinstance(expected_size, int)
            or not 0 <= expected_size <= self.maximum_output_bytes
        ):
            raise WorkerFailed("audit_git_blob_request_invalid")
        if self._repository_base is None:
            raise WorkerFailed("audit_git_repository_unbound")
        output = self._run_raw(
            (*self._repository_base, "cat-file", "blob", object_id),
            maximum_bytes=max(1, expected_size),
            pass_fds=self._repository_descriptors,
        )
        if len(output) != expected_size:
            raise WorkerRejected("audit_git_blob_invalid")
        return output

    def verify_object_integrity(self) -> None:
        if self._repository_base is None:
            raise WorkerFailed("audit_git_repository_unbound")
        try:
            self._run_raw(
                (
                    *self._repository_base,
                    "fsck",
                    "--strict",
                    "--full",
                    "--no-dangling",
                    "--no-reflogs",
                    "--no-progress",
                ),
                pass_fds=self._repository_descriptors,
                reject_success_output_code="audit_git_object_integrity_invalid",
            )
        except WorkerRejected as exc:
            if exc.code == "audit_git_command_rejected":
                raise WorkerRejected("audit_git_object_integrity_invalid") from exc
            raise

    def parse_config_snapshot(self, path: Path) -> bytes:
        return self._run_raw(
            (
                self.executable,
                "--no-pager",
                "config",
                "--no-includes",
                "--null",
                "--file",
                str(path),
                "--list",
            ),
            maximum_bytes=MAX_CONFIG_BYTES,
        )

    def _run_raw(
        self,
        argv: tuple[str, ...],
        *,
        maximum_bytes: int | None = None,
        pass_fds: tuple[int, ...] = (),
        reject_success_output_code: str | None = None,
    ) -> bytes:
        limit = maximum_bytes or self.maximum_output_bytes
        try:
            process = subprocess.Popen(
                argv,
                cwd="/",
                env=self.environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                close_fds=True,
                pass_fds=pass_fds,
                start_new_session=True,
            )
        except OSError as exc:
            raise WorkerFailed("audit_git_spawn_failed") from exc
        stdout, stderr = _communicate_bounded(
            process,
            timeout_seconds=self.timeout_seconds,
            maximum_bytes=limit,
        )
        if process.returncode != 0:
            del stderr
            raise WorkerRejected("audit_git_command_rejected")
        if reject_success_output_code is not None and (stdout or stderr):
            raise WorkerRejected(reject_success_output_code)
        return stdout


def _communicate_bounded(
    process: subprocess.Popen[bytes],
    *,
    timeout_seconds: int,
    maximum_bytes: int,
) -> tuple[bytes, bytes]:
    if process.stdout is None or process.stderr is None:
        process.kill()
        raise WorkerFailed("audit_git_pipe_unavailable")
    selector = selectors.DefaultSelector()
    streams = {process.stdout: bytearray(), process.stderr: bytearray()}
    for stream in streams:
        os.set_blocking(stream.fileno(), False)
        selector.register(stream, selectors.EVENT_READ)
    deadline = time.monotonic() + timeout_seconds
    total = 0
    try:
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _terminate_process(process)
                raise WorkerFailed("audit_git_command_timeout")
            events = selector.select(min(remaining, 0.25))
            if not events and process.poll() is not None:
                events = [(key, selectors.EVENT_READ) for key in selector.get_map().values()]
            for key, _ in events:
                stream = cast(Any, key.fileobj)
                try:
                    chunk = os.read(stream.fileno(), 64 * 1024)
                except BlockingIOError:
                    continue
                if not chunk:
                    selector.unregister(stream)
                    continue
                total += len(chunk)
                if total > maximum_bytes:
                    _terminate_process(process)
                    raise WorkerRejected("audit_git_output_limit_exceeded")
                streams[stream].extend(chunk)
        remaining = max(0.0, deadline - time.monotonic())
        try:
            process.wait(timeout=remaining)
        except subprocess.TimeoutExpired as exc:
            _terminate_process(process)
            raise WorkerFailed("audit_git_command_timeout") from exc
        return bytes(streams[process.stdout]), bytes(streams[process.stderr])
    finally:
        selector.close()
        process.stdout.close()
        process.stderr.close()


def _terminate_process(process: subprocess.Popen[bytes]) -> None:
    try:
        process.kill()
    except OSError:
        pass
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        pass


def _read_request(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise WorkerFailed("audit_preflight_request_unavailable") from exc
    if not raw or len(raw) > MAX_INPUT_BYTES:
        raise WorkerFailed("audit_preflight_request_invalid")

    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate")
            result[key] = value
        return result

    try:
        value = json.loads(raw, object_pairs_hook=reject_duplicates)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise WorkerFailed("audit_preflight_request_invalid") from exc
    if not isinstance(value, dict) or value.get("schema_version") != REQUEST_SCHEMA:
        raise WorkerFailed("audit_preflight_request_invalid")
    _validate_request_shape(value)
    return value


def _validate_request_shape(request: dict[str, Any]) -> None:
    allowed = {
        "base_revision",
        "capsule_id",
        "command_timeout_seconds",
        "exclude_paths",
        "expected_source_mount_identity_digest",
        "include_paths",
        "include_untracked",
        "max_file_bytes",
        "max_files",
        "max_git_output_bytes",
        "max_repository_bytes",
        "mode",
        "repository_descriptor_identity_digest",
        "request_digest",
        "revision",
        "schema_version",
        "source_root_identity_digest",
        "target_kind",
    }
    if set(request) != allowed:
        raise WorkerFailed("audit_preflight_request_invalid")
    for key in (
        "request_digest",
        "source_root_identity_digest",
        "repository_descriptor_identity_digest",
        "expected_source_mount_identity_digest",
    ):
        _require_digest(request.get(key))
    _require_identifier(request.get("capsule_id"), maximum=128)
    revision = request.get("revision")
    base_revision = request.get("base_revision")
    _require_revision(revision)
    if base_revision is not None:
        _require_revision(base_revision)
    if request.get("target_kind") not in {"revision", "working_tree"}:
        raise WorkerFailed("audit_preflight_request_invalid")
    if request.get("mode") not in {"standard", "deep", "diff"}:
        raise WorkerFailed("audit_preflight_request_invalid")
    if request["mode"] == "diff" and base_revision is None:
        raise WorkerFailed("audit_preflight_request_invalid")
    if request["mode"] != "diff" and base_revision is not None:
        raise WorkerFailed("audit_preflight_request_invalid")
    if type(request.get("include_untracked")) is not bool:
        raise WorkerFailed("audit_preflight_request_invalid")
    if request["target_kind"] == "revision" and request["include_untracked"]:
        raise WorkerFailed("audit_preflight_request_invalid")
    includes = _require_paths(request.get("include_paths"))
    excludes = _require_paths(request.get("exclude_paths"))
    if set(includes).intersection(excludes):
        raise WorkerFailed("audit_preflight_request_invalid")
    for key, minimum, maximum in (
        ("max_files", 1, 2**63 - 1),
        ("max_repository_bytes", 1, 2**63 - 1),
        ("max_file_bytes", 1, 2**63 - 1),
        ("max_git_output_bytes", 1024, 16 * 1024 * 1024),
        ("command_timeout_seconds", 1, 300),
    ):
        value = request.get(key)
        if type(value) is not int or not minimum <= value <= maximum:
            raise WorkerFailed("audit_preflight_request_invalid")
    if request["max_file_bytes"] > request["max_repository_bytes"]:
        raise WorkerFailed("audit_preflight_request_invalid")


def _require_paths(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > 512:
        raise WorkerFailed("audit_preflight_request_invalid")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item or len(item.encode("utf-8")) > 4096:
            raise WorkerFailed("audit_preflight_request_invalid")
        candidate = PurePosixPath(item)
        if (
            candidate.is_absolute()
            or candidate.as_posix() != item
            or item.endswith("/")
            or "//" in item
            or "\\" in item
            or "\x00" in item
            or any(part in {"", ".", ".."} for part in item.split("/"))
        ):
            raise WorkerFailed("audit_preflight_request_invalid")
        result.append(item)
    if result != sorted(set(result)):
        raise WorkerFailed("audit_preflight_request_invalid")
    return tuple(result)


def _require_digest(value: object) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise WorkerFailed("audit_preflight_request_invalid")
    return value


def _require_identifier(value: object, *, maximum: int) -> str:
    allowed = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._:@+~-")
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= maximum
        or value[0] not in allowed
        or any(character not in allowed for character in value)
    ):
        raise WorkerFailed("audit_preflight_request_invalid")
    return value


def _require_revision(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value.startswith("-")
        or len(value.encode("utf-8")) > 1024
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
    ):
        raise WorkerFailed("audit_preflight_request_invalid")
    return value


def _open_source_root() -> int:
    try:
        descriptor = os.open(
            SOURCE_ROOT,
            os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW,
        )
        value = os.fstat(descriptor)
    except OSError as exc:
        raise WorkerFailed("audit_source_mount_unavailable") from exc
    if not stat.S_ISDIR(value.st_mode):
        os.close(descriptor)
        raise WorkerFailed("audit_source_mount_unavailable")
    return descriptor


def _descriptor_path(descriptor: int, *, fallback: Path | None = None) -> str:
    expected = os.fstat(descriptor)
    for directory in (Path("/proc/self/fd"), Path("/dev/fd")):
        candidate = directory / str(descriptor)
        try:
            observed = candidate.stat()
        except OSError:
            continue
        if observed.st_dev == expected.st_dev and observed.st_ino == expected.st_ino:
            return str(candidate)
    if sys.platform != "linux" and fallback is not None:
        try:
            observed = fallback.stat(follow_symlinks=False)
        except OSError as exc:
            raise WorkerFailed("audit_git_descriptor_view_unavailable") from exc
        if observed.st_dev == expected.st_dev and observed.st_ino == expected.st_ino:
            return str(fallback)
    raise WorkerFailed("audit_git_descriptor_view_unavailable")


def _source_mount_evidence(source_descriptor: int) -> SourceMountEvidence:
    source_stat = os.fstat(source_descriptor)
    if source_stat.st_dev < 0 or source_stat.st_ino <= 0:
        raise WorkerFailed("audit_source_mount_identity_invalid")
    try:
        raw = _read_bounded_special_file(MOUNTINFO_PATH, maximum=MAX_MOUNTINFO_BYTES)
    except WorkerRejected as exc:
        raise WorkerFailed("audit_source_mount_identity_unavailable") from exc
    source_path = os.path.abspath(os.fspath(SOURCE_ROOT))
    device = f"{os.major(source_stat.st_dev)}:{os.minor(source_stat.st_dev)}"
    selected: tuple[int, str, str] | None = None
    for raw_line in raw.splitlines():
        left, separator, right = raw_line.partition(b" - ")
        if not separator:
            raise WorkerFailed("audit_source_mount_identity_invalid")
        left_fields = left.split()
        right_fields = right.split()
        if len(left_fields) < 6 or len(right_fields) < 3:
            raise WorkerFailed("audit_source_mount_identity_invalid")
        try:
            mount_id = int(left_fields[0])
            mount_device = left_fields[2].decode("ascii", errors="strict")
            mount_point = os.fsdecode(_decode_mountinfo_field(left_fields[4]))
            filesystem_type = right_fields[0].decode("ascii", errors="strict")
        except (UnicodeDecodeError, ValueError) as exc:
            raise WorkerFailed("audit_source_mount_identity_invalid") from exc
        if mount_id <= 0 or not mount_point.startswith("/"):
            raise WorkerFailed("audit_source_mount_identity_invalid")
        if (
            not filesystem_type
            or len(filesystem_type) > 128
            or any(
                character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
                for character in filesystem_type
            )
        ):
            raise WorkerFailed("audit_source_mount_identity_invalid")
        normalized_mount_point = mount_point.rstrip("/") or "/"
        path_matches = source_path == normalized_mount_point or (
            normalized_mount_point != "/" and source_path.startswith(normalized_mount_point + "/")
        )
        if normalized_mount_point == "/":
            path_matches = source_path.startswith("/")
        if mount_device != device or not path_matches:
            continue
        candidate = (mount_id, normalized_mount_point, filesystem_type)
        if selected is None or len(candidate[1]) > len(selected[1]):
            selected = candidate
    if selected is None:
        raise WorkerFailed("audit_source_mount_identity_unavailable")
    mount_id, _, filesystem_type = selected
    if filesystem_type not in _ALLOWED_LOCAL_FILESYSTEM_TYPES:
        raise WorkerFailed("audit_source_ingest_filesystem_unsupported")
    identity_payload = {
        "filesystem_type": filesystem_type,
        "schema_version": SOURCE_MOUNT_IDENTITY_SCHEMA,
        "st_dev": int(source_stat.st_dev),
        "st_ino": int(source_stat.st_ino),
    }
    identity_digest = _domain_digest(SOURCE_MOUNT_IDENTITY_SCHEMA, identity_payload)
    proof_digest = _domain_digest(
        SOURCE_MOUNT_PROOF_SCHEMA,
        {
            **identity_payload,
            "mount_id": mount_id,
            "schema_version": SOURCE_MOUNT_PROOF_SCHEMA,
        },
    )
    return SourceMountEvidence(
        identity_digest=identity_digest,
        proof_digest=proof_digest,
    )


def _decode_mountinfo_field(value: bytes) -> bytes:
    decoded = bytearray()
    index = 0
    escapes = {b"040": b" ", b"011": b"\t", b"012": b"\n", b"134": b"\\"}
    while index < len(value):
        if value[index : index + 1] == b"\\":
            escape = value[index + 1 : index + 4]
            replacement = escapes.get(escape)
            if replacement is None:
                raise WorkerFailed("audit_source_mount_identity_invalid")
            decoded.extend(replacement)
            index += 4
            continue
        decoded.append(value[index])
        index += 1
    return bytes(decoded)


def _read_bounded_special_file(path: Path, *, maximum: int) -> bytes:
    descriptor = -1
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        raw = bytearray()
        while True:
            chunk = os.read(descriptor, min(64 * 1024, maximum + 1 - len(raw)))
            if not chunk:
                break
            raw.extend(chunk)
            if len(raw) > maximum:
                raise WorkerRejected("audit_git_structure_limit_exceeded")
        return bytes(raw)
    except WorkerRejected:
        raise
    except OSError as exc:
        raise WorkerRejected("audit_git_structure_unreadable") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _validate_structure() -> GitStructure:
    try:
        source_stat = SOURCE_ROOT.lstat()
    except OSError as exc:
        raise WorkerRejected("audit_repository_unavailable") from exc
    if not stat.S_ISDIR(source_stat.st_mode) or stat.S_ISLNK(source_stat.st_mode):
        raise WorkerRejected("audit_repository_not_directory")
    dot_git = SOURCE_ROOT / ".git"
    try:
        dot_git_stat = dot_git.lstat()
    except OSError as exc:
        raise WorkerRejected("audit_git_directory_missing") from exc
    if stat.S_ISDIR(dot_git_stat.st_mode):
        git_dir = _resolve_inside_source(dot_git)
    elif stat.S_ISREG(dot_git_stat.st_mode):
        raw = _read_small_file(dot_git, maximum=4096)
        if raw.count(b"\n") > 1 or not raw.startswith(b"gitdir: "):
            raise WorkerRejected("audit_git_file_invalid")
        value = raw.removeprefix(b"gitdir: ").strip()
        try:
            decoded = value.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise WorkerRejected("audit_git_file_invalid") from exc
        candidate = Path(decoded)
        git_dir = _resolve_inside_source(
            candidate if candidate.is_absolute() else dot_git.parent / candidate
        )
    else:
        raise WorkerRejected("audit_git_file_invalid")
    _require_directory_chain_without_symlinks(git_dir)

    common_dir = git_dir
    commondir_file = git_dir / "commondir"
    if commondir_file.exists():
        raw = _read_small_file(commondir_file, maximum=4096).strip()
        try:
            decoded = raw.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise WorkerRejected("audit_git_commondir_invalid") from exc
        candidate = Path(decoded)
        common_dir = _resolve_inside_source(
            candidate if candidate.is_absolute() else git_dir / candidate
        )
        _require_directory_chain_without_symlinks(common_dir)

    warnings: set[str] = set()
    common_info = common_dir / "info"
    if common_info.exists():
        _require_directory_chain_without_symlinks(common_info)
    grafts = common_info / "grafts"
    if (
        grafts.exists()
        and _read_small_file(
            grafts,
            maximum=MAX_ADMIN_SNAPSHOT_BYTES,
        ).strip()
    ):
        raise WorkerRejected("audit_git_grafts_rejected")
    shallow = common_dir / "shallow"
    if shallow.exists():
        raw_shallow = _read_small_file(
            shallow,
            maximum=MAX_ADMIN_SNAPSHOT_BYTES,
        )
        if raw_shallow.strip():
            for object_id in raw_shallow.splitlines():
                if len(object_id) not in {40, 64} or any(
                    character not in b"0123456789abcdef" for character in object_id
                ):
                    raise WorkerRejected("audit_git_shallow_invalid")
            warnings.add("audit_git_shallow_repository")

    objects = common_dir / "objects"
    _require_directory_chain_without_symlinks(objects)
    objects_info = objects / "info"
    if objects_info.exists():
        _require_directory_chain_without_symlinks(objects_info)
    for filename in ("alternates", "http-alternates"):
        alternate_file = objects_info / filename
        if (
            alternate_file.exists()
            and _read_small_file(
                alternate_file,
                maximum=MAX_CONFIG_BYTES,
            ).strip()
        ):
            raise WorkerRejected("audit_git_object_alternate_rejected")

    refs_dir = common_dir / "refs"
    if refs_dir.exists():
        _require_directory_chain_without_symlinks(refs_dir)
        replace_dir = refs_dir / "replace"
        if replace_dir.exists():
            _require_directory_chain_without_symlinks(replace_dir)
            warnings.add("audit_git_replace_refs_disabled")
    hooks_dir = common_dir / "hooks"
    if hooks_dir.exists():
        _require_directory_chain_without_symlinks(hooks_dir)
        try:
            if any(hooks_dir.iterdir()):
                warnings.add("audit_git_hooks_disabled")
        except OSError as exc:
            raise WorkerRejected("audit_git_structure_unreadable") from exc
    return GitStructure(
        git_dir=git_dir,
        common_dir=common_dir,
        git_dir_relative=git_dir.relative_to(SOURCE_ROOT).as_posix(),
        common_dir_relative=common_dir.relative_to(SOURCE_ROOT).as_posix(),
        warnings=tuple(sorted(warnings)),
    )


def _validate_local_config(
    git: SafeGitAdapter,
    structure: GitStructure,
    scratch: Path,
) -> tuple[dict[str, str], str]:
    common_raw, common_identity = _read_optional_file_snapshot(
        structure.common_dir / "config",
        maximum=MAX_CONFIG_BYTES,
    )
    config = _parse_config_snapshot(git, common_raw, scratch / "config.common.raw")
    worktree_raw, worktree_identity = _read_optional_file_snapshot(
        structure.git_dir / "config.worktree",
        maximum=MAX_CONFIG_BYTES,
    )
    if len(common_raw) + len(worktree_raw) > MAX_CONFIG_BYTES:
        raise WorkerRejected("audit_git_config_limit_exceeded")
    worktree_config_enabled = _parse_git_boolean(config.get("extensions.worktreeconfig", "false"))
    if worktree_config_enabled:
        config.update(_parse_config_snapshot(git, worktree_raw, scratch / "config.worktree.raw"))
    _validate_repository_format(config)
    return config, _domain_digest(
        "riftx.audit-git-config-snapshot/v1",
        {"common": common_identity, "worktree": worktree_identity},
    )


def _config_snapshot_identity_digest(structure: GitStructure) -> str:
    _, common_identity = _read_optional_file_snapshot(
        structure.common_dir / "config",
        maximum=MAX_CONFIG_BYTES,
    )
    _, worktree_identity = _read_optional_file_snapshot(
        structure.git_dir / "config.worktree",
        maximum=MAX_CONFIG_BYTES,
    )
    return _domain_digest(
        "riftx.audit-git-config-snapshot/v1",
        {"common": common_identity, "worktree": worktree_identity},
    )


def _parse_config_snapshot(
    git: SafeGitAdapter,
    raw: bytes,
    path: Path,
) -> dict[str, str]:
    _write_private_file(path, raw)
    parsed = git.parse_config_snapshot(path)
    if len(parsed) > MAX_CONFIG_BYTES:
        raise WorkerRejected("audit_git_config_limit_exceeded")
    entries = parsed.split(b"\x00")
    config: dict[str, str] = {}
    for entry in entries:
        if not entry:
            continue
        key, separator, value = entry.partition(b"\n")
        if not separator:
            key, separator, value = entry.partition(b"=")
        try:
            normalized_key = key.decode("utf-8", errors="strict").lower()
            decoded_value = value.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise WorkerRejected("audit_git_config_invalid") from exc
        if (
            normalized_key in _DANGEROUS_CONFIG_KEYS
            or normalized_key in {"include.path", "includeif.path"}
            or normalized_key.startswith(_DANGEROUS_CONFIG_PREFIXES)
            or normalized_key.startswith("diff.")
            and normalized_key.endswith((".command", ".textconv"))
        ):
            raise WorkerRejected("audit_git_config_unsafe")
        if len(normalized_key) > 512 or len(decoded_value.encode("utf-8")) > 16 * 1024:
            raise WorkerRejected("audit_git_config_limit_exceeded")
        config[normalized_key] = decoded_value
    return config


def _validate_repository_format(config: dict[str, str]) -> None:
    try:
        repository_format = int(config.get("core.repositoryformatversion", "0"), 10)
    except ValueError as exc:
        raise WorkerRejected("audit_git_config_invalid") from exc
    if repository_format not in {0, 1}:
        raise WorkerRejected("audit_git_repository_format_unsupported")
    object_format = config.get("extensions.objectformat", "sha1").lower()
    if object_format not in {"sha1", "sha256"}:
        raise WorkerRejected("audit_git_object_format_unsupported")
    if object_format == "sha256" and repository_format != 1:
        raise WorkerRejected("audit_git_repository_format_unsupported")
    allowed_extensions = {"extensions.objectformat", "extensions.worktreeconfig"}
    if any(key.startswith("extensions.") and key not in allowed_extensions for key in config):
        raise WorkerRejected("audit_git_repository_extension_unsupported")
    for key in _SAFE_BOOLEAN_CONFIG_KEYS:
        if key in config:
            _parse_git_boolean(config[key])


def _parse_git_boolean(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"", "1", "on", "true", "yes"}:
        return True
    if normalized in {"0", "off", "false", "no"}:
        return False
    raise WorkerRejected("audit_git_config_invalid")


def _safe_repository_config(config: dict[str, str]) -> bytes:
    repository_format = int(config.get("core.repositoryformatversion", "0"), 10)
    object_format = config.get("extensions.objectformat", "sha1").lower()
    lines = [
        "[core]",
        f"\trepositoryFormatVersion = {repository_format}",
        "\tbare = false",
        "\tfsmonitor = false",
        "\thooksPath = /dev/null",
    ]
    for key in _SAFE_BOOLEAN_CONFIG_KEYS:
        if key not in config:
            continue
        name = key.removeprefix("core.")
        value = "true" if _parse_git_boolean(config[key]) else "false"
        lines.append(f"\t{name} = {value}")
    if object_format != "sha1":
        lines.extend(("[extensions]", f"\tobjectFormat = {object_format}"))
    return ("\n".join(lines) + "\n").encode("ascii")


def _prepare_git_snapshot(
    structure: GitStructure,
    config: dict[str, str],
    scratch: Path,
) -> GitSnapshot:
    shadow = scratch / "git"
    try:
        shadow.mkdir(mode=0o700)
    except OSError as exc:
        raise WorkerFailed("audit_git_snapshot_unavailable") from exc
    budget = SnapshotBudget()
    descriptors: list[int] = []
    try:
        _write_private_file(shadow / "config", _safe_repository_config(config))
        _copy_snapshot_file(
            structure.git_dir / "HEAD",
            shadow / "HEAD",
            budget=budget,
            required=True,
            maximum=64 * 1024,
        )
        _copy_snapshot_file(
            structure.git_dir / "index",
            shadow / "index",
            budget=budget,
            required=False,
            maximum=MAX_ADMIN_SNAPSHOT_BYTES,
        )
        for filename in _PSEUDO_REF_FILES:
            _copy_snapshot_file(
                structure.git_dir / filename,
                shadow / filename,
                budget=budget,
                required=False,
                maximum=MAX_ADMIN_SNAPSHOT_BYTES,
            )
        _copy_snapshot_file(
            structure.common_dir / "packed-refs",
            shadow / "packed-refs",
            budget=budget,
            required=False,
            maximum=MAX_ADMIN_SNAPSHOT_BYTES,
        )
        _copy_snapshot_file(
            structure.common_dir / "shallow",
            shadow / "shallow",
            budget=budget,
            required=False,
            maximum=MAX_ADMIN_SNAPSHOT_BYTES,
        )
        for filename in ("exclude", "sparse-checkout"):
            _copy_snapshot_file(
                structure.common_dir / "info" / filename,
                shadow / "info" / filename,
                budget=budget,
                required=False,
                maximum=MAX_ADMIN_SNAPSHOT_BYTES,
            )
        _copy_snapshot_tree(
            structure.common_dir / "refs",
            shadow / "refs",
            budget=budget,
        )
        if structure.git_dir != structure.common_dir:
            _copy_snapshot_tree(
                structure.git_dir / "refs",
                shadow / "refs",
                budget=budget,
                replace=True,
            )
        _copy_shared_indexes(structure.git_dir, shadow, budget=budget)
        descriptors.extend(_prepare_object_view(structure.common_dir / "objects", shadow))
        return GitSnapshot(
            git_dir=shadow,
            inherited_descriptors=tuple(descriptors),
        )
    except Exception:
        for descriptor in descriptors:
            os.close(descriptor)
        raise


def _copy_snapshot_file(
    source: Path,
    destination: Path,
    *,
    budget: SnapshotBudget,
    required: bool,
    maximum: int,
    replace: bool = False,
) -> None:
    try:
        source.lstat()
    except FileNotFoundError:
        if required:
            raise WorkerRejected("audit_git_structure_unreadable") from None
        return
    except OSError as exc:
        raise WorkerRejected("audit_git_structure_unreadable") from exc
    raw = _read_small_file(source, maximum=maximum)
    budget.add(len(raw))
    _write_private_file(destination, raw, replace=replace)


def _bounded_scandir(path: Path) -> list[os.DirEntry[str]]:
    entries: list[os.DirEntry[str]] = []
    try:
        with os.scandir(path) as iterator:
            for entry in iterator:
                entries.append(entry)
                if len(entries) > MAX_ADMIN_SNAPSHOT_FILES:
                    raise WorkerRejected("audit_git_administrative_limit_exceeded")
    except WorkerRejected:
        raise
    except OSError as exc:
        raise WorkerRejected("audit_git_structure_unreadable") from exc
    return sorted(entries, key=lambda entry: os.fsencode(entry.name))


def _copy_snapshot_tree(
    source: Path,
    destination: Path,
    *,
    budget: SnapshotBudget,
    replace: bool = False,
) -> None:
    try:
        source_stat = source.lstat()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise WorkerRejected("audit_git_structure_unreadable") from exc
    if stat.S_ISLNK(source_stat.st_mode) or not stat.S_ISDIR(source_stat.st_mode):
        raise WorkerRejected("audit_git_administrative_symlink_rejected")
    budget.add(0)
    try:
        destination.mkdir(mode=0o700, parents=True, exist_ok=True)
        entries = _bounded_scandir(source)
    except OSError as exc:
        raise WorkerRejected("audit_git_structure_unreadable") from exc
    for entry in entries:
        path = Path(entry.path)
        target = destination / entry.name
        try:
            value = entry.stat(follow_symlinks=False)
        except OSError as exc:
            raise WorkerRejected("audit_git_structure_unreadable") from exc
        if stat.S_ISLNK(value.st_mode):
            raise WorkerRejected("audit_git_administrative_symlink_rejected")
        if stat.S_ISDIR(value.st_mode):
            _copy_snapshot_tree(path, target, budget=budget, replace=replace)
            continue
        if not stat.S_ISREG(value.st_mode):
            raise WorkerRejected("audit_git_structure_invalid")
        _copy_snapshot_file(
            path,
            target,
            budget=budget,
            required=True,
            maximum=MAX_ADMIN_SNAPSHOT_BYTES,
            replace=replace,
        )


def _copy_shared_indexes(
    git_dir: Path,
    shadow: Path,
    *,
    budget: SnapshotBudget,
) -> None:
    try:
        entries = _bounded_scandir(git_dir)
    except OSError as exc:
        raise WorkerRejected("audit_git_structure_unreadable") from exc
    for entry in entries:
        if not _is_shared_index_name(entry.name):
            continue
        _copy_snapshot_file(
            Path(entry.path),
            shadow / entry.name,
            budget=budget,
            required=True,
            maximum=MAX_ADMIN_SNAPSHOT_BYTES,
        )


def _is_shared_index_name(name: str) -> bool:
    prefix = "sharedindex."
    if not name.startswith(prefix):
        return False
    suffix = name.removeprefix(prefix)
    return len(suffix) in {40, 64} and all(character in "0123456789abcdef" for character in suffix)


def _prepare_object_view(objects: Path, shadow: Path) -> tuple[int, ...]:
    destination = shadow / "objects"
    try:
        destination.mkdir(mode=0o700)
        entries = _bounded_scandir(objects)
    except OSError as exc:
        raise WorkerRejected("audit_git_structure_unreadable") from exc
    descriptors: list[int] = []
    try:
        for entry in entries:
            if entry.name != "pack" and not _is_loose_object_directory(entry.name):
                continue
            try:
                value = entry.stat(follow_symlinks=False)
            except OSError as exc:
                raise WorkerRejected("audit_git_structure_unreadable") from exc
            if stat.S_ISLNK(value.st_mode) or not stat.S_ISDIR(value.st_mode):
                raise WorkerRejected("audit_git_administrative_symlink_rejected")
            descriptor = os.open(
                entry.path,
                os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW,
            )
            opened = os.fstat(descriptor)
            if _stat_fingerprint(opened) != _stat_fingerprint(value):
                os.close(descriptor)
                raise WorkerRejected("audit_git_structure_changed")
            try:
                os.symlink(
                    _descriptor_path(descriptor, fallback=Path(entry.path)),
                    destination / entry.name,
                    target_is_directory=True,
                )
            except OSError:
                os.close(descriptor)
                raise
            descriptors.append(descriptor)
        return tuple(descriptors)
    except WorkerRejected:
        for descriptor in descriptors:
            os.close(descriptor)
        raise
    except OSError as exc:
        for descriptor in descriptors:
            os.close(descriptor)
        raise WorkerFailed("audit_git_snapshot_unavailable") from exc


def _is_loose_object_directory(name: str) -> bool:
    return len(name) == 2 and all(character in "0123456789abcdef" for character in name)


def _repository_guard_digest(
    structure: GitStructure,
    *,
    object_id_length: int,
) -> str:
    budget = SnapshotBudget()
    object_budget = ObjectStoreBudget()
    payload = {
        "common_config": _guard_optional_file(
            structure.common_dir / "config",
            budget=budget,
            maximum=MAX_CONFIG_BYTES,
        ),
        "common_dir": _stat_fingerprint(structure.common_dir.lstat()),
        "common_dir_relative": structure.common_dir_relative,
        "common_refs": _guard_tree(structure.common_dir / "refs", budget=budget),
        "commondir": _guard_optional_file(
            structure.git_dir / "commondir",
            budget=budget,
            maximum=64 * 1024,
        ),
        "dot_git": _guard_path(SOURCE_ROOT / ".git", budget=budget),
        "git_dir": _stat_fingerprint(structure.git_dir.lstat()),
        "git_dir_relative": structure.git_dir_relative,
        "git_refs": (
            _guard_tree(structure.git_dir / "refs", budget=budget)
            if structure.git_dir != structure.common_dir
            else None
        ),
        "head": _guard_optional_file(
            structure.git_dir / "HEAD",
            budget=budget,
            maximum=64 * 1024,
        ),
        "index": _guard_optional_file(
            structure.git_dir / "index",
            budget=budget,
            maximum=MAX_ADMIN_SNAPSHOT_BYTES,
        ),
        "info_exclude": _guard_optional_file(
            structure.common_dir / "info" / "exclude",
            budget=budget,
            maximum=MAX_ADMIN_SNAPSHOT_BYTES,
        ),
        "info_grafts": _guard_optional_file(
            structure.common_dir / "info" / "grafts",
            budget=budget,
            maximum=MAX_ADMIN_SNAPSHOT_BYTES,
        ),
        "info_sparse_checkout": _guard_optional_file(
            structure.common_dir / "info" / "sparse-checkout",
            budget=budget,
            maximum=MAX_ADMIN_SNAPSHOT_BYTES,
        ),
        "objects": _object_store_guard(
            structure.common_dir / "objects",
            budget=budget,
            object_budget=object_budget,
            object_id_length=object_id_length,
        ),
        "packed_refs": _guard_optional_file(
            structure.common_dir / "packed-refs",
            budget=budget,
            maximum=MAX_ADMIN_SNAPSHOT_BYTES,
        ),
        "pseudo_refs": {
            filename: _guard_optional_file(
                structure.git_dir / filename,
                budget=budget,
                maximum=MAX_ADMIN_SNAPSHOT_BYTES,
            )
            for filename in _PSEUDO_REF_FILES
        },
        "shallow": _guard_optional_file(
            structure.common_dir / "shallow",
            budget=budget,
            maximum=MAX_ADMIN_SNAPSHOT_BYTES,
        ),
        "shared_indexes": _guard_shared_indexes(structure.git_dir, budget=budget),
        "source_root": _stat_fingerprint(SOURCE_ROOT.lstat()),
        "worktree_config": _guard_optional_file(
            structure.git_dir / "config.worktree",
            budget=budget,
            maximum=MAX_CONFIG_BYTES,
        ),
    }
    return _domain_digest("riftx.audit-git-repository-guard/v1", payload)


def _guard_path(path: Path, *, budget: SnapshotBudget) -> object:
    try:
        value = path.lstat()
    except OSError as exc:
        raise WorkerRejected("audit_git_structure_unreadable") from exc
    if stat.S_ISLNK(value.st_mode):
        raise WorkerRejected("audit_git_administrative_symlink_rejected")
    if stat.S_ISDIR(value.st_mode):
        return {"kind": "directory", "stat": _stat_fingerprint(value)}
    if stat.S_ISREG(value.st_mode):
        return _guard_file(path, budget=budget, maximum=64 * 1024)
    raise WorkerRejected("audit_git_structure_invalid")


def _guard_optional_file(
    path: Path,
    *,
    budget: SnapshotBudget,
    maximum: int,
) -> object | None:
    try:
        path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise WorkerRejected("audit_git_structure_unreadable") from exc
    return _guard_file(path, budget=budget, maximum=maximum)


def _guard_file(
    path: Path,
    *,
    budget: SnapshotBudget,
    maximum: int,
) -> dict[str, object]:
    raw = _read_small_file(path, maximum=maximum)
    budget.add(len(raw))
    try:
        value = path.lstat()
    except OSError as exc:
        raise WorkerRejected("audit_git_structure_unreadable") from exc
    return {
        "content_digest": hashlib.sha256(raw).hexdigest(),
        "kind": "file",
        "stat": _stat_fingerprint(value),
    }


def _guard_tree(path: Path, *, budget: SnapshotBudget) -> object | None:
    try:
        root_stat = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise WorkerRejected("audit_git_structure_unreadable") from exc
    if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
        raise WorkerRejected("audit_git_administrative_symlink_rejected")
    budget.add(0)
    entries: list[dict[str, object]] = []

    def visit(directory: Path, prefix: str) -> None:
        try:
            children = _bounded_scandir(directory)
        except OSError as exc:
            raise WorkerRejected("audit_git_structure_unreadable") from exc
        for child in children:
            relative = f"{prefix}/{child.name}" if prefix else child.name
            try:
                value = child.stat(follow_symlinks=False)
            except OSError as exc:
                raise WorkerRejected("audit_git_structure_unreadable") from exc
            if stat.S_ISLNK(value.st_mode):
                raise WorkerRejected("audit_git_administrative_symlink_rejected")
            if stat.S_ISDIR(value.st_mode):
                budget.add(0)
                entries.append(
                    {"kind": "directory", "path": relative, "stat": _stat_fingerprint(value)}
                )
                visit(Path(child.path), relative)
                continue
            if not stat.S_ISREG(value.st_mode):
                raise WorkerRejected("audit_git_structure_invalid")
            entries.append(
                {
                    **_guard_file(
                        Path(child.path),
                        budget=budget,
                        maximum=MAX_ADMIN_SNAPSHOT_BYTES,
                    ),
                    "path": relative,
                }
            )

    visit(path, "")
    return {"entries": entries, "stat": _stat_fingerprint(root_stat)}


def _guard_shared_indexes(git_dir: Path, *, budget: SnapshotBudget) -> object:
    try:
        entries = _bounded_scandir(git_dir)
    except OSError as exc:
        raise WorkerRejected("audit_git_structure_unreadable") from exc
    return {
        entry.name: _guard_file(
            Path(entry.path),
            budget=budget,
            maximum=MAX_ADMIN_SNAPSHOT_BYTES,
        )
        for entry in entries
        if _is_shared_index_name(entry.name)
    }


def _object_store_guard(
    objects: Path,
    *,
    budget: SnapshotBudget,
    object_budget: ObjectStoreBudget,
    object_id_length: int,
) -> object:
    try:
        root_stat = objects.lstat()
        entries = _bounded_scandir(objects)
    except OSError as exc:
        raise WorkerRejected("audit_git_structure_unreadable") from exc
    if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
        raise WorkerRejected("audit_git_administrative_symlink_rejected")
    children: dict[str, object] = {}
    for entry in entries:
        budget.add(0)
        try:
            value = entry.stat(follow_symlinks=False)
        except OSError as exc:
            raise WorkerRejected("audit_git_structure_unreadable") from exc
        if stat.S_ISLNK(value.st_mode):
            raise WorkerRejected("audit_git_administrative_symlink_rejected")
        if entry.name == "info":
            if not stat.S_ISDIR(value.st_mode):
                raise WorkerRejected("audit_git_structure_invalid")
            children[entry.name] = {
                "alternates": _guard_optional_file(
                    Path(entry.path) / "alternates",
                    budget=budget,
                    maximum=MAX_CONFIG_BYTES,
                ),
                "http_alternates": _guard_optional_file(
                    Path(entry.path) / "http-alternates",
                    budget=budget,
                    maximum=MAX_CONFIG_BYTES,
                ),
                "stat": _stat_fingerprint(value),
            }
            continue
        if entry.name == "pack":
            children[entry.name] = _guard_object_directory_entries(
                Path(entry.path),
                object_budget=object_budget,
                object_id_length=object_id_length,
                loose=False,
            )
            continue
        if not _is_loose_object_directory(entry.name) or not stat.S_ISDIR(value.st_mode):
            raise WorkerRejected("audit_git_structure_invalid")
        children[entry.name] = _guard_object_directory_entries(
            Path(entry.path),
            object_budget=object_budget,
            object_id_length=object_id_length,
            loose=True,
        )
    return {"children": children, "stat": _stat_fingerprint(root_stat)}


def _guard_object_directory_entries(
    path: Path,
    *,
    object_budget: ObjectStoreBudget,
    object_id_length: int,
    loose: bool,
) -> object:
    try:
        root_stat = path.lstat()
        entries = _bounded_scandir(path)
    except OSError as exc:
        raise WorkerRejected("audit_git_structure_unreadable") from exc
    if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
        raise WorkerRejected("audit_git_administrative_symlink_rejected")
    values: dict[str, object] = {}
    for entry in entries:
        if loose:
            if not _is_loose_object_filename(
                entry.name,
                object_id_length=object_id_length,
            ):
                raise WorkerRejected("audit_git_structure_invalid")
        elif not _is_pack_object_filename(
            entry.name,
            object_id_length=object_id_length,
        ):
            raise WorkerRejected("audit_git_structure_invalid")
        try:
            value = entry.stat(follow_symlinks=False)
        except OSError as exc:
            raise WorkerRejected("audit_git_structure_unreadable") from exc
        if stat.S_ISLNK(value.st_mode):
            raise WorkerRejected("audit_git_administrative_symlink_rejected")
        if not stat.S_ISREG(value.st_mode):
            raise WorkerRejected("audit_git_structure_invalid")
        if value.st_nlink != 1:
            raise WorkerRejected("audit_git_object_hardlink_rejected")
        object_budget.add(int(value.st_size))
        values[entry.name] = _stat_fingerprint(value)
    if not loose:
        _validate_pack_object_entry_set(
            set(values),
            object_id_length=object_id_length,
        )
    return {"entries": values, "stat": _stat_fingerprint(root_stat)}


def _is_loose_object_filename(name: str, *, object_id_length: int) -> bool:
    return len(name) == object_id_length - 2 and all(
        character in "0123456789abcdef" for character in name
    )


def _is_pack_object_filename(name: str, *, object_id_length: int) -> bool:
    if name == "multi-pack-index":
        return True
    pack_extensions = {
        ".bitmap",
        ".idx",
        ".keep",
        ".mtimes",
        ".pack",
        ".promisor",
        ".rev",
    }
    if name.startswith("pack-"):
        object_id, extension = os.path.splitext(name.removeprefix("pack-"))
        return (
            len(object_id) == object_id_length
            and all(character in "0123456789abcdef" for character in object_id)
            and extension in pack_extensions
        )
    if name.startswith("multi-pack-index-"):
        object_id, extension = os.path.splitext(name.removeprefix("multi-pack-index-"))
        return (
            len(object_id) == object_id_length
            and all(character in "0123456789abcdef" for character in object_id)
            and extension in {".bitmap", ".rev"}
        )
    return False


def _validate_pack_object_entry_set(
    names: set[str],
    *,
    object_id_length: int,
) -> None:
    pack_groups: dict[str, set[str]] = defaultdict(set)
    has_multi_pack_index = "multi-pack-index" in names
    for name in names:
        if name == "multi-pack-index":
            continue
        if name.startswith("pack-"):
            object_id, extension = os.path.splitext(name.removeprefix("pack-"))
            pack_groups[object_id].add(extension)
            continue
        object_id, extension = os.path.splitext(name.removeprefix("multi-pack-index-"))
        if (
            not has_multi_pack_index
            or len(object_id) != object_id_length
            or extension not in {".bitmap", ".rev"}
        ):
            raise WorkerRejected("audit_git_object_pack_set_invalid")
    if any(not {".pack", ".idx"} <= extensions for extensions in pack_groups.values()):
        raise WorkerRejected("audit_git_object_pack_set_invalid")


def _assert_repository_unchanged(
    structure: GitStructure,
    expected_digest: str,
    *,
    object_id_length: int,
) -> None:
    observed_structure = _validate_structure()
    if (
        observed_structure.git_dir_relative != structure.git_dir_relative
        or observed_structure.common_dir_relative != structure.common_dir_relative
        or _repository_guard_digest(
            observed_structure,
            object_id_length=object_id_length,
        )
        != expected_digest
    ):
        raise WorkerRejected("audit_repository_changed_during_preflight")


def _repository_identity(
    request: dict[str, Any],
    structure: GitStructure,
    config: dict[str, str],
) -> str:
    payload = {
        "common_dir": structure.common_dir_relative,
        "config_digest": _domain_digest("riftx.audit-git-config/v1", config),
        "descriptor_identity_digest": request["repository_descriptor_identity_digest"],
        "git_dir": structure.git_dir_relative,
        "head_digest": _optional_file_digest(structure.git_dir / "HEAD"),
        "index_identity": _optional_stat_identity(structure.git_dir / "index"),
        "object_format": config.get("extensions.objectformat", "sha1").lower(),
        "objects_identity": _stat_identity(structure.common_dir / "objects"),
        "schema_version": REPOSITORY_IDENTITY_SCHEMA,
        "source_root_identity_digest": request["source_root_identity_digest"],
    }
    if payload["object_format"] not in {"sha1", "sha256"}:
        raise WorkerRejected("audit_git_object_format_unsupported")
    return _domain_digest(REPOSITORY_IDENTITY_SCHEMA, payload)


def _resolve_revision(git: SafeGitAdapter, revision: str) -> str:
    output = git.run(
        "rev-parse",
        "--verify",
        "--end-of-options",
        f"{revision}^{{commit}}",
    ).strip()
    try:
        value = output.decode("ascii", errors="strict")
    except UnicodeDecodeError as exc:
        raise WorkerRejected("audit_git_revision_unresolved") from exc
    if len(value) not in {40, 64} or any(
        character not in "0123456789abcdef" for character in value
    ):
        raise WorkerRejected("audit_git_revision_unresolved")
    return value


def _status(git: SafeGitAdapter, *, include_untracked: bool) -> tuple[bool, bool, bool, set[bytes]]:
    raw = git.run(
        "status",
        "--porcelain=v2",
        "-z",
        "--ignore-submodules=all",
        "--no-renames",
        "--untracked-files=all" if include_untracked else "--untracked-files=no",
    )
    staged = False
    unstaged = False
    untracked_paths: set[bytes] = set()
    records = raw.split(b"\x00")
    index = 0
    while index < len(records):
        record = records[index]
        index += 1
        if not record:
            continue
        marker = record[:1]
        if marker in {b"1", b"2", b"u"}:
            if len(record) < 4 or record[1:2] != b" ":
                raise WorkerRejected("audit_git_status_invalid")
            x, y = record[2:3], record[3:4]
            staged = staged or x not in {b".", b" "}
            unstaged = unstaged or y not in {b".", b" "}
            if marker == b"2":
                if index >= len(records):
                    raise WorkerRejected("audit_git_status_invalid")
                index += 1
        elif marker == b"?":
            if not record.startswith(b"? "):
                raise WorkerRejected("audit_git_status_invalid")
            untracked_paths.add(_validate_git_path(record[2:]))
        elif marker == b"!":
            continue
        else:
            raise WorkerRejected("audit_git_status_invalid")
    return staged, unstaged, bool(untracked_paths), untracked_paths


def _revision_inventory(
    git: SafeGitAdapter,
    revision: str,
    request: dict[str, Any],
) -> Inventory:
    identity = hashlib.sha256()
    identity.update(CONTENT_IDENTITY_SCHEMA.encode("ascii") + b"\0")
    inventory = Inventory(identity=identity)
    raw = git.run("ls-tree", "-r", "-z", "-l", "--full-tree", revision)
    for record in raw.split(b"\x00"):
        if not record:
            continue
        metadata, separator, path = record.partition(b"\t")
        fields = metadata.split()
        if not separator or len(fields) != 4:
            raise WorkerRejected("audit_git_tree_invalid")
        mode, object_type, object_id, raw_size = fields
        path = _validate_git_path(path)
        if not _path_selected(path, request):
            continue
        if object_type == b"commit" or mode == b"160000":
            assert inventory.warnings is not None
            inventory.warnings.add("audit_git_submodule_not_materialized")
            continue
        if object_type != b"blob":
            continue
        try:
            size = int(raw_size)
        except ValueError as exc:
            raise WorkerRejected("audit_git_tree_invalid") from exc
        if size < 0:
            raise WorkerRejected("audit_git_tree_invalid")
        _enforce_inventory_limits(inventory, size=size, request=request)
        try:
            object_digest = object_id.decode("ascii", errors="strict")
        except UnicodeDecodeError as exc:
            raise WorkerRejected("audit_git_tree_invalid") from exc
        inventory.add(
            path=path,
            size=size,
            content_digest=object_digest,
            kind="symlink" if mode == b"120000" else "blob",
        )
        if path == b".gitmodules":
            assert inventory.warnings is not None
            inventory.warnings.add("audit_git_submodule_metadata_present")
    return inventory


def _working_tree_inventory(
    git: SafeGitAdapter,
    request: dict[str, Any],
    *,
    source_descriptor: int,
    untracked_paths: set[bytes],
) -> Inventory:
    identity = hashlib.sha256()
    identity.update(CONTENT_IDENTITY_SCHEMA.encode("ascii") + b"\0")
    inventory = Inventory(identity=identity)
    tracked = {
        _validate_git_path(path)
        for path in git.run("ls-files", "-z", "--cached").split(b"\x00")
        if path
    }
    candidates = tracked | (untracked_paths if request["include_untracked"] else set())
    root_fd = -1
    try:
        root_fd = os.dup(source_descriptor)
        for path in sorted(candidates):
            if not _path_selected(path, request):
                continue
            try:
                kind, size, content_digest = _hash_worktree_entry(root_fd, path, request)
            except FileNotFoundError:
                _hash_record(
                    inventory.identity,
                    {"kind": "missing", "path_digest": hashlib.sha256(path).hexdigest()},
                )
                continue
            _enforce_inventory_limits(inventory, size=size, request=request)
            inventory.add(
                path=path,
                size=size,
                content_digest=content_digest,
                kind=kind,
            )
            if path == b".gitmodules":
                assert inventory.warnings is not None
                inventory.warnings.add("audit_git_submodule_metadata_present")
    finally:
        if root_fd >= 0:
            os.close(root_fd)
    return inventory


def _hash_worktree_entry(
    root_fd: int,
    path: bytes,
    request: dict[str, Any],
) -> tuple[str, int, str]:
    components = path.split(b"/")
    parent_fd = os.dup(root_fd)
    final_fd = -1
    try:
        for component in components[:-1]:
            next_fd = os.open(
                component,
                os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=parent_fd,
            )
            os.close(parent_fd)
            parent_fd = next_fd
        final = components[-1]
        value = os.stat(final, dir_fd=parent_fd, follow_symlinks=False)
        if stat.S_ISLNK(value.st_mode):
            target = os.readlink(final, dir_fd=parent_fd)
            observed = os.stat(final, dir_fd=parent_fd, follow_symlinks=False)
            if _stat_fingerprint(observed) != _stat_fingerprint(value):
                raise WorkerRejected("audit_repository_changed_during_preflight")
            encoded = os.fsencode(target)
            if len(encoded) > request["max_file_bytes"]:
                raise WorkerRejected("audit_repository_file_limit_exceeded")
            return "symlink", len(encoded), hashlib.sha256(encoded).hexdigest()
        if not stat.S_ISREG(value.st_mode):
            raise WorkerRejected("audit_repository_special_file_rejected")
        if value.st_nlink != 1:
            raise WorkerRejected("audit_repository_hardlink_rejected")
        if value.st_size > request["max_file_bytes"]:
            raise WorkerRejected("audit_repository_file_limit_exceeded")
        final_fd = os.open(
            final,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
            dir_fd=parent_fd,
        )
        opened = os.fstat(final_fd)
        if (
            opened.st_dev != value.st_dev
            or opened.st_ino != value.st_ino
            or opened.st_mode != value.st_mode
            or opened.st_size != value.st_size
        ):
            raise WorkerRejected("audit_repository_changed_during_preflight")
        digest = hashlib.sha256()
        total = 0
        while True:
            chunk = os.read(final_fd, 64 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > request["max_file_bytes"]:
                raise WorkerRejected("audit_repository_file_limit_exceeded")
            digest.update(chunk)
        completed = os.fstat(final_fd)
        if total != value.st_size or _stat_fingerprint(completed) != _stat_fingerprint(value):
            raise WorkerRejected("audit_repository_changed_during_preflight")
        return "regular", total, digest.hexdigest()
    except OSError as exc:
        if exc.errno == 2:
            raise FileNotFoundError(path) from exc
        if exc.errno in {20, 40}:
            raise WorkerRejected("audit_repository_symlink_rejected") from exc
        raise WorkerRejected("audit_repository_read_failed") from exc
    finally:
        if final_fd >= 0:
            os.close(final_fd)
        os.close(parent_fd)


def _stat_fingerprint(value: os.stat_result) -> tuple[int, ...]:
    return (
        int(value.st_dev),
        int(value.st_ino),
        int(value.st_mode),
        int(value.st_nlink),
        int(value.st_uid),
        int(value.st_gid),
        int(value.st_size),
        int(value.st_mtime_ns),
        int(value.st_ctime_ns),
    )


def _enforce_inventory_limits(
    inventory: Inventory,
    *,
    size: int,
    request: dict[str, Any],
) -> None:
    if inventory.file_count + 1 > request["max_files"]:
        raise WorkerRejected("audit_repository_file_count_exceeded")
    if size > request["max_file_bytes"]:
        raise WorkerRejected("audit_repository_file_limit_exceeded")
    if inventory.total_bytes + size > request["max_repository_bytes"]:
        raise WorkerRejected("audit_repository_size_exceeded")


def _path_selected(path: bytes, request: dict[str, Any]) -> bool:
    includes = tuple(item.encode("utf-8") for item in request["include_paths"])
    excludes = tuple(item.encode("utf-8") for item in request["exclude_paths"])

    def matches(prefix: bytes) -> bool:
        return path == prefix or path.startswith(prefix + b"/")

    return (not includes or any(matches(prefix) for prefix in includes)) and not any(
        matches(prefix) for prefix in excludes
    )


def _validate_git_path(path: bytes) -> bytes:
    if (
        not path
        or path.startswith(b"/")
        or b"\x00" in path
        or b"\\" in path
        or any(component in {b"", b".", b".."} for component in path.split(b"/"))
    ):
        raise WorkerRejected("audit_git_path_invalid")
    return path


def _language_for_path(path: bytes) -> str:
    name = path.rsplit(b"/", 1)[-1]
    try:
        decoded = name.decode("utf-8", errors="strict").lower()
    except UnicodeDecodeError:
        return "unknown"
    suffix = Path(decoded).suffix
    if decoded in {"dockerfile", "containerfile"}:
        return "dockerfile"
    if decoded in {"makefile", "gnumakefile"}:
        return "make"
    return _LANGUAGE_BY_SUFFIX.get(suffix, "unknown")


def _build_completed_result(
    *,
    request: dict[str, Any],
    outcome: str,
    safe_error_code: str | None,
    repository_identity_digest: str,
    content_identity_digest: str,
    git_version: str,
    worker_digest: str,
    source_mount_identity_digest: str,
    source_mount_proof_digest: str,
    head_revision: str | None,
    resolved_revision: str | None,
    resolved_base_revision: str | None,
    merge_base_revision: str | None,
    staged: bool,
    unstaged: bool,
    untracked: bool,
    inventory: Inventory,
    warnings: set[str],
) -> dict[str, Any]:
    git_component_digest = _domain_digest(
        "riftx.audit-safe-git-component/v1",
        {"git_version": git_version, "worker_digest": worker_digest},
    )
    git_proof_digest = _domain_digest(
        GIT_PROOF_SCHEMA,
        {
            "content_identity_digest": content_identity_digest,
            "git_component_digest": git_component_digest,
            "repository_identity_digest": repository_identity_digest,
            "request_digest": request["request_digest"],
            "schema_version": GIT_PROOF_SCHEMA,
        },
    )
    assert inventory.languages is not None
    return {
        "blocking_errors": [safe_error_code] if outcome == "rejected" else [],
        "capability_warnings": sorted(warnings)[:MAX_SAFE_CODES],
        "content_identity_digest": content_identity_digest,
        "dirty": staged or unstaged or untracked,
        "file_count": inventory.file_count,
        "git_component_digest": git_component_digest,
        "git_proof_digest": git_proof_digest,
        "git_version": git_version,
        "head_revision": head_revision,
        "language_estimates": [
            {
                "file_count": values[0],
                "language_id": language,
                "total_bytes": values[1],
            }
            for language, values in sorted(inventory.languages.items())
        ],
        "max_file_bytes": inventory.max_file_bytes,
        "merge_base_revision": merge_base_revision,
        "outcome": outcome,
        "repository_descriptor_identity_digest": request["repository_descriptor_identity_digest"],
        "repository_identity_digest": repository_identity_digest,
        "request_digest": request["request_digest"],
        "resolved_base_revision": resolved_base_revision,
        "resolved_revision": resolved_revision,
        "safe_error_code": safe_error_code,
        "schema_version": RESULT_SCHEMA,
        "source_root_identity_digest": request["source_root_identity_digest"],
        "source_mount_identity_digest": source_mount_identity_digest,
        "source_mount_proof_digest": source_mount_proof_digest,
        "staged": staged,
        "total_bytes": inventory.total_bytes,
        "unstaged": unstaged,
        "untracked": untracked,
    }


def _build_rejected_result(
    *,
    request: dict[str, Any],
    code: str,
    git_version: str,
    worker_digest: str,
    repository_identity_digest: str | None,
    source_mount_identity_digest: str,
    source_mount_proof_digest: str,
    warnings: set[str],
) -> dict[str, Any]:
    repository_digest = repository_identity_digest or _domain_digest(
        REPOSITORY_IDENTITY_SCHEMA,
        {
            "descriptor_identity_digest": request["repository_descriptor_identity_digest"],
            "rejection_code": code,
            "schema_version": REPOSITORY_IDENTITY_SCHEMA,
            "source_root_identity_digest": request["source_root_identity_digest"],
        },
    )
    content_digest = _domain_digest(
        CONTENT_IDENTITY_SCHEMA,
        {
            "rejection_code": code,
            "repository_identity_digest": repository_digest,
            "request_digest": request["request_digest"],
            "schema_version": CONTENT_IDENTITY_SCHEMA,
        },
    )
    inventory = Inventory(identity=hashlib.sha256())
    return _build_completed_result(
        request=request,
        outcome="rejected",
        safe_error_code=code,
        repository_identity_digest=repository_digest,
        content_identity_digest=content_digest,
        git_version=git_version,
        worker_digest=worker_digest,
        source_mount_identity_digest=source_mount_identity_digest,
        source_mount_proof_digest=source_mount_proof_digest,
        head_revision=None,
        resolved_revision=None,
        resolved_base_revision=None,
        merge_base_revision=None,
        staged=False,
        unstaged=False,
        untracked=False,
        inventory=inventory,
        warnings=warnings,
    )


def _failed_result(request: dict[str, Any] | None, code: str) -> dict[str, Any]:
    fallback = "0" * 64
    return {
        "blocking_errors": [],
        "capability_warnings": [],
        "content_identity_digest": None,
        "dirty": False,
        "file_count": 0,
        "git_component_digest": None,
        "git_proof_digest": None,
        "git_version": None,
        "head_revision": None,
        "language_estimates": [],
        "max_file_bytes": 0,
        "merge_base_revision": None,
        "outcome": "failed",
        "repository_descriptor_identity_digest": (
            request.get("repository_descriptor_identity_digest", fallback)
            if request is not None
            else fallback
        ),
        "repository_identity_digest": None,
        "request_digest": request.get("request_digest", fallback) if request else fallback,
        "resolved_base_revision": None,
        "resolved_revision": None,
        "safe_error_code": code,
        "schema_version": RESULT_SCHEMA,
        "source_root_identity_digest": (
            request.get("source_root_identity_digest", fallback) if request else fallback
        ),
        "source_mount_identity_digest": None,
        "source_mount_proof_digest": None,
        "staged": False,
        "total_bytes": 0,
        "unstaged": False,
        "untracked": False,
    }


def _execute(request: dict[str, Any]) -> dict[str, Any]:
    worker_digest = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    source_descriptor = _open_source_root()
    snapshot: GitSnapshot | None = None
    try:
        mount_evidence = _source_mount_evidence(source_descriptor)
        if not secrets.compare_digest(
            mount_evidence.identity_digest,
            request["expected_source_mount_identity_digest"],
        ):
            raise WorkerFailed("audit_source_ingest_mount_identity_changed")
        git = SafeGitAdapter(
            timeout_seconds=request["command_timeout_seconds"],
            maximum_output_bytes=request["max_git_output_bytes"],
            source_descriptor=source_descriptor,
        )
        git_version = git.version()
        warnings: set[str] = set()
        repository_identity_digest: str | None = None
        with tempfile.TemporaryDirectory(prefix="riftx-audit-", dir="/tmp") as temporary:
            scratch = Path(temporary)
            try:
                structure = _validate_structure()
                warnings.update(structure.warnings)
                if (
                    request["mode"] == "diff"
                    and "audit_git_shallow_repository" in structure.warnings
                ):
                    raise WorkerRejected("audit_git_shallow_diff_unsupported")
                config, config_snapshot_digest = _validate_local_config(git, structure, scratch)
                object_id_length = (
                    64 if config.get("extensions.objectformat", "sha1").lower() == "sha256" else 40
                )
                if _config_snapshot_identity_digest(structure) != config_snapshot_digest:
                    raise WorkerRejected("audit_repository_changed_during_preflight")
                repository_guard_digest = _repository_guard_digest(
                    structure,
                    object_id_length=object_id_length,
                )
                if _config_snapshot_identity_digest(structure) != config_snapshot_digest:
                    raise WorkerRejected("audit_repository_changed_during_preflight")
                snapshot = _prepare_git_snapshot(structure, config, scratch)
                _assert_repository_unchanged(
                    structure,
                    repository_guard_digest,
                    object_id_length=object_id_length,
                )
                git.bind_repository(snapshot)
                git.verify_object_integrity()
                repository_identity_digest = _repository_identity(request, structure, config)
                head_revision = _resolve_revision(git, "HEAD")
                resolved_revision = _resolve_revision(git, request["revision"])
                resolved_base_revision = None
                merge_base_revision = None
                if request["mode"] == "diff":
                    resolved_base_revision = _resolve_revision(git, request["base_revision"])
                    merge_base_revision = (
                        git.run(
                            "merge-base",
                            resolved_base_revision,
                            resolved_revision,
                        )
                        .decode("ascii", errors="strict")
                        .strip()
                    )
                    if len(merge_base_revision) not in {40, 64} or any(
                        character not in "0123456789abcdef" for character in merge_base_revision
                    ):
                        raise WorkerRejected("audit_git_merge_base_unresolved")
                staged, unstaged, untracked, untracked_paths = _status(
                    git,
                    include_untracked=request["include_untracked"],
                )
                if request["target_kind"] == "revision":
                    inventory = _revision_inventory(git, resolved_revision, request)
                else:
                    inventory = _working_tree_inventory(
                        git,
                        request,
                        source_descriptor=source_descriptor,
                        untracked_paths=untracked_paths,
                    )
                assert inventory.warnings is not None
                warnings.update(inventory.warnings)
                _assert_repository_unchanged(
                    structure,
                    repository_guard_digest,
                    object_id_length=object_id_length,
                )
                git.verify_object_integrity()
                _assert_repository_unchanged(
                    structure,
                    repository_guard_digest,
                    object_id_length=object_id_length,
                )
                _hash_record(
                    inventory.identity,
                    {
                        "head_revision": head_revision,
                        "merge_base_revision": merge_base_revision,
                        "request_digest": request["request_digest"],
                        "resolved_base_revision": resolved_base_revision,
                        "resolved_revision": resolved_revision,
                        "staged": staged,
                        "unstaged": unstaged,
                        "untracked": untracked,
                    },
                )
                content_identity_digest = inventory.identity.hexdigest()
                return _build_completed_result(
                    request=request,
                    outcome="succeeded",
                    safe_error_code=None,
                    repository_identity_digest=repository_identity_digest,
                    content_identity_digest=content_identity_digest,
                    git_version=git_version,
                    worker_digest=worker_digest,
                    source_mount_identity_digest=mount_evidence.identity_digest,
                    source_mount_proof_digest=mount_evidence.proof_digest,
                    head_revision=head_revision,
                    resolved_revision=resolved_revision,
                    resolved_base_revision=resolved_base_revision,
                    merge_base_revision=merge_base_revision,
                    staged=staged,
                    unstaged=unstaged,
                    untracked=untracked,
                    inventory=inventory,
                    warnings=warnings,
                )
            except WorkerRejected as exc:
                return _build_rejected_result(
                    request=request,
                    code=exc.code,
                    git_version=git_version,
                    worker_digest=worker_digest,
                    repository_identity_digest=repository_identity_digest,
                    source_mount_identity_digest=mount_evidence.identity_digest,
                    source_mount_proof_digest=mount_evidence.proof_digest,
                    warnings=warnings,
                )
            finally:
                if snapshot is not None:
                    for descriptor in snapshot.inherited_descriptors:
                        os.close(descriptor)
    finally:
        os.close(source_descriptor)


def _resolve_inside_source(path: Path) -> Path:
    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(SOURCE_ROOT)
    except (OSError, ValueError) as exc:
        raise WorkerRejected("audit_git_administrative_path_escape") from exc
    return resolved


def _require_directory_chain_without_symlinks(path: Path) -> None:
    try:
        relative = path.relative_to(SOURCE_ROOT)
    except ValueError as exc:
        raise WorkerRejected("audit_git_administrative_path_escape") from exc
    current = SOURCE_ROOT
    for component in relative.parts:
        current = current / component
        try:
            value = current.lstat()
        except OSError as exc:
            raise WorkerRejected("audit_git_structure_unreadable") from exc
        if stat.S_ISLNK(value.st_mode) or not stat.S_ISDIR(value.st_mode):
            raise WorkerRejected("audit_git_administrative_symlink_rejected")


def _read_small_file(path: Path, *, maximum: int) -> bytes:
    try:
        value = path.lstat()
        if not stat.S_ISREG(value.st_mode) or stat.S_ISLNK(value.st_mode):
            raise WorkerRejected("audit_git_structure_invalid")
        if value.st_nlink != 1:
            raise WorkerRejected("audit_git_administrative_hardlink_rejected")
        if value.st_size > maximum:
            raise WorkerRejected("audit_git_structure_limit_exceeded")
        flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
        descriptor = os.open(path, flags)
        try:
            opened = os.fstat(descriptor)
            if opened.st_dev != value.st_dev or opened.st_ino != value.st_ino:
                raise WorkerRejected("audit_git_structure_changed")
            raw = bytearray()
            while True:
                chunk = os.read(descriptor, min(64 * 1024, maximum + 1 - len(raw)))
                if not chunk:
                    break
                raw.extend(chunk)
                if len(raw) > maximum:
                    raise WorkerRejected("audit_git_structure_limit_exceeded")
            completed = os.fstat(descriptor)
            try:
                observed = path.lstat()
            except OSError as exc:
                raise WorkerRejected("audit_git_structure_changed") from exc
            if (
                len(raw) != opened.st_size
                or _stat_fingerprint(completed) != _stat_fingerprint(opened)
                or _stat_fingerprint(observed) != _stat_fingerprint(opened)
            ):
                raise WorkerRejected("audit_git_structure_changed")
            return bytes(raw)
        finally:
            os.close(descriptor)
    except WorkerRejected:
        raise
    except OSError as exc:
        raise WorkerRejected("audit_git_structure_unreadable") from exc


def _read_optional_file_snapshot(
    path: Path,
    *,
    maximum: int,
) -> tuple[bytes, dict[str, object] | None]:
    try:
        path.lstat()
    except FileNotFoundError:
        return b"", None
    except OSError as exc:
        raise WorkerRejected("audit_git_structure_unreadable") from exc
    raw = _read_small_file(path, maximum=maximum)
    try:
        value = path.lstat()
    except OSError as exc:
        raise WorkerRejected("audit_git_structure_changed") from exc
    return raw, {
        "content_digest": hashlib.sha256(raw).hexdigest(),
        "stat": _stat_fingerprint(value),
    }


def _write_private_file(path: Path, raw: bytes, *, replace: bool = False) -> None:
    descriptor = -1
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        flags = os.O_WRONLY | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW
        flags |= os.O_TRUNC if replace else os.O_EXCL
        descriptor = os.open(path, flags, 0o600)
        offset = 0
        while offset < len(raw):
            written = os.write(descriptor, raw[offset:])
            if written <= 0:
                raise OSError("short write")
            offset += written
        os.fsync(descriptor)
    except OSError as exc:
        raise WorkerFailed("audit_git_snapshot_unavailable") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _stat_identity(path: Path) -> dict[str, int]:
    try:
        value = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise WorkerRejected("audit_git_structure_unreadable") from exc
    return {
        "device": int(value.st_dev),
        "inode": int(value.st_ino),
        "mode": int(value.st_mode),
        "size": int(value.st_size),
    }


def _optional_stat_identity(path: Path) -> dict[str, int] | None:
    try:
        return _stat_identity(path)
    except WorkerRejected:
        if not path.exists():
            return None
        raise


def _optional_file_digest(path: Path) -> str | None:
    if not path.exists():
        return None
    return hashlib.sha256(_read_small_file(path, maximum=64 * 1024)).hexdigest()


def _domain_digest(domain: str, value: object) -> str:
    canonical = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")
    return hashlib.sha256(domain.encode("ascii") + b"\0" + canonical).hexdigest()


def _hash_record(digest: hashlib._Hash, value: object) -> None:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")
    digest.update(len(encoded).to_bytes(8, "big"))
    digest.update(encoded)


def _write_result(path: Path, result: dict[str, Any]) -> None:
    encoded = json.dumps(
        result,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")
    if len(encoded) > 256 * 1024:
        raise WorkerFailed("audit_preflight_result_limit_exceeded")
    descriptor = -1
    try:
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
        )
        offset = 0
        while offset < len(encoded):
            offset += os.write(descriptor, encoded[offset:])
        os.fsync(descriptor)
    except OSError as exc:
        raise WorkerFailed("audit_preflight_result_write_failed") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        return 64
    input_path = Path(argv[1])
    output_path = Path(argv[2])
    request: dict[str, Any] | None = None
    try:
        if not input_path.is_absolute() or not output_path.is_absolute():
            raise WorkerFailed("audit_preflight_path_invalid")
        request = _read_request(input_path)
        result = _execute(request)
    except WorkerFailed as exc:
        result = _failed_result(request, exc.code)
    except Exception:
        result = _failed_result(request, "audit_preflight_worker_internal_failure")
    try:
        _write_result(output_path, result)
    except WorkerFailed:
        return 74
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
