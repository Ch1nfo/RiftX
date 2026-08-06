from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

import pytest

from riftx.application.errors import ApplicationConflictError
from riftx.code import GitWorkspaceService
from riftx.code import git as git_module
from riftx.domain import Objective, Run, RunKind


class _Runs:
    def __init__(self, *runs: Run) -> None:
        self._runs = {run.id: run for run in runs}

    async def get(self, run_id: str) -> Run | None:
        return self._runs.get(run_id)


def _git(root: Path, *arguments: str) -> str:
    result = subprocess.run(
        ("git", "-C", str(root), *arguments),
        check=True,
        capture_output=True,
        env={
            **os.environ,
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
        },
    )
    return result.stdout.decode("utf-8", errors="strict").strip()


def _repository(tmp_path: Path) -> Path:
    root = tmp_path / "repository"
    root.mkdir()
    _git(root, "init", "--quiet")
    _git(root, "config", "user.email", "code@example.invalid")
    _git(root, "config", "user.name", "Code Fixture")
    (root / "app.py").write_text("print('one')\n")
    (root / "README.md").write_text("# Fixture\n")
    _git(root, "add", "app.py", "README.md")
    _git(root, "commit", "--quiet", "-m", "initial")
    return root


def _run(run_id: str, root: Path, *, kind: RunKind = RunKind.GENERAL) -> Run:
    return Run(
        id=run_id,
        engagement_id=f"engagement-{run_id}",
        node_id="local",
        kind=kind,
        objective=Objective(description="inspect git"),
        workspace_path=str(root),
    )


def _service(run: Run) -> GitWorkspaceService:
    return GitWorkspaceService(_Runs(run))  # type: ignore[arg-type]


