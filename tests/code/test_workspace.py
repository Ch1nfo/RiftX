from __future__ import annotations

import hashlib
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from riftx.application.errors import ApplicationConflictError
from riftx.audit import (
    LocalSnapshotStore,
    SnapshotBlobMetadata,
    SnapshotCASDescriptor,
    SnapshotStagedTree,
)
from riftx.code import CodeWorkspaceService
from riftx.domain import Objective, Run, RunKind


class _Runs:
    def __init__(self, *runs: Run) -> None:
        self._runs = {run.id: run for run in runs}

    async def get(self, run_id: str) -> Run | None:
        return self._runs.get(run_id)


class _UnusedAudits:
    async def get_by_run_authorized(self, *_: object, **__: object) -> None:
        raise AssertionError("General workspace must not query Audit state")


class _UnusedSnapshots:
    async def get(self, *_: object, **__: object) -> None:
        raise AssertionError("General workspace must not query Snapshot state")


class _AuditReads:
    def __init__(
        self,
        *,
        run_id: str,
        project_id: str,
        snapshot_id: str,
        audit_id: str = "audit-1",
    ) -> None:
        self._run_id = run_id
        self._project_id = project_id
        self._snapshot_id = snapshot_id
        self._audit_id = audit_id

    async def get_by_run_authorized(self, run_id: str, *, authorize: object) -> object:
        assert run_id == self._run_id
        authorize(  # type: ignore[operator]
            SimpleNamespace(
                scan_run_id=run_id,
                run_id=run_id,
                run_kind=RunKind.CODE_AUDIT.value,
            )
        )
        return SimpleNamespace(
            audit=SimpleNamespace(
                value=SimpleNamespace(id=self._audit_id, snapshot_id=self._snapshot_id)
            ),
            project=SimpleNamespace(value=SimpleNamespace(id=self._project_id)),
        )


class _Snapshots:
    def __init__(self, snapshot: object) -> None:
        self._snapshot = snapshot

    async def get(self, project_id: str, snapshot_id: str) -> object:
        assert project_id == self._snapshot.project_id  # type: ignore[attr-defined]
        assert snapshot_id == self._snapshot.id  # type: ignore[attr-defined]
        return self._snapshot


class _ArtifactPublisher:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def publish(self, run_id: str, **kwargs: object) -> str:
        self.calls.append({"run_id": run_id, **kwargs})
        return f"artifact-{len(self.calls)}"


def _run(run_id: str, root: Path, *, kind: RunKind = RunKind.GENERAL) -> Run:
    return Run(
        id=run_id,
        engagement_id=f"engagement-{run_id}",
        node_id="local",
        kind=kind,
        objective=Objective(description="inspect code"),
        workspace_path=str(root),
    )


def _general_service(*runs: Run) -> CodeWorkspaceService:
    return CodeWorkspaceService(
        runs=_Runs(*runs),  # type: ignore[arg-type]
        audits=_UnusedAudits(),  # type: ignore[arg-type]
        snapshots=_UnusedSnapshots(),  # type: ignore[arg-type]
        snapshot_store=None,
        max_snapshot_file_bytes=5 * 1024 * 1024,
    )


