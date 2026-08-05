from __future__ import annotations

import hashlib
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest

from riftx.audit import (
    LOCAL_SNAPSHOT_VIEW_SCHEMA_VERSION,
    LocalSnapshotStore,
    LocalSnapshotViewError,
    LocalSnapshotViewFailure,
    SnapshotBlobMetadata,
    SnapshotBlobObjectType,
    SnapshotCASBinding,
    SnapshotCASDescriptor,
    SnapshotStagedTree,
    open_local_snapshot_view,
    parse_snapshot_content_storage_key,
)


def _digest(content: bytes | str) -> str:
    value = content.encode("utf-8") if isinstance(content, str) else content
    return hashlib.sha256(value).hexdigest()


def _stored_snapshot(tmp_path: Path):
    source = tmp_path / "staged"
    source.mkdir(parents=True)
    (source / "a.txt").write_text("安全\n", encoding="utf-8")
    (source / "binary.dat").write_bytes(b"\xff\x00\x80")
    (source / "z.py").write_text("print('safe')\n", encoding="utf-8")
    os.symlink("a.txt", source / "link")
    blobs = tuple(
        sorted(
            (
                SnapshotBlobMetadata(
                    relative_path="a.txt",
                    blob_digest=_digest("安全\n"),
                    size=len("安全\n".encode()),
                    mode=0o100644,
                ),
                SnapshotBlobMetadata(
                    relative_path="binary.dat",
                    blob_digest=_digest(b"\xff\x00\x80"),
                    size=3,
                    mode=0o100644,
                ),
                SnapshotBlobMetadata(
                    relative_path="link",
                    blob_digest=_digest("a.txt"),
                    size=len(b"a.txt"),
                    mode=0o120000,
                    object_type=SnapshotBlobObjectType.SYMLINK,
                ),
                SnapshotBlobMetadata(
                    relative_path="z.py",
                    blob_digest=_digest("print('safe')\n"),
                    size=len(b"print('safe')\n"),
                    mode=0o100644,
                ),
            ),
            key=lambda blob: blob.relative_path,
        )
    )
    descriptor = SnapshotCASDescriptor(
        project_id="project-1",
        snapshot_digest=_digest("snapshot-1"),
        manifest_digest=_digest("manifest-1"),
        blobs=blobs,
    )
    store = LocalSnapshotStore(
        tmp_path / "cas",
        max_blob_bytes=1024,
        max_tree_bytes=4096,
    )
    stored = store.put_staged_tree(SnapshotStagedTree(root=source, descriptor=descriptor))
    binding = SnapshotCASBinding(
        project_id=descriptor.project_id,
        snapshot_digest=descriptor.snapshot_digest,
        manifest_digest=descriptor.manifest_digest,
    )
    return store, stored, descriptor, binding


def _open_view(tmp_path: Path, *, total_bytes: int = 4096, file_bytes: int = 1024):
    store, stored, descriptor, binding = _stored_snapshot(tmp_path)
    view = open_local_snapshot_view(
        store,
        binding=binding,
        content_storage_key=stored.content_storage_key,
        expected_descriptor_digest=stored.descriptor_digest,
        max_file_read_bytes=file_bytes,
        max_total_read_bytes=total_bytes,
        max_text_characters=1024,
    )
    return view, store, stored, descriptor, binding


def test_local_snapshot_view_is_owner_bound_sorted_and_locator_free(
    tmp_path: Path,
) -> None:
    view, store, stored, descriptor, binding = _open_view(tmp_path)
    with view:
        assert view.summary.schema_version == LOCAL_SNAPSHOT_VIEW_SCHEMA_VERSION
        assert view.summary.descriptor_digest == stored.descriptor_digest
        assert view.summary.file_count == 4
        assert view.summary.total_bytes == descriptor.total_bytes
        assert tuple(entry.relative_path for entry in view.entries()) == (
            "a.txt",
            "binary.dat",
            "link",
            "z.py",
        )
        assert all(not entry.relative_path.startswith("/") for entry in view.entries())
        assert not hasattr(view.summary, "content_storage_key")
        assert stored.content_storage_key not in repr(view)
        assert str(store.root) not in repr(view)

        replay = open_local_snapshot_view(
            store,
            binding=binding,
            content_storage_key=stored.content_storage_key,
            expected_descriptor_digest=stored.descriptor_digest,
            max_file_read_bytes=1024,
            max_total_read_bytes=4096,
            max_text_characters=1024,
        )
        try:
            assert replay.summary.view_digest == view.summary.view_digest
        finally:
            replay.close()


