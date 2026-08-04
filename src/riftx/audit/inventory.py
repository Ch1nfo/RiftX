"""Deterministic file Inventory derived from one sealed Source Manifest."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from riftx.domain import (
    AuditRiskTier,
    AuditScopeKind,
    AuditScopeUnit,
    SourceSnapshot,
)

from .snapshot import (
    SnapshotBlobObjectType,
    SnapshotCASBinding,
    SnapshotStore,
    SnapshotStoreError,
    SnapshotStoreFailure,
)
from .source_manifest import (
    SOURCE_MANIFEST_BLOB_NAME,
    SourceCaptureDecision,
    SourceClassification,
    SourceManifest,
    SourceManifestEntry,
    SourceManifestObjectType,
)

FILE_INVENTORY_SCHEMA_VERSION = "riftx.file-inventory/v1"
FILE_SCOPE_KEY_SCHEMA_VERSION = "riftx.file-scope-key/v1"
DEFAULT_FILE_INVENTORY_POLICY_VERSION = "riftx.file-inventory-policy/default-v1"
DEFAULT_SCOPE_ANALYSES = ("static_rules",)
MAX_SOURCE_MANIFEST_BYTES = 256 * 1024 * 1024
_READ_CHUNK_BYTES = 64 * 1024


class FileInventoryDecision(StrEnum):
    INCLUDED = "included"
    EXCLUDED = "excluded"
    SKIPPED = "skipped"


class FileInventoryReason(StrEnum):
    INCLUDED = "included"
    GENERATED_EXCLUDED = "generated_excluded"
    VENDOR_EXCLUDED = "vendor_excluded"
    UNSUPPORTED_OBJECT_TYPE = "unsupported_object_type"


class FileInventoryFailure(StrEnum):
    REQUEST_INVALID = "audit_file_inventory_request_invalid"
    OWNER_MISMATCH = "audit_file_inventory_owner_mismatch"
    MANIFEST_INTEGRITY = "audit_file_inventory_manifest_integrity"
    STORAGE_UNAVAILABLE = "audit_file_inventory_storage_unavailable"


class FileInventoryError(RuntimeError):
    """Stable, path-free Inventory failure."""

    def __init__(self, failure: FileInventoryFailure) -> None:
        super().__init__(failure.value)
        self.failure = failure


@dataclass(frozen=True, slots=True)
class FileInventoryEntry:
    path_digest: str
    relative_path: str | None
    object_type: SourceManifestObjectType
    language: str
    category: SourceClassification
    size: int | None
    blob_digest: str | None
    decision: FileInventoryDecision
    reason: str

    def canonical_payload(self) -> dict[str, object]:
        return {
            "blob_digest": self.blob_digest,
            "category": self.category.value,
            "decision": self.decision.value,
            "language": self.language,
            "object_type": self.object_type.value,
            "path_digest": self.path_digest,
            "reason": self.reason,
            "relative_path": self.relative_path,
            "size": self.size,
        }


@dataclass(frozen=True, slots=True)
class FileInventoryBreakdown:
    key: str
    files: int
    bytes: int


@dataclass(frozen=True, slots=True)
class FileInventoryStatistics:
    total_files: int
    total_known_bytes: int
    included_files: int
    included_bytes: int
    excluded_files: int
    excluded_bytes: int
    skipped_files: int
    skipped_bytes: int
    by_language: tuple[FileInventoryBreakdown, ...]
    by_category: tuple[FileInventoryBreakdown, ...]


@dataclass(frozen=True, slots=True)
class FileInventory:
    manifest_digest: str
    entries: tuple[FileInventoryEntry, ...]
    statistics: FileInventoryStatistics
    inventory_digest: str
    policy_version: str = DEFAULT_FILE_INVENTORY_POLICY_VERSION
    schema_version: str = field(default=FILE_INVENTORY_SCHEMA_VERSION, init=False)

    def included_entries(self) -> tuple[FileInventoryEntry, ...]:
        return tuple(
            entry
            for entry in self.entries
            if entry.decision is FileInventoryDecision.INCLUDED
        )


def load_source_manifest(
    store: SnapshotStore,
    snapshot: SourceSnapshot,
) -> SourceManifest:
    """Load and validate the owner-bound canonical Manifest for a sealed Snapshot."""

    if not isinstance(snapshot, SourceSnapshot) or not all(
        callable(getattr(store, name, None)) for name in ("describe", "open_blob")
    ):
        raise FileInventoryError(FileInventoryFailure.REQUEST_INVALID)
    binding = SnapshotCASBinding(
        project_id=snapshot.project_id,
        snapshot_digest=snapshot.snapshot_digest,
        manifest_digest=snapshot.manifest_digest,
    )
    try:
        descriptor = store.describe(binding, snapshot.manifest_storage_key)
    except SnapshotStoreError as exc:
        raise FileInventoryError(_map_store_failure(exc.failure)) from exc
    if (
        descriptor.file_count != 1
        or descriptor.total_bytes > MAX_SOURCE_MANIFEST_BYTES
        or descriptor.blobs[0].relative_path != SOURCE_MANIFEST_BLOB_NAME
        or descriptor.blobs[0].object_type is not SnapshotBlobObjectType.REGULAR_FILE
    ):
        raise FileInventoryError(FileInventoryFailure.MANIFEST_INTEGRITY)
    metadata = descriptor.blobs[0]
    try:
        reader = store.open_blob(
            binding,
            snapshot.manifest_storage_key,
            SOURCE_MANIFEST_BLOB_NAME,
            metadata.blob_digest,
            max_bytes=MAX_SOURCE_MANIFEST_BYTES,
        )
    except SnapshotStoreError as exc:
        raise FileInventoryError(_map_store_failure(exc.failure)) from exc
    try:
        content = bytearray()
        try:
            while chunk := reader.read(_READ_CHUNK_BYTES):
                content.extend(chunk)
                if len(content) > metadata.size:
                    raise FileInventoryError(FileInventoryFailure.MANIFEST_INTEGRITY)
            reader.verify_complete()
        except SnapshotStoreError as exc:
            raise FileInventoryError(_map_store_failure(exc.failure)) from exc
    finally:
        reader.close()
    if len(content) != metadata.size:
        raise FileInventoryError(FileInventoryFailure.MANIFEST_INTEGRITY)
    try:
        manifest = SourceManifest.from_json(bytes(content))
    except (TypeError, ValueError) as exc:
        raise FileInventoryError(FileInventoryFailure.MANIFEST_INTEGRITY) from exc
    if (
        manifest.manifest_digest != snapshot.manifest_digest
        or manifest.snapshot_digest != snapshot.snapshot_digest
        or manifest.source_kind.value != snapshot.source_kind.value
        or manifest.commit_sha != snapshot.commit_sha
        or manifest.working_tree_digest != snapshot.working_tree_digest
        or manifest.tree_digest != snapshot.tree_digest
        or manifest.capture_policy_digest != snapshot.capture_policy_digest
        or manifest.materializer_schema_version != snapshot.materializer_schema_version
        or manifest.file_count != snapshot.file_count
        or manifest.total_bytes != snapshot.total_bytes
    ):
        raise FileInventoryError(FileInventoryFailure.MANIFEST_INTEGRITY)
    return manifest


def build_file_inventory(manifest: SourceManifest) -> FileInventory:
    """Project one validated Manifest into the minimal local-static audit Inventory."""

    if not isinstance(manifest, SourceManifest):
        raise FileInventoryError(FileInventoryFailure.REQUEST_INVALID)
    entries = tuple(_inventory_entry(entry) for entry in manifest.entries)
    statistics = _statistics(entries)
    payload = {
        "entries": [entry.canonical_payload() for entry in entries],
        "manifest_digest": manifest.manifest_digest,
        "policy_version": DEFAULT_FILE_INVENTORY_POLICY_VERSION,
        "schema_version": FILE_INVENTORY_SCHEMA_VERSION,
        "statistics": _statistics_payload(statistics),
    }
    return FileInventory(
        manifest_digest=manifest.manifest_digest,
        entries=entries,
        statistics=statistics,
        inventory_digest=_domain_digest(FILE_INVENTORY_SCHEMA_VERSION, payload),
    )


def build_file_scope_units(
    inventory: FileInventory,
    *,
    audit_id: str,
    snapshot_id: str,
    created_at: datetime,
) -> tuple[AuditScopeUnit, ...]:
    """Create deterministic file Scope Units for included Inventory entries only."""

    if not isinstance(inventory, FileInventory):
        raise FileInventoryError(FileInventoryFailure.REQUEST_INVALID)
    scopes: list[AuditScopeUnit] = []
    for entry in inventory.included_entries():
        if entry.relative_path is None or entry.blob_digest is None:
            raise FileInventoryError(FileInventoryFailure.MANIFEST_INTEGRITY)
        stable_key = _domain_digest(
            FILE_SCOPE_KEY_SCHEMA_VERSION,
            {
                "audit_id": audit_id,
                "blob_digest": entry.blob_digest,
                "inventory_digest": inventory.inventory_digest,
                "relative_path": entry.relative_path,
                "schema_version": FILE_SCOPE_KEY_SCHEMA_VERSION,
                "snapshot_id": snapshot_id,
            },
        )
        scopes.append(
            AuditScopeUnit(
                id=f"scope-{stable_key}",
                audit_id=audit_id,
                snapshot_id=snapshot_id,
                kind=AuditScopeKind.FILE,
                relative_path=entry.relative_path,
                blob_digest=entry.blob_digest,
                risk_tier=AuditRiskTier.LOW,
                required_analyses=DEFAULT_SCOPE_ANALYSES,
                stable_key=stable_key,
                created_at=created_at,
                updated_at=created_at,
            )
        )
    return tuple(scopes)


def _inventory_entry(entry: SourceManifestEntry) -> FileInventoryEntry:
    decision: FileInventoryDecision
    reason: str
    if entry.decision is SourceCaptureDecision.EXCLUDED:
        decision = FileInventoryDecision.EXCLUDED
        reason = entry.reason.value
    elif entry.decision is SourceCaptureDecision.DEFERRED:
        decision = FileInventoryDecision.SKIPPED
        reason = entry.reason.value
    elif entry.classification is SourceClassification.GENERATED:
        decision = FileInventoryDecision.EXCLUDED
        reason = FileInventoryReason.GENERATED_EXCLUDED.value
    elif entry.classification is SourceClassification.VENDOR:
        decision = FileInventoryDecision.EXCLUDED
        reason = FileInventoryReason.VENDOR_EXCLUDED.value
    elif entry.object_type is not SourceManifestObjectType.REGULAR_FILE:
        decision = FileInventoryDecision.SKIPPED
        reason = FileInventoryReason.UNSUPPORTED_OBJECT_TYPE.value
    else:
        decision = FileInventoryDecision.INCLUDED
        reason = FileInventoryReason.INCLUDED.value
    return FileInventoryEntry(
        path_digest=entry.path.path_digest,
        relative_path=entry.path.relative_path,
        object_type=entry.object_type,
        language=entry.language,
        category=entry.classification,
        size=entry.size,
        blob_digest=entry.sha256,
        decision=decision,
        reason=reason,
    )


def _statistics(entries: tuple[FileInventoryEntry, ...]) -> FileInventoryStatistics:
    by_decision: dict[FileInventoryDecision, tuple[int, int]] = {
        decision: (0, 0) for decision in FileInventoryDecision
    }
    language: dict[str, tuple[int, int]] = {}
    category: dict[str, tuple[int, int]] = {}
    for entry in entries:
        size = entry.size or 0
        files, byte_count = by_decision[entry.decision]
        by_decision[entry.decision] = (files + 1, byte_count + size)
        _increment_breakdown(language, entry.language, size)
        _increment_breakdown(category, entry.category.value, size)
    included = by_decision[FileInventoryDecision.INCLUDED]
    excluded = by_decision[FileInventoryDecision.EXCLUDED]
    skipped = by_decision[FileInventoryDecision.SKIPPED]
    return FileInventoryStatistics(
        total_files=len(entries),
        total_known_bytes=sum(entry.size or 0 for entry in entries),
        included_files=included[0],
        included_bytes=included[1],
        excluded_files=excluded[0],
        excluded_bytes=excluded[1],
        skipped_files=skipped[0],
        skipped_bytes=skipped[1],
        by_language=_breakdowns(language),
        by_category=_breakdowns(category),
    )


def _increment_breakdown(values: dict[str, tuple[int, int]], key: str, size: int) -> None:
    files, byte_count = values.get(key, (0, 0))
    values[key] = (files + 1, byte_count + size)


def _breakdowns(values: dict[str, tuple[int, int]]) -> tuple[FileInventoryBreakdown, ...]:
    return tuple(
        FileInventoryBreakdown(key=key, files=files, bytes=byte_count)
        for key, (files, byte_count) in sorted(values.items())
    )


def _statistics_payload(value: FileInventoryStatistics) -> dict[str, object]:
    return {
        "by_category": [
            {"bytes": item.bytes, "files": item.files, "key": item.key}
            for item in value.by_category
        ],
        "by_language": [
            {"bytes": item.bytes, "files": item.files, "key": item.key}
            for item in value.by_language
        ],
        "excluded_bytes": value.excluded_bytes,
        "excluded_files": value.excluded_files,
        "included_bytes": value.included_bytes,
        "included_files": value.included_files,
        "skipped_bytes": value.skipped_bytes,
        "skipped_files": value.skipped_files,
        "total_files": value.total_files,
        "total_known_bytes": value.total_known_bytes,
    }


def _domain_digest(domain: str, payload: object) -> str:
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(domain.encode("ascii") + b"\0" + canonical).hexdigest()


def _map_store_failure(failure: SnapshotStoreFailure) -> FileInventoryFailure:
    if failure is SnapshotStoreFailure.OWNER_MISMATCH:
        return FileInventoryFailure.OWNER_MISMATCH
    if failure in {
        SnapshotStoreFailure.BLOB_MISSING,
        SnapshotStoreFailure.MANIFEST_MISMATCH,
        SnapshotStoreFailure.SIZE_LIMIT_EXCEEDED,
        SnapshotStoreFailure.STORAGE_INTEGRITY,
        SnapshotStoreFailure.STORAGE_MISSING,
    }:
        return FileInventoryFailure.MANIFEST_INTEGRITY
    if failure is SnapshotStoreFailure.REQUEST_INVALID:
        return FileInventoryFailure.REQUEST_INVALID
    return FileInventoryFailure.STORAGE_UNAVAILABLE


__all__ = [
    "DEFAULT_FILE_INVENTORY_POLICY_VERSION",
    "DEFAULT_SCOPE_ANALYSES",
    "FILE_INVENTORY_SCHEMA_VERSION",
    "FILE_SCOPE_KEY_SCHEMA_VERSION",
    "FileInventory",
    "FileInventoryBreakdown",
    "FileInventoryDecision",
    "FileInventoryEntry",
    "FileInventoryError",
    "FileInventoryFailure",
    "FileInventoryReason",
    "FileInventoryStatistics",
    "build_file_inventory",
    "build_file_scope_units",
    "load_source_manifest",
]
