from __future__ import annotations

import os
import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest

from riftx.audit import (
    FileInventoryDecision,
    FileInventoryError,
    FileInventoryFailure,
    LocalSnapshotStore,
    LocalSourceMaterializer,
    SnapshotCASBinding,
    SourceCapturePolicy,
    SourceClassification,
    build_file_inventory,
    build_file_scope_units,
    build_source_snapshot,
    load_source_manifest,
    open_authorized_local_source,
    publish_source_manifest,
)
from riftx.domain import SourceTargetKind

NOW = datetime(2026, 8, 4, 12, tzinfo=UTC)


def _capture(source: Path, tmp_path: Path, *, policy: SourceCapturePolicy):
    materializer = LocalSourceMaterializer(tmp_path / "staging")
    with open_authorized_local_source(source, allowed_roots=(tmp_path,)) as authorized:
        captured = materializer.materialize(authorized, policy=policy)
    return materializer, captured


def _remove_test_store(root: Path) -> None:
    for current, directories, files in os.walk(root, topdown=False, followlinks=False):
        current_path = Path(current)
        for name in files:
            candidate = current_path / name
            if not candidate.is_symlink():
                candidate.chmod(0o600)
        for name in directories:
            candidate = current_path / name
            if not candidate.is_symlink():
                candidate.chmod(0o700)
        current_path.chmod(0o700)
    shutil.rmtree(root)