def test_local_snapshot_view_reads_bounded_bytes_text_and_symlink_payload(
    tmp_path: Path,
) -> None:
    view, _, _, _, _ = _open_view(tmp_path)
    start = view.remaining_read_bytes

    assert view.read_text("a.txt", max_bytes=32) == "安全\n"
    assert view.read_bytes("link", max_bytes=16) == b"a.txt"
    assert view.remaining_read_bytes == start - len("安全\n".encode()) - len(b"a.txt")

    before = view.remaining_read_bytes
    with pytest.raises(LocalSnapshotViewError) as link_text:
        view.read_text("link", max_bytes=16)
    assert link_text.value.failure is LocalSnapshotViewFailure.ENTRY_TYPE_UNSUPPORTED
    assert view.remaining_read_bytes == before


def test_local_snapshot_view_rejects_owner_descriptor_and_entry_mismatch(
    tmp_path: Path,
) -> None:
    _, store, stored, _, binding = _open_view(tmp_path)
    foreign = replace(binding, project_id="project-2")
    with pytest.raises(LocalSnapshotViewError) as owner_error:
        open_local_snapshot_view(
            store,
            binding=foreign,
            content_storage_key=stored.content_storage_key,
            expected_descriptor_digest=stored.descriptor_digest,
            max_file_read_bytes=1024,
            max_total_read_bytes=4096,
            max_text_characters=1024,
        )
    assert owner_error.value.failure is LocalSnapshotViewFailure.OWNER_MISMATCH

    with pytest.raises(LocalSnapshotViewError) as descriptor_error:
        open_local_snapshot_view(
            store,
            binding=binding,
            content_storage_key=stored.content_storage_key,
            expected_descriptor_digest="0" * 64,
            max_file_read_bytes=1024,
            max_total_read_bytes=4096,
            max_text_characters=1024,
        )
    assert descriptor_error.value.failure is LocalSnapshotViewFailure.DESCRIPTOR_MISMATCH

    view = open_local_snapshot_view(
        store,
        binding=binding,
        content_storage_key=stored.content_storage_key,
        expected_descriptor_digest=stored.descriptor_digest,
        max_file_read_bytes=1024,
        max_total_read_bytes=4096,
        max_text_characters=1024,
    )
    with pytest.raises(LocalSnapshotViewError) as missing:
        view.read_bytes("missing.py", max_bytes=32)
    assert missing.value.failure is LocalSnapshotViewFailure.ENTRY_MISSING


def test_local_snapshot_view_enforces_file_total_and_character_budgets(
    tmp_path: Path,
) -> None:
    view, _, _, _, _ = _open_view(tmp_path, total_bytes=8, file_bytes=8)
    initial = view.remaining_read_bytes

    with pytest.raises(LocalSnapshotViewError) as file_limit:
        view.read_bytes("z.py", max_bytes=1024)
    assert file_limit.value.failure is LocalSnapshotViewFailure.SIZE_LIMIT_EXCEEDED
    assert view.remaining_read_bytes == initial

    assert view.read_bytes("link", max_bytes=8) == b"a.txt"
    with pytest.raises(LocalSnapshotViewError) as total_limit:
        view.read_bytes("a.txt", max_bytes=8)
    assert total_limit.value.failure is LocalSnapshotViewFailure.SIZE_LIMIT_EXCEEDED

    text_view, _, _, _, _ = _open_view(tmp_path / "text")
    with pytest.raises(LocalSnapshotViewError) as character_limit:
        text_view.read_text("a.txt", max_bytes=32, max_characters=2)
    assert character_limit.value.failure is LocalSnapshotViewFailure.SIZE_LIMIT_EXCEEDED


