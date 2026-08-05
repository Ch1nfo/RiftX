from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from riftx.application.errors import ApplicationConflictError
from riftx.code import GitWorkspaceService
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
