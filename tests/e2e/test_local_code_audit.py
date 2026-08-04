"""End-to-end acceptance for the simplified same-machine Code Audit product."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import platform
import stat
import subprocess
from pathlib import Path
from typing import NoReturn

import pytest

from riftx.audit import (
    LocalAuditJobService,
    LocalAuditJobStatus,
    LocalAuditWorker,
    LocalAuditWorkerConfig,
)
from riftx.persistence import Database, SQLAlchemyLocalAuditJobRepository

EXPECTED_RULES = {
    "configuration.insecure_setting",
    "dependency.unpinned",
    "javascript.dangerous_api",
    "python.dangerous_api",
    "secret.hardcoded_credential",
}


@pytest.mark.parametrize("platform_name", ["Darwin", "Linux"])
@pytest.mark.parametrize("git_marked", [False, True], ids=["ordinary", "git-marked"])
async def test_local_folder_acceptance_matrix_never_executes_the_project(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    platform_name: str,
    git_marked: bool,
) -> None:
    source_root = tmp_path / "sources"
    state_root = tmp_path / "state"
    vulnerable = source_root / "vulnerable"
    safe = source_root / "safe"
    vulnerable.mkdir(parents=True)
    safe.mkdir(parents=True)
    state_root.mkdir()
    state_root.chmod(0o700)
    _seed_vulnerable(vulnerable)
    _seed_safe(safe)
    hook_markers = []
    if git_marked:
        hook_markers = [_seed_hostile_git_marker(value) for value in (vulnerable, safe)]

    before = {path.name: _tree_digest(path) for path in (vulnerable, safe)}
    database_url = f"sqlite+aiosqlite:///{state_root / 'riftx.db'}"
    database = Database(database_url)
    await database.create_schema()
    repository = SQLAlchemyLocalAuditJobRepository(database.session_factory)
    worker = LocalAuditWorker(
        repository,
        LocalAuditWorkerConfig(
            allowed_roots=(source_root,),
            protected_paths=(state_root,),
            staging_root=state_root / "staging",
            snapshot_root=state_root / "snapshots",
            max_file_bytes=64 * 1024,
            max_repository_bytes=1024 * 1024,
            max_manifest_entries=100,
            max_text_characters=64 * 1024,
            max_total_matches=100,
            max_matches_per_rule_file=20,
        ),
    )
    ids = iter(
        [
            f"audit-{platform_name.lower()}-{'git' if git_marked else 'ordinary'}-vulnerable",
            f"audit-{platform_name.lower()}-{'git' if git_marked else 'ordinary'}-safe",
        ]
    )
    service = LocalAuditJobService(repository, worker, id_factory=lambda: next(ids))

    monkeypatch.setattr(platform, "system", lambda: platform_name)
    monkeypatch.setattr(subprocess, "Popen", _forbid_effect)
    monkeypatch.setattr(subprocess, "run", _forbid_effect)
    monkeypatch.setattr(subprocess, "call", _forbid_effect)
    monkeypatch.setattr(subprocess, "check_call", _forbid_effect)
    monkeypatch.setattr(subprocess, "check_output", _forbid_effect)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _forbid_async_effect)
    monkeypatch.setattr(asyncio, "create_subprocess_shell", _forbid_async_effect)
    monkeypatch.setattr(os, "system", _forbid_effect)

    vulnerable_result = await _run(service, worker, vulnerable)
    safe_result = await _run(service, worker, safe)

    assert vulnerable_result.status is LocalAuditJobStatus.COMPLETED
    assert safe_result.status is LocalAuditJobStatus.COMPLETED
    assert {finding.rule_id for finding in vulnerable_result.findings} == EXPECTED_RULES
    assert safe_result.findings == ()
    assert vulnerable_result.snapshot_digest
    assert vulnerable_result.inventory_digest
    assert vulnerable_result.detector_run_digest
    assert vulnerable_result.report_digest
    assert vulnerable_result.json_report is not None
    assert vulnerable_result.markdown_report is not None
    report = json.loads(vulnerable_result.json_report)
    assert report["summary"]["finding_count"] == len(vulnerable_result.findings)
    assert "# RiftX Local Code Audit Report" in vulnerable_result.markdown_report
    assert all(
        "correct-horse" not in finding.evidence_excerpt
        for finding in vulnerable_result.findings
    )
    assert {path.name: _tree_digest(path) for path in (vulnerable, safe)} == before
    assert all(not marker.exists() for marker in hook_markers)

    await database.dispose()
    reopened = Database(database_url)
    await reopened.create_schema()
    restarted = SQLAlchemyLocalAuditJobRepository(reopened.session_factory)
    assert await restarted.get(vulnerable_result.id) == vulnerable_result
    assert await restarted.get(safe_result.id) == safe_result
    await reopened.dispose()


async def _run(
    service: LocalAuditJobService,
    worker: LocalAuditWorker,
    source: Path,
):
    draft = await service.create(str(source))
    await service.start(draft.id)
    return await worker.run(draft.id)


def _seed_vulnerable(root: Path) -> None:
    (root / "app.py").write_text(
        'import os\npassword = "correct-horse-battery-staple"\neval(user_input)\n',
        encoding="utf-8",
    )
    (root / "web.ts").write_text("element.innerHTML = input;\n", encoding="utf-8")
    (root / "requirements.txt").write_text("flask>=3\n", encoding="utf-8")
    (root / "config.yaml").write_text("debug: true\n", encoding="utf-8")


def _seed_safe(root: Path) -> None:
    (root / "app.py").write_text(
        "import ast\nresult = ast.literal_eval(serialized_literal)\n",
        encoding="utf-8",
    )
    (root / "web.ts").write_text("element.textContent = input;\n", encoding="utf-8")
    (root / "requirements.txt").write_text("flask==3.1.2\n", encoding="utf-8")
    (root / "config.yaml").write_text("debug: false\nverify_ssl: true\n", encoding="utf-8")
    (root / ".env.example").write_text('api_key = "${API_KEY}"\n', encoding="utf-8")


def _seed_hostile_git_marker(root: Path) -> Path:
    hook = root / ".git" / "hooks" / "pre-commit"
    hook.parent.mkdir(parents=True)
    marker = root / "hook-executed"
    hook.write_text(f"#!/bin/sh\ntouch {marker}\n", encoding="utf-8")
    hook.chmod(0o755)
    (root / ".git" / "config").write_text(
        "[core]\n\thooksPath = hooks\n",
        encoding="utf-8",
    )
    return marker


def _tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for directory, directories, files in os.walk(root, followlinks=False):
        directories.sort()
        files.sort()
        parent = Path(directory)
        for name in (*directories, *files):
            path = parent / name
            relative = path.relative_to(root).as_posix().encode()
            value = path.lstat()
            digest.update(relative + b"\0" + str(stat.S_IFMT(value.st_mode)).encode() + b"\0")
            if stat.S_ISLNK(value.st_mode):
                digest.update(os.fsencode(os.readlink(path)))
            elif stat.S_ISREG(value.st_mode):
                digest.update(path.read_bytes())
    return digest.hexdigest()


def _forbid_effect(*_args: object, **_kwargs: object) -> NoReturn:
    raise AssertionError("local Code Audit attempted a forbidden external effect")


async def _forbid_async_effect(*_args: object, **_kwargs: object) -> NoReturn:
    raise AssertionError("local Code Audit attempted a forbidden async external effect")
