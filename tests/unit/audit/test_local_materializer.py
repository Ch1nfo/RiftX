from __future__ import annotations

import os
from pathlib import Path

import pytest

from riftx.audit import (
    LOCAL_DIRECTORY_MATERIALIZER_SCHEMA_VERSION,
    LocalSnapshotStore,
    LocalSourceKind,
    LocalSourceMaterializationError,
    LocalSourceMaterializationFailure,
    LocalSourceMaterializer,
    SourceCaptureDecision,
    SourceCapturePolicy,
    SourceCaptureReason,
    SourceManifest,
    SourceManifestObjectType,
    SourceManifestOrigin,
    SourceManifestSourceKind,
    build_source_snapshot,
    open_authorized_local_source,
    publish_source_manifest,
)
from riftx.domain import SourceTargetKind


def _open(source: Path, root: Path):
    return open_authorized_local_source(
        source,
        allowed_roots=(root,),
    )


def test_local_materializer_captures_directory_without_git_or_link_following(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "src").mkdir()
    (source / "src" / "main.py").write_text("print('safe')\n", encoding="utf-8")
    executable = source / "tool.sh"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)
    outside = tmp_path / "outside.txt"
    outside.write_text("must not be read", encoding="utf-8")
    (source / "outside-link").symlink_to(outside)
    (source / ".git").mkdir()
    (source / ".git" / "config").write_text("unsafe", encoding="utf-8")
    staging = tmp_path / "staging"
    materializer = LocalSourceMaterializer(staging)

    with _open(source, tmp_path) as authorized:
        assert authorized.source_kind is LocalSourceKind.GIT_DIRECTORY
        captured = materializer.materialize(
            authorized,
            policy=SourceCapturePolicy(),
        )

    manifest = captured.manifest
    assert SourceManifest.from_json(manifest.canonical_json()) == manifest
    assert manifest.source_kind is SourceManifestSourceKind.DIRECTORY
    assert manifest.commit_sha is None
    assert manifest.head_commit_sha is None
    assert (
        manifest.materializer_schema_version
        == LOCAL_DIRECTORY_MATERIALIZER_SCHEMA_VERSION
    )
    assert all(entry.origin is SourceManifestOrigin.LOCAL_DIRECTORY for entry in manifest.entries)
    entries = {entry.path.relative_path: entry for entry in manifest.entries}
    assert set(entries) == {"outside-link", "src/main.py", "tool.sh"}
    assert entries["src/main.py"].decision is SourceCaptureDecision.INCLUDED
    assert entries["tool.sh"].mode == 0o100755
    assert entries["outside-link"].object_type is SourceManifestObjectType.SYMLINK
    assert (captured.root / "outside-link").is_symlink()
    assert os.readlink(captured.root / "outside-link") == os.fspath(outside)
    assert not (captured.root / ".git").exists()
    materializer.discard(captured)
    assert not captured.root.exists()


def test_local_materializer_records_exclusions_and_special_files(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "keep.py").write_text("value = 1\n", encoding="utf-8")
    (source / "vendor").mkdir()
    (source / "vendor" / "library.py").write_text("value = 2\n", encoding="utf-8")
    fifo = source / "events.pipe"
    os.mkfifo(fifo)
    materializer = LocalSourceMaterializer(tmp_path / "staging")

    with _open(source, tmp_path) as authorized:
        captured = materializer.materialize(
            authorized,
            policy=SourceCapturePolicy(),
        )

    entries = {entry.path.relative_path: entry for entry in captured.manifest.entries}
    assert entries["keep.py"].decision is SourceCaptureDecision.INCLUDED
    assert entries["vendor/library.py"].reason is SourceCaptureReason.VENDOR_EXCLUDED
    assert entries["events.pipe"].reason is SourceCaptureReason.SPECIAL_FILE
    assert entries["events.pipe"].decision is SourceCaptureDecision.DEFERRED
    materializer.discard(captured)


def test_local_materializer_rejects_source_mutation_and_cleans_staging(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    target = source / "main.py"
    target.write_text("value = 1\n", encoding="utf-8")

    def mutate(stage: str, path: bytes | None) -> None:
        if stage == "after_entry" and path == b"main.py":
            target.write_text("value = 2\n", encoding="utf-8")

    staging = tmp_path / "staging"
    materializer = LocalSourceMaterializer(staging, fault_injector=mutate)
    with _open(source, tmp_path) as authorized, pytest.raises(
        LocalSourceMaterializationError
    ) as captured:
        materializer.materialize(authorized, policy=SourceCapturePolicy())

    assert captured.value.failure is LocalSourceMaterializationFailure.SOURCE_CHANGED
    assert os.fspath(source) not in str(captured.value)
    assert tuple(staging.iterdir()) == ()


def test_local_materializer_requires_admitted_filters(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "main.py").write_text("value = 1\n", encoding="utf-8")
    materializer = LocalSourceMaterializer(tmp_path / "staging")
    with _open(source, tmp_path) as authorized, pytest.raises(
        LocalSourceMaterializationError
    ) as captured:
        materializer.materialize(
            authorized,
            policy=SourceCapturePolicy(exclude_paths=("main.py",)),
        )
    assert captured.value.failure is LocalSourceMaterializationFailure.REQUEST_INVALID


def test_local_directory_manifest_publish_replay_builds_directory_snapshot(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "main.py").write_text("value = 1\n", encoding="utf-8")
    materializer = LocalSourceMaterializer(tmp_path / "staging")
    with _open(source, tmp_path) as authorized:
        captured = materializer.materialize(authorized, policy=SourceCapturePolicy())

    store = LocalSnapshotStore(
        tmp_path / "cas",
        max_blob_bytes=1024 * 1024,
        max_tree_bytes=4 * 1024 * 1024,
    )
    first = publish_source_manifest(
        project_id="project-1",
        manifest=captured.manifest,
        staging_root=captured.root,
        snapshot_store=store,
        temporary_root=tmp_path / "manifest-temp",
    )
    replay = publish_source_manifest(
        project_id="project-1",
        manifest=captured.manifest,
        staging_root=captured.root,
        snapshot_store=store,
        temporary_root=tmp_path / "manifest-temp",
    )
    snapshot = build_source_snapshot(
        project_id="project-1",
        snapshot_id="snapshot-1",
        manifest=captured.manifest,
        published=first,
    )

    assert replay.content_storage_key == first.content_storage_key
    assert replay.manifest_storage_key == first.manifest_storage_key
    assert replay.content_reused is True
    assert replay.manifest_reused is True
    assert snapshot.source_kind is SourceTargetKind.DIRECTORY
    assert snapshot.commit_sha is None
    assert snapshot.snapshot_digest == captured.manifest.snapshot_digest
    materializer.discard(captured)
