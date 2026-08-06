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
from riftx.code import CodePatchReceipt, CodeWorkspaceService
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
        self.receipts: dict[str, CodePatchReceipt] = {}
        self.on_publish_patch: object | None = None
        self.publish_patch_error: Exception | None = None

    async def publish(self, run_id: str, **kwargs: object) -> str:
        self.calls.append({"run_id": run_id, **kwargs})
        return f"artifact-{len(self.calls)}"

    async def publish_patch_receipt(
        self,
        run_id: str,
        receipt: CodePatchReceipt,
    ) -> str:
        if self.publish_patch_error is not None:
            raise self.publish_patch_error
        artifact_id = f"patch-receipt-{len(self.receipts) + 1}"
        self.receipts[artifact_id] = receipt
        if callable(self.on_publish_patch):
            self.on_publish_patch()
        return artifact_id

    async def load_patch_receipt(
        self,
        run_id: str,
        artifact_id: str,
    ) -> CodePatchReceipt:
        receipt = self.receipts[artifact_id]
        assert receipt.run_id == run_id
        return receipt


def _run(run_id: str, root: Path, *, kind: RunKind = RunKind.GENERAL) -> Run:
    return Run(
        id=run_id,
        engagement_id=f"engagement-{run_id}",
        node_id="local",
        kind=kind,
        objective=Objective(description="inspect code"),
        workspace_path=str(root),
    )


def _general_service(
    *runs: Run,
    artifacts: _ArtifactPublisher | None = None,
) -> CodeWorkspaceService:
    return CodeWorkspaceService(
        runs=_Runs(*runs),  # type: ignore[arg-type]
        audits=_UnusedAudits(),  # type: ignore[arg-type]
        snapshots=_UnusedSnapshots(),  # type: ignore[arg-type]
        snapshot_store=None,
        max_snapshot_file_bytes=5 * 1024 * 1024,
        artifacts=artifacts,
    )