async def test_git_status_diff_and_log_are_bounded_native_reads(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    (root / "app.py").write_text("print('two')\n")
    (root / "README.md").write_text("# Staged\n")
    _git(root, "add", "README.md")
    (root / "new.txt").write_text("untracked\n")
    service = _service(_run("run-1", root))

    status = await service.status("run-1")
    unstaged = await service.diff("run-1", path="app.py")
    staged = await service.diff("run-1", staged=True, path="README.md")
    history = await service.log("run-1", max_entries=1)

    assert status.branch is not None
    observed_status = {
        (entry.path, entry.index_status, entry.worktree_status)
        for entry in status.entries
    }
    assert observed_status == {
        ("README.md", "M", " "),
        ("app.py", " ", "M"),
        ("new.txt", "?", "?"),
    }
    assert "-print('one')" in unstaged.content
    assert "+print('two')" in unstaged.content
    assert "-# Fixture" in staged.content
    assert "+# Staged" in staged.content
    assert len(history.commits) == 1
    assert history.commits[0].subject == "initial"
    assert history.commits[0].author == "Code Fixture"
    assert history.truncated is False


async def test_git_log_reports_entry_and_output_truncation(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    for index in range(3):
        (root / "app.py").write_text(f"print({index})\n")
        _git(root, "add", "app.py")
        _git(root, "commit", "--quiet", "-m", f"change-{index}")
    (root / "app.py").write_text("x" * 4096)
    service = _service(_run("run-1", root))

    history = await service.log("run-1", path="app.py", max_entries=2)
    diff = await service.diff("run-1", path="app.py", max_bytes=32)

    assert [commit.subject for commit in history.commits] == ["change-2", "change-1"]
    assert history.truncated is True
    assert diff.bytes_returned == 32
    assert diff.truncated is True


async def test_git_log_returns_empty_history_for_unborn_repository(tmp_path: Path) -> None:
    root = tmp_path / "repository"
    root.mkdir()
    _git(root, "init", "--quiet")
    service = _service(_run("run-1", root))

    history = await service.log("run-1")

    assert history.commits == []
    assert history.truncated is False


async def test_git_status_does_not_refresh_index_or_run_hooks(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    marker = tmp_path / "hook-ran"
    hook = root / ".git" / "hooks" / "post-index-change"
    hook.write_text(f"#!/bin/sh\ntouch {marker}\n")
    hook.chmod(0o755)
    index = root / ".git" / "index"
    before = index.stat()
    service = _service(_run("run-1", root))

    await service.status("run-1")

    after = index.stat()
    assert (after.st_size, after.st_mtime_ns, after.st_ctime_ns) == (
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    )
    assert marker.exists() is False


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("core.fsmonitor", "/bin/false"),
        ("diff.external", "/bin/false"),
        ("filter.evil.clean", "/bin/false"),
        ("include.path", "/tmp/external-git-config"),
    ],
)
async def test_git_tools_reject_repository_config_with_external_behavior(
    tmp_path: Path,
    key: str,
    value: str,
) -> None:
    root = _repository(tmp_path)
    _git(root, "config", key, value)
    service = _service(_run("run-1", root))

    with pytest.raises(ApplicationConflictError) as captured:
        await service.status("run-1")

    assert captured.value.code == "code_git_config_unsafe"

    with pytest.raises(ApplicationConflictError) as captured:
        await service.create_worktree("run-1", name="fix")
    assert captured.value.code == "code_git_config_unsafe"
    assert not any(root.glob(".riftx-wt-*"))


async def test_git_tools_reject_admin_symlink_and_external_object_store(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    target = tmp_path / "outside"
    target.mkdir()
    (root / ".git" / "refs" / "escape").symlink_to(target)
    service = _service(_run("run-1", root))

    with pytest.raises(ApplicationConflictError) as captured:
        await service.log("run-1")
    assert captured.value.code == "code_git_admin_unsafe"

    (root / ".git" / "refs" / "escape").unlink()
    alternates = root / ".git" / "objects" / "info" / "alternates"
    alternates.write_text(str(target))
    with pytest.raises(ApplicationConflictError) as captured:
        await service.log("run-1")
    assert captured.value.code == "code_git_admin_unsafe"


async def test_git_tools_reject_code_audit_snapshot_and_non_normalized_paths(
    tmp_path: Path,
) -> None:
    root = _repository(tmp_path)
    audit = _service(_run("run-audit", root, kind=RunKind.CODE_AUDIT))
    general = _service(_run("run-1", root))

    with pytest.raises(ApplicationConflictError) as captured:
        await audit.status("run-audit")
    assert captured.value.code == "code_git_unavailable"

    with pytest.raises(ApplicationConflictError) as captured:
        await general.diff("run-1", path="../outside")
    assert captured.value.code == "code_path_invalid"


async def test_create_worktree_is_run_owned_detached_and_idempotent(
    tmp_path: Path,
) -> None:
    root = _repository(tmp_path)
    marker = tmp_path / "post-checkout-ran"
    hook = root / ".git" / "hooks" / "post-checkout"
    hook.write_text(f"#!/bin/sh\ntouch {marker}\n")
    hook.chmod(0o755)
    service = _service(_run("run-1", root))
    expected_head = _git(root, "rev-parse", "HEAD")

    created = await service.create_worktree(
        "run-1",
        name="audit-fix",
    )
    replayed = await service.create_worktree(
        "run-1",
        name="audit-fix",
        start_point=expected_head,
    )

    owner = hashlib.sha256(b"run-1").hexdigest()[:24]
    expected_path = f".riftx-wt-{owner}-audit-fix"
    worktree = root / expected_path
    assert created.action == "created"
    assert replayed.action == "existing"
    assert created.path == replayed.path == expected_path
    assert created.head_commit == replayed.head_commit == expected_head
    assert created.detached is replayed.detached is True
    assert (worktree / "app.py").read_text() == "print('one')\n"
    assert (worktree / ".git").is_file()
    assert _git(worktree, "rev-parse", "--abbrev-ref", "HEAD") == "HEAD"
    assert marker.exists() is False


async def test_create_worktree_rejects_code_audit_invalid_inputs_and_conflicts(
    tmp_path: Path,
) -> None:
    root = _repository(tmp_path)
    general = _service(_run("run-1", root))
    audit = _service(_run("run-audit", root, kind=RunKind.CODE_AUDIT))

    with pytest.raises(ApplicationConflictError) as captured:
        await audit.create_worktree("run-audit", name="fix")
    assert captured.value.code == "code_git_unavailable"

    for name in ("../escape", "nested/path", ".", "bad name"):
        with pytest.raises(ApplicationConflictError) as captured:
            await general.create_worktree("run-1", name=name)
        assert captured.value.code == "code_worktree_name_invalid"

    with pytest.raises(ApplicationConflictError) as captured:
        await general.create_worktree("run-1", name="fix", start_point="main~1")
    assert captured.value.code == "code_worktree_start_point_invalid"

    with pytest.raises(ApplicationConflictError) as captured:
        await general.create_worktree("run-1", name="fix", start_point="f" * 40)
    assert captured.value.code == "code_worktree_start_point_unavailable"

    first = await general.create_worktree("run-1", name="fix")
    (root / "next.txt").write_text("next\n")
    _git(root, "add", "next.txt")
    _git(root, "commit", "--quiet", "-m", "next")
    with pytest.raises(ApplicationConflictError) as captured:
        await general.create_worktree("run-1", name="fix")
    assert captured.value.code == "code_worktree_conflict"
    assert (root / first.path / "next.txt").exists() is False


@pytest.mark.parametrize("entry_type", ["symlink", "fifo", "directory"])
async def test_create_worktree_rejects_preexisting_unowned_destination(
    tmp_path: Path,
    entry_type: str,
) -> None:
    root = _repository(tmp_path)
    owner = hashlib.sha256(b"run-1").hexdigest()[:24]
    destination = root / f".riftx-wt-{owner}-fix"
    outside = tmp_path / "outside"
    outside.mkdir()
    if entry_type == "symlink":
        destination.symlink_to(outside, target_is_directory=True)
    elif entry_type == "fifo":
        os.mkfifo(destination)
    else:
        destination.mkdir()
    service = _service(_run("run-1", root))

    with pytest.raises(ApplicationConflictError) as captured:
        await service.create_worktree("run-1", name="fix")

    expected = (
        "code_worktree_conflict"
        if entry_type == "directory"
        else "code_worktree_path_unsafe"
    )
    assert captured.value.code == expected
    assert list(outside.iterdir()) == []


async def test_create_worktree_uses_distinct_run_owned_destinations(
    tmp_path: Path,
) -> None:
    root = _repository(tmp_path)
    service = GitWorkspaceService(
        _Runs(_run("run-1", root), _run("run-2", root))  # type: ignore[arg-type]
    )

    first = await service.create_worktree("run-1", name="fix")
    second = await service.create_worktree("run-2", name="fix")

    assert first.path != second.path
    assert (root / first.path / "app.py").is_file()
    assert (root / second.path / "app.py").is_file()


async def test_create_worktree_rolls_back_failed_postcondition(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _repository(tmp_path)
    owner = hashlib.sha256(b"run-1").hexdigest()[:24]
    relative_path = f".riftx-wt-{owner}-fix"
    service = _service(_run("run-1", root))

    def reject_postcondition(*_: object, **__: object) -> None:
        raise ApplicationConflictError(
            "code_worktree_invalid",
            "synthetic postcondition failure",
        )

    monkeypatch.setattr(
        git_module._SafeGitRepository,
        "_validate_worktree",
        reject_postcondition,
    )

    with pytest.raises(ApplicationConflictError) as captured:
        await service.create_worktree("run-1", name="fix")

    assert captured.value.code == "code_worktree_invalid"
    assert (root / relative_path).exists() is False
    assert relative_path not in _git(root, "worktree", "list", "--porcelain")
