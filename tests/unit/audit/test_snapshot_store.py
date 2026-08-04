from __future__ import annotations

import hashlib
import os
import stat
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from riftx.audit import (
    LocalSnapshotStore,
    SnapshotBlobMetadata,
    SnapshotBlobObjectType,
    SnapshotCASBinding,
    SnapshotCASDescriptor,
    SnapshotStagedTree,
    SnapshotStoreCrash,
    SnapshotStoreError,
    SnapshotStoreFailure,
    parse_snapshot_content_storage_key,
)

NOW = datetime(2026, 8, 4, 9, tzinfo=UTC)


def _digest(content: bytes | str) -> str:
    value = content.encode() if isinstance(content, str) else content
    return hashlib.sha256(value).hexdigest()


def _staged_tree(root: Path, *, project_id: str = "project-1") -> SnapshotStagedTree:
    root.mkdir(parents=True)
    (root / "bin").mkdir()
    (root / "src").mkdir()
    (root / "bin" / "check.sh").write_bytes(b"#!/bin/sh\nexit 0\n")
    os.chmod(root / "bin" / "check.sh", 0o755)
    (root / "src" / "main.py").write_bytes(b"print('safe')\n")
    os.symlink("src/main.py", root / "main-link")
    link_content = b"src/main.py"
    blobs = (
        SnapshotBlobMetadata(
            relative_path="bin/check.sh",
            blob_digest=_digest(b"#!/bin/sh\nexit 0\n"),
            size=len(b"#!/bin/sh\nexit 0\n"),
            mode=0o100755,
        ),
        SnapshotBlobMetadata(
            relative_path="main-link",
            blob_digest=_digest(link_content),
            size=len(link_content),
            mode=0o120000,
            object_type=SnapshotBlobObjectType.SYMLINK,
        ),
        SnapshotBlobMetadata(
            relative_path="src/main.py",
            blob_digest=_digest(b"print('safe')\n"),
            size=len(b"print('safe')\n"),
            mode=0o100644,
        ),
    )
    descriptor = SnapshotCASDescriptor(
        project_id=project_id,
        snapshot_digest=_digest("snapshot"),
        manifest_digest=_digest("manifest"),
        blobs=blobs,
    )
    return SnapshotStagedTree(root=root, descriptor=descriptor)


def _binding(staged: SnapshotStagedTree) -> SnapshotCASBinding:
    descriptor = staged.descriptor
    return SnapshotCASBinding(
        project_id=descriptor.project_id,
        snapshot_digest=descriptor.snapshot_digest,
        manifest_digest=descriptor.manifest_digest,
    )


def _object_path(store: LocalSnapshotStore, key: str) -> Path:
    digest = parse_snapshot_content_storage_key(key)
    return store.root / "objects" / digest[:2] / digest


def test_snapshot_descriptor_is_domain_separated_owner_bound_and_path_safe(
    tmp_path: Path,
) -> None:
    staged = _staged_tree(tmp_path / "private-source")
    foreign = SnapshotCASDescriptor(
        project_id="project-2",
        snapshot_digest=staged.descriptor.snapshot_digest,
        manifest_digest=staged.descriptor.manifest_digest,
        blobs=staged.descriptor.blobs,
    )

    assert staged.descriptor.content_storage_key != foreign.content_storage_key
    assert staged.descriptor.descriptor_digest in staged.descriptor.content_storage_key
    assert str(staged.root) not in repr(staged)
    assert staged.descriptor.canonical_json() == staged.descriptor.canonical_json()

    with pytest.raises(ValueError, match="normalized relative POSIX"):
        replace(staged.descriptor.blobs[0], relative_path="../secret")


def test_local_snapshot_store_seals_reuses_verifies_and_opens_bounded_blobs(
    tmp_path: Path,
) -> None:
    staged = _staged_tree(tmp_path / "source")
    store = LocalSnapshotStore(
        tmp_path / "cas",
        max_blob_bytes=1024,
        max_tree_bytes=4096,
    )

    created = store.put_staged_tree(staged)
    replayed = store.put_staged_tree(staged)

    assert created.reused is False
    assert replayed == replace(created, reused=True)
    integrity = store.verify(_binding(staged), created.content_storage_key)
    assert integrity.valid is True
    assert integrity.file_count == 3
    assert integrity.total_bytes == staged.descriptor.total_bytes

    with store.open_blob(
        _binding(staged),
        created.content_storage_key,
        "src/main.py",
        _digest(b"print('safe')\n"),
        max_bytes=1024,
    ) as reader:
        assert reader.read(5) == b"print"
        assert reader.read(1024) == b"('safe')\n"
        assert reader.read(1) == b""
        reader.verify_complete()

    with store.open_blob(
        _binding(staged),
        created.content_storage_key,
        "main-link",
        _digest(b"src/main.py"),
        max_bytes=1024,
    ) as reader:
        assert reader.read(1024) == b"src/main.py"
        reader.verify_complete()

    object_path = _object_path(store, created.content_storage_key)
    assert object_path.stat().st_mode & 0o222 == 0
    assert (object_path / "src" / "main.py").stat().st_mode & 0o222 == 0
    assert stat.S_ISLNK((object_path / "main-link").lstat().st_mode)


def test_owner_manifest_and_digest_mismatches_fail_without_locator_oracle(
    tmp_path: Path,
) -> None:
    staged = _staged_tree(tmp_path / "source")
    store = LocalSnapshotStore(tmp_path / "cas", max_blob_bytes=1024, max_tree_bytes=4096)
    stored = store.put_staged_tree(staged)

    foreign = replace(_binding(staged), project_id="project-2")
    result = store.verify(foreign, stored.content_storage_key)
    assert result.valid is False
    assert result.failure is SnapshotStoreFailure.OWNER_MISMATCH

    with pytest.raises(SnapshotStoreError) as digest_mismatch:
        store.open_blob(
            _binding(staged),
            stored.content_storage_key,
            "src/main.py",
            _digest("different"),
            max_bytes=1024,
        )
    assert digest_mismatch.value.failure is SnapshotStoreFailure.MANIFEST_MISMATCH


