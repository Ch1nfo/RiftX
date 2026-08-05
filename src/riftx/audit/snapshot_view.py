"""Owner-bound, bounded local read view over an immutable SnapshotStore object."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from enum import StrEnum
from threading import Lock
from typing import Self

from .snapshot import (
    SnapshotBlobMetadata,
    SnapshotBlobObjectType,
    SnapshotCASBinding,
    SnapshotStore,
    SnapshotStoreError,
    SnapshotStoreFailure,
)

LOCAL_SNAPSHOT_VIEW_SCHEMA_VERSION = "riftx.local-snapshot-view/v1"
_DIGEST_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_READ_CHUNK_BYTES = 64 * 1024


class LocalSnapshotViewFailure(StrEnum):
    """Stable, path-free Local Snapshot View failures."""

    REQUEST_INVALID = "audit_local_snapshot_view_request_invalid"
    CLOSED = "audit_local_snapshot_view_closed"
    OWNER_MISMATCH = "audit_local_snapshot_view_owner_mismatch"
    DESCRIPTOR_MISMATCH = "audit_local_snapshot_view_descriptor_mismatch"
    ENTRY_MISSING = "audit_local_snapshot_view_entry_missing"
    ENTRY_TYPE_UNSUPPORTED = "audit_local_snapshot_view_entry_type_unsupported"
    SIZE_LIMIT_EXCEEDED = "audit_local_snapshot_view_size_limit_exceeded"
    TEXT_DECODE_FAILED = "audit_local_snapshot_view_text_decode_failed"
    SNAPSHOT_INTEGRITY = "audit_local_snapshot_view_snapshot_integrity"
    STORAGE_UNAVAILABLE = "audit_local_snapshot_view_storage_unavailable"


class LocalSnapshotViewError(RuntimeError):
    """Path-free error raised by the bounded Snapshot read view."""

    def __init__(self, failure: LocalSnapshotViewFailure) -> None:
        super().__init__(failure.value)
        self.failure = failure


@dataclass(frozen=True, slots=True)
class LocalSnapshotViewEntry:
    """Public, locator-free metadata for one deterministic Snapshot entry."""

    relative_path: str
    object_type: SnapshotBlobObjectType
    size: int
    mode: int
    content_digest: str

    @classmethod
    def from_metadata(cls, value: SnapshotBlobMetadata) -> LocalSnapshotViewEntry:
        return cls(
            relative_path=value.relative_path,
            object_type=value.object_type,
            size=value.size,
            mode=value.mode,
            content_digest=value.blob_digest,
        )


@dataclass(frozen=True, slots=True)
class LocalSnapshotViewSummary:
    """Stable path-free view metadata safe for internal status projection."""

    schema_version: str
    view_digest: str
    descriptor_digest: str
    file_count: int
    total_bytes: int
    max_file_read_bytes: int
    max_total_read_bytes: int
    max_text_characters: int


class LocalSnapshotView:
    """A closeable, thread-safe read budget over one owner-bound Snapshot."""

    def __init__(
        self,
        *,
        store: SnapshotStore,
        binding: SnapshotCASBinding,
        content_storage_key: str,
        expected_descriptor_digest: str,
        max_file_read_bytes: int,
        max_total_read_bytes: int,
        max_text_characters: int,
    ) -> None:
        _require_positive_int(max_file_read_bytes)
        _require_positive_int(max_total_read_bytes)
        _require_positive_int(max_text_characters)
        if not all(callable(getattr(store, name, None)) for name in ("describe", "open_blob")):
            raise LocalSnapshotViewError(LocalSnapshotViewFailure.REQUEST_INVALID)
        if not isinstance(binding, SnapshotCASBinding):
            raise LocalSnapshotViewError(LocalSnapshotViewFailure.REQUEST_INVALID)
        _require_digest(expected_descriptor_digest)

        try:
            descriptor = store.describe(binding, content_storage_key)
        except SnapshotStoreError as exc:
            raise LocalSnapshotViewError(_map_store_failure(exc.failure)) from exc
        if descriptor.descriptor_digest != expected_descriptor_digest:
            raise LocalSnapshotViewError(LocalSnapshotViewFailure.DESCRIPTOR_MISMATCH)

        self._store = store
        self._binding = binding
        self._content_storage_key: str | None = content_storage_key
        self._entries = tuple(
            LocalSnapshotViewEntry.from_metadata(blob) for blob in descriptor.blobs
        )
        self._metadata = {blob.relative_path: blob for blob in descriptor.blobs}
        self._max_file_read_bytes = max_file_read_bytes
        self._max_total_read_bytes = max_total_read_bytes
        self._max_text_characters = max_text_characters
        self._remaining_read_bytes = max_total_read_bytes
        self._lock = Lock()
        self._summary = LocalSnapshotViewSummary(
            schema_version=LOCAL_SNAPSHOT_VIEW_SCHEMA_VERSION,
            view_digest=_view_digest(
                binding=binding,
                descriptor_digest=descriptor.descriptor_digest,
                max_file_read_bytes=max_file_read_bytes,
                max_total_read_bytes=max_total_read_bytes,
                max_text_characters=max_text_characters,
            ),
            descriptor_digest=descriptor.descriptor_digest,
            file_count=descriptor.file_count,
            total_bytes=descriptor.total_bytes,
            max_file_read_bytes=max_file_read_bytes,
            max_total_read_bytes=max_total_read_bytes,
            max_text_characters=max_text_characters,
        )

    @property
    def summary(self) -> LocalSnapshotViewSummary:
        return self._summary

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._content_storage_key is None

    @property
    def remaining_read_bytes(self) -> int:
        with self._lock:
            return self._remaining_read_bytes

    def entries(self) -> tuple[LocalSnapshotViewEntry, ...]:
        self._require_open()
        return self._entries

    def read_bytes(self, relative_path: str, *, max_bytes: int) -> bytes:
        _require_positive_int(max_bytes)
        metadata = self._entry(relative_path)
        storage_key = self._reserve(metadata.size, max_bytes=max_bytes)
        try:
            try:
                reader = self._store.open_blob(
                    self._binding,
                    storage_key,
                    metadata.relative_path,
                    metadata.blob_digest,
                    max_bytes=min(max_bytes, self._max_file_read_bytes),
                )
            except SnapshotStoreError as exc:
                raise LocalSnapshotViewError(_map_store_failure(exc.failure)) from exc
            try:
                content = bytearray()
                try:
                    while chunk := reader.read(_READ_CHUNK_BYTES):
                        content.extend(chunk)
                        if len(content) > metadata.size:
                            raise LocalSnapshotViewError(
                                LocalSnapshotViewFailure.SNAPSHOT_INTEGRITY
                            )
                    reader.verify_complete()
                except SnapshotStoreError as exc:
                    raise LocalSnapshotViewError(_map_store_failure(exc.failure)) from exc
            finally:
                reader.close()
            if len(content) != metadata.size:
                raise LocalSnapshotViewError(LocalSnapshotViewFailure.SNAPSHOT_INTEGRITY)
            return bytes(content)
        except BaseException:
            self._release_reservation(metadata.size)
            raise

    def read_text(
        self,
        relative_path: str,
        *,
        max_bytes: int,
        max_characters: int | None = None,
    ) -> str:
        metadata = self._entry(relative_path)
        if metadata.object_type is not SnapshotBlobObjectType.REGULAR_FILE:
            raise LocalSnapshotViewError(
                LocalSnapshotViewFailure.ENTRY_TYPE_UNSUPPORTED
            )
        character_limit = (
            self._max_text_characters if max_characters is None else max_characters
        )
        _require_positive_int(character_limit)
        content = self.read_bytes(relative_path, max_bytes=max_bytes)
        try:
            text = content.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise LocalSnapshotViewError(LocalSnapshotViewFailure.TEXT_DECODE_FAILED) from exc
        if len(text) > min(character_limit, self._max_text_characters):
            raise LocalSnapshotViewError(LocalSnapshotViewFailure.SIZE_LIMIT_EXCEEDED)
        return text

    def close(self) -> None:
        with self._lock:
            self._content_storage_key = None

    def __enter__(self) -> Self:
        self._require_open()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def __repr__(self) -> str:
        return (
            "LocalSnapshotView("
            f"view_digest={self._summary.view_digest!r}, "
            f"file_count={self._summary.file_count}, "
            f"total_bytes={self._summary.total_bytes}, "
            f"closed={self.closed})"
        )

    def _entry(self, relative_path: str) -> SnapshotBlobMetadata:
        self._require_open()
        if not isinstance(relative_path, str):
            raise LocalSnapshotViewError(LocalSnapshotViewFailure.REQUEST_INVALID)
        metadata = self._metadata.get(relative_path)
        if metadata is None:
            raise LocalSnapshotViewError(LocalSnapshotViewFailure.ENTRY_MISSING)
        return metadata

    def _reserve(self, size: int, *, max_bytes: int) -> str:
        with self._lock:
            storage_key = self._content_storage_key
            if storage_key is None:
                raise LocalSnapshotViewError(LocalSnapshotViewFailure.CLOSED)
            if (
                size > max_bytes
                or size > self._max_file_read_bytes
                or size > self._remaining_read_bytes
            ):
                raise LocalSnapshotViewError(
                    LocalSnapshotViewFailure.SIZE_LIMIT_EXCEEDED
                )
            self._remaining_read_bytes -= size
            return storage_key

    def _release_reservation(self, size: int) -> None:
        with self._lock:
            self._remaining_read_bytes = min(
                self._max_total_read_bytes,
                self._remaining_read_bytes + size,
            )

    def _require_open(self) -> None:
        with self._lock:
            if self._content_storage_key is None:
                raise LocalSnapshotViewError(LocalSnapshotViewFailure.CLOSED)


def open_local_snapshot_view(
    store: SnapshotStore,
    *,
    binding: SnapshotCASBinding,
    content_storage_key: str,
    expected_descriptor_digest: str,
    max_file_read_bytes: int,
    max_total_read_bytes: int,
    max_text_characters: int,
) -> LocalSnapshotView:
    """Open one verified view without returning its private locator."""

    return LocalSnapshotView(
        store=store,
        binding=binding,
        content_storage_key=content_storage_key,
        expected_descriptor_digest=expected_descriptor_digest,
        max_file_read_bytes=max_file_read_bytes,
        max_total_read_bytes=max_total_read_bytes,
        max_text_characters=max_text_characters,
    )


def _require_positive_int(value: object) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise LocalSnapshotViewError(LocalSnapshotViewFailure.REQUEST_INVALID)


def _require_digest(value: object) -> None:
    if not isinstance(value, str) or _DIGEST_PATTERN.fullmatch(value) is None:
        raise LocalSnapshotViewError(LocalSnapshotViewFailure.REQUEST_INVALID)


def _view_digest(
    *,
    binding: SnapshotCASBinding,
    descriptor_digest: str,
    max_file_read_bytes: int,
    max_total_read_bytes: int,
    max_text_characters: int,
) -> str:
    payload = {
        "descriptor_digest": descriptor_digest,
        "manifest_digest": binding.manifest_digest,
        "max_file_read_bytes": max_file_read_bytes,
        "max_text_characters": max_text_characters,
        "max_total_read_bytes": max_total_read_bytes,
        "project_id": binding.project_id,
        "schema_version": LOCAL_SNAPSHOT_VIEW_SCHEMA_VERSION,
        "snapshot_digest": binding.snapshot_digest,
    }
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")
    return hashlib.sha256(
        LOCAL_SNAPSHOT_VIEW_SCHEMA_VERSION.encode("ascii") + b"\0" + encoded
    ).hexdigest()


def _map_store_failure(failure: SnapshotStoreFailure) -> LocalSnapshotViewFailure:
    if failure is SnapshotStoreFailure.OWNER_MISMATCH:
        return LocalSnapshotViewFailure.OWNER_MISMATCH
    if failure is SnapshotStoreFailure.SIZE_LIMIT_EXCEEDED:
        return LocalSnapshotViewFailure.SIZE_LIMIT_EXCEEDED
    if failure is SnapshotStoreFailure.REQUEST_INVALID:
        return LocalSnapshotViewFailure.REQUEST_INVALID
    if failure in {
        SnapshotStoreFailure.STORAGE_MISSING,
        SnapshotStoreFailure.STORAGE_INTEGRITY,
        SnapshotStoreFailure.MANIFEST_MISMATCH,
        SnapshotStoreFailure.BLOB_MISSING,
    }:
        return LocalSnapshotViewFailure.SNAPSHOT_INTEGRITY
    return LocalSnapshotViewFailure.STORAGE_UNAVAILABLE


__all__ = [
    "LOCAL_SNAPSHOT_VIEW_SCHEMA_VERSION",
    "LocalSnapshotView",
    "LocalSnapshotViewEntry",
    "LocalSnapshotViewError",
    "LocalSnapshotViewFailure",
    "LocalSnapshotViewSummary",
    "open_local_snapshot_view",
]
