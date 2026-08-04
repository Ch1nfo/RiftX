"""Versioned Source Manifest and publication contracts for Code Audit snapshots."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import tempfile
import unicodedata
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Any

from .snapshot import (
    SnapshotBlobMetadata,
    SnapshotBlobObjectType,
    SnapshotCASDescriptor,
    SnapshotStagedTree,
    SnapshotStore,
    StoredSnapshotTree,
)

SOURCE_MANIFEST_SCHEMA_VERSION = "riftx.source-manifest/v1"
SOURCE_MATERIALIZER_SCHEMA_VERSION = "riftx.source-materializer/v1"
SOURCE_CAPTURE_POLICY_SCHEMA_VERSION = "riftx.source-capture-policy/v1"
SOURCE_TREE_DIGEST_DOMAIN = "riftx.source-tree/v1"
SOURCE_WORKING_TREE_DIGEST_DOMAIN = "riftx.working-tree/v1"
SOURCE_SNAPSHOT_DIGEST_DOMAIN = "riftx.source-snapshot/v1"
SOURCE_MANIFEST_BLOB_NAME = "source-manifest.json"

_DIGEST_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_GIT_OBJECT_ID_PATTERN = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@+~\-]{0,127}$")
_MAX_COUNTER = 2**63 - 1
_MAX_PATH_BYTES = 16 * 1024
_MAX_CANONICAL_PATH_BYTES = 4_096
_MAX_MANIFEST_BYTES = 256 * 1024 * 1024


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


def _require_digest(value: str, *, label: str) -> None:
    if not isinstance(value, str) or _DIGEST_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")


def _require_git_object_id(value: str, *, label: str) -> None:
    if not isinstance(value, str) or _GIT_OBJECT_ID_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{label} must be a lowercase Git object ID")


def _validate_raw_path(value: bytes) -> None:
    if (
        not value
        or len(value) > _MAX_PATH_BYTES
        or value.startswith(b"/")
        or b"\0" in value
        or b"\\" in value
        or any(part in {b"", b".", b".."} for part in value.split(b"/"))
    ):
        raise ValueError("Manifest path bytes are invalid")


def _canonical_path_text(value: bytes) -> str | None:
    if len(value) > _MAX_CANONICAL_PATH_BYTES:
        return None
    try:
        decoded = value.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return None
    if unicodedata.normalize("NFC", decoded) != decoded:
        return None
    if any(unicodedata.category(character).startswith("C") for character in decoded):
        return None
    path = PurePosixPath(decoded)
    if str(path) != decoded or any(part == ".git" for part in path.parts):
        return None
    return decoded


class SourceManifestSourceKind(StrEnum):
    REVISION = "revision"
    WORKING_TREE = "working_tree"


class SourceCaptureDecision(StrEnum):
    INCLUDED = "included"
    EXCLUDED = "excluded"
    DEFERRED = "deferred"


class SourceCaptureReason(StrEnum):
    INCLUDED = "included"
    PATH_EXCLUDED = "path_excluded"
    UNTRACKED_EXCLUDED = "untracked_excluded"
    IGNORED = "ignored"
    GENERATED_EXCLUDED = "generated_excluded"
    VENDOR_EXCLUDED = "vendor_excluded"
    INVALID_UTF8_PATH = "invalid_utf8_path"
    NONCANONICAL_PATH = "noncanonical_path"
    INVALID_UTF8_CONTENT = "invalid_utf8_content"
    OVERSIZED_FILE = "oversized_file"
    REPOSITORY_BUDGET_EXCEEDED = "repository_budget_exceeded"
    SPECIAL_FILE = "special_file"
    HARDLINK = "hardlink"
    SUBMODULE = "submodule"
    LFS_POINTER = "lfs_pointer"
    MISSING_WORKTREE_ENTRY = "missing_worktree_entry"


class SourceManifestObjectType(StrEnum):
    REGULAR_FILE = "regular_file"
    SYMLINK = "symlink"
    SUBMODULE = "submodule"
    SPECIAL = "special"
    MISSING = "missing"


class SourceManifestOrigin(StrEnum):
    COMMIT = "commit"
    TRACKED_WORKTREE = "tracked_worktree"
    UNTRACKED = "untracked"
    IGNORED = "ignored"


class SourceClassification(StrEnum):
    SOURCE = "source"
    CONFIGURATION = "configuration"
    DOCUMENTATION = "documentation"
    DATA = "data"
    GENERATED = "generated"
    VENDOR = "vendor"
    SYMLINK = "symlink"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class SourceManifestPath:
    path_digest: str
    relative_path: str | None = None
    raw_path_b64: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        _require_digest(self.path_digest, label="path_digest")
        if (self.relative_path is None) == (self.raw_path_b64 is None):
            raise ValueError("Manifest path must use exactly one path representation")
        raw = self.raw_bytes
        _validate_raw_path(raw)
        if hashlib.sha256(raw).hexdigest() != self.path_digest:
            raise ValueError("Manifest path digest does not match its bytes")
        canonical = _canonical_path_text(raw)
        if self.relative_path is not None:
            if canonical != self.relative_path:
                raise ValueError("Manifest relative path is not canonical")
        elif canonical is not None:
            raise ValueError("Canonical Manifest path must not use opaque encoding")

    @classmethod
    def from_bytes(cls, value: bytes) -> SourceManifestPath:
        _validate_raw_path(value)
        canonical = _canonical_path_text(value)
        return cls(
            path_digest=hashlib.sha256(value).hexdigest(),
            relative_path=canonical,
            raw_path_b64=(
                None if canonical is not None else base64.b64encode(value).decode("ascii")
            ),
        )

    @property
    def raw_bytes(self) -> bytes:
        if self.relative_path is not None:
            return self.relative_path.encode("utf-8")
        assert self.raw_path_b64 is not None
        try:
            raw = base64.b64decode(self.raw_path_b64, validate=True)
        except ValueError as exc:
            raise ValueError("Manifest opaque path is not canonical base64") from exc
        if base64.b64encode(raw).decode("ascii") != self.raw_path_b64:
            raise ValueError("Manifest opaque path is not canonical base64")
        return raw

    def canonical_payload(self) -> dict[str, object]:
        return {
            "path_digest": self.path_digest,
            "raw_path_b64": self.raw_path_b64,
            "relative_path": self.relative_path,
        }


@dataclass(frozen=True, slots=True)
class SourceCapturePolicy:
    include_paths: tuple[str, ...] = ()
    exclude_paths: tuple[str, ...] = ()
    include_untracked: bool = False
    include_generated: bool = False
    include_vendor: bool = False
    max_file_bytes: int = 5 * 1024 * 1024
    max_repository_bytes: int = 2 * 1024 * 1024 * 1024
    max_manifest_entries: int = 200_000
    submodule_mode: str = field(default="exclude", init=False)
    lfs_pointer_mode: str = field(default="defer", init=False)
    symlink_mode: str = field(default="capture_link", init=False)
    hardlink_mode: str = field(default="defer", init=False)
    special_file_mode: str = field(default="defer", init=False)
    text_decoding: str = field(default="utf-8-strict", init=False)
    ignored_mode: str = field(default="record_excluded", init=False)
    schema_version: str = field(default=SOURCE_CAPTURE_POLICY_SCHEMA_VERSION, init=False)

    def __post_init__(self) -> None:
        for label, value in (
            ("include_untracked", self.include_untracked),
            ("include_generated", self.include_generated),
            ("include_vendor", self.include_vendor),
        ):
            if type(value) is not bool:
                raise ValueError(f"{label} must be boolean")
        for label, paths in (
            ("include_paths", self.include_paths),
            ("exclude_paths", self.exclude_paths),
        ):
            if not isinstance(paths, tuple) or paths != tuple(sorted(set(paths))):
                raise ValueError(f"{label} must be a sorted unique tuple")
            for value in paths:
                raw = value.encode("utf-8")
                _validate_raw_path(raw)
                if _canonical_path_text(raw) != value:
                    raise ValueError(f"{label} contains a noncanonical path")
        if set(self.include_paths).intersection(self.exclude_paths):
            raise ValueError("capture policy include/exclude paths overlap")
        for label, value in (
            ("max_file_bytes", self.max_file_bytes),
            ("max_repository_bytes", self.max_repository_bytes),
            ("max_manifest_entries", self.max_manifest_entries),
        ):
            if type(value) is not int or not 1 <= value <= _MAX_COUNTER:
                raise ValueError(f"{label} is invalid")
        if self.max_file_bytes > self.max_repository_bytes:
            raise ValueError("capture policy file limit exceeds repository limit")

    def canonical_payload(self) -> dict[str, object]:
        return {
            "exclude_paths": list(self.exclude_paths),
            "hardlink_mode": self.hardlink_mode,
            "ignored_mode": self.ignored_mode,
            "include_generated": self.include_generated,
            "include_paths": list(self.include_paths),
            "include_untracked": self.include_untracked,
            "include_vendor": self.include_vendor,
            "lfs_pointer_mode": self.lfs_pointer_mode,
            "max_file_bytes": self.max_file_bytes,
            "max_manifest_entries": self.max_manifest_entries,
            "max_repository_bytes": self.max_repository_bytes,
            "schema_version": self.schema_version,
            "special_file_mode": self.special_file_mode,
            "submodule_mode": self.submodule_mode,
            "symlink_mode": self.symlink_mode,
            "text_decoding": self.text_decoding,
        }

    @property
    def digest(self) -> str:
        return _domain_digest(self.schema_version, self.canonical_payload())

    def selects(self, raw_path: bytes) -> bool:
        includes = tuple(value.encode("utf-8") for value in self.include_paths)
        excludes = tuple(value.encode("utf-8") for value in self.exclude_paths)

        def matches(prefix: bytes) -> bool:
            return raw_path == prefix or raw_path.startswith(prefix + b"/")

        return (not includes or any(matches(value) for value in includes)) and not any(
            matches(value) for value in excludes
        )


@dataclass(frozen=True, slots=True)
class SourceManifestEntry:
    path: SourceManifestPath
    object_type: SourceManifestObjectType
    origin: SourceManifestOrigin
    mode: int
    size: int | None
    sha256: str | None
    git_blob_id: str | None
    language: str
    classification: SourceClassification
    decision: SourceCaptureDecision
    reason: SourceCaptureReason

    def __post_init__(self) -> None:
        if not isinstance(self.path, SourceManifestPath):
            raise ValueError("Manifest entry path is invalid")
        for label, value, expected in (
            ("object_type", self.object_type, SourceManifestObjectType),
            ("origin", self.origin, SourceManifestOrigin),
            ("classification", self.classification, SourceClassification),
            ("decision", self.decision, SourceCaptureDecision),
            ("reason", self.reason, SourceCaptureReason),
        ):
            if not isinstance(value, expected):
                raise ValueError(f"Manifest entry {label} is invalid")
        if type(self.mode) is not int or not 0 <= self.mode <= 0o177777:
            raise ValueError("Manifest entry mode is invalid")
        if self.size is not None and (
            type(self.size) is not int or not 0 <= self.size <= _MAX_COUNTER
        ):
            raise ValueError("Manifest entry size is invalid")
        if self.sha256 is not None:
            _require_digest(self.sha256, label="entry sha256")
            if self.size is None:
                raise ValueError("Manifest entry digest requires a size")
        if self.git_blob_id is not None:
            _require_git_object_id(self.git_blob_id, label="git_blob_id")
        if not isinstance(self.language, str) or not self.language or len(self.language) > 128:
            raise ValueError("Manifest entry language is invalid")
        if self.decision is SourceCaptureDecision.INCLUDED:
            if (
                self.reason is not SourceCaptureReason.INCLUDED
                or self.path.relative_path is None
                or self.object_type
                not in {
                    SourceManifestObjectType.REGULAR_FILE,
                    SourceManifestObjectType.SYMLINK,
                }
                or self.size is None
                or self.sha256 is None
            ):
                raise ValueError("included Manifest entry is incomplete")
            if self.object_type is SourceManifestObjectType.REGULAR_FILE and self.mode not in {
                0o100644,
                0o100755,
            }:
                raise ValueError("included regular-file mode is invalid")
            if self.object_type is SourceManifestObjectType.SYMLINK and self.mode != 0o120000:
                raise ValueError("included symlink mode is invalid")
        elif self.reason is SourceCaptureReason.INCLUDED:
            raise ValueError("non-included Manifest entry cannot use included reason")

    def canonical_payload(self) -> dict[str, object]:
        return {
            "classification": self.classification.value,
            "decision": self.decision.value,
            "git_blob_id": self.git_blob_id,
            "language": self.language,
            "mode": self.mode,
            "object_type": self.object_type.value,
            "origin": self.origin.value,
            "path": self.path.canonical_payload(),
            "reason": self.reason.value,
            "sha256": self.sha256,
            "size": self.size,
        }

    @classmethod
    def from_payload(cls, value: object) -> SourceManifestEntry:
        if not isinstance(value, dict):
            raise ValueError("Manifest entry must be an object")
        expected = {
            "classification",
            "decision",
            "git_blob_id",
            "language",
            "mode",
            "object_type",
            "origin",
            "path",
            "reason",
            "sha256",
            "size",
        }
        if set(value) != expected or not isinstance(value["path"], dict):
            raise ValueError("Manifest entry shape is invalid")
        raw_path = value["path"]
        if set(raw_path) != {"path_digest", "raw_path_b64", "relative_path"}:
            raise ValueError("Manifest path shape is invalid")
        return cls(
            path=SourceManifestPath(
                path_digest=raw_path["path_digest"],
                relative_path=raw_path["relative_path"],
                raw_path_b64=raw_path["raw_path_b64"],
            ),
            object_type=SourceManifestObjectType(value["object_type"]),
            origin=SourceManifestOrigin(value["origin"]),
            mode=value["mode"],
            size=value["size"],
            sha256=value["sha256"],
            git_blob_id=value["git_blob_id"],
            language=value["language"],
            classification=SourceClassification(value["classification"]),
            decision=SourceCaptureDecision(value["decision"]),
            reason=SourceCaptureReason(value["reason"]),
        )


@dataclass(frozen=True, slots=True)
class SourceManifest:
    source_kind: SourceManifestSourceKind
    commit_sha: str
    head_commit_sha: str
    capture_policy_digest: str
    entries: tuple[SourceManifestEntry, ...]
    staged: bool = False
    unstaged: bool = False
    untracked: bool = False
    working_tree_digest: str | None = None
    tree_digest: str = ""
    snapshot_digest: str = ""
    manifest_digest: str = ""
    file_count: int = 0
    total_bytes: int = 0
    schema_version: str = field(default=SOURCE_MANIFEST_SCHEMA_VERSION, init=False)
    materializer_schema_version: str = field(
        default=SOURCE_MATERIALIZER_SCHEMA_VERSION,
        init=False,
    )

    def __post_init__(self) -> None:
        if not isinstance(self.source_kind, SourceManifestSourceKind):
            raise ValueError("Manifest source_kind is invalid")
        for label, value in (
            ("staged", self.staged),
            ("unstaged", self.unstaged),
            ("untracked", self.untracked),
        ):
            if type(value) is not bool:
                raise ValueError(f"Manifest {label} must be boolean")
        for label, value in (
            ("file_count", self.file_count),
            ("total_bytes", self.total_bytes),
        ):
            if type(value) is not int or not 0 <= value <= _MAX_COUNTER:
                raise ValueError(f"Manifest {label} is invalid")
        _require_git_object_id(self.commit_sha, label="commit_sha")
        _require_git_object_id(self.head_commit_sha, label="head_commit_sha")
        _require_digest(self.capture_policy_digest, label="capture_policy_digest")
        if not isinstance(self.entries, tuple) or any(
            not isinstance(entry, SourceManifestEntry) for entry in self.entries
        ):
            raise ValueError("Manifest entries must be a tuple")
        raw_paths = tuple(entry.path.raw_bytes for entry in self.entries)
        if raw_paths != tuple(sorted(raw_paths)) or len(raw_paths) != len(set(raw_paths)):
            raise ValueError("Manifest entries must use unique byte-sorted paths")
        included = tuple(
            entry for entry in self.entries if entry.decision is SourceCaptureDecision.INCLUDED
        )
        expected_count = len(included)
        expected_bytes = sum(entry.size or 0 for entry in included)
        if self.file_count != expected_count or self.total_bytes != expected_bytes:
            raise ValueError("Manifest included counters are inconsistent")
        expected_tree = _domain_digest(
            SOURCE_TREE_DIGEST_DOMAIN,
            {
                "entries": [entry.canonical_payload() for entry in self.entries],
                "schema_version": SOURCE_TREE_DIGEST_DOMAIN,
            },
        )
        if self.tree_digest != expected_tree:
            raise ValueError("Manifest tree_digest is invalid")
        dirty = any((self.staged, self.unstaged, self.untracked))
        if self.source_kind is SourceManifestSourceKind.REVISION:
            if (
                dirty
                or self.working_tree_digest is not None
                or self.commit_sha != self.head_commit_sha
            ):
                raise ValueError("revision Manifest carries working-tree state")
        else:
            expected_working = _domain_digest(
                SOURCE_WORKING_TREE_DIGEST_DOMAIN,
                {
                    "entries": [entry.canonical_payload() for entry in self.entries],
                    "head_commit_sha": self.head_commit_sha,
                    "schema_version": SOURCE_WORKING_TREE_DIGEST_DOMAIN,
                    "staged": self.staged,
                    "unstaged": self.unstaged,
                    "untracked": self.untracked,
                },
            )
            if self.working_tree_digest != expected_working:
                raise ValueError("Manifest working_tree_digest is invalid")
        expected_snapshot = _domain_digest(
            SOURCE_SNAPSHOT_DIGEST_DOMAIN,
            {
                "capture_policy_digest": self.capture_policy_digest,
                "materializer_schema_version": self.materializer_schema_version,
                "tree_digest": self.tree_digest,
            },
        )
        if self.snapshot_digest != expected_snapshot:
            raise ValueError("Manifest snapshot_digest is invalid")
        expected_manifest = hashlib.sha256(
            _canonical_json(self.canonical_payload(include_manifest_digest=False)).encode("utf-8")
        ).hexdigest()
        if self.manifest_digest != expected_manifest:
            raise ValueError("Manifest digest is invalid")
        if len(self.canonical_json().encode("utf-8")) > _MAX_MANIFEST_BYTES:
            raise ValueError("Source Manifest exceeds its byte limit")

    @classmethod
    def create(
        cls,
        *,
        source_kind: SourceManifestSourceKind,
        commit_sha: str,
        head_commit_sha: str,
        capture_policy_digest: str,
        entries: tuple[SourceManifestEntry, ...],
        staged: bool = False,
        unstaged: bool = False,
        untracked: bool = False,
    ) -> SourceManifest:
        ordered = tuple(sorted(entries, key=lambda entry: entry.path.raw_bytes))
        included = tuple(
            entry for entry in ordered if entry.decision is SourceCaptureDecision.INCLUDED
        )
        tree_digest = _domain_digest(
            SOURCE_TREE_DIGEST_DOMAIN,
            {
                "entries": [entry.canonical_payload() for entry in ordered],
                "schema_version": SOURCE_TREE_DIGEST_DOMAIN,
            },
        )
        working_tree_digest = None
        if source_kind is SourceManifestSourceKind.WORKING_TREE:
            working_tree_digest = _domain_digest(
                SOURCE_WORKING_TREE_DIGEST_DOMAIN,
                {
                    "entries": [entry.canonical_payload() for entry in ordered],
                    "head_commit_sha": head_commit_sha,
                    "schema_version": SOURCE_WORKING_TREE_DIGEST_DOMAIN,
                    "staged": staged,
                    "unstaged": unstaged,
                    "untracked": untracked,
                },
            )
        snapshot_digest = _domain_digest(
            SOURCE_SNAPSHOT_DIGEST_DOMAIN,
            {
                "capture_policy_digest": capture_policy_digest,
                "materializer_schema_version": SOURCE_MATERIALIZER_SCHEMA_VERSION,
                "tree_digest": tree_digest,
            },
        )
        manifest_payload = {
            "capture_policy_digest": capture_policy_digest,
            "commit_sha": commit_sha,
            "entries": [entry.canonical_payload() for entry in ordered],
            "file_count": len(included),
            "head_commit_sha": head_commit_sha,
            "materializer_schema_version": SOURCE_MATERIALIZER_SCHEMA_VERSION,
            "schema_version": SOURCE_MANIFEST_SCHEMA_VERSION,
            "snapshot_digest": snapshot_digest,
            "source_kind": source_kind.value,
            "staged": staged,
            "total_bytes": sum(entry.size or 0 for entry in included),
            "tree_digest": tree_digest,
            "unstaged": unstaged,
            "untracked": untracked,
            "working_tree_digest": working_tree_digest,
        }
        manifest_digest = hashlib.sha256(
            _canonical_json(manifest_payload).encode("utf-8")
        ).hexdigest()
        return cls(
            source_kind=source_kind,
            commit_sha=commit_sha,
            head_commit_sha=head_commit_sha,
            capture_policy_digest=capture_policy_digest,
            entries=ordered,
            staged=staged,
            unstaged=unstaged,
            untracked=untracked,
            working_tree_digest=working_tree_digest,
            tree_digest=tree_digest,
            snapshot_digest=snapshot_digest,
            manifest_digest=manifest_digest,
            file_count=len(included),
            total_bytes=sum(entry.size or 0 for entry in included),
        )

    def canonical_payload(self, *, include_manifest_digest: bool = True) -> dict[str, object]:
        value: dict[str, object] = {
            "capture_policy_digest": self.capture_policy_digest,
            "commit_sha": self.commit_sha,
            "entries": [entry.canonical_payload() for entry in self.entries],
            "file_count": self.file_count,
            "head_commit_sha": self.head_commit_sha,
            "materializer_schema_version": self.materializer_schema_version,
            "schema_version": self.schema_version,
            "snapshot_digest": self.snapshot_digest,
            "source_kind": self.source_kind.value,
            "staged": self.staged,
            "total_bytes": self.total_bytes,
            "tree_digest": self.tree_digest,
            "unstaged": self.unstaged,
            "untracked": self.untracked,
            "working_tree_digest": self.working_tree_digest,
        }
        if include_manifest_digest:
            value["manifest_digest"] = self.manifest_digest
        return value

    def canonical_json(self) -> str:
        return _canonical_json(self.canonical_payload())

    @property
    def manifest_blob_digest(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()

    @classmethod
    def from_json(cls, value: str | bytes) -> SourceManifest:
        raw = value.encode("utf-8") if isinstance(value, str) else value
        if not raw or len(raw) > _MAX_MANIFEST_BYTES:
            raise ValueError("Source Manifest bytes are invalid")

        def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for key, item in pairs:
                if key in result:
                    raise ValueError("duplicate Manifest key")
                result[key] = item
            return result

        try:
            payload = json.loads(raw, object_pairs_hook=reject_duplicates)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("Source Manifest is not valid JSON") from exc
        expected = {
            "capture_policy_digest",
            "commit_sha",
            "entries",
            "file_count",
            "head_commit_sha",
            "manifest_digest",
            "materializer_schema_version",
            "schema_version",
            "snapshot_digest",
            "source_kind",
            "staged",
            "total_bytes",
            "tree_digest",
            "unstaged",
            "untracked",
            "working_tree_digest",
        }
        if not isinstance(payload, dict) or set(payload) != expected:
            raise ValueError("Source Manifest shape is invalid")
        if (
            payload["schema_version"] != SOURCE_MANIFEST_SCHEMA_VERSION
            or payload["materializer_schema_version"] != SOURCE_MATERIALIZER_SCHEMA_VERSION
            or not isinstance(payload["entries"], list)
        ):
            raise ValueError("Source Manifest schema is unsupported")
        manifest = cls(
            source_kind=SourceManifestSourceKind(payload["source_kind"]),
            commit_sha=payload["commit_sha"],
            head_commit_sha=payload["head_commit_sha"],
            capture_policy_digest=payload["capture_policy_digest"],
            entries=tuple(SourceManifestEntry.from_payload(item) for item in payload["entries"]),
            staged=payload["staged"],
            unstaged=payload["unstaged"],
            untracked=payload["untracked"],
            working_tree_digest=payload["working_tree_digest"],
            tree_digest=payload["tree_digest"],
            snapshot_digest=payload["snapshot_digest"],
            manifest_digest=payload["manifest_digest"],
            file_count=payload["file_count"],
            total_bytes=payload["total_bytes"],
        )
        if manifest.canonical_json().encode("utf-8") != raw:
            raise ValueError("Source Manifest JSON is not canonical")
        return manifest

    def content_descriptor(self, *, project_id: str) -> SnapshotCASDescriptor:
        return SnapshotCASDescriptor(
            project_id=project_id,
            snapshot_digest=self.snapshot_digest,
            manifest_digest=self.manifest_digest,
            blobs=tuple(
                SnapshotBlobMetadata(
                    relative_path=entry.path.relative_path or "",
                    blob_digest=entry.sha256 or "",
                    size=entry.size or 0,
                    mode=entry.mode,
                    object_type=(
                        SnapshotBlobObjectType.SYMLINK
                        if entry.object_type is SourceManifestObjectType.SYMLINK
                        else SnapshotBlobObjectType.REGULAR_FILE
                    ),
                )
                for entry in self.entries
                if entry.decision is SourceCaptureDecision.INCLUDED
            ),
        )

    def manifest_descriptor(self, *, project_id: str) -> SnapshotCASDescriptor:
        content = self.canonical_json().encode("utf-8")
        return SnapshotCASDescriptor(
            project_id=project_id,
            snapshot_digest=self.snapshot_digest,
            manifest_digest=self.manifest_digest,
            blobs=(
                SnapshotBlobMetadata(
                    relative_path=SOURCE_MANIFEST_BLOB_NAME,
                    blob_digest=self.manifest_blob_digest,
                    size=len(content),
                    mode=0o100644,
                ),
            ),
        )


@dataclass(frozen=True, slots=True)
class PublishedSourceSnapshot:
    snapshot_digest: str
    manifest_digest: str
    tree_digest: str
    working_tree_digest: str | None
    content_storage_key: str = field(repr=False)
    manifest_storage_key: str = field(repr=False)
    file_count: int = 0
    total_bytes: int = 0
    content_reused: bool = False
    manifest_reused: bool = False


def publish_source_manifest(
    *,
    project_id: str,
    manifest: SourceManifest,
    staging_root: Path,
    snapshot_store: SnapshotStore,
    temporary_root: Path,
) -> PublishedSourceSnapshot:
    """Publish independently verified source bytes and Manifest into owner-bound CAS."""

    if not isinstance(project_id, str) or _ID_PATTERN.fullmatch(project_id) is None:
        raise ValueError("project_id is invalid")
    if not isinstance(staging_root, Path) or not staging_root.is_absolute():
        raise ValueError("materialized staging root must be absolute")
    if not isinstance(temporary_root, Path) or not temporary_root.is_absolute():
        raise ValueError("materializer temporary root must be absolute")
    temporary_root.mkdir(parents=True, exist_ok=True)
    content_tree = snapshot_store.put_staged_tree(
        SnapshotStagedTree(
            root=staging_root,
            descriptor=manifest.content_descriptor(project_id=project_id),
        )
    )
    manifest_root = Path(tempfile.mkdtemp(prefix="source-manifest-", dir=temporary_root))
    try:
        manifest_path = manifest_root / SOURCE_MANIFEST_BLOB_NAME
        descriptor = os.open(
            manifest_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
            0o600,
        )
        try:
            content = manifest.canonical_json().encode("utf-8")
            written = 0
            while written < len(content):
                written += os.write(descriptor, content[written:])
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        manifest_tree = snapshot_store.put_staged_tree(
            SnapshotStagedTree(
                root=manifest_root,
                descriptor=manifest.manifest_descriptor(project_id=project_id),
            )
        )
    finally:
        shutil.rmtree(manifest_root, ignore_errors=False)
    return _published_snapshot(manifest, content_tree, manifest_tree)


def _published_snapshot(
    manifest: SourceManifest,
    content_tree: StoredSnapshotTree,
    manifest_tree: StoredSnapshotTree,
) -> PublishedSourceSnapshot:
    return PublishedSourceSnapshot(
        snapshot_digest=manifest.snapshot_digest,
        manifest_digest=manifest.manifest_digest,
        tree_digest=manifest.tree_digest,
        working_tree_digest=manifest.working_tree_digest,
        content_storage_key=content_tree.content_storage_key,
        manifest_storage_key=manifest_tree.content_storage_key,
        file_count=manifest.file_count,
        total_bytes=manifest.total_bytes,
        content_reused=content_tree.reused,
        manifest_reused=manifest_tree.reused,
    )


__all__ = [
    "SOURCE_CAPTURE_POLICY_SCHEMA_VERSION",
    "SOURCE_MANIFEST_BLOB_NAME",
    "SOURCE_MANIFEST_SCHEMA_VERSION",
    "SOURCE_MATERIALIZER_SCHEMA_VERSION",
    "PublishedSourceSnapshot",
    "SourceCaptureDecision",
    "SourceCapturePolicy",
    "SourceCaptureReason",
    "SourceClassification",
    "SourceManifest",
    "SourceManifestEntry",
    "SourceManifestObjectType",
    "SourceManifestOrigin",
    "SourceManifestPath",
    "SourceManifestSourceKind",
    "publish_source_manifest",
]