async def test_workspace_tools_list_read_glob_and_literal_grep(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    (root / "src").mkdir(parents=True)
    (root / "src" / "app.py").write_text("def main():\n    return 'needle'\n")
    (root / "README.md").write_text("needle docs\n")
    service = _general_service(_run("run-1", root))

    listed = await service.list_files("run-1", recursive=True)
    read = await service.read_file("run-1", path="src/app.py", max_bytes=12)
    globbed = await service.glob("run-1", pattern="*.py")
    grepped = await service.grep(
        "run-1",
        query="NEEDLE",
        file_glob="*.py",
        case_sensitive=False,
    )

    assert [entry.path for entry in listed.entries] == [
        "README.md",
        "src",
        "src/app.py",
    ]
    assert read.content == "def main():\n"
    assert read.eof is False
    assert read.next_offset == 12
    assert [entry.path for entry in globbed.entries] == ["src/app.py"]
    assert [(match.path, match.line_number) for match in grepped.matches] == [
        ("src/app.py", 2)
    ]
    assert grepped.files_scanned == 1


async def test_workspace_reads_are_run_scoped_and_bounded(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    (first / "owner.txt").write_text("first")
    (second / "owner.txt").write_text("second")
    (first / "large.txt").write_bytes(b"x" * (5 * 1024 * 1024 + 1))
    service = _general_service(
        _run("run-1", first),
        _run("run-2", second),
    )

    result = await service.read_file("run-1", path="owner.txt")
    assert result.content == "first"

    with pytest.raises(ApplicationConflictError) as captured:
        await service.read_file("run-1", path="large.txt")
    assert captured.value.code == "code_file_too_large"


async def test_partial_workspace_read_publishes_full_owner_bound_artifact(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    content = b"0123456789" + b"x" * (64 * 1024)
    (root / "large.txt").write_bytes(content)
    artifacts = _ArtifactPublisher()
    service = CodeWorkspaceService(
        runs=_Runs(_run("run-1", root)),  # type: ignore[arg-type]
        audits=_UnusedAudits(),  # type: ignore[arg-type]
        snapshots=_UnusedSnapshots(),  # type: ignore[arg-type]
        snapshot_store=None,
        max_snapshot_file_bytes=128 * 1024,
        artifacts=artifacts,
    )

    result = await service.read_file("run-1", path="large.txt", max_bytes=4)
    many = await service.read_many_files(
        "run-1",
        paths=["large.txt"],
        max_bytes_per_file=3,
    )

    assert result.content == "0123"
    assert result.artifact_id == "artifact-1"
    assert many.files[0].content == "012"
    assert many.files[0].artifact_id == "artifact-2"
    assert artifacts.calls == [
        {
            "run_id": "run-1",
            "audit_id": None,
            "path": "large.txt",
            "content": content,
            "source_digest": None,
        },
        {
            "run_id": "run-1",
            "audit_id": None,
            "path": "large.txt",
            "content": content,
            "source_digest": None,
        },
    ]


@pytest.mark.parametrize("path", ["../secret", "/etc/passwd", "src/../secret", "src//x"])
async def test_workspace_rejects_non_normalized_or_absolute_paths(
    tmp_path: Path,
    path: str,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    service = _general_service(_run("run-1", root))

    with pytest.raises(ApplicationConflictError) as captured:
        await service.read_file("run-1", path=path)
    assert captured.value.code == "code_path_invalid"


async def test_workspace_never_follows_symlinks_or_reads_special_files(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    (root / "escape").symlink_to(outside)
    fifo = root / "pipe"
    os.mkfifo(fifo)
    service = _general_service(_run("run-1", root))

    listed = await service.list_files("run-1")
    assert [(entry.path, entry.type) for entry in listed.entries] == [
        ("escape", "symlink"),
        ("pipe", "special"),
    ]
    for path in ("escape", "pipe"):
        with pytest.raises(ApplicationConflictError):
            await service.read_file("run-1", path=path)


async def test_code_audit_reads_owner_bound_snapshot_not_run_output(tmp_path: Path) -> None:
    project_id = "project-1"
    snapshot_id = "snapshot-1"
    snapshot_digest = "1" * 64
    manifest_digest = "2" * 64
    content = b"snapshot needle\n" + b"x" * (64 * 1024)
    staged = tmp_path / "staged"
    staged.mkdir()
    (staged / "audit.py").write_bytes(content)
    descriptor = SnapshotCASDescriptor(
        project_id=project_id,
        snapshot_digest=snapshot_digest,
        manifest_digest=manifest_digest,
        blobs=(
            SnapshotBlobMetadata(
                relative_path="audit.py",
                blob_digest=hashlib.sha256(content).hexdigest(),
                size=len(content),
                mode=0o100644,
            ),
        ),
    )
    store = LocalSnapshotStore(
        (tmp_path / "snapshots").resolve(),
        max_blob_bytes=128 * 1024,
        max_tree_bytes=256 * 1024,
    )
    published = store.put_staged_tree(
        SnapshotStagedTree(root=staged.resolve(), descriptor=descriptor)
    )
    output = tmp_path / "audit-output"
    output.mkdir()
    (output / "audit.py").write_text("wrong mutable output")
    run = _run("run-audit", output, kind=RunKind.CODE_AUDIT)
    snapshot = SimpleNamespace(
        id=snapshot_id,
        project_id=project_id,
        snapshot_digest=snapshot_digest,
        manifest_digest=manifest_digest,
        content_storage_key=published.content_storage_key,
    )
    artifacts = _ArtifactPublisher()
    service = CodeWorkspaceService(
        runs=_Runs(run),  # type: ignore[arg-type]
        audits=_AuditReads(
            run_id=run.id,
            project_id=project_id,
            snapshot_id=snapshot_id,
        ),  # type: ignore[arg-type]
        snapshots=_Snapshots(snapshot),  # type: ignore[arg-type]
        snapshot_store=store,
        max_snapshot_file_bytes=128 * 1024,
        artifacts=artifacts,
    )

    read = await service.read_file(run.id, path="audit.py", max_bytes=8)
    grepped = await service.grep(run.id, query="needle")

    assert read.source == "audit_snapshot"
    assert read.source_digest == snapshot_digest
    assert read.content == content[:8].decode()
    assert read.content_digest == hashlib.sha256(content).hexdigest()
    assert read.artifact_id == "artifact-1"
    assert artifacts.calls[0]["audit_id"] == "audit-1"
    assert artifacts.calls[0]["content"] == content
    assert grepped.matches[0].path == "audit.py"


async def test_binary_preview_is_bounded_base64(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "blob.bin").write_bytes(b"\x00\xffpayload")
    service = _general_service(_run("run-1", root))

    result = await service.read_file("run-1", path="blob.bin", max_bytes=4)

    assert result.encoding == "base64"
    assert result.content == "AP9wYQ=="
    assert result.eof is False