def test_staging_tree_must_be_disjoint_exact_and_within_limits(tmp_path: Path) -> None:
    store = LocalSnapshotStore(tmp_path / "cas", max_blob_bytes=1024, max_tree_bytes=4096)
    overlapping = _staged_tree(store.root / "source")
    with pytest.raises(SnapshotStoreError) as overlap:
        store.put_staged_tree(overlapping)
    assert overlap.value.failure is SnapshotStoreFailure.SOURCE_INTEGRITY

    staged = _staged_tree(tmp_path / "source")
    (staged.root / "undeclared.txt").write_text("not in the descriptor")
    with pytest.raises(SnapshotStoreError) as undeclared:
        store.put_staged_tree(staged)
    assert undeclared.value.failure is SnapshotStoreFailure.SOURCE_INTEGRITY

    bounded = _staged_tree(tmp_path / "bounded")
    small_store = LocalSnapshotStore(
        tmp_path / "small-cas",
        max_blob_bytes=8,
        max_tree_bytes=128,
    )
    with pytest.raises(SnapshotStoreError) as too_large:
        small_store.put_staged_tree(bounded)
    assert too_large.value.failure is SnapshotStoreFailure.SIZE_LIMIT_EXCEEDED


def test_corrupt_or_half_written_objects_are_quarantined_before_retry(
    tmp_path: Path,
) -> None:
    staged = _staged_tree(tmp_path / "source")
    store = LocalSnapshotStore(tmp_path / "cas", max_blob_bytes=1024, max_tree_bytes=4096)
    stored = store.put_staged_tree(staged)
    content = _object_path(store, stored.content_storage_key) / "src" / "main.py"
    os.chmod(content, 0o640)
    content.write_bytes(b"tampered")

    with pytest.raises(SnapshotStoreError) as corrupt:
        store.put_staged_tree(staged)
    assert corrupt.value.failure is SnapshotStoreFailure.STORAGE_INTEGRITY
    assert not _object_path(store, stored.content_storage_key).exists()
    assert len(tuple((store.root / "quarantine").iterdir())) == 1
    assert store.put_staged_tree(staged).reused is False

    second = _staged_tree(tmp_path / "source-2", project_id="project-2")
    half_path = _object_path(store, second.descriptor.content_storage_key)
    half_path.mkdir(parents=True)
    os.chmod(half_path, 0o550)
    with pytest.raises(SnapshotStoreError) as half:
        store.put_staged_tree(second)
    assert half.value.failure is SnapshotStoreFailure.STORAGE_INTEGRITY
    assert len(tuple((store.root / "quarantine").iterdir())) == 2

    third = _staged_tree(tmp_path / "source-3", project_id="project-3")
    linked_path = _object_path(store, third.descriptor.content_storage_key)
    linked_path.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(tmp_path / "foreign-object", linked_path)
    with pytest.raises(SnapshotStoreError) as linked:
        store.put_staged_tree(third)
    assert linked.value.failure is SnapshotStoreFailure.STORAGE_INTEGRITY
    assert not linked_path.exists() and not linked_path.is_symlink()
    assert len(tuple((store.root / "quarantine").iterdir())) == 3


def test_power_loss_before_and_after_publish_is_recoverable(tmp_path: Path) -> None:
    staged = _staged_tree(tmp_path / "source")

    def crash_before_publish(stage: str) -> None:
        if stage == "staging_synced":
            raise SnapshotStoreCrash(stage)

    crashed = LocalSnapshotStore(
        tmp_path / "cas",
        max_blob_bytes=1024,
        max_tree_bytes=4096,
        fault_injector=crash_before_publish,
    )
    with pytest.raises(SnapshotStoreCrash):
        crashed.put_staged_tree(staged)
    assert len(tuple((crashed.root / "staging").iterdir())) == 1

    dry_run = crashed.cleanup_staging_orphans(
        older_than=NOW + timedelta(days=1),
        dry_run=True,
    )
    assert (dry_run.examined, dry_run.eligible, dry_run.removed) == (1, 1, 0)
    cleaned = crashed.cleanup_staging_orphans(
        older_than=NOW + timedelta(days=1),
        dry_run=False,
    )
    assert cleaned.removed == 1
    assert tuple((crashed.root / "staging").iterdir()) == ()

    def crash_after_publish(stage: str) -> None:
        if stage == "published":
            raise SnapshotStoreCrash(stage)

    published = LocalSnapshotStore(
        tmp_path / "cas",
        max_blob_bytes=1024,
        max_tree_bytes=4096,
        fault_injector=crash_after_publish,
    )
    with pytest.raises(SnapshotStoreCrash):
        published.put_staged_tree(staged)
    recovered = LocalSnapshotStore(
        tmp_path / "cas",
        max_blob_bytes=1024,
        max_tree_bytes=4096,
    ).put_staged_tree(staged)
    assert recovered.reused is True


def test_concurrent_identical_capture_publishes_one_object(tmp_path: Path) -> None:
    staged = _staged_tree(tmp_path / "source")
    store = LocalSnapshotStore(tmp_path / "cas", max_blob_bytes=1024, max_tree_bytes=4096)

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = tuple(executor.map(lambda _: store.put_staged_tree(staged), range(2)))

    assert sorted(outcome.reused for outcome in outcomes) == [False, True]
    assert outcomes[0].content_storage_key == outcomes[1].content_storage_key
