from __future__ import annotations

import hashlib
import os
import stat
import subprocess
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from riftx.audit import (
    LocalSnapshotStore,
    SnapshotCASBinding,
    SnapshotStoreFailure,
    SourceCaptureDecision,
    SourceCapturePolicy,
    SourceCaptureReason,
    SourceManifest,
    SourceManifestObjectType,
    SourceManifestOrigin,
    SourceManifestSourceKind,
    publish_source_manifest,
)
from riftx.audit.source_manifest import SOURCE_MANIFEST_BLOB_NAME
from riftx.audit_worker import preflight as preflight_worker
from riftx.audit_worker.materializer import (
    GitSourceMaterializer,
    SourceMaterializationError,
    SourceMaterializationFailure,
)
from riftx.domain.audit_records import SourceSnapshot

NOW = datetime(2026, 8, 4, 12, tzinfo=UTC)


def _git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(
        ("git", "-C", str(repository), *arguments),
        check=True,
        capture_output=True,
        env={
            **os.environ,
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
        },
    )
    return result.stdout.decode("ascii", errors="strict").strip()


def _repository(tmp_path: Path) -> Path:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init", "--quiet")
    _git(repository, "config", "user.email", "audit@example.invalid")
    _git(repository, "config", "user.name", "Audit Fixture")
    (repository / ".gitignore").write_text("*.ignored\n", encoding="utf-8")
    (repository / "main.py").write_text("print('initial')\n", encoding="utf-8")
    (repository / "README.md").write_text("# Fixture\n", encoding="utf-8")
    _git(repository, "add", ".gitignore", "main.py", "README.md")
    _git(repository, "commit", "--quiet", "-m", "initial")
    return repository


def _materializer(
    tmp_path: Path,
    repository: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    fault_injector=None,
) -> GitSourceMaterializer:
    monkeypatch.setattr(preflight_worker, "SOURCE_ROOT", repository)
    return GitSourceMaterializer(
        tmp_path / "materialized",
        fault_injector=fault_injector,
    )


def _entries(manifest: SourceManifest) -> dict[str, object]:
    return {
        entry.path.relative_path: entry
        for entry in manifest.entries
        if entry.path.relative_path is not None
    }


def _repository_digest(repository: Path) -> str:
    digest = hashlib.sha256()
    for root, directories, files in os.walk(repository, followlinks=False):
        directories.sort()
        files.sort()
        root_path = Path(root)
        for name in directories + files:
            path = root_path / name
            relative = path.relative_to(repository).as_posix().encode("utf-8")
            value = path.lstat()
            digest.update(relative + b"\0" + str(stat.S_IFMT(value.st_mode)).encode() + b"\0")
            if stat.S_ISLNK(value.st_mode):
                digest.update(os.fsencode(os.readlink(path)))
            elif stat.S_ISREG(value.st_mode):
                digest.update(path.read_bytes())
    return digest.hexdigest()


