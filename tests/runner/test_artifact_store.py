from __future__ import annotations

import os
import stat
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier, Event, Lock
from typing import BinaryIO, cast

import pytest

from riftx.runner import (
    ArtifactContentFailure,
    ArtifactContentStoreError,
    LocalArtifactContentStore,
    RunnerPaths,
)
from riftx.runner import artifact_store as artifact_store_module


def _store(
    tmp_path: Path,
    *,
    max_bytes: int = 1024,
    max_verified_fingerprints: int = 128,
) -> tuple[
    LocalArtifactContentStore,
    RunnerPaths,
    str,
]:
    paths = RunnerPaths(tmp_path / "runner")
    key = paths.artifact_storage_key("run-1", "artifact-1", "evidence.bin")
    return (
        LocalArtifactContentStore(
            paths,
            max_artifact_bytes=max_bytes,
            max_verified_fingerprints=max_verified_fingerprints,
        ),
        paths,
        key,
    )


def test_bytes_snapshot_is_atomic_read_only_and_streamed_from_one_verified_fd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, paths, key = _store(tmp_path)
    content = b"immutable evidence"
    stored = store.snapshot_bytes(content, storage_key=key)
    path = paths.artifact_from_storage_key(key)

    assert path.read_bytes() == content
    assert path.stat().st_mode & 0o777 == 0o444
    assert not (path.parent / ".content.partial").exists()

    original_open = os.open
    final_file_opens = 0

    def counted_open(path_value, flags, *args, **kwargs):
        nonlocal final_file_opens
        if not flags & os.O_DIRECTORY and flags & os.O_RDONLY == os.O_RDONLY:
            final_file_opens += 1
        return original_open(path_value, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", counted_open)
    lease = store.open_verified(
        storage_key=key,
        expected_sha256=stored.sha256,
        expected_size=stored.size,
    )
    assert lease.read(5) + lease.read(1024) == content
    lease.verify_complete()
    lease.close()

    assert lease.closed is True
    assert final_file_opens == 1


def test_artifact_storage_hardens_only_private_artifact_directories(
    tmp_path: Path,
) -> None:
    store, paths, key = _store(tmp_path)
    run_directory = paths.run_directory("run-1")
    artifacts_directory = run_directory / "artifacts"
    artifacts_directory.mkdir(parents=True, mode=0o755)
    run_directory.chmod(0o755)
    artifacts_directory.chmod(0o755)

    store.snapshot_bytes(b"private", storage_key=key)

    content = paths.artifact_from_storage_key(key)
    assert run_directory.stat().st_mode & 0o777 == 0o755
    assert artifacts_directory.stat().st_mode & 0o777 == 0o700
    assert content.parent.stat().st_mode & 0o777 == 0o700


def test_repeated_verified_opens_hash_unchanged_content_only_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, _, key = _store(tmp_path)
    content = b"immutable evidence"
    stored = store.snapshot_bytes(content, storage_key=key)
    original_read = artifact_store_module._read_fd_chunk
    hash_reads = 0

    def count_hash_reads(file_fd: int, max_bytes: int) -> bytes:
        nonlocal hash_reads
        chunk = original_read(file_fd, max_bytes)
        if chunk:
            hash_reads += 1
        return chunk

    monkeypatch.setattr(artifact_store_module, "_read_fd_chunk", count_hash_reads)

    for offset in (0, 2, len(content)):
        with store.open_verified(
            storage_key=key,
            expected_sha256=stored.sha256,
            expected_size=stored.size,
        ) as lease:
            lease.seek(offset)
            if offset < len(content):
                assert lease.read(3) == content[offset : offset + 3]
            lease.verify_unchanged()

    assert hash_reads == 1


@pytest.mark.parametrize("mutation", ["mtime", "inode"])
def test_verified_fingerprint_change_forces_rehash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    store, paths, key = _store(tmp_path)
    content = b"trusted"
    stored = store.snapshot_bytes(content, storage_key=key)
    content_path = paths.artifact_from_storage_key(key)
    original_read = artifact_store_module._read_fd_chunk
    hash_reads = 0

    def count_hash_reads(file_fd: int, max_bytes: int) -> bytes:
        nonlocal hash_reads
        chunk = original_read(file_fd, max_bytes)
        if chunk:
            hash_reads += 1
        return chunk

    monkeypatch.setattr(artifact_store_module, "_read_fd_chunk", count_hash_reads)

    with store.open_verified(
        storage_key=key,
        expected_sha256=stored.sha256,
        expected_size=stored.size,
    ):
        pass

    before = content_path.stat()
    if mutation == "mtime":
        os.utime(
            content_path,
            ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000_000),
        )
    else:
        replacement = content_path.parent / "replacement.bin"
        replacement.write_bytes(content)
        replacement.chmod(0o444)
        os.replace(replacement, content_path)
        assert content_path.stat().st_ino != before.st_ino

    with store.open_verified(
        storage_key=key,
        expected_sha256=stored.sha256,
        expected_size=stored.size,
    ):
        pass

    assert hash_reads == 2