async def test_apply_patch_is_digest_bound_atomic_and_revertible(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    (root / "src").mkdir(parents=True)
    target = root / "src" / "app.py"
    target.write_text("def value():\n    return 1\n")
    target.chmod(0o640)
    artifacts = _ArtifactPublisher()
    service = _general_service(_run("run-1", root), artifacts=artifacts)
    original = await service.read_file("run-1", path="src/app.py")
    assert original.content_digest is not None

    applied = await service.apply_patch(
        "run-1",
        patch=(
            "*** Begin Patch\n"
            "*** Update File: src/app.py\n"
            "@@ def value():\n"
            "-    return 1\n"
            "+    return 2\n"
            "*** End Patch"
        ),
        expected_sha256=original.content_digest,
    )

    assert applied.action == "applied"
    assert applied.operation == "update"
    assert target.read_text() == "def value():\n    return 2\n"
    assert target.stat().st_mode & 0o777 == 0o640
    assert applied.receipt_artifact_id in artifacts.receipts
    assert "-    return 1" in applied.diff
    assert "+    return 2" in applied.diff

    target.write_text("external drift\n")
    with pytest.raises(ApplicationConflictError) as captured:
        await service.revert_patch(
            "run-1",
            receipt_artifact_id=applied.receipt_artifact_id,
        )
    assert captured.value.code == "code_patch_revert_digest_mismatch"

    target.write_text("def value():\n    return 2\n")
    reverted = await service.revert_patch(
        "run-1",
        receipt_artifact_id=applied.receipt_artifact_id,
    )
    assert reverted.action == "reverted"
    assert target.read_text() == "def value():\n    return 1\n"
    assert target.stat().st_mode & 0o777 == 0o640


async def test_apply_patch_add_delete_and_precommit_drift_fail_closed(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    artifacts = _ArtifactPublisher()
    service = _general_service(_run("run-1", root), artifacts=artifacts)

    artifacts.publish_patch_error = RuntimeError("receipt unavailable")
    with pytest.raises(RuntimeError, match="receipt unavailable"):
        await service.apply_patch(
            "run-1",
            patch=(
                "*** Begin Patch\n"
                "*** Add File: not-written.txt\n"
                "+content\n"
                "*** End Patch"
            ),
        )
    assert not (root / "not-written.txt").exists()
    artifacts.publish_patch_error = None

    added = await service.apply_patch(
        "run-1",
        patch=(
            "*** Begin Patch\n"
            "*** Add File: added.txt\n"
            "+hello\n"
            "*** End Patch"
        ),
    )
    assert (root / "added.txt").read_text() == "hello\n"
    await service.revert_patch("run-1", receipt_artifact_id=added.receipt_artifact_id)
    assert not (root / "added.txt").exists()

    target = root / "gone.txt"
    target.write_text("remove me\n")
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    deleted = await service.apply_patch(
        "run-1",
        patch=(
            "*** Begin Patch\n"
            "*** Delete File: gone.txt\n"
            "*** End Patch"
        ),
        expected_sha256=digest,
    )
    assert not target.exists()
    await service.revert_patch("run-1", receipt_artifact_id=deleted.receipt_artifact_id)
    assert target.read_text() == "remove me\n"

    drift = root / "drift.txt"
    drift.write_text("before\n")
    expected = hashlib.sha256(drift.read_bytes()).hexdigest()
    artifacts.on_publish_patch = lambda: drift.write_text("raced\n")
    with pytest.raises(ApplicationConflictError) as captured:
        await service.apply_patch(
            "run-1",
            patch=(
                "*** Begin Patch\n"
                "*** Update File: drift.txt\n"
                "@@\n"
                "-before\n"
                "+after\n"
                "*** End Patch"
            ),
            expected_sha256=expected,
        )
    assert captured.value.code == "code_patch_digest_mismatch"
    assert drift.read_text() == "raced\n"


@pytest.mark.parametrize(
    "path",
    [".git/config", "nested/.git", "nested/.git/hooks/post-checkout"],
)
async def test_patch_and_revert_reject_git_administrative_paths(
    tmp_path: Path,
    path: str,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    artifacts = _ArtifactPublisher()
    service = _general_service(_run("run-1", root), artifacts=artifacts)
    patch = (
        "*** Begin Patch\n"
        f"*** Add File: {path}\n"
        "+unsafe\n"
        "*** End Patch"
    )

    with pytest.raises(ApplicationConflictError) as captured:
        await service.apply_patch("run-1", patch=patch)
    assert captured.value.code == "code_patch_git_admin_forbidden"
    assert artifacts.receipts == {}

    receipt = CodePatchReceipt(
        run_id="run-1",
        operation="add",
        path=path,
        result_sha256=hashlib.sha256(b"unsafe\n").hexdigest(),
        patch=patch,
        patch_sha256=hashlib.sha256(patch.encode()).hexdigest(),
    )
    artifacts.receipts["unsafe-receipt"] = receipt
    with pytest.raises(ApplicationConflictError) as captured:
        await service.revert_patch(
            "run-1",
            receipt_artifact_id="unsafe-receipt",
        )
    assert captured.value.code == "code_patch_git_admin_forbidden"


async def test_code_audit_patch_is_read_only(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    artifacts = _ArtifactPublisher()
    service = _general_service(
        _run("audit-run", root, kind=RunKind.CODE_AUDIT),
        artifacts=artifacts,
    )

    with pytest.raises(ApplicationConflictError) as captured:
        await service.apply_patch(
            "audit-run",
            patch=(
                "*** Begin Patch\n"
                "*** Add File: denied.txt\n"
                "+denied\n"
                "*** End Patch"
            ),
        )
    assert captured.value.code == "code_workspace_read_only"


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


async def test_workspace_symbol_search_is_bounded_and_reports_fallback_quality(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "app.py").write_text(
        "class Handler:\n    def handle_request(self):\n        return True\n"
    )
    (root / "api.ts").write_text("export function handleResponse() {}\n")
    (root / "broken.py").write_text("def broken(:\n")
    (root / "binary.py").write_bytes(b"\x00not source")
    (root / "huge.py").write_bytes(b"x" * (512 * 1024 + 1))
    (root / "README.md").write_text("handle docs\n")
    service = _general_service(_run("run-1", root))

    result = await service.symbol_search("run-1", query="handle")

    assert result.backend == "builtin_static"
    assert [(item.name, item.kind, item.path) for item in result.symbols] == [
        ("handleResponse", "function", "api.ts"),
        ("Handler", "class", "app.py"),
        ("handle_request", "method", "app.py"),
    ]
    assert result.files_scanned == 3
    assert result.skipped_binary_files == 1
    assert result.skipped_large_files == 1
    assert result.skipped_unsupported_files == 1
    assert result.parse_errors == 1

    with pytest.raises(ApplicationConflictError) as captured:
        await service.symbol_search("run-1", query="\n")
    assert captured.value.code == "code_symbol_query_invalid"


async def test_workspace_find_references_skips_non_code_and_reports_ambiguity(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "api.ts").write_text(
        'function call() { return handle(); }\nconst label = "handle"; // handle\n'
    )
    (root / "app.py").write_text('def handle():\n    return handle()\n# handle\nlabel = "handle"\n')
    (root / "other.py").write_text("def handle():\n    return True\n")
    service = _general_service(_run("run-1", root))

    result = await service.find_references("run-1", symbol="handle")
    references_only = await service.find_references(
        "run-1",
        symbol="handle",
        include_declarations=False,
    )
    bounded = await service.find_references("run-1", symbol="handle", max_results=1)

    assert result.backend == "builtin_static"
    assert result.resolution == "ambiguous"
    assert result.definitions_found == 2
    assert [
        (item.path, item.line_number, item.column, item.kind) for item in result.references
    ] == [
        ("api.ts", 1, 25, "reference"),
        ("app.py", 1, 4, "definition"),
        ("app.py", 2, 11, "reference"),
        ("other.py", 1, 4, "definition"),
    ]
    assert {item.kind for item in references_only.references} == {"reference"}
    assert references_only.definitions_found == 2
    assert len(bounded.references) == 1
    assert bounded.definitions_found == 2
    assert bounded.resolution == "ambiguous"
    assert bounded.truncated is True
    assert result.files_scanned == 3
    assert result.parse_errors == 0

    with pytest.raises(ApplicationConflictError) as captured:
        await service.find_references("run-1", symbol="Handler.handle")
    assert captured.value.code == "code_reference_symbol_invalid"


async def test_workspace_find_references_marks_incomplete_parse_indeterminate(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "broken.py").write_text("def broken(:\n")
    (root / "valid.py").write_text("def handle():\n    return True\n")
    service = _general_service(_run("run-1", root))

    result = await service.find_references("run-1", symbol="handle")

    assert result.definitions_found == 1
    assert result.parse_errors == 1
    assert result.resolution == "indeterminate"
    assert result.truncated is True


async def test_workspace_call_hierarchy_reports_ast_and_lexical_edges(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "app.py").write_text(
        "def target():\n"
        "    pass\n"
        "\n"
        "def caller():\n"
        "    target()\n"
        "    helper()\n"
        "\n"
        "target()\n"
    )
    (root / "api.ts").write_text(
        "function target() {}\n"
        "function tsCaller() {\n"
        "  target();\n"
        "}\n"
    )
    service = _general_service(_run("run-1", root))

    incoming = await service.call_hierarchy(
        "run-1",
        symbol="target",
        direction="incoming",
    )
    outgoing = await service.call_hierarchy(
        "run-1",
        symbol="caller",
        direction="outgoing",
    )
    bounded = await service.call_hierarchy(
        "run-1",
        symbol="target",
        direction="incoming",
        max_results=1,
    )

    assert incoming.backend == "builtin_static"
    assert incoming.resolution == "ambiguous"
    assert incoming.definitions_found == 2
    assert incoming.analysis_modes == ["lexical", "python_ast"]
    assert [
        (item.path, item.caller, item.callee, item.confidence)
        for item in incoming.calls
    ] == [
        ("api.ts", "tsCaller", "target", "lexical"),
        ("app.py", "caller", "target", "python_ast"),
        ("app.py", None, "target", "python_ast"),
    ]
    assert outgoing.resolution == "unique"
    assert [(item.caller, item.callee) for item in outgoing.calls] == [
        ("caller", "target"),
        ("caller", "helper"),
    ]
    assert bounded.definitions_found == 2
    assert len(bounded.calls) == 1
    assert bounded.truncated is True

    with pytest.raises(ApplicationConflictError) as captured:
        await service.call_hierarchy("run-1", symbol="target", direction="sideways")  # type: ignore[arg-type]
    assert captured.value.code == "code_call_direction_invalid"


async def test_workspace_diagnostics_reports_bounded_static_parse_issues(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "broken.py").write_text("def broken(:\n")
    (root / "broken.ts").write_text("function broken( {\n")
    (root / "clean.py").write_text("def clean():\n    return True\n")
    (root / "README.md").write_text("not source")
    service = _general_service(_run("run-1", root))

    result = await service.diagnostics("run-1")
    bounded = await service.diagnostics("run-1", max_results=1)

    assert result.backend == "builtin_static"
    assert result.analysis_modes == ["lexical", "python_ast"]
    assert [(item.path, item.code, item.confidence) for item in result.diagnostics] == [
        ("broken.py", "python_syntax_error", "python_ast"),
        ("broken.ts", "unclosed_delimiter", "lexical"),
        ("broken.ts", "unclosed_delimiter", "lexical"),
    ]
    assert result.files_scanned == 3
    assert result.skipped_unsupported_files == 1
    assert result.parse_errors == 2
    assert len(bounded.diagnostics) == 1
    assert bounded.parse_errors == 2
    assert bounded.truncated is True


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


async def test_workspace_reads_exact_utf8_code_location(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    content = "αβ\nhello world\n".encode()
    (root / "source.py").write_bytes(content)
    service = _general_service(_run("run-1", root))

    location = await service.read_location(
        "run-1",
        path="source.py",
        start_line=1,
        start_column=1,
        end_line=2,
        end_column=5,
    )

    assert location.data == "β\nhello".encode()
    assert location.content_digest == hashlib.sha256(content).hexdigest()
    assert location.source == "workspace"


async def test_workspace_rejects_code_location_outside_line(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "source.py").write_text("short\n")
    service = _general_service(_run("run-1", root))

    with pytest.raises(ApplicationConflictError) as captured:
        await service.read_location(
            "run-1",
            path="source.py",
            start_line=1,
            start_column=0,
            end_line=1,
            end_column=6,
        )
    assert captured.value.code == "code_location_invalid"


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
    symbol_content = (
        b"class SnapshotHandler:\n    pass\n\n"
        b"def invoke():\n    SnapshotHandler()\n"
    )
    staged = tmp_path / "staged"
    staged.mkdir()
    (staged / "audit.py").write_bytes(content)
    (staged / "symbols.py").write_bytes(symbol_content)
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
            SnapshotBlobMetadata(
                relative_path="symbols.py",
                blob_digest=hashlib.sha256(symbol_content).hexdigest(),
                size=len(symbol_content),
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
    (output / "symbols.py").write_text("class MutableOutputOnly:\n    pass\n")
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
    symbols = await service.symbol_search(run.id, query="SnapshotHandler")
    references = await service.find_references(
        run.id,
        symbol="SnapshotHandler",
        file_glob="symbols.py",
    )
    calls = await service.call_hierarchy(
        run.id,
        symbol="SnapshotHandler",
        direction="incoming",
        file_glob="symbols.py",
    )
    diagnostics = await service.diagnostics(
        run.id,
        file_glob="audit.py",
    )

    assert read.source == "audit_snapshot"
    assert read.source_digest == snapshot_digest
    assert read.content == content[:8].decode()
    assert read.content_digest == hashlib.sha256(content).hexdigest()
    assert read.artifact_id == "artifact-1"
    assert artifacts.calls[0]["audit_id"] == "audit-1"
    assert artifacts.calls[0]["content"] == content
    assert grepped.matches[0].path == "audit.py"
    assert symbols.source == "audit_snapshot"
    assert symbols.source_digest == snapshot_digest
    assert [item.name for item in symbols.symbols] == ["SnapshotHandler"]
    assert references.source == "audit_snapshot"
    assert references.source_digest == snapshot_digest
    assert references.resolution == "unique"
    assert references.references[0].path == "symbols.py"
    assert calls.source == "audit_snapshot"
    assert calls.source_digest == snapshot_digest
    assert calls.calls[0].caller == "invoke"
    assert diagnostics.source == "audit_snapshot"
    assert diagnostics.source_digest == snapshot_digest
    assert diagnostics.diagnostics[0].path == "audit.py"


async def test_binary_preview_is_bounded_base64(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "blob.bin").write_bytes(b"\x00\xffpayload")
    service = _general_service(_run("run-1", root))

    result = await service.read_file("run-1", path="blob.bin", max_bytes=4)

    assert result.encoding == "base64"
    assert result.content == "AP9wYQ=="
    assert result.eof is False