def test_revision_materialization_is_deterministic_read_only_and_owner_bound(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _repository(tmp_path)
    os.symlink("main.py", repository / "main-link")
    _git(repository, "add", "main-link")
    _git(repository, "commit", "--quiet", "-m", "link")
    head = _git(repository, "rev-parse", "HEAD")
    before = _repository_digest(repository)
    materializer = _materializer(tmp_path, repository, monkeypatch)
    policy = SourceCapturePolicy(include_generated=True, include_vendor=True)

    first = materializer.materialize(
        source_kind=SourceManifestSourceKind.REVISION,
        revision="HEAD",
        policy=policy,
    )
    second = materializer.materialize(
        source_kind=SourceManifestSourceKind.REVISION,
        revision=head,
        policy=policy,
    )

    assert first.manifest.canonical_json() == second.manifest.canonical_json()
    assert SourceManifest.from_json(first.manifest.canonical_json()) == first.manifest
    assert first.manifest.commit_sha == head
    assert first.manifest.working_tree_digest is None
    assert first.manifest.snapshot_digest == SourceSnapshot.compute_snapshot_digest(
        tree_digest=first.manifest.tree_digest,
        capture_policy_digest=first.manifest.capture_policy_digest,
        materializer_schema_version=first.manifest.materializer_schema_version,
    )
    assert _entries(first.manifest)["main-link"].object_type is SourceManifestObjectType.SYMLINK
    assert (first.root / "main-link").is_symlink()
    assert _repository_digest(repository) == before
    assert _git(repository, "status", "--porcelain") == ""

    store = LocalSnapshotStore(
        tmp_path / "cas",
        max_blob_bytes=1024 * 1024,
        max_tree_bytes=4 * 1024 * 1024,
    )
    published = publish_source_manifest(
        project_id="project-1",
        manifest=first.manifest,
        staging_root=first.root,
        snapshot_store=store,
        temporary_root=tmp_path / "manifest-temp",
    )
    binding = SnapshotCASBinding(
        project_id="project-1",
        snapshot_digest=published.snapshot_digest,
        manifest_digest=published.manifest_digest,
    )
    assert store.verify(binding, published.content_storage_key).valid is True
    assert store.verify(binding, published.manifest_storage_key).valid is True
    with store.open_blob(
        binding,
        published.manifest_storage_key,
        SOURCE_MANIFEST_BLOB_NAME,
        first.manifest.manifest_blob_digest,
        max_bytes=1024 * 1024,
    ) as reader:
        manifest_bytes = reader.read(1024 * 1024)
        reader.verify_complete()
    assert SourceManifest.from_json(manifest_bytes) == first.manifest
    foreign = SnapshotCASBinding(
        project_id="project-2",
        snapshot_digest=published.snapshot_digest,
        manifest_digest=published.manifest_digest,
    )
    assert store.verify(foreign, published.content_storage_key).failure is (
        SnapshotStoreFailure.OWNER_MISMATCH
    )
    materializer.discard(first)
    materializer.discard(second)


def test_working_tree_materializes_staged_unstaged_untracked_and_records_ignored(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _repository(tmp_path)
    (repository / "main.py").write_text("print('unstaged')\n", encoding="utf-8")
    (repository / "staged.py").write_text("print('staged')\n", encoding="utf-8")
    _git(repository, "add", "staged.py")
    (repository / "both.py").write_text("print('index')\n", encoding="utf-8")
    _git(repository, "add", "both.py")
    (repository / "both.py").write_text("print('worktree')\n", encoding="utf-8")
    (repository / "untracked.py").write_text("print('untracked')\n", encoding="utf-8")
    (repository / "cache.ignored").write_text("secret cache\n", encoding="utf-8")
    before = _repository_digest(repository)
    materializer = _materializer(tmp_path, repository, monkeypatch)
    policy = SourceCapturePolicy(include_untracked=True)

    first = materializer.materialize(
        source_kind=SourceManifestSourceKind.WORKING_TREE,
        revision="HEAD",
        policy=policy,
    )
    second = materializer.materialize(
        source_kind=SourceManifestSourceKind.WORKING_TREE,
        revision="HEAD",
        policy=policy,
    )

    assert (first.manifest.staged, first.manifest.unstaged, first.manifest.untracked) == (
        True,
        True,
        True,
    )
    assert first.manifest.working_tree_digest is not None
    assert first.manifest.canonical_json() == second.manifest.canonical_json()
    entries = _entries(first.manifest)
    assert entries["staged.py"].origin is SourceManifestOrigin.TRACKED_WORKTREE
    assert entries["untracked.py"].origin is SourceManifestOrigin.UNTRACKED
    assert entries["untracked.py"].decision is SourceCaptureDecision.INCLUDED
    assert entries["cache.ignored"].origin is SourceManifestOrigin.IGNORED
    assert entries["cache.ignored"].reason is SourceCaptureReason.IGNORED
    assert not (first.root / "cache.ignored").exists()
    assert (first.root / "both.py").read_text(encoding="utf-8") == "print('worktree')\n"
    assert _repository_digest(repository) == before
    materializer.discard(first)
    materializer.discard(second)


def test_revision_manifest_distinguishes_requested_commit_from_current_head(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _repository(tmp_path)
    previous = _git(repository, "rev-parse", "HEAD")
    (repository / "main.py").write_text("print('second')\n", encoding="utf-8")
    _git(repository, "add", "main.py")
    _git(repository, "commit", "--quiet", "-m", "second")
    head = _git(repository, "rev-parse", "HEAD")
    assert head != previous
    materializer = _materializer(tmp_path, repository, monkeypatch)

    result = materializer.materialize(
        source_kind=SourceManifestSourceKind.REVISION,
        revision=previous,
        policy=SourceCapturePolicy(),
    )

    assert result.manifest.commit_sha == previous
    assert result.manifest.head_commit_sha == previous
    assert (result.root / "main.py").read_text(encoding="utf-8") == "print('initial')\n"
    materializer.discard(result)


def test_materializer_records_symlink_hardlink_special_oversized_and_invalid_utf8(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _repository(tmp_path)
    os.symlink("main.py", repository / "safe-link")
    (repository / "hard.py").write_text("print('hard')\n", encoding="utf-8")
    _git(repository, "add", "safe-link", "hard.py")
    _git(repository, "commit", "--quiet", "-m", "objects")
    os.link(repository / "hard.py", tmp_path / "outside-hardlink")
    os.mkfifo(repository / "special.pipe")
    (repository / "large.py").write_bytes(b"x" * 33)
    (repository / "invalid.py").write_bytes(b"print('\xff')\n")
    materializer = _materializer(tmp_path, repository, monkeypatch)
    policy = SourceCapturePolicy(
        include_untracked=True,
        max_file_bytes=32,
        max_repository_bytes=1024,
    )

    result = materializer.materialize(
        source_kind=SourceManifestSourceKind.WORKING_TREE,
        revision="HEAD",
        policy=policy,
    )

    entries = _entries(result.manifest)
    assert entries["safe-link"].decision is SourceCaptureDecision.INCLUDED
    assert entries["safe-link"].object_type is SourceManifestObjectType.SYMLINK
    assert entries["hard.py"].reason is SourceCaptureReason.HARDLINK
    assert entries["special.pipe"].reason is SourceCaptureReason.SPECIAL_FILE
    assert entries["large.py"].reason is SourceCaptureReason.OVERSIZED_FILE
    assert entries["invalid.py"].reason is SourceCaptureReason.INVALID_UTF8_CONTENT
    assert not (result.root / "hard.py").exists()
    assert not (result.root / "special.pipe").exists()
    assert not (result.root / "large.py").exists()
    assert not (result.root / "invalid.py").exists()
    materializer.discard(result)


def test_revision_records_invalid_utf8_path_without_materializing_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _repository(tmp_path)
    blob = subprocess.run(
        ("git", "-C", str(repository), "hash-object", "-w", "--stdin"),
        input=b"print('opaque path')\n",
        check=True,
        capture_output=True,
    ).stdout.strip()
    tree_input = b"100644 blob " + blob + b"\tinvalid-\xff.py\0"
    tree = subprocess.run(
        ("git", "-C", str(repository), "mktree", "-z"),
        input=tree_input,
        check=True,
        capture_output=True,
    ).stdout.decode("ascii", errors="strict").strip()
    commit = subprocess.run(
        ("git", "-C", str(repository), "commit-tree", tree, "-m", "opaque path"),
        check=True,
        capture_output=True,
        env={
            **os.environ,
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
        },
    ).stdout.decode("ascii", errors="strict").strip()
    _git(repository, "update-ref", "HEAD", commit)
    materializer = _materializer(tmp_path, repository, monkeypatch)

    result = materializer.materialize(
        source_kind=SourceManifestSourceKind.REVISION,
        revision="HEAD",
        policy=SourceCapturePolicy(),
    )

    assert len(result.manifest.entries) == 1
    entry = result.manifest.entries[0]
    assert entry.path.relative_path is None
    assert entry.reason is SourceCaptureReason.INVALID_UTF8_PATH
    assert entry.decision is SourceCaptureDecision.DEFERRED
    assert tuple(result.root.iterdir()) == ()
    materializer.discard(result)


def test_revision_records_submodule_and_lfs_pointer_without_materializing_them(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _repository(tmp_path)
    head = _git(repository, "rev-parse", "HEAD")
    pointer = (
        "version https://git-lfs.github.com/spec/v1\n"
        f"oid sha256:{'a' * 64}\n"
        "size 123456\n"
    )
    (repository / "large.asset").write_text(pointer, encoding="ascii")
    _git(repository, "add", "large.asset")
    _git(repository, "update-index", "--add", "--cacheinfo", f"160000,{head},deps/sub")
    _git(repository, "commit", "--quiet", "-m", "lfs and submodule")
    materializer = _materializer(tmp_path, repository, monkeypatch)

    result = materializer.materialize(
        source_kind=SourceManifestSourceKind.REVISION,
        revision="HEAD",
        policy=SourceCapturePolicy(),
    )

    entries = _entries(result.manifest)
    assert entries["large.asset"].reason is SourceCaptureReason.LFS_POINTER
    assert entries["large.asset"].decision is SourceCaptureDecision.DEFERRED
    assert entries["deps/sub"].reason is SourceCaptureReason.SUBMODULE
    assert entries["deps/sub"].decision is SourceCaptureDecision.EXCLUDED
    assert not (result.root / "large.asset").exists()
    assert not (result.root / "deps" / "sub").exists()
    materializer.discard(result)


def test_path_vendor_generated_and_untracked_policy_decisions_are_frozen(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _repository(tmp_path)
    (repository / "vendor").mkdir()
    (repository / "vendor" / "library.py").write_text("print('vendor')\n", encoding="utf-8")
    (repository / "dist").mkdir()
    (repository / "dist" / "bundle.js").write_text("const generated = true;\n", encoding="utf-8")
    (repository / "untracked.py").write_text("print('untracked')\n", encoding="utf-8")
    _git(repository, "add", "vendor/library.py", "dist/bundle.js")
    _git(repository, "commit", "--quiet", "-m", "policy fixtures")
    materializer = _materializer(tmp_path, repository, monkeypatch)
    policy = SourceCapturePolicy(exclude_paths=("README.md",), include_untracked=False)

    result = materializer.materialize(
        source_kind=SourceManifestSourceKind.WORKING_TREE,
        revision="HEAD",
        policy=policy,
    )

    entries = _entries(result.manifest)
    assert entries["README.md"].reason is SourceCaptureReason.PATH_EXCLUDED
    assert entries["vendor/library.py"].reason is SourceCaptureReason.VENDOR_EXCLUDED
    assert entries["dist/bundle.js"].reason is SourceCaptureReason.GENERATED_EXCLUDED
    assert entries["untracked.py"].reason is SourceCaptureReason.UNTRACKED_EXCLUDED
    assert result.manifest.capture_policy_digest == policy.digest
    materializer.discard(result)


def test_working_tree_toctou_fails_closed_and_removes_partial_staging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _repository(tmp_path)

    def mutate_after_read(stage: str, path: bytes | None) -> None:
        if stage == "after_entry" and path == b"main.py":
            (repository / "main.py").write_text("print('changed concurrently')\n", encoding="utf-8")

    materializer = _materializer(
        tmp_path,
        repository,
        monkeypatch,
        fault_injector=mutate_after_read,
    )

    with pytest.raises(SourceMaterializationError) as error:
        materializer.materialize(
            source_kind=SourceManifestSourceKind.WORKING_TREE,
            revision="HEAD",
            policy=SourceCapturePolicy(),
        )

    assert error.value.code == SourceMaterializationFailure.REPOSITORY_CHANGED.value
    assert tuple((tmp_path / "materialized").iterdir()) == ()


def test_concurrent_identical_capture_reuses_both_content_and_manifest_cas(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _repository(tmp_path)
    materializer = _materializer(tmp_path, repository, monkeypatch)
    store = LocalSnapshotStore(
        tmp_path / "cas",
        max_blob_bytes=1024 * 1024,
        max_tree_bytes=4 * 1024 * 1024,
    )
    policy = SourceCapturePolicy()

    def capture_and_publish(index: int):
        captured = materializer.materialize(
            source_kind=SourceManifestSourceKind.REVISION,
            revision="HEAD",
            policy=policy,
        )
        try:
            return publish_source_manifest(
                project_id="project-1",
                manifest=captured.manifest,
                staging_root=captured.root,
                snapshot_store=store,
                temporary_root=tmp_path / f"manifest-temp-{index}",
            )
        finally:
            materializer.discard(captured)

    with ThreadPoolExecutor(max_workers=2) as executor:
        first, second = tuple(executor.map(capture_and_publish, range(2)))

    assert first.snapshot_digest == second.snapshot_digest
    assert first.content_storage_key == second.content_storage_key
    assert first.manifest_storage_key == second.manifest_storage_key
    assert {first.content_reused, second.content_reused} == {False, True}
    assert {first.manifest_reused, second.manifest_reused} == {False, True}


def test_working_tree_content_change_produces_new_identity_and_retry_succeeds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _repository(tmp_path)
    materializer = _materializer(tmp_path, repository, monkeypatch)
    policy = SourceCapturePolicy()
    first = materializer.materialize(
        source_kind=SourceManifestSourceKind.WORKING_TREE,
        revision="HEAD",
        policy=policy,
    )
    (repository / "main.py").write_text("print('new bytes')\n", encoding="utf-8")
    second = materializer.materialize(
        source_kind=SourceManifestSourceKind.WORKING_TREE,
        revision="HEAD",
        policy=policy,
    )

    assert first.manifest.tree_digest != second.manifest.tree_digest
    assert first.manifest.snapshot_digest != second.manifest.snapshot_digest
    assert first.manifest.manifest_digest != second.manifest.manifest_digest
    materializer.discard(first)
    materializer.discard(second)


def test_failed_cleanup_leaves_private_orphan_for_bounded_cleanup_and_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _repository(tmp_path)

    def fail_capture_and_cleanup(stage: str, path: bytes | None) -> None:
        if stage == "after_entry" and path == b"main.py":
            raise SourceMaterializationError(SourceMaterializationFailure.OUTPUT_WRITE_FAILED)
        if stage == "before_cleanup":
            raise RuntimeError("simulated cleanup failure")

    failed = _materializer(
        tmp_path,
        repository,
        monkeypatch,
        fault_injector=fail_capture_and_cleanup,
    )
    with pytest.raises(SourceMaterializationError) as error:
        failed.materialize(
            source_kind=SourceManifestSourceKind.REVISION,
            revision="HEAD",
            policy=SourceCapturePolicy(),
        )
    assert error.value.code == SourceMaterializationFailure.CLEANUP_FAILED.value
    materialized_orphans = tuple((tmp_path / "materialized").iterdir())
    assert len(materialized_orphans) == 1
    cleanup_cutoff = datetime.fromtimestamp(
        materialized_orphans[0].lstat().st_mtime,
        UTC,
    ) + timedelta(seconds=1)

    retry = _materializer(tmp_path, repository, monkeypatch)
    dry_run = retry.cleanup_orphans(older_than=cleanup_cutoff, dry_run=True)
    cleaned = retry.cleanup_orphans(older_than=cleanup_cutoff, dry_run=False)
    assert (dry_run.examined, dry_run.eligible, dry_run.removed) == (1, 1, 0)
    assert (cleaned.examined, cleaned.eligible, cleaned.removed) == (1, 1, 1)
    result = retry.materialize(
        source_kind=SourceManifestSourceKind.REVISION,
        revision="HEAD",
        policy=SourceCapturePolicy(),
    )
    retry.discard(result)


def test_manifest_rejects_noncanonical_or_tampered_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _repository(tmp_path)
    materializer = _materializer(tmp_path, repository, monkeypatch)
    result = materializer.materialize(
        source_kind=SourceManifestSourceKind.REVISION,
        revision="HEAD",
        policy=SourceCapturePolicy(),
    )
    canonical = result.manifest.canonical_json()

    with pytest.raises(ValueError, match="not canonical"):
        SourceManifest.from_json(canonical.replace(":", ": ", 1))
    with pytest.raises(ValueError, match="digest is invalid"):
        SourceManifest.from_json(canonical.replace(result.manifest.tree_digest, "0" * 64, 1))
    materializer.discard(result)


def test_manifest_entry_limit_fails_before_publishing_partial_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _repository(tmp_path)
    materializer = _materializer(tmp_path, repository, monkeypatch)

    with pytest.raises(SourceMaterializationError) as error:
        materializer.materialize(
            source_kind=SourceManifestSourceKind.WORKING_TREE,
            revision="HEAD",
            policy=SourceCapturePolicy(max_manifest_entries=2),
        )

    assert error.value.code == SourceMaterializationFailure.MANIFEST_LIMIT_EXCEEDED.value
    assert tuple((tmp_path / "materialized").iterdir()) == ()


def test_working_tree_rejects_unmerged_index_without_publishing_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _repository(tmp_path)
    blob = _git(repository, "rev-parse", "HEAD:main.py")
    _git(repository, "update-index", "--force-remove", "main.py")
    subprocess.run(
        ("git", "-C", str(repository), "update-index", "--index-info"),
        input=(
            f"100644 {blob} 1\tmain.py\n"
            f"100644 {blob} 2\tmain.py\n"
        ).encode("ascii"),
        check=True,
        capture_output=True,
    )
    materializer = _materializer(tmp_path, repository, monkeypatch)

    with pytest.raises(SourceMaterializationError) as error:
        materializer.materialize(
            source_kind=SourceManifestSourceKind.WORKING_TREE,
            revision="HEAD",
            policy=SourceCapturePolicy(),
        )

    assert error.value.code == SourceMaterializationFailure.UNMERGED_STATE.value
    assert tuple((tmp_path / "materialized").iterdir()) == ()


def test_materializer_rejects_output_inside_source_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _repository(tmp_path)
    monkeypatch.setattr(preflight_worker, "SOURCE_ROOT", repository)
    with pytest.raises(SourceMaterializationError) as error:
        GitSourceMaterializer(repository / "unsafe-output")

    assert error.value.code == SourceMaterializationFailure.OUTPUT_INVALID.value
    assert not (repository / "unsafe-output").exists()


def test_materializer_rejects_external_excludes_file_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _repository(tmp_path)
    external = tmp_path / "external-ignore"
    external.write_text("*.py\n", encoding="utf-8")
    _git(repository, "config", "core.excludesFile", str(external))
    materializer = _materializer(tmp_path, repository, monkeypatch)

    with pytest.raises(SourceMaterializationError) as error:
        materializer.materialize(
            source_kind=SourceManifestSourceKind.WORKING_TREE,
            revision="HEAD",
            policy=SourceCapturePolicy(include_untracked=True),
        )

    assert error.value.code == "audit_git_config_unsafe"
    assert tuple((tmp_path / "materialized").iterdir()) == ()
