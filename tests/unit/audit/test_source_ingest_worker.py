from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import zlib
from pathlib import Path
from typing import Any

import pytest

from riftx.audit.source_ingest_contract import (
    SourceIngestWorkerRequest,
    SourceIngestWorkerResult,
)
from riftx.audit_worker import preflight as worker
from riftx.domain.audit import AuditMode, SourceTargetKind


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(
        ("git", "-C", str(repository), *arguments),
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _git_bytes(repository: Path, *arguments: str) -> bytes:
    result = subprocess.run(
        ("git", "-C", str(repository), *arguments),
        check=True,
        capture_output=True,
    )
    return result.stdout


def _replace_loose_object(
    repository: Path,
    *,
    object_id: str,
    object_type: str,
    body: bytes,
) -> None:
    object_path = repository / ".git" / "objects" / object_id[:2] / object_id[2:]
    assert object_path.is_file()
    raw = f"{object_type} {len(body)}\0".encode("ascii") + body
    object_path.chmod(0o600)
    object_path.write_bytes(zlib.compress(raw))


def _repository(tmp_path: Path, *, object_format: str | None = None) -> Path:
    repository = tmp_path / "repository"
    repository.mkdir()
    init_arguments = ["init", "--quiet"]
    if object_format is not None:
        init_arguments.append(f"--object-format={object_format}")
    _git(repository, *init_arguments)
    _git(repository, "config", "user.email", "audit@example.invalid")
    _git(repository, "config", "user.name", "Audit Fixture")
    (repository / "main.py").write_text("print('riftx')\n", encoding="utf-8")
    (repository / "README.md").write_text("# fixture\n", encoding="utf-8")
    _git(repository, "add", "main.py", "README.md")
    _git(repository, "commit", "--quiet", "-m", "fixture")
    return repository


def _request() -> SourceIngestWorkerRequest:
    source = worker.SOURCE_ROOT.stat()
    expected_mount_identity_digest = worker._domain_digest(
        worker.SOURCE_MOUNT_IDENTITY_SCHEMA,
        {
            "filesystem_type": "ext4",
            "schema_version": worker.SOURCE_MOUNT_IDENTITY_SCHEMA,
            "st_dev": int(source.st_dev),
            "st_ino": int(source.st_ino),
        },
    )
    return SourceIngestWorkerRequest(
        capsule_id="capsule-1",
        request_digest=_digest("request"),
        source_root_identity_digest=_digest("source-root"),
        repository_descriptor_identity_digest=_digest("descriptor"),
        expected_source_mount_identity_digest=expected_mount_identity_digest,
        target_kind=SourceTargetKind.WORKING_TREE,
        revision="HEAD",
        mode=AuditMode.STANDARD,
        include_untracked=True,
        max_files=100,
        max_repository_bytes=1024 * 1024,
        max_file_bytes=128 * 1024,
        max_git_output_bytes=1024 * 1024,
        command_timeout_seconds=10,
    )


def _revision_request() -> SourceIngestWorkerRequest:
    payload = _request().model_dump(mode="python")
    payload.update(
        {
            "include_untracked": False,
            "target_kind": SourceTargetKind.REVISION,
        }
    )
    return SourceIngestWorkerRequest.model_validate(payload)


def _parse_result(value: dict[str, object]) -> SourceIngestWorkerResult:
    return SourceIngestWorkerResult.model_validate_json(
        json.dumps(value, sort_keys=True, separators=(",", ":"))
    )


@pytest.fixture(autouse=True)
def _local_mountinfo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    value = tmp_path.stat()
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text(
        f"42 1 {os.major(value.st_dev)}:{os.minor(value.st_dev)} / / rw - ext4 /dev/test rw\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(worker, "MOUNTINFO_PATH", mountinfo)


def test_worker_produces_bounded_metadata_without_returning_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _repository(tmp_path)
    (repository / "main.py").write_text("print('changed')\n", encoding="utf-8")
    (repository / "extra.ts").write_text("export const value = 1;\n", encoding="utf-8")
    monkeypatch.setattr(worker, "SOURCE_ROOT", repository)

    payload = _request().model_dump(mode="json")
    result = _parse_result(worker._execute(payload))

    assert result.outcome.value == "succeeded"
    assert result.dirty is True
    assert result.unstaged is True
    assert result.untracked is True
    assert result.file_count == 3
    assert result.source_mount_identity_digest is not None
    assert result.source_mount_proof_digest is not None
    assert result.source_mount_identity_digest == payload["expected_source_mount_identity_digest"]
    assert {item.language_id for item in result.language_estimates} == {
        "markdown",
        "python",
        "typescript",
    }
    serialized = result.model_dump_json()
    assert str(repository) not in serialized
    assert "main.py" not in serialized
    assert "extra.ts" not in serialized


def test_worker_rejects_repository_controlled_execution_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _repository(tmp_path)
    _git(repository, "config", "core.hooksPath", "/tmp/hostile-hooks")
    monkeypatch.setattr(worker, "SOURCE_ROOT", repository)

    result = _parse_result(worker._execute(_request().model_dump(mode="json")))

    assert result.outcome.value == "rejected"
    assert result.safe_error_code == "audit_git_config_unsafe"
    assert result.blocking_errors == ("audit_git_config_unsafe",)


def test_worker_rejects_mount_identity_drift_before_starting_git(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _repository(tmp_path)
    monkeypatch.setattr(worker, "SOURCE_ROOT", repository)
    payload = _request().model_dump(mode="python")
    payload["expected_source_mount_identity_digest"] = _digest("drifted-mount")

    class UnexpectedGitAdapter:
        def __init__(self, **_kwargs: object) -> None:
            raise AssertionError("Git must not start before mount identity validation")

    monkeypatch.setattr(worker, "SafeGitAdapter", UnexpectedGitAdapter)

    with pytest.raises(worker.WorkerFailed) as error:
        worker._execute(payload)

    assert error.value.code == "audit_source_ingest_mount_identity_changed"


@pytest.mark.parametrize(
    ("key", "value"),
    (
        ("credential.helper", "/tmp/credential-helper"),
        ("filter.hostile.clean", "/tmp/clean-filter"),
        ("filter.hostile.smudge", "/tmp/smudge-filter"),
        ("core.fsmonitor", "/tmp/fsmonitor"),
        ("diff.hostile.textconv", "/tmp/textconv"),
        ("url.https://example.invalid/.insteadOf", "safe:"),
        ("remote.origin.proxy", "https://example.invalid"),
    ),
)
def test_worker_rejects_repository_config_that_could_spawn_or_reach_network(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    key: str,
    value: str,
) -> None:
    repository = _repository(tmp_path)
    _git(repository, "config", key, value)
    monkeypatch.setattr(worker, "SOURCE_ROOT", repository)

    result = _parse_result(worker._execute(_request().model_dump(mode="json")))

    assert result.outcome.value == "rejected"
    assert result.safe_error_code == "audit_git_config_unsafe"


def test_worker_rejects_object_alternates_without_reading_external_objects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _repository(tmp_path)
    alternates = repository / ".git" / "objects" / "info" / "alternates"
    alternates.parent.mkdir(parents=True, exist_ok=True)
    alternates.write_text("/tmp/external-objects\n", encoding="utf-8")
    monkeypatch.setattr(worker, "SOURCE_ROOT", repository)

    result = _parse_result(worker._execute(_request().model_dump(mode="json")))

    assert result.outcome.value == "rejected"
    assert result.safe_error_code == "audit_git_object_alternate_rejected"


def test_worker_rejects_grafts_even_when_git_fsck_accepts_them(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _repository(tmp_path)
    head = _git(repository, "rev-parse", "HEAD")
    grafts = repository / ".git" / "info" / "grafts"
    grafts.write_text(f"{head}\n", encoding="ascii")
    _git(
        repository,
        "fsck",
        "--strict",
        "--full",
        "--no-dangling",
        "--no-reflogs",
        "--no-progress",
    )
    monkeypatch.setattr(worker, "SOURCE_ROOT", repository)

    result = _parse_result(worker._execute(_revision_request().model_dump(mode="json")))

    assert result.outcome.value == "rejected"
    assert result.safe_error_code == "audit_git_grafts_rejected"


@pytest.mark.parametrize("object_type", ("blob", "tree", "commit"))
def test_worker_rejects_loose_object_content_that_does_not_match_its_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    object_type: str,
) -> None:
    repository = _repository(tmp_path)
    head = _git(repository, "rev-parse", "HEAD")
    if object_type == "blob":
        object_id = _git(repository, "rev-parse", "HEAD:main.py")
        body = _git_bytes(repository, "cat-file", "blob", object_id)
        replacement = bytes((body[0] ^ 1,)) + body[1:]
    elif object_type == "tree":
        object_id = _git(repository, "rev-parse", "HEAD^{tree}")
        body = _git_bytes(repository, "cat-file", "tree", object_id)
        main_id = bytes.fromhex(_git(repository, "rev-parse", "HEAD:main.py"))
        readme_id = bytes.fromhex(_git(repository, "rev-parse", "HEAD:README.md"))
        assert main_id != readme_id and main_id in body
        replacement = body.replace(main_id, readme_id, 1)
    else:
        object_id = head
        body = _git_bytes(repository, "cat-file", "commit", object_id)
        assert b"fixture" in body
        replacement = body.replace(b"fixture", b"fixturE", 1)
    _replace_loose_object(
        repository,
        object_id=object_id,
        object_type=object_type,
        body=replacement,
    )
    monkeypatch.setattr(worker, "SOURCE_ROOT", repository)

    result = _parse_result(worker._execute(_revision_request().model_dump(mode="json")))

    assert result.outcome.value == "rejected"
    assert result.safe_error_code == "audit_git_object_integrity_invalid"


def test_worker_rejects_corrupt_pack_before_resolving_revision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _repository(tmp_path)
    _git(repository, "gc", "--quiet")
    pack = next((repository / ".git" / "objects" / "pack").glob("*.pack"))
    raw = bytearray(pack.read_bytes())
    assert len(raw) > 64
    raw[len(raw) // 2] ^= 1
    pack.chmod(0o600)
    pack.write_bytes(raw)
    monkeypatch.setattr(worker, "SOURCE_ROOT", repository)

    result = _parse_result(worker._execute(_revision_request().model_dump(mode="json")))

    assert result.outcome.value == "rejected"
    assert result.safe_error_code == "audit_git_object_integrity_invalid"


@pytest.mark.parametrize("missing_extension", (".idx", ".pack"))
def test_worker_rejects_orphan_pack_components(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    missing_extension: str,
) -> None:
    repository = _repository(tmp_path)
    _git(repository, "gc", "--quiet")
    pack_directory = repository / ".git" / "objects" / "pack"
    next(pack_directory.glob(f"*{missing_extension}")).unlink()
    monkeypatch.setattr(worker, "SOURCE_ROOT", repository)

    result = _parse_result(worker._execute(_revision_request().model_dump(mode="json")))

    assert result.outcome.value == "rejected"
    assert result.safe_error_code == "audit_git_object_pack_set_invalid"


def test_worker_rejects_loose_object_name_from_another_hash_format(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _repository(tmp_path)
    directory = repository / ".git" / "objects" / "aa"
    directory.mkdir(exist_ok=True)
    foreign_length = directory / ("b" * 62)
    foreign_length.write_bytes(zlib.compress(b"blob 1\0x"))
    monkeypatch.setattr(worker, "SOURCE_ROOT", repository)

    result = _parse_result(worker._execute(_revision_request().model_dump(mode="json")))

    assert result.outcome.value == "rejected"
    assert result.safe_error_code == "audit_git_structure_invalid"


@pytest.mark.parametrize("stream", ("stdout", "stderr"))
def test_git_integrity_check_rejects_success_diagnostics(
    tmp_path: Path,
    stream: str,
) -> None:
    descriptor = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        adapter = worker.SafeGitAdapter(
            timeout_seconds=10,
            maximum_output_bytes=1024,
            source_descriptor=descriptor,
        )
        script = f"import sys; sys.{stream}.write('diagnostic')"

        with pytest.raises(worker.WorkerRejected) as error:
            adapter._run_raw(
                (sys.executable, "-c", script),
                maximum_bytes=1024,
                reject_success_output_code="audit_git_object_integrity_invalid",
            )
    finally:
        os.close(descriptor)

    assert error.value.code == "audit_git_object_integrity_invalid"


@pytest.mark.parametrize("packed", (False, True))
def test_worker_rejects_object_store_hardlinks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    packed: bool,
) -> None:
    repository = _repository(tmp_path)
    if packed:
        _git(repository, "gc", "--quiet")
        object_path = next((repository / ".git" / "objects" / "pack").glob("*.pack"))
    else:
        object_id = _git(repository, "rev-parse", "HEAD:main.py")
        object_path = repository / ".git" / "objects" / object_id[:2] / object_id[2:]
    os.link(object_path, tmp_path / f"outside-object-{'pack' if packed else 'loose'}")
    monkeypatch.setattr(worker, "SOURCE_ROOT", repository)

    result = _parse_result(worker._execute(_revision_request().model_dump(mode="json")))

    assert result.outcome.value == "rejected"
    assert result.safe_error_code == "audit_git_object_hardlink_rejected"


@pytest.mark.parametrize("replacement_kind", ("symlink", "fifo"))
def test_worker_rejects_non_regular_loose_object_entries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    replacement_kind: str,
) -> None:
    repository = _repository(tmp_path)
    object_id = _git(repository, "rev-parse", "HEAD:main.py")
    object_path = repository / ".git" / "objects" / object_id[:2] / object_id[2:]
    outside = tmp_path / "outside-object"
    object_path.rename(outside)
    if replacement_kind == "symlink":
        object_path.symlink_to(outside)
        expected_code = "audit_git_administrative_symlink_rejected"
    else:
        os.mkfifo(object_path)
        expected_code = "audit_git_structure_invalid"
    monkeypatch.setattr(worker, "SOURCE_ROOT", repository)

    result = _parse_result(worker._execute(_revision_request().model_dump(mode="json")))

    assert result.outcome.value == "rejected"
    assert result.safe_error_code == expected_code


def test_worker_rejects_transient_blob_mutation_restored_before_final_fsck(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _repository(tmp_path)
    blob_id = _git(repository, "rev-parse", "HEAD:main.py")
    blob_path = repository / ".git" / "objects" / blob_id[:2] / blob_id[2:]
    original_loose = blob_path.read_bytes()
    body = _git_bytes(repository, "cat-file", "blob", blob_id)
    replacement = body + b"transient"
    original_inventory = worker._revision_inventory

    def mutate_inventory_then_restore(
        git: worker.SafeGitAdapter,
        revision: str,
        request: dict[str, Any],
    ) -> worker.Inventory:
        _replace_loose_object(
            repository,
            object_id=blob_id,
            object_type="blob",
            body=replacement,
        )
        try:
            return original_inventory(git, revision, request)
        finally:
            blob_path.write_bytes(original_loose)

    monkeypatch.setattr(worker, "SOURCE_ROOT", repository)
    monkeypatch.setattr(worker, "_revision_inventory", mutate_inventory_then_restore)

    result = _parse_result(worker._execute(_revision_request().model_dump(mode="json")))

    assert result.outcome.value == "rejected"
    assert result.safe_error_code == "audit_repository_changed_during_preflight"


def test_worker_reports_shallow_history_and_rejects_diff_semantics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _repository(tmp_path)
    head = _git(repository, "rev-parse", "HEAD")
    (repository / ".git" / "shallow").write_text(f"{head}\n", encoding="ascii")
    monkeypatch.setattr(worker, "SOURCE_ROOT", repository)

    standard = _parse_result(worker._execute(_revision_request().model_dump(mode="json")))
    assert standard.outcome.value == "succeeded"
    assert "audit_git_shallow_repository" in standard.capability_warnings

    payload = _revision_request().model_dump(mode="python")
    payload.update({"base_revision": head, "mode": AuditMode.DIFF})
    diff_request = SourceIngestWorkerRequest.model_validate(payload)
    diff = _parse_result(worker._execute(diff_request.model_dump(mode="json")))

    assert diff.outcome.value == "rejected"
    assert diff.safe_error_code == "audit_git_shallow_diff_unsupported"


def test_worker_rejects_symlinked_object_info_parent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _repository(tmp_path)
    info = repository / ".git" / "objects" / "info"
    info.rmdir()
    external = tmp_path / "external-object-info"
    external.mkdir()
    (external / "alternates").write_text("/tmp/external-objects\n", encoding="utf-8")
    info.symlink_to(external, target_is_directory=True)
    monkeypatch.setattr(worker, "SOURCE_ROOT", repository)

    result = _parse_result(worker._execute(_request().model_dump(mode="json")))

    assert result.outcome.value == "rejected"
    assert result.safe_error_code == "audit_git_administrative_symlink_rejected"


def test_worker_rejects_symlinked_hooks_directory_without_enumerating_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _repository(tmp_path)
    hooks = repository / ".git" / "hooks"
    for child in hooks.iterdir():
        child.unlink()
    hooks.rmdir()
    external = tmp_path / "external-hooks"
    external.mkdir()
    (external / "secret-name").write_text("not executable\n", encoding="utf-8")
    hooks.symlink_to(external, target_is_directory=True)
    monkeypatch.setattr(worker, "SOURCE_ROOT", repository)

    result = _parse_result(worker._execute(_request().model_dump(mode="json")))

    assert result.outcome.value == "rejected"
    assert result.safe_error_code == "audit_git_administrative_symlink_rejected"


def test_worker_rejects_worktree_hardlink_that_can_alias_outside_source_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _repository(tmp_path)
    outside_alias = tmp_path / "outside-main.py"
    os.link(repository / "main.py", outside_alias)
    monkeypatch.setattr(worker, "SOURCE_ROOT", repository)

    result = _parse_result(worker._execute(_request().model_dump(mode="json")))

    assert result.outcome.value == "rejected"
    assert result.safe_error_code == "audit_repository_hardlink_rejected"


@pytest.mark.parametrize("admin_name", ("config", "HEAD", "index"))
def test_worker_rejects_git_administrative_hardlinks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    admin_name: str,
) -> None:
    repository = _repository(tmp_path)
    admin_file = repository / ".git" / admin_name
    os.link(admin_file, tmp_path / f"outside-{admin_name}")
    monkeypatch.setattr(worker, "SOURCE_ROOT", repository)

    result = _parse_result(worker._execute(_request().model_dump(mode="json")))

    assert result.outcome.value == "rejected"
    assert result.safe_error_code == "audit_git_administrative_hardlink_rejected"


def test_worker_enforces_file_size_before_reading_the_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _repository(tmp_path)
    oversized = repository / "oversized.bin"
    oversized.write_bytes(b"x" * 4096)
    monkeypatch.setattr(worker, "SOURCE_ROOT", repository)
    request_payload = _request().model_dump(mode="python")
    request_payload["max_file_bytes"] = 1024
    request = SourceIngestWorkerRequest.model_validate(request_payload)

    result = _parse_result(worker._execute(request.model_dump(mode="json")))

    assert result.outcome.value == "rejected"
    assert result.safe_error_code == "audit_repository_file_limit_exceeded"


def test_worker_uses_config_snapshot_when_execution_config_is_injected_concurrently(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _repository(tmp_path)
    canary = tmp_path / "external-filter-canary"
    marker = Path(f"{canary}.executed")
    canary.write_text('#!/bin/sh\n: > "$0.executed"\ncat\n', encoding="utf-8")
    canary.chmod(0o700)
    original_status = worker._status

    def inject_then_status(
        git: worker.SafeGitAdapter,
        *,
        include_untracked: bool,
    ) -> tuple[bool, bool, bool, set[bytes]]:
        _git(repository, "config", "filter.hostile.clean", str(canary))
        _git(repository, "config", "filter.hostile.required", "true")
        (repository / ".gitattributes").write_text("*.py filter=hostile\n", encoding="utf-8")
        (repository / "main.py").write_text("print('injected')\n", encoding="utf-8")
        return original_status(git, include_untracked=include_untracked)

    monkeypatch.setattr(worker, "SOURCE_ROOT", repository)
    monkeypatch.setattr(worker, "_status", inject_then_status)

    result = _parse_result(worker._execute(_request().model_dump(mode="json")))

    assert result.outcome.value == "rejected"
    assert result.safe_error_code == "audit_repository_changed_during_preflight"
    assert not marker.exists()


@pytest.mark.parametrize("changed_identity", ("config", "index", "admin", "objects"))
def test_worker_rejects_git_identity_drift_before_publishing_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    changed_identity: str,
) -> None:
    repository = _repository(tmp_path)
    original_inventory = worker._working_tree_inventory

    def inventory_then_mutate(
        git: worker.SafeGitAdapter,
        request: dict[str, Any],
        *,
        source_descriptor: int,
        untracked_paths: set[bytes],
    ) -> worker.Inventory:
        inventory = original_inventory(
            git,
            request,
            source_descriptor=source_descriptor,
            untracked_paths=untracked_paths,
        )
        git_dir = repository / ".git"
        if changed_identity == "config":
            _git(repository, "config", "user.audit-drift", "changed")
        elif changed_identity == "index":
            index = git_dir / "index"
            index.write_bytes(index.read_bytes() + b"drift")
        elif changed_identity == "admin":
            (git_dir / "HEAD").write_text("ref: refs/heads/drift\n", encoding="utf-8")
        else:
            objects = git_dir / "objects"
            objects.rename(git_dir / "objects-before-drift")
            objects.mkdir()
        return inventory

    monkeypatch.setattr(worker, "SOURCE_ROOT", repository)
    monkeypatch.setattr(worker, "_working_tree_inventory", inventory_then_mutate)

    result = _parse_result(worker._execute(_request().model_dump(mode="json")))

    assert result.outcome.value == "rejected"
    assert result.safe_error_code == "audit_repository_changed_during_preflight"


def test_worker_preserves_revision_and_diff_resolution_with_shadow_git_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _repository(tmp_path)
    base_revision = _git(repository, "rev-parse", "HEAD")
    (repository / "main.py").write_text("print('second')\n", encoding="utf-8")
    _git(repository, "add", "main.py")
    _git(repository, "commit", "--quiet", "-m", "second")
    revision = _git(repository, "rev-parse", "HEAD")
    monkeypatch.setattr(worker, "SOURCE_ROOT", repository)
    payload = _request().model_dump(mode="python")
    payload.update(
        {
            "base_revision": base_revision,
            "include_untracked": False,
            "mode": AuditMode.DIFF,
            "revision": revision,
            "target_kind": SourceTargetKind.REVISION,
        }
    )
    request = SourceIngestWorkerRequest.model_validate(payload)

    result = _parse_result(worker._execute(request.model_dump(mode="json")))

    assert result.outcome.value == "succeeded"
    assert result.resolved_base_revision == base_revision
    assert result.resolved_revision == revision
    assert result.merge_base_revision == base_revision
    assert result.file_count == 2


def test_worker_preserves_sha256_repository_object_format(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    try:
        repository = _repository(tmp_path, object_format="sha256")
    except subprocess.CalledProcessError:
        pytest.skip("installed Git does not support SHA-256 repositories")
    monkeypatch.setattr(worker, "SOURCE_ROOT", repository)
    payload = _request().model_dump(mode="python")
    payload.update(
        {
            "include_untracked": False,
            "target_kind": SourceTargetKind.REVISION,
        }
    )
    request = SourceIngestWorkerRequest.model_validate(payload)

    result = _parse_result(worker._execute(request.model_dump(mode="json")))

    assert result.outcome.value == "succeeded"
    assert result.resolved_revision is not None
    assert len(result.resolved_revision) == 64


def test_worker_fails_closed_for_non_local_source_filesystem(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _repository(tmp_path)
    value = repository.stat()
    worker.MOUNTINFO_PATH.write_text(
        f"42 1 {os.major(value.st_dev)}:{os.minor(value.st_dev)} / / rw - overlay overlay rw\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(worker, "SOURCE_ROOT", repository)
    input_path = tmp_path / "request.json"
    output_path = tmp_path / "result.json"
    input_path.write_text(_request().model_dump_json(), encoding="utf-8")

    exit_code = worker.main(["preflight.py", str(input_path), str(output_path)])
    result = _parse_result(json.loads(output_path.read_text(encoding="utf-8")))

    assert exit_code == 0
    assert result.outcome.value == "failed"
    assert result.safe_error_code == "audit_source_ingest_filesystem_unsupported"
    assert result.source_mount_identity_digest is None
    assert result.source_mount_proof_digest is None