def test_local_snapshot_view_rejects_invalid_utf8_and_consumes_read_budget(
    tmp_path: Path,
) -> None:
    view, _, _, _, _ = _open_view(tmp_path)
    before = view.remaining_read_bytes

    with pytest.raises(LocalSnapshotViewError) as captured:
        view.read_text("binary.dat", max_bytes=16)

    assert captured.value.failure is LocalSnapshotViewFailure.TEXT_DECODE_FAILED
    assert view.remaining_read_bytes == before - 3


def test_local_snapshot_view_concurrent_reads_cannot_overspend_budget(
    tmp_path: Path,
) -> None:
    view, _, _, _, _ = _open_view(tmp_path, total_bytes=5, file_bytes=16)

    def read(path: str) -> bytes | LocalSnapshotViewFailure:
        try:
            return view.read_bytes(path, max_bytes=16)
        except LocalSnapshotViewError as exc:
            return exc.failure

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = tuple(executor.map(read, ("link", "binary.dat")))

    successful = [value for value in outcomes if isinstance(value, bytes)]
    failures = [value for value in outcomes if isinstance(value, LocalSnapshotViewFailure)]
    assert len(successful) == 1
    assert failures == [LocalSnapshotViewFailure.SIZE_LIMIT_EXCEEDED]
    assert view.remaining_read_bytes == 5 - len(successful[0])


def test_local_snapshot_view_detects_late_store_corruption_and_restores_budget(
    tmp_path: Path,
) -> None:
    view, store, stored, _, _ = _open_view(tmp_path)
    object_digest = parse_snapshot_content_storage_key(stored.content_storage_key)
    content = store.root / "objects" / object_digest[:2] / object_digest / "a.txt"
    os.chmod(content, 0o640)
    content.write_bytes(b"tampered")
    before = view.remaining_read_bytes

    with pytest.raises(LocalSnapshotViewError) as captured:
        view.read_bytes("a.txt", max_bytes=32)

    assert captured.value.failure is LocalSnapshotViewFailure.SNAPSHOT_INTEGRITY
    assert view.remaining_read_bytes == before


def test_local_snapshot_view_close_revokes_entry_and_content_reads(
    tmp_path: Path,
) -> None:
    view, _, _, _, _ = _open_view(tmp_path)
    view.close()

    assert view.closed is True
    with pytest.raises(LocalSnapshotViewError) as entries_error:
        view.entries()
    assert entries_error.value.failure is LocalSnapshotViewFailure.CLOSED
    with pytest.raises(LocalSnapshotViewError) as read_error:
        view.read_bytes("a.txt", max_bytes=32)
    assert read_error.value.failure is LocalSnapshotViewFailure.CLOSED


@pytest.mark.parametrize(
    "overrides",
    [
        {"expected_descriptor_digest": "invalid"},
        {"max_file_read_bytes": 0},
        {"max_total_read_bytes": True},
        {"max_text_characters": -1},
    ],
)
def test_local_snapshot_view_rejects_invalid_open_requests(
    tmp_path: Path,
    overrides: dict[str, object],
) -> None:
    _, store, stored, _, binding = _open_view(tmp_path)
    arguments: dict[str, object] = {
        "binding": binding,
        "content_storage_key": stored.content_storage_key,
        "expected_descriptor_digest": stored.descriptor_digest,
        "max_file_read_bytes": 1024,
        "max_total_read_bytes": 4096,
        "max_text_characters": 1024,
        **overrides,
    }

    with pytest.raises(LocalSnapshotViewError) as captured:
        open_local_snapshot_view(store, **arguments)  # type: ignore[arg-type]

    assert captured.value.failure is LocalSnapshotViewFailure.REQUEST_INVALID