def test_verified_cache_rehashes_changed_and_restored_content(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, paths, key = _store(tmp_path)
    content = b"trusted"
    stored = store.snapshot_bytes(content, storage_key=key)
    content_path = paths.artifact_from_storage_key(key)
    original_read = artifact_store_module._read_fd_chunk
    hash_reads = 0

    def count_hash_reads(file_fd: int, max_bytes: int) -> bytes:
        nonlocal hash_reads
        chunk = original_read(file_fd, max_bytes)
        if chunk:
            hash_reads += 1
        return chunk

    monkeypatch.setattr(artifact_store_module, "_read_fd_chunk", count_hash_reads)

    with store.open_verified(
        storage_key=key,
        expected_sha256=stored.sha256,
        expected_size=stored.size,
    ):
        pass

    initial_stat = content_path.stat()
    content_path.chmod(0o644)
    content_path.write_bytes(b"hostile")
    content_path.chmod(0o444)
    os.utime(
        content_path,
        ns=(initial_stat.st_atime_ns, initial_stat.st_mtime_ns + 1_000_000_000),
    )
    with pytest.raises(ArtifactContentStoreError) as captured:
        store.open_verified(
            storage_key=key,
            expected_sha256=stored.sha256,
            expected_size=stored.size,
        )
    assert captured.value.failure is ArtifactContentFailure.STORAGE_INTEGRITY

    content_path.chmod(0o644)
    content_path.write_bytes(content)
    content_path.chmod(0o444)
    os.utime(
        content_path,
        ns=(initial_stat.st_atime_ns, initial_stat.st_mtime_ns + 2_000_000_000),
    )
    with store.open_verified(
        storage_key=key,
        expected_sha256=stored.sha256,
        expected_size=stored.size,
    ):
        pass
    with store.open_verified(
        storage_key=key,
        expected_sha256=stored.sha256,
        expected_size=stored.size,
    ):
        pass

    assert hash_reads == 3


def test_verified_cache_key_includes_declared_digest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, _, key = _store(tmp_path)
    stored = store.snapshot_bytes(b"trusted", storage_key=key)
    original_read = artifact_store_module._read_fd_chunk
    hash_reads = 0

    def count_hash_reads(file_fd: int, max_bytes: int) -> bytes:
        nonlocal hash_reads
        chunk = original_read(file_fd, max_bytes)
        if chunk:
            hash_reads += 1
        return chunk

    monkeypatch.setattr(artifact_store_module, "_read_fd_chunk", count_hash_reads)

    with store.open_verified(
        storage_key=key,
        expected_sha256=stored.sha256,
        expected_size=stored.size,
    ):
        pass
    with pytest.raises(ArtifactContentStoreError) as captured:
        store.open_verified(
            storage_key=key,
            expected_sha256="0" * 64,
            expected_size=stored.size,
        )
    assert captured.value.failure is ArtifactContentFailure.STORAGE_INTEGRITY
    with store.open_verified(
        storage_key=key,
        expected_sha256=stored.sha256,
        expected_size=stored.size,
    ):
        pass

    assert hash_reads == 2


def test_concurrent_verified_opens_single_flight_full_hash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, _, key = _store(tmp_path)
    content = b"trusted"
    stored = store.snapshot_bytes(content, storage_key=key)
    original_read = artifact_store_module._read_fd_chunk
    workers = 8
    ready = Barrier(workers)
    first_hash_started = Event()
    release_hash = Event()
    counter_lock = Lock()
    hash_reads = 0

    def block_first_hash(file_fd: int, max_bytes: int) -> bytes:
        nonlocal hash_reads
        chunk = original_read(file_fd, max_bytes)
        is_first = False
        if chunk:
            with counter_lock:
                hash_reads += 1
                is_first = hash_reads == 1
        if is_first:
            first_hash_started.set()
            if not release_hash.wait(timeout=5):
                raise AssertionError("timed out waiting to release the first full hash")
        return chunk

    monkeypatch.setattr(artifact_store_module, "_read_fd_chunk", block_first_hash)

    def open_content() -> bytes:
        ready.wait(timeout=5)
        with store.open_verified(
            storage_key=key,
            expected_sha256=stored.sha256,
            expected_size=stored.size,
        ) as lease:
            return lease.read(stored.size)

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(open_content) for _ in range(workers)]
        try:
            assert first_hash_started.wait(timeout=5)
        finally:
            release_hash.set()
        results = [future.result(timeout=5) for future in futures]

    assert results == [content] * workers
    assert hash_reads == 1


