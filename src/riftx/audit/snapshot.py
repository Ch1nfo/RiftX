"""SnapshotStore/CAS contracts for immutable Code Audit source bytes."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Protocol

SNAPSHOT_STORE_SCHEMA_VERSION = "riftx.snapshot-store/v1"
SNAPSHOT_CAS_OBJECT_SCHEMA_VERSION = "riftx.snapshot-cas-object/v1"
SNAPSHOT_CAS_INDEX_SCHEMA_VERSION = "riftx.snapshot-cas-index/v1"
SNAPSHOT_REFERENCE_SCHEMA_VERSION = "riftx.snapshot-reference/v1"
SNAPSHOT_CONTENT_STORAGE_KEY_PREFIX = "snapshot-cas:v1:"
SNAPSHOT_MANIFEST_INDEX_NAME = ".riftx-snapshot-cas-index.json"

_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@+~\-]{0,127}$")
_DIGEST_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_MAX_COUNTER = 2**63 - 1
_MAX_RELATIVE_PATH_BYTES = 4_096
_MAX_INDEX_BYTES = 256 * 1_024 * 1_024
_REGULAR_FILE_MODES = frozenset({0o100644, 0o100755})
_SYMLINK_MODE = 0o120000


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _domain_digest(domain: str, value: object) -> str:
    canonical = _canonical_json(value).encode("utf-8")
    return hashlib.sha256(domain.encode("ascii") + b"\0" + canonical).hexdigest()


def _require_safe_id(value: str, *, label: str) -> None:
    if not isinstance(value, str) or _ID_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{label} is invalid")


def _require_digest(value: str, *, label: str) -> None:
    if not isinstance(value, str) or _DIGEST_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")


def _require_aware(value: datetime, *, label: str) -> None:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")


def _validate_relative_path(value: str) -> None:
    if not isinstance(value, str) or not value:
        raise ValueError("snapshot blob path must be non-empty")
    if value != value.strip() or unicodedata.normalize("NFC", value) != value:
        raise ValueError("snapshot blob path must be canonical NFC text")
    if len(value.encode("utf-8")) > _MAX_RELATIVE_PATH_BYTES:
        raise ValueError("snapshot blob path exceeds its byte limit")
    if any(unicodedata.category(character).startswith("C") for character in value):
        raise ValueError("snapshot blob path must not contain control characters")
    path = PurePosixPath(value)
    if (
        value.startswith(("/", "~"))
        or "\\" in value
        or any(part in {"", ".", ".."} for part in path.parts)
        or str(path) != value
        or path.parts[0] == SNAPSHOT_MANIFEST_INDEX_NAME
    ):
        raise ValueError("snapshot blob path must be a normalized relative POSIX path")


class SnapshotCASObjectType(StrEnum):
    TREE = "tree"


class SnapshotBlobObjectType(StrEnum):
    REGULAR_FILE = "regular_file"
    SYMLINK = "symlink"


class SnapshotReferenceRole(StrEnum):
    PRIMARY = "primary"
    BASE = "base"
    BASELINE = "baseline"
    FINDING_EVIDENCE = "finding_evidence"
    RETEST_PARENT = "retest_parent"
    DISTRIBUTION_REVISION = "distribution_revision"


class SnapshotStoreFailure(StrEnum):
    REQUEST_INVALID = "request_invalid"
    SOURCE_UNAVAILABLE = "source_unavailable"
    SOURCE_INTEGRITY = "source_integrity"
    SIZE_LIMIT_EXCEEDED = "size_limit_exceeded"
    STORAGE_MISSING = "storage_missing"
    STORAGE_INTEGRITY = "storage_integrity"
    STORAGE_UNAVAILABLE = "storage_unavailable"
    OWNER_MISMATCH = "owner_mismatch"
    MANIFEST_MISMATCH = "manifest_mismatch"
    BLOB_MISSING = "blob_missing"


class SnapshotStoreError(RuntimeError):
    """Stable, path-free SnapshotStore failure."""

    def __init__(self, failure: SnapshotStoreFailure) -> None:
        super().__init__(failure.value)
        self.failure = failure


class SnapshotStoreCrash(RuntimeError):
    """Fault-injection signal that intentionally leaves crash-recovery state."""


@dataclass(frozen=True, slots=True)
class SnapshotBlobMetadata:
    relative_path: str
    blob_digest: str
    size: int
    mode: int
    object_type: SnapshotBlobObjectType = SnapshotBlobObjectType.REGULAR_FILE

    def __post_init__(self) -> None:
        _validate_relative_path(self.relative_path)
        _require_digest(self.blob_digest, label="blob_digest")
        if not isinstance(self.size, int) or not 0 <= self.size <= _MAX_COUNTER:
            raise ValueError("snapshot blob size is invalid")
        if self.object_type is SnapshotBlobObjectType.REGULAR_FILE:
            if self.mode not in _REGULAR_FILE_MODES:
                raise ValueError("regular snapshot blob mode is invalid")
        elif self.object_type is SnapshotBlobObjectType.SYMLINK:
            if self.mode != _SYMLINK_MODE:
                raise ValueError("symlink snapshot blob mode is invalid")
        else:
            raise ValueError("snapshot blob object_type is invalid")

    def canonical_payload(self) -> dict[str, object]:
        return {
            "blob_digest": self.blob_digest,
            "mode": self.mode,
            "object_type": self.object_type.value,
            "relative_path": self.relative_path,
            "size": self.size,
        }


@dataclass(frozen=True, slots=True)
class SnapshotCASDescriptor:
    project_id: str
    snapshot_digest: str
    manifest_digest: str
    blobs: tuple[SnapshotBlobMetadata, ...]
    object_type: SnapshotCASObjectType = SnapshotCASObjectType.TREE
    schema_version: str = field(default=SNAPSHOT_CAS_OBJECT_SCHEMA_VERSION, init=False)

    def __post_init__(self) -> None:
        _require_safe_id(self.project_id, label="project_id")
        _require_digest(self.snapshot_digest, label="snapshot_digest")
        _require_digest(self.manifest_digest, label="manifest_digest")
        if self.object_type is not SnapshotCASObjectType.TREE:
            raise ValueError("SnapshotStore v1 accepts only tree objects")
        if not isinstance(self.blobs, tuple) or any(
            not isinstance(blob, SnapshotBlobMetadata) for blob in self.blobs
        ):
            raise ValueError("snapshot blobs must be a tuple of metadata entries")
        paths = tuple(blob.relative_path for blob in self.blobs)
        if paths != tuple(sorted(paths)) or len(paths) != len(set(paths)):
            raise ValueError("snapshot blob metadata must use unique sorted paths")
        if self.total_bytes > _MAX_COUNTER:
            raise ValueError("snapshot tree exceeds its total byte limit")
        if len(self.canonical_json().encode("utf-8")) > _MAX_INDEX_BYTES:
            raise ValueError("snapshot CAS index exceeds its byte limit")

    @property
    def file_count(self) -> int:
        return len(self.blobs)

    @property
    def total_bytes(self) -> int:
        return sum(blob.size for blob in self.blobs)

    def canonical_payload(self) -> dict[str, object]:
        return {
            "blobs": [blob.canonical_payload() for blob in self.blobs],
            "file_count": self.file_count,
            "manifest_digest": self.manifest_digest,
            "object_type": self.object_type.value,
            "project_id": self.project_id,
            "schema_version": self.schema_version,
            "snapshot_digest": self.snapshot_digest,
            "total_bytes": self.total_bytes,
        }

    def canonical_json(self) -> str:
        return _canonical_json(self.canonical_payload())

    @property
    def descriptor_digest(self) -> str:
        return _domain_digest(self.schema_version, self.canonical_payload())

    @property
    def content_storage_key(self) -> str:
        return f"{SNAPSHOT_CONTENT_STORAGE_KEY_PREFIX}{self.descriptor_digest}"


@dataclass(frozen=True, slots=True)
class SnapshotCASBinding:
    project_id: str
    snapshot_digest: str
    manifest_digest: str

    def __post_init__(self) -> None:
        _require_safe_id(self.project_id, label="project_id")
        _require_digest(self.snapshot_digest, label="snapshot_digest")
        _require_digest(self.manifest_digest, label="manifest_digest")

    def accepts(self, descriptor: SnapshotCASDescriptor) -> bool:
        return (
            self.project_id == descriptor.project_id
            and self.snapshot_digest == descriptor.snapshot_digest
            and self.manifest_digest == descriptor.manifest_digest
        )


@dataclass(frozen=True, slots=True)
class SnapshotStagedTree:
    root: Path = field(repr=False)
    descriptor: SnapshotCASDescriptor

    def __post_init__(self) -> None:
        if not isinstance(self.root, Path) or not self.root.is_absolute():
            raise ValueError("snapshot staging root must be an absolute path")
        if ".." in self.root.parts:
            raise ValueError("snapshot staging root must not contain parent traversal")


@dataclass(frozen=True, slots=True)
class StoredSnapshotTree:
    content_storage_key: str
    descriptor_digest: str
    file_count: int
    total_bytes: int
    reused: bool
    store_version: str = SNAPSHOT_STORE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        parse_snapshot_content_storage_key(self.content_storage_key)
        _require_digest(self.descriptor_digest, label="descriptor_digest")
        if self.content_storage_key != (
            f"{SNAPSHOT_CONTENT_STORAGE_KEY_PREFIX}{self.descriptor_digest}"
        ):
            raise ValueError("stored Snapshot tree key and descriptor digest differ")
        if not 0 <= self.file_count <= _MAX_COUNTER or not 0 <= self.total_bytes <= _MAX_COUNTER:
            raise ValueError("stored Snapshot tree counters are invalid")


@dataclass(frozen=True, slots=True)
class SnapshotIntegrityResult:
    valid: bool
    content_storage_key: str
    descriptor_digest: str | None
    file_count: int
    total_bytes: int
    failure: SnapshotStoreFailure | None = None

    def __post_init__(self) -> None:
        parse_snapshot_content_storage_key(self.content_storage_key)
        if self.valid:
            if self.failure is not None or self.descriptor_digest is None:
                raise ValueError("valid Snapshot integrity result is incomplete")
            _require_digest(self.descriptor_digest, label="descriptor_digest")
        elif self.failure is None or self.descriptor_digest is not None:
            raise ValueError("invalid Snapshot integrity result is inconsistent")
        if not 0 <= self.file_count <= _MAX_COUNTER or not 0 <= self.total_bytes <= _MAX_COUNTER:
            raise ValueError("Snapshot integrity counters are invalid")


@dataclass(frozen=True, slots=True)
class SnapshotStagingCleanupReceipt:
    examined: int
    eligible: int
    removed: int
    removed_bytes: int
    dry_run: bool
    completed_at: datetime

    def __post_init__(self) -> None:
        for label, value in (
            ("examined", self.examined),
            ("eligible", self.eligible),
            ("removed", self.removed),
            ("removed_bytes", self.removed_bytes),
        ):
            if not isinstance(value, int) or not 0 <= value <= _MAX_COUNTER:
                raise ValueError(f"{label} is invalid")
        if self.removed > self.eligible or self.eligible > self.examined:
            raise ValueError("Snapshot staging cleanup counters are inconsistent")
        _require_aware(self.completed_at, label="completed_at")


@dataclass(frozen=True, slots=True)
class SnapshotReference:
    audit_id: str
    snapshot_id: str
    project_id: str
    role: SnapshotReferenceRole
    created_at: datetime
    schema_version: str = field(default=SNAPSHOT_REFERENCE_SCHEMA_VERSION, init=False)

    def __post_init__(self) -> None:
        for label, value in (
            ("audit_id", self.audit_id),
            ("snapshot_id", self.snapshot_id),
            ("project_id", self.project_id),
        ):
            _require_safe_id(value, label=label)
        if not isinstance(self.role, SnapshotReferenceRole):
            raise ValueError("snapshot reference role is invalid")
        _require_aware(self.created_at, label="created_at")

    @property
    def reference_digest(self) -> str:
        return _domain_digest(
            self.schema_version,
            {
                "audit_id": self.audit_id,
                "project_id": self.project_id,
                "role": self.role.value,
                "schema_version": self.schema_version,
                "snapshot_id": self.snapshot_id,
            },
        )


class OpenedSnapshotBlob(Protocol):
    size: int

    @property
    def closed(self) -> bool: ...

    def read(self, max_bytes: int) -> bytes: ...

    def verify_complete(self) -> None: ...

    def close(self) -> None: ...


class SnapshotStore(Protocol):
    def put_staged_tree(self, staged: SnapshotStagedTree) -> StoredSnapshotTree: ...

    def open_blob(
        self,
        binding: SnapshotCASBinding,
        content_storage_key: str,
        relative_path: str,
        expected_blob_digest: str,
        *,
        max_bytes: int,
    ) -> OpenedSnapshotBlob: ...

    def describe(
        self,
        binding: SnapshotCASBinding,
        content_storage_key: str,
    ) -> SnapshotCASDescriptor: ...

    def verify(
        self,
        binding: SnapshotCASBinding,
        content_storage_key: str,
    ) -> SnapshotIntegrityResult: ...

    def cleanup_staging_orphans(
        self,
        *,
        older_than: datetime,
        dry_run: bool,
    ) -> SnapshotStagingCleanupReceipt: ...


class SnapshotReferenceRepository(Protocol):
    async def add(self, reference: SnapshotReference) -> tuple[SnapshotReference, bool]: ...

    async def release(
        self,
        *,
        audit_id: str,
        snapshot_id: str,
        role: SnapshotReferenceRole,
    ) -> bool: ...

    async def list_for_snapshot(
        self,
        snapshot_id: str,
        *,
        project_id: str,
    ) -> tuple[SnapshotReference, ...]: ...


def parse_snapshot_content_storage_key(value: str) -> str:
    if not isinstance(value, str) or not value.startswith(SNAPSHOT_CONTENT_STORAGE_KEY_PREFIX):
        raise ValueError("Snapshot content storage key is invalid")
    digest = value.removeprefix(SNAPSHOT_CONTENT_STORAGE_KEY_PREFIX)
    _require_digest(digest, label="Snapshot content storage key digest")
    return digest


__all__ = [
    "SNAPSHOT_CAS_INDEX_SCHEMA_VERSION",
    "SNAPSHOT_CAS_OBJECT_SCHEMA_VERSION",
    "SNAPSHOT_CONTENT_STORAGE_KEY_PREFIX",
    "SNAPSHOT_MANIFEST_INDEX_NAME",
    "SNAPSHOT_REFERENCE_SCHEMA_VERSION",
    "SNAPSHOT_STORE_SCHEMA_VERSION",
    "OpenedSnapshotBlob",
    "SnapshotBlobMetadata",
    "SnapshotBlobObjectType",
    "SnapshotCASBinding",
    "SnapshotCASDescriptor",
    "SnapshotCASObjectType",
    "SnapshotIntegrityResult",
    "SnapshotReference",
    "SnapshotReferenceRepository",
    "SnapshotReferenceRole",
    "SnapshotStagedTree",
    "SnapshotStagingCleanupReceipt",
    "SnapshotStore",
    "SnapshotStoreCrash",
    "SnapshotStoreError",
    "SnapshotStoreFailure",
    "StoredSnapshotTree",
    "parse_snapshot_content_storage_key",
]