def test_inventory_loads_sealed_manifest_and_applies_minimal_defaults(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    files = {
        "README.md": "review notes\n",
        "app/config.conf": "debug=false\n",
        "app/main.py": "print('safe')\n",
        "build/generated.js": "const generated = true;\n",
        "node_modules/library.js": "module.exports = {};\n",
        ".pytest_cache/state.py": "cached = true\n",
    }
    for relative_path, content in files.items():
        target = source / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    (source / "outside-link").symlink_to(tmp_path / "outside")
    materializer, captured = _capture(
        source,
        tmp_path,
        policy=SourceCapturePolicy(include_generated=True, include_vendor=True),
    )
    store = LocalSnapshotStore(
        tmp_path / "cas",
        max_blob_bytes=1024 * 1024,
        max_tree_bytes=8 * 1024 * 1024,
    )
    published = publish_source_manifest(
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
        published=published,
        created_at=NOW,
        sealed_at=NOW,
    )

    loaded = load_source_manifest(store, snapshot)
    first = build_file_inventory(loaded)
    second = build_file_inventory(loaded)

    assert first == second
    assert [entry.relative_path for entry in first.entries] == sorted(
        [*files, "outside-link"]
    )
    entries = {entry.relative_path: entry for entry in first.entries}
    assert entries["app/main.py"].language == "python"
    assert entries["app/main.py"].category is SourceClassification.SOURCE
    assert entries["app/config.conf"].category is SourceClassification.CONFIGURATION
    assert entries["README.md"].category is SourceClassification.DOCUMENTATION
    for relative_path in (
        ".pytest_cache/state.py",
        "build/generated.js",
        "node_modules/library.js",
    ):
        assert entries[relative_path].decision is FileInventoryDecision.EXCLUDED
    assert entries["outside-link"].decision is FileInventoryDecision.SKIPPED
    assert entries["outside-link"].reason == "unsupported_object_type"
    statistics = first.statistics
    assert statistics.total_files == len(files) + 1
    assert statistics.included_files == 3
    assert statistics.excluded_files == 3
    assert statistics.skipped_files == 1
    assert sum(item.files for item in statistics.by_language) == statistics.total_files
    assert sum(item.files for item in statistics.by_category) == statistics.total_files
    assert "cas" not in repr(first)
    assert os.fspath(source) not in repr(first)
    materializer.discard(captured)
    _remove_test_store(tmp_path / "cas")


def test_inventory_preserves_capture_exclusion_and_skip_reasons(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "main.py").write_text("value = 1\n", encoding="utf-8")
    (source / "vendor").mkdir()
    (source / "vendor" / "library.py").write_text("value = 2\n", encoding="utf-8")
    os.mkfifo(source / "events.pipe")
    materializer, captured = _capture(source, tmp_path, policy=SourceCapturePolicy())

    inventory = build_file_inventory(captured.manifest)
    entries = {entry.relative_path: entry for entry in inventory.entries}

    assert entries["main.py"].decision is FileInventoryDecision.INCLUDED
    assert entries["vendor/library.py"].decision is FileInventoryDecision.EXCLUDED
    assert entries["vendor/library.py"].reason == "vendor_excluded"
    assert entries["events.pipe"].decision is FileInventoryDecision.SKIPPED
    assert entries["events.pipe"].reason == "special_file"
    assert inventory.statistics.included_files == 1
    assert inventory.statistics.excluded_files == 1
    assert inventory.statistics.skipped_files == 1
    materializer.discard(captured)


def test_file_scope_units_are_deterministic_and_only_cover_included_files(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "main.py").write_text("value = 1\n", encoding="utf-8")
    (source / "vendor").mkdir()
    (source / "vendor" / "library.py").write_text("value = 2\n", encoding="utf-8")
    materializer, captured = _capture(source, tmp_path, policy=SourceCapturePolicy())
    inventory = build_file_inventory(captured.manifest)

    first = build_file_scope_units(
        inventory,
        audit_id="audit-1",
        snapshot_id="snapshot-1",
        created_at=NOW,
    )
    second = build_file_scope_units(
        inventory,
        audit_id="audit-1",
        snapshot_id="snapshot-1",
        created_at=NOW,
    )

    assert first == second
    assert len(first) == 1
    assert first[0].relative_path == "main.py"
    assert first[0].required_analyses == ("static_rules",)
    assert first[0].id == f"scope-{first[0].stable_key}"
    assert "agent_review" not in first[0].required_analyses
    materializer.discard(captured)


class _BytesReader:
    def __init__(self, content: bytes) -> None:
        self._content = content
        self._read = False

    def read(self, _max_bytes: int) -> bytes:
        if self._read:
            return b""
        self._read = True
        return self._content

    def verify_complete(self) -> None:
        return None

    def close(self) -> None:
        return None


class _CorruptManifestStore:
    def __init__(self, descriptor) -> None:
        self._descriptor = descriptor

    def describe(self, _binding: SnapshotCASBinding, _storage_key: str):
        return self._descriptor

    def open_blob(self, *_args, **_kwargs):
        return _BytesReader(b"not-json")


def test_manifest_load_fails_closed_without_rendering_private_storage(
    tmp_path: Path,
) -> None:
    source = tmp_path / "private-source"
    source.mkdir()
    (source / "main.py").write_text("value = 1\n", encoding="utf-8")
    materializer, captured = _capture(source, tmp_path, policy=SourceCapturePolicy())
    store = LocalSnapshotStore(
        tmp_path / "private-cas",
        max_blob_bytes=1024 * 1024,
        max_tree_bytes=4 * 1024 * 1024,
    )
    published = publish_source_manifest(
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
        published=published,
        created_at=NOW,
        sealed_at=NOW,
    )
    binding = SnapshotCASBinding(
        project_id=snapshot.project_id,
        snapshot_digest=snapshot.snapshot_digest,
        manifest_digest=snapshot.manifest_digest,
    )
    descriptor = store.describe(binding, snapshot.manifest_storage_key)

    forged_snapshot = type(snapshot).model_validate(
        {
            **snapshot.model_dump(mode="python"),
            "source_kind": SourceTargetKind.REVISION,
            "commit_sha": "a" * 64,
        }
    )
    with pytest.raises(FileInventoryError) as owner_mismatch:
        load_source_manifest(store, forged_snapshot)
    assert owner_mismatch.value.failure is FileInventoryFailure.MANIFEST_INTEGRITY

    with pytest.raises(FileInventoryError) as raised:
        load_source_manifest(_CorruptManifestStore(descriptor), snapshot)

    assert raised.value.failure is FileInventoryFailure.MANIFEST_INTEGRITY
    assert os.fspath(source) not in str(raised.value)
    assert snapshot.manifest_storage_key not in str(raised.value)
    materializer.discard(captured)
    _remove_test_store(tmp_path / "private-cas")