def test_verified_fingerprint_cache_is_strict_lru(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, paths, first_key = _store(tmp_path, max_verified_fingerprints=2)
    keys = (
        first_key,
        paths.artifact_storage_key("run-1", "artifact-2", "evidence.bin"),
        paths.artifact_storage_key("run-1", "artifact-3", "evidence.bin"),
    )
    stored = tuple(
        store.snapshot_bytes(f"content-{index}".encode(), storage_key=key)
        for index, key in enumerate(keys)
    )
    original_read = artifact_store_module._read_fd_chunk
    hash_reads = 0

    def count_hash_reads(file_fd: int, max_bytes: int) -> bytes:
        nonlocal hash_reads
        chunk = original_read(file_fd, max_bytes)
        if chunk:
            hash_reads += 1
        return chunk

    monkeypatch.setattr(artifact_store_module, "_read_fd_chunk", count_hash_reads)

    def open_index(index: int) -> None:
        with store.open_verified(
            storage_key=keys[index],
            expected_sha256=stored[index].sha256,
            expected_size=stored[index].size,
        ):
            pass

    open_index(0)
    open_index(1)
    open_index(0)
    open_index(2)
    open_index(0)
    open_index(1)

    assert hash_reads == 4


def test_verified_fingerprint_cache_capacity_must_be_positive(tmp_path: Path) -> None:
    paths = RunnerPaths(tmp_path / "runner")

    with pytest.raises(ValueError, match="max_verified_fingerprints"):
        LocalArtifactContentStore(
            paths,
            max_artifact_bytes=1024,
            max_verified_fingerprints=0,
        )


@pytest.mark.parametrize("value", ["\x00", "\x1f", "\x7f", "\x80", "\x9f", "é", "证据"])
def test_descriptor_path_component_rejects_non_printable_ascii(value: str) -> None:
    with pytest.raises(ValueError, match="unsafe path component"):
        artifact_store_module._validate_component(value)


@pytest.mark.parametrize(
    "value",
    ["artifact 1.bin", "!#$%&'()+,-.;:=@[]^_`{}~"],
)
def test_descriptor_path_component_accepts_safe_printable_ascii(value: str) -> None:
    artifact_store_module._validate_component(value)


@pytest.mark.parametrize("value", ["", ".", "..", "a/b", "a\\b"])
def test_descriptor_path_component_rejects_reserved_values(value: str) -> None:
    with pytest.raises(ValueError, match="unsafe path component"):
        artifact_store_module._validate_component(value)


@pytest.mark.parametrize("kind", ["leaf_symlink", "parent_symlink", "hardlink", "fifo"])
def test_local_file_snapshot_rejects_links_and_special_files(
    tmp_path: Path,
    kind: str,
) -> None:
    store, paths, key = _store(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    regular = workspace / "regular.bin"
    regular.write_bytes(b"evidence")
    source = regular

    if kind == "leaf_symlink":
        source = workspace / "linked.bin"
        source.symlink_to(regular)
    elif kind == "parent_symlink":
        real_parent = workspace / "real-parent"
        real_parent.mkdir()
        (real_parent / "evidence.bin").write_bytes(b"evidence")
        linked_parent = workspace / "linked-parent"
        linked_parent.symlink_to(real_parent, target_is_directory=True)
        source = linked_parent / "evidence.bin"
    elif kind == "hardlink":
        alias = workspace / "alias.bin"
        os.link(regular, alias)
        source = regular
    else:
        source = workspace / "pipe"
        os.mkfifo(source)

    with pytest.raises(ArtifactContentStoreError):
        store.snapshot_file(
            str(source),
            allowed_roots=(workspace,),
            storage_key=key,
        )

    assert not paths.artifact_from_storage_key(key).parent.exists()


def test_local_file_snapshot_rejects_outside_and_oversize_before_staging(
    tmp_path: Path,
) -> None:
    store, paths, key = _store(tmp_path, max_bytes=4)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"x")
    oversize = workspace / "oversize.bin"
    oversize.write_bytes(b"12345")

    with pytest.raises(ArtifactContentStoreError) as outside_error:
        store.snapshot_file(
            str(outside),
            allowed_roots=(workspace,),
            storage_key=key,
        )
    assert outside_error.value.failure is ArtifactContentFailure.SOURCE_OUTSIDE_ROOT

    with pytest.raises(ArtifactContentStoreError) as size_error:
        store.snapshot_file(
            str(oversize),
            allowed_roots=(workspace,),
            storage_key=key,
        )
    assert size_error.value.failure is ArtifactContentFailure.SIZE_LIMIT_EXCEEDED
    assert not paths.artifact_from_storage_key(key).parent.exists()


def test_local_file_snapshot_rejects_parent_components_even_when_they_stay_in_root(
    tmp_path: Path,
) -> None:
    store, paths, key = _store(tmp_path)
    workspace = tmp_path / "workspace"
    nested = workspace / "nested"
    nested.mkdir(parents=True)
    source = workspace / "source.bin"
    source.write_bytes(b"evidence")

    with pytest.raises(ArtifactContentStoreError) as captured:
        store.snapshot_file(
            str(nested / ".." / source.name),
            allowed_roots=(workspace,),
            storage_key=key,
        )

    assert captured.value.failure is ArtifactContentFailure.SOURCE_UNAVAILABLE
    assert not paths.artifact_from_storage_key(key).parent.exists()


def test_local_file_snapshot_accepts_unicode_source_components(
    tmp_path: Path,
) -> None:
    store, paths, key = _store(tmp_path)
    workspace = tmp_path / "工作区"
    nested = workspace / "证据"
    nested.mkdir(parents=True)
    source = nested / "结果.bin"
    source.write_bytes(b"evidence")

    stored = store.snapshot_file(
        str(source),
        allowed_roots=(workspace,),
        storage_key=key,
    )

    assert stored.size == len(b"evidence")
    assert paths.artifact_from_storage_key(key).read_bytes() == b"evidence"


@pytest.mark.parametrize("mutation", ["replace", "grow"])
def test_local_file_snapshot_rejects_concurrent_source_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    store, paths, key = _store(tmp_path, max_bytes=32)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = workspace / "source.bin"
    source.write_bytes(b"first")
    original_read = artifact_store_module._read_fd_chunk
    mutated = False

    def mutate_after_read(file_fd: int, max_bytes: int) -> bytes:
        nonlocal mutated
        chunk = original_read(file_fd, max_bytes)
        if chunk and not mutated:
            mutated = True
            if mutation == "replace":
                replacement = workspace / "replacement.bin"
                replacement.write_bytes(b"other")
                os.replace(replacement, source)
            else:
                with source.open("ab") as writer:
                    writer.write(b"-growth")
        return chunk

    monkeypatch.setattr(artifact_store_module, "_read_fd_chunk", mutate_after_read)

    with pytest.raises(ArtifactContentStoreError) as captured:
        store.snapshot_file(
            str(source),
            allowed_roots=(workspace,),
            storage_key=key,
        )
    assert captured.value.failure is ArtifactContentFailure.SOURCE_CHANGED
    assert not paths.artifact_from_storage_key(key).parent.exists()


def test_local_file_snapshot_rejects_source_truncation_during_copy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, paths, key = _store(tmp_path, max_bytes=4 * 1024 * 1024)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = workspace / "source.bin"
    source.write_bytes(b"x" * (2 * 1024 * 1024))
    original_read = artifact_store_module._read_fd_chunk
    truncated = False

    def truncate_after_first_read(file_fd: int, max_bytes: int) -> bytes:
        nonlocal truncated
        chunk = original_read(file_fd, max_bytes)
        if chunk and not truncated:
            truncated = True
            with source.open("r+b") as writer:
                writer.truncate(len(chunk) // 2)
        return chunk

    monkeypatch.setattr(
        artifact_store_module,
        "_read_fd_chunk",
        truncate_after_first_read,
    )

    with pytest.raises(ArtifactContentStoreError) as captured:
        store.snapshot_file(
            str(source),
            allowed_roots=(workspace,),
            storage_key=key,
        )

    assert captured.value.failure is ArtifactContentFailure.SOURCE_CHANGED
    assert not paths.artifact_from_storage_key(key).parent.exists()


def test_partial_snapshot_failure_removes_staging_and_object_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, paths, key = _store(tmp_path, max_bytes=4 * 1024 * 1024)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = workspace / "source.bin"
    source.write_bytes(b"x" * (2 * 1024 * 1024))
    original_read = artifact_store_module._read_fd_chunk
    calls = 0

    def fail_second_read(file_fd: int, max_bytes: int) -> bytes:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("synthetic read failure")
        return original_read(file_fd, max_bytes)

    monkeypatch.setattr(artifact_store_module, "_read_fd_chunk", fail_second_read)

    with pytest.raises(ArtifactContentStoreError):
        store.snapshot_file(
            str(source),
            allowed_roots=(workspace,),
            storage_key=key,
        )

    assert not paths.artifact_from_storage_key(key).parent.exists()


@pytest.mark.parametrize(
    "fault",
    ["write", "file_fsync", "rename", "directory_fsync"],
)
def test_bytes_snapshot_publish_failure_removes_partial_and_object_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
) -> None:
    store, paths, key = _store(tmp_path)
    content_path = paths.artifact_from_storage_key(key)
    original_fdopen = os.fdopen
    original_fsync = os.fsync

    class _FailingWriter:
        def __init__(self, writer: BinaryIO) -> None:
            self._writer = writer

        def __enter__(self) -> _FailingWriter:
            return self

        def __exit__(self, *_: object) -> None:
            self._writer.close()

        def close(self) -> None:
            self._writer.close()

        def write(self, content: bytes) -> int:
            self._writer.write(content[:1])
            raise OSError("synthetic write failure")

    def failing_fdopen(
        file_fd: int,
        mode: str,
        *,
        closefd: bool = True,
    ) -> _FailingWriter:
        writer = cast(BinaryIO, original_fdopen(file_fd, mode, closefd=closefd))
        return _FailingWriter(writer)

    def failing_fsync(file_fd: int) -> None:
        mode = os.fstat(file_fd).st_mode
        if fault == "file_fsync" and stat.S_ISREG(mode):
            raise OSError("synthetic file fsync failure")
        if fault == "directory_fsync" and stat.S_ISDIR(mode) and content_path.exists():
            raise OSError("synthetic directory fsync failure")
        original_fsync(file_fd)

    def failing_replace(*_: object, **__: object) -> None:
        raise OSError("synthetic rename failure")

    with monkeypatch.context() as scoped_patch:
        if fault == "write":
            scoped_patch.setattr(os, "fdopen", failing_fdopen)
        elif fault == "rename":
            scoped_patch.setattr(os, "replace", failing_replace)
        else:
            scoped_patch.setattr(os, "fsync", failing_fsync)

        with pytest.raises(ArtifactContentStoreError) as captured:
            store.snapshot_bytes(b"immutable", storage_key=key)

    assert captured.value.failure is ArtifactContentFailure.STORAGE_UNAVAILABLE
    assert not content_path.parent.exists()


def test_bytes_snapshot_closes_unclaimed_staging_fd_when_fdopen_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, paths, key = _store(tmp_path)
    content_path = paths.artifact_from_storage_key(key)
    original_open_staging = artifact_store_module._open_staging
    staging_fd = -1

    def capture_staging_fd(directory_fd: int) -> int:
        nonlocal staging_fd
        staging_fd = original_open_staging(directory_fd)
        return staging_fd

    def failing_fdopen(
        file_fd: int,
        mode: str,
        *,
        closefd: bool = True,
    ) -> BinaryIO:
        del file_fd, mode, closefd
        raise OSError("synthetic fdopen failure")

    with monkeypatch.context() as scoped_patch:
        scoped_patch.setattr(
            artifact_store_module,
            "_open_staging",
            capture_staging_fd,
        )
        scoped_patch.setattr(os, "fdopen", failing_fdopen)
        with pytest.raises(ArtifactContentStoreError) as captured:
            store.snapshot_bytes(b"immutable", storage_key=key)

    assert captured.value.failure is ArtifactContentFailure.STORAGE_UNAVAILABLE
    assert staging_fd >= 0
    with pytest.raises(OSError):
        os.fstat(staging_fd)
    assert not content_path.parent.exists()


def test_bytes_snapshot_removes_object_directory_when_creation_fsync_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, paths, key = _store(tmp_path)
    content_path = paths.artifact_from_storage_key(key)
    original_fsync = os.fsync
    failed = False

    def fail_object_directory_parent_fsync(file_fd: int) -> None:
        nonlocal failed
        if not failed and content_path.parent.exists() and not content_path.exists():
            failed = True
            raise OSError("synthetic object directory fsync failure")
        original_fsync(file_fd)

    with monkeypatch.context() as scoped_patch:
        scoped_patch.setattr(os, "fsync", fail_object_directory_parent_fsync)
        with pytest.raises(ArtifactContentStoreError) as captured:
            store.snapshot_bytes(b"immutable", storage_key=key)

    assert failed is True
    assert captured.value.failure is ArtifactContentFailure.STORAGE_UNAVAILABLE
    assert not content_path.parent.exists()


def test_bytes_snapshot_retries_short_writes_until_content_is_complete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, paths, key = _store(tmp_path)
    content = b"immutable"
    original_fdopen = os.fdopen
    write_calls = 0

    class _ShortWriter:
        def __init__(self, writer: BinaryIO) -> None:
            self._writer = writer

        def write(self, value: bytes | memoryview) -> int:
            nonlocal write_calls
            write_calls += 1
            return self._writer.write(value[:2])

        def read(self, size: int = -1) -> bytes:
            return self._writer.read(size)

        def seek(self, offset: int, whence: int = os.SEEK_SET) -> int:
            return self._writer.seek(offset, whence)

        def flush(self) -> None:
            self._writer.flush()

        def fileno(self) -> int:
            return self._writer.fileno()

        def close(self) -> None:
            self._writer.close()

    def short_writing_fdopen(
        file_fd: int,
        mode: str,
        *,
        closefd: bool = True,
    ) -> _ShortWriter:
        writer = cast(BinaryIO, original_fdopen(file_fd, mode, closefd=closefd))
        return _ShortWriter(writer)

    with monkeypatch.context() as scoped_patch:
        scoped_patch.setattr(os, "fdopen", short_writing_fdopen)
        stored = store.snapshot_bytes(content, storage_key=key)

    assert write_calls > 1
    assert stored.size == len(content)
    assert paths.artifact_from_storage_key(key).read_bytes() == content


def test_bytes_snapshot_rehash_rejects_staging_corruption(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, paths, key = _store(tmp_path)
    content_path = paths.artifact_from_storage_key(key)
    original_sync = artifact_store_module._sync_staging

    def corrupt_after_sync(writer: BinaryIO) -> None:
        original_sync(writer)
        writer.seek(0)
        writer.write(b"X")
        writer.flush()

    monkeypatch.setattr(
        artifact_store_module,
        "_sync_staging",
        corrupt_after_sync,
    )

    with pytest.raises(ArtifactContentStoreError) as captured:
        store.snapshot_bytes(b"immutable", storage_key=key)

    assert captured.value.failure is ArtifactContentFailure.STORAGE_INTEGRITY
    assert not content_path.parent.exists()


def test_bytes_snapshot_rejects_destination_swap_after_publish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, paths, key = _store(tmp_path)
    content_path = paths.artifact_from_storage_key(key)
    original_replace = os.replace
    swapped = False

    def swap_published_inode(
        source: str,
        destination: str,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
    ) -> None:
        nonlocal swapped
        original_replace(
            source,
            destination,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
        )
        if not swapped:
            swapped = True
            replacement = content_path.parent / "replacement.bin"
            replacement.write_bytes(b"immutable")
            replacement.chmod(0o444)
            original_replace(replacement, content_path)

    monkeypatch.setattr(os, "replace", swap_published_inode)

    with pytest.raises(ArtifactContentStoreError) as captured:
        store.snapshot_bytes(b"immutable", storage_key=key)

    assert swapped is True
    assert captured.value.failure is ArtifactContentFailure.STORAGE_INTEGRITY
    assert not content_path.parent.exists()


def test_bytes_snapshot_rejects_destination_swap_during_directory_fsync(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, paths, key = _store(tmp_path)
    content_path = paths.artifact_from_storage_key(key)
    original_fsync = os.fsync
    original_replace = os.replace
    swapped = False

    def swap_after_published_directory_fsync(file_fd: int) -> None:
        nonlocal swapped
        original_fsync(file_fd)
        if not swapped and stat.S_ISDIR(os.fstat(file_fd).st_mode) and content_path.exists():
            swapped = True
            replacement = content_path.parent / "replacement.bin"
            replacement.write_bytes(b"immutable")
            replacement.chmod(0o444)
            original_replace(replacement, content_path)

    monkeypatch.setattr(os, "fsync", swap_after_published_directory_fsync)

    with pytest.raises(ArtifactContentStoreError) as captured:
        store.snapshot_bytes(b"immutable", storage_key=key)

    assert swapped is True
    assert captured.value.failure is ArtifactContentFailure.STORAGE_INTEGRITY
    assert not content_path.parent.exists()


@pytest.mark.parametrize("mutation", ["digest", "size", "symlink", "hardlink"])
def test_verified_open_rejects_storage_tampering(tmp_path: Path, mutation: str) -> None:
    store, paths, key = _store(tmp_path)
    stored = store.snapshot_bytes(b"trusted", storage_key=key)
    content_path = paths.artifact_from_storage_key(key)

    if mutation == "digest":
        content_path.chmod(0o644)
        content_path.write_bytes(b"altered")
    elif mutation == "size":
        content_path.chmod(0o644)
        content_path.write_bytes(b"trusted-longer")
    elif mutation == "symlink":
        content_path.unlink()
        outside = tmp_path / "outside.bin"
        outside.write_bytes(b"trusted")
        content_path.symlink_to(outside)
    else:
        alias = tmp_path / "alias.bin"
        os.link(content_path, alias)

    with pytest.raises(ArtifactContentStoreError) as captured:
        store.open_verified(
            storage_key=key,
            expected_sha256=stored.sha256,
            expected_size=stored.size,
        )
    assert captured.value.failure is ArtifactContentFailure.STORAGE_INTEGRITY


def test_verified_open_rejects_path_replacement_during_hash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, paths, key = _store(tmp_path)
    stored = store.snapshot_bytes(b"trusted", storage_key=key)
    content_path = paths.artifact_from_storage_key(key)
    original_read = artifact_store_module._read_fd_chunk
    replaced = False

    def replace_after_read(file_fd: int, max_bytes: int) -> bytes:
        nonlocal replaced
        chunk = original_read(file_fd, max_bytes)
        if chunk and not replaced:
            replaced = True
            replacement = content_path.parent / "replacement.bin"
            replacement.write_bytes(b"hostile")
            os.replace(replacement, content_path)
        return chunk

    monkeypatch.setattr(artifact_store_module, "_read_fd_chunk", replace_after_read)

    with pytest.raises(ArtifactContentStoreError) as captured:
        store.open_verified(
            storage_key=key,
            expected_sha256=stored.sha256,
            expected_size=stored.size,
        )
    assert captured.value.failure is ArtifactContentFailure.STORAGE_INTEGRITY


def test_verified_open_rejects_truncation_during_hash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, paths, key = _store(tmp_path, max_bytes=4 * 1024 * 1024)
    stored = store.snapshot_bytes(b"x" * (2 * 1024 * 1024), storage_key=key)
    content_path = paths.artifact_from_storage_key(key)
    content_path.chmod(0o644)
    original_read = artifact_store_module._read_fd_chunk
    truncated = False

    def truncate_after_first_read(file_fd: int, max_bytes: int) -> bytes:
        nonlocal truncated
        chunk = original_read(file_fd, max_bytes)
        if chunk and not truncated:
            truncated = True
            with content_path.open("r+b") as writer:
                writer.truncate(len(chunk) // 2)
        return chunk

    monkeypatch.setattr(
        artifact_store_module,
        "_read_fd_chunk",
        truncate_after_first_read,
    )

    with pytest.raises(ArtifactContentStoreError) as captured:
        store.open_verified(
            storage_key=key,
            expected_sha256=stored.sha256,
            expected_size=stored.size,
        )

    assert captured.value.failure is ArtifactContentFailure.STORAGE_INTEGRITY


def test_opened_content_detects_truncation_after_verification(tmp_path: Path) -> None:
    store, paths, key = _store(tmp_path)
    stored = store.snapshot_bytes(b"trusted", storage_key=key)
    content_path = paths.artifact_from_storage_key(key)
    lease = store.open_verified(
        storage_key=key,
        expected_sha256=stored.sha256,
        expected_size=stored.size,
    )
    content_path.chmod(0o644)
    with content_path.open("r+b") as writer:
        writer.truncate(0)

    try:
        with pytest.raises(ArtifactContentStoreError) as captured:
            lease.read(stored.size)
        assert captured.value.failure is ArtifactContentFailure.STORAGE_INTEGRITY
    finally:
        lease.close()

    assert lease.closed is True


def test_declared_bytes_mismatch_and_discard_leave_no_partial_content(
    tmp_path: Path,
) -> None:
    store, paths, key = _store(tmp_path)

    with pytest.raises(ArtifactContentStoreError) as captured:
        store.snapshot_bytes(
            b"content",
            storage_key=key,
            expected_sha256="0" * 64,
            expected_size=7,
        )
    assert captured.value.failure is ArtifactContentFailure.DECLARED_CONTENT_MISMATCH
    assert not paths.artifact_from_storage_key(key).parent.exists()

    store.snapshot_bytes(b"content", storage_key=key)
    store.discard(key)
    assert not paths.artifact_from_storage_key(key).parent.exists()
