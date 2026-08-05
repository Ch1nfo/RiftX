"""SourceIngest-only deterministic Git and working-tree materializer.

This module deliberately reuses the reviewed Git/config/object-store boundary in
``preflight``.  It must run only inside the SourceIngest security boundary; the
Control Plane and ordinary Worker must never import or instantiate it.
"""

from __future__ import annotations

import errno
import hashlib
import os
import shutil
import stat
import tempfile
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path

from riftx.audit.source_manifest import (
    SourceCaptureDecision,
    SourceCapturePolicy,
    SourceCaptureReason,
    SourceClassification,
    SourceManifest,
    SourceManifestEntry,
    SourceManifestObjectType,
    SourceManifestOrigin,
    SourceManifestPath,
    SourceManifestSourceKind,
)

from . import preflight as secure_git

_STAGING_PREFIX = "source-materialization-"
_COPY_CHUNK_SIZE = 64 * 1024
_MAX_GIT_OUTPUT_BYTES = 16 * 1024 * 1024
_LFS_POINTER_PREFIX = b"version https://git-lfs.github.com/spec/v1\n"


class SourceMaterializationFailure(StrEnum):
    REQUEST_INVALID = "audit_snapshot_materializer_request_invalid"
    OUTPUT_INVALID = "audit_snapshot_materializer_output_invalid"
    MANIFEST_LIMIT_EXCEEDED = "audit_snapshot_manifest_limit_exceeded"
    GIT_TREE_INVALID = "audit_snapshot_git_tree_invalid"
    GIT_INDEX_INVALID = "audit_snapshot_git_index_invalid"
    GIT_BLOB_INVALID = "audit_snapshot_git_blob_invalid"
    UNMERGED_STATE = "audit_snapshot_unmerged_state_rejected"
    REPOSITORY_CHANGED = "audit_repository_changed_during_materialization"
    OUTPUT_WRITE_FAILED = "audit_snapshot_materializer_write_failed"
    CLEANUP_FAILED = "audit_snapshot_materializer_cleanup_failed"


class SourceMaterializationError(RuntimeError):
    """Stable, path-free materialization failure."""

    def __init__(self, code: str | SourceMaterializationFailure) -> None:
        value = code.value if isinstance(code, SourceMaterializationFailure) else code
        super().__init__(value)
        self.code = value


@dataclass(frozen=True, slots=True)
class MaterializedSource:
    root: Path = field(repr=False)
    manifest: SourceManifest

    def __post_init__(self) -> None:
        if not isinstance(self.root, Path) or not self.root.is_absolute():
            raise ValueError("materialized source root must be absolute")


@dataclass(frozen=True, slots=True)
class MaterializationCleanupReceipt:
    examined: int
    eligible: int
    removed: int
    dry_run: bool


@dataclass(frozen=True, slots=True)
class _IndexEntry:
    path: bytes
    mode: int
    object_id: str


@dataclass(frozen=True, slots=True)
class _RevisionEntry:
    path: bytes
    mode: int
    object_type: bytes
    object_id: str
    size: int | None


@dataclass(frozen=True, slots=True)
class _WorktreeCandidate:
    path: bytes
    origin: SourceManifestOrigin
    mode: int
    object_id: str | None


@dataclass(frozen=True, slots=True)
class _WorktreeObservation:
    path: bytes
    fingerprint: tuple[int, ...] | None
    object_type: SourceManifestObjectType
    mode: int
    size: int | None
    content: bytes | None


class GitSourceMaterializer:
    """Build one immutable staging tree without mutating or checking out the source."""

    def __init__(
        self,
        staging_parent: Path,
        *,
        command_timeout_seconds: int = 300,
        maximum_git_output_bytes: int = _MAX_GIT_OUTPUT_BYTES,
        fault_injector: Callable[[str, bytes | None], None] | None = None,
    ) -> None:
        if not isinstance(staging_parent, Path) or not staging_parent.is_absolute():
            raise ValueError("materializer staging parent must be absolute")
        if not isinstance(command_timeout_seconds, int) or not 1 <= command_timeout_seconds <= 300:
            raise ValueError("materializer command timeout is invalid")
        if (
            not isinstance(maximum_git_output_bytes, int)
            or not 1024 <= maximum_git_output_bytes <= _MAX_GIT_OUTPUT_BYTES
        ):
            raise ValueError("materializer Git output limit is invalid")
        absolute = Path(os.path.abspath(os.fspath(staging_parent)))
        try:
            canonical_parent = absolute.parent.resolve(strict=True)
        except OSError as exc:
            raise ValueError("materializer staging parent parent is unavailable") from exc
        if canonical_parent != absolute.parent:
            raise ValueError("materializer staging parent must not traverse symlinks")
        self._staging_parent = absolute
        self._command_timeout_seconds = command_timeout_seconds
        self._maximum_git_output_bytes = maximum_git_output_bytes
        self._fault_injector = fault_injector
        self._assert_output_disjoint_from_source(require_existing=False)
        absolute.mkdir(exist_ok=True)
        if absolute.resolve() != absolute:
            raise ValueError("materializer staging parent must not traverse symlinks")

    def materialize(
        self,
        *,
        source_kind: SourceManifestSourceKind,
        revision: str,
        policy: SourceCapturePolicy,
    ) -> MaterializedSource:
        if not isinstance(source_kind, SourceManifestSourceKind):
            raise SourceMaterializationError(SourceMaterializationFailure.REQUEST_INVALID)
        if (
            not isinstance(revision, str)
            or not revision
            or revision.startswith("-")
            or len(revision.encode("utf-8")) > 1024
        ):
            raise SourceMaterializationError(SourceMaterializationFailure.REQUEST_INVALID)
        if not isinstance(policy, SourceCapturePolicy):
            raise SourceMaterializationError(SourceMaterializationFailure.REQUEST_INVALID)
        self._assert_output_disjoint_from_source(require_existing=True)
        staging_root = Path(tempfile.mkdtemp(prefix=_STAGING_PREFIX, dir=self._staging_parent))
        os.chmod(staging_root, 0o700)
        try:
            manifest = self._materialize_into(
                staging_root,
                source_kind=source_kind,
                revision=revision,
                policy=policy,
            )
            return MaterializedSource(root=staging_root, manifest=manifest)
        except SourceMaterializationError:
            self._cleanup_failed(staging_root)
            raise
        except secure_git.SafeWorkerError as exc:
            self._cleanup_failed(staging_root)
            code = (
                SourceMaterializationFailure.REPOSITORY_CHANGED.value
                if exc.code == "audit_repository_changed_during_preflight"
                else exc.code
            )
            raise SourceMaterializationError(code) from exc
        except Exception as exc:
            self._cleanup_failed(staging_root)
            raise SourceMaterializationError(
                SourceMaterializationFailure.OUTPUT_WRITE_FAILED
            ) from exc

    def discard(self, materialized: MaterializedSource) -> None:
        if not isinstance(materialized, MaterializedSource):
            raise ValueError("materialized source is invalid")
        self._remove_owned_staging(materialized.root)

    def cleanup_orphans(
        self,
        *,
        older_than: datetime,
        dry_run: bool,
    ) -> MaterializationCleanupReceipt:
        if not isinstance(older_than, datetime) or older_than.utcoffset() is None:
            raise ValueError("older_than must be timezone-aware")
        examined = 0
        eligible = 0
        removed = 0
        cutoff = older_than.timestamp()
        for candidate in sorted(self._staging_parent.iterdir(), key=lambda value: value.name):
            examined += 1
            try:
                value = candidate.lstat()
            except FileNotFoundError:
                continue
            if (
                not candidate.name.startswith(_STAGING_PREFIX)
                or not stat.S_ISDIR(value.st_mode)
                or stat.S_ISLNK(value.st_mode)
                or value.st_mtime > cutoff
            ):
                continue
            eligible += 1
            if not dry_run:
                self._remove_owned_staging(candidate)
                removed += 1
        return MaterializationCleanupReceipt(
            examined=examined,
            eligible=eligible,
            removed=removed,
            dry_run=bool(dry_run),
        )

    def _materialize_into(
        self,
        staging_root: Path,
        *,
        source_kind: SourceManifestSourceKind,
        revision: str,
        policy: SourceCapturePolicy,
    ) -> SourceManifest:
        source_descriptor = secure_git._open_source_root()
        snapshot: secure_git.GitSnapshot | None = None
        try:
            git = secure_git.SafeGitAdapter(
                timeout_seconds=self._command_timeout_seconds,
                maximum_output_bytes=self._maximum_git_output_bytes,
                source_descriptor=source_descriptor,
            )
            git.version()
            with tempfile.TemporaryDirectory(prefix="riftx-materializer-", dir="/tmp") as temp:
                scratch = Path(temp)
                structure = secure_git._validate_structure()
                config, config_snapshot_digest = secure_git._validate_local_config(
                    git,
                    structure,
                    scratch,
                )
                object_id_length = (
                    64
                    if config.get("extensions.objectformat", "sha1").lower() == "sha256"
                    else 40
                )
                if secure_git._config_snapshot_identity_digest(structure) != config_snapshot_digest:
                    raise SourceMaterializationError(
                        SourceMaterializationFailure.REPOSITORY_CHANGED
                    )
                repository_guard = secure_git._repository_guard_digest(
                    structure,
                    object_id_length=object_id_length,
                )
                snapshot = secure_git._prepare_git_snapshot(structure, config, scratch)
                secure_git._assert_repository_unchanged(
                    structure,
                    repository_guard,
                    object_id_length=object_id_length,
                )
                git.bind_repository(snapshot)
                git.verify_object_integrity()
                head_commit = secure_git._resolve_revision(git, "HEAD")
                resolved = secure_git._resolve_revision(git, revision)
                if source_kind is SourceManifestSourceKind.REVISION:
                    entries = self._materialize_revision(
                        git,
                        staging_root,
                        revision=resolved,
                        policy=policy,
                    )
                    staged = unstaged = untracked = False
                    commit_sha = resolved
                    manifest_head = resolved
                else:
                    if resolved != head_commit:
                        raise SourceMaterializationError(
                            SourceMaterializationFailure.REQUEST_INVALID
                        )
                    entries, staged, unstaged, untracked = self._materialize_working_tree(
                        git,
                        staging_root,
                        source_descriptor=source_descriptor,
                        policy=policy,
                    )
                    commit_sha = head_commit
                    manifest_head = head_commit
                self._fault("before_repository_recheck", None)
                secure_git._assert_repository_unchanged(
                    structure,
                    repository_guard,
                    object_id_length=object_id_length,
                )
                git.verify_object_integrity()
                secure_git._assert_repository_unchanged(
                    structure,
                    repository_guard,
                    object_id_length=object_id_length,
                )
                return SourceManifest.create(
                    source_kind=source_kind,
                    commit_sha=commit_sha,
                    head_commit_sha=manifest_head,
                    capture_policy_digest=policy.digest,
                    entries=entries,
                    staged=staged,
                    unstaged=unstaged,
                    untracked=untracked,
                )
        finally:
            if snapshot is not None:
                for descriptor in snapshot.inherited_descriptors:
                    os.close(descriptor)
            os.close(source_descriptor)

    def _materialize_revision(
        self,
        git: secure_git.SafeGitAdapter,
        staging_root: Path,
        *,
        revision: str,
        policy: SourceCapturePolicy,
    ) -> tuple[SourceManifestEntry, ...]:
        raw = git.run("ls-tree", "-r", "-z", "-l", "--full-tree", revision)
        revisions: list[_RevisionEntry] = []
        for record in raw.split(b"\0"):
            if not record:
                continue
            metadata, separator, path = record.partition(b"\t")
            fields = metadata.split()
            if not separator or len(fields) != 4:
                raise SourceMaterializationError(SourceMaterializationFailure.GIT_TREE_INVALID)
            raw_mode, object_type, raw_object_id, raw_size = fields
            path = secure_git._validate_git_path(path)
            try:
                mode = int(raw_mode, 8)
                object_id = raw_object_id.decode("ascii", errors="strict")
            except (ValueError, UnicodeDecodeError) as exc:
                raise SourceMaterializationError(
                    SourceMaterializationFailure.GIT_TREE_INVALID
                ) from exc
            size: int | None
            if object_type == b"commit" or mode == 0o160000:
                size = None
            else:
                try:
                    size = int(raw_size)
                except ValueError as exc:
                    raise SourceMaterializationError(
                        SourceMaterializationFailure.GIT_TREE_INVALID
                    ) from exc
                if size < 0:
                    raise SourceMaterializationError(
                        SourceMaterializationFailure.GIT_TREE_INVALID
                    )
            revisions.append(
                _RevisionEntry(
                    path=path,
                    mode=mode,
                    object_type=object_type,
                    object_id=object_id,
                    size=size,
                )
            )
        revisions.sort(key=lambda entry: entry.path)
        self._enforce_manifest_limit(len(revisions), policy)
        included_bytes = 0
        entries: list[SourceManifestEntry] = []
        root_fd = os.open(staging_root, os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY)
        try:
            for item in revisions:
                entry, included_bytes = self._revision_manifest_entry(
                    git,
                    root_fd,
                    item,
                    policy=policy,
                    included_bytes=included_bytes,
                )
                entries.append(entry)
                self._fault("after_entry", item.path)
        finally:
            os.close(root_fd)
        return tuple(entries)

    def _revision_manifest_entry(
        self,
        git: secure_git.SafeGitAdapter,
        root_fd: int,
        item: _RevisionEntry,
        *,
        policy: SourceCapturePolicy,
        included_bytes: int,
    ) -> tuple[SourceManifestEntry, int]:
        path = SourceManifestPath.from_bytes(item.path)
        language, classification = _classify(item.path)
        path_reason = _path_capture_reason(item.path)
        if path_reason is not None:
            return (
                _entry(
                    path=path,
                    object_type=_revision_object_type(item),
                    origin=SourceManifestOrigin.COMMIT,
                    mode=item.mode,
                    size=item.size,
                    sha256=None,
                    git_blob_id=item.object_id,
                    language=language,
                    classification=classification,
                    decision=SourceCaptureDecision.DEFERRED,
                    reason=path_reason,
                ),
                included_bytes,
            )
        policy_reason = _policy_exclusion(item.path, classification, policy)
        if policy_reason is not None:
            return (
                _entry(
                    path=path,
                    object_type=_revision_object_type(item),
                    origin=SourceManifestOrigin.COMMIT,
                    mode=item.mode,
                    size=item.size,
                    sha256=None,
                    git_blob_id=item.object_id,
                    language=language,
                    classification=classification,
                    decision=SourceCaptureDecision.EXCLUDED,
                    reason=policy_reason,
                ),
                included_bytes,
            )
        if item.object_type == b"commit" or item.mode == 0o160000:
            return (
                _entry(
                    path=path,
                    object_type=SourceManifestObjectType.SUBMODULE,
                    origin=SourceManifestOrigin.COMMIT,
                    mode=0o160000,
                    size=None,
                    sha256=None,
                    git_blob_id=item.object_id,
                    language=language,
                    classification=classification,
                    decision=SourceCaptureDecision.EXCLUDED,
                    reason=SourceCaptureReason.SUBMODULE,
                ),
                included_bytes,
            )
        if item.object_type != b"blob" or item.mode not in {0o100644, 0o100755, 0o120000}:
            return (
                _entry(
                    path=path,
                    object_type=SourceManifestObjectType.SPECIAL,
                    origin=SourceManifestOrigin.COMMIT,
                    mode=item.mode,
                    size=item.size,
                    sha256=None,
                    git_blob_id=item.object_id,
                    language=language,
                    classification=classification,
                    decision=SourceCaptureDecision.DEFERRED,
                    reason=SourceCaptureReason.SPECIAL_FILE,
                ),
                included_bytes,
            )
        assert item.size is not None
        if item.size > policy.max_file_bytes:
            return (
                _entry(
                    path=path,
                    object_type=_revision_object_type(item),
                    origin=SourceManifestOrigin.COMMIT,
                    mode=item.mode,
                    size=item.size,
                    sha256=None,
                    git_blob_id=item.object_id,
                    language=language,
                    classification=classification,
                    decision=SourceCaptureDecision.DEFERRED,
                    reason=SourceCaptureReason.OVERSIZED_FILE,
                ),
                included_bytes,
            )
        try:
            content = git.read_blob(item.object_id, expected_size=item.size)
        except secure_git.SafeWorkerError as exc:
            raise SourceMaterializationError(
                SourceMaterializationFailure.GIT_BLOB_INVALID
            ) from exc
        digest = hashlib.sha256(content).hexdigest()
        object_type = _revision_object_type(item)
        reason = _content_defer_reason(content, object_type)
        if reason is not None:
            return (
                _entry(
                    path=path,
                    object_type=object_type,
                    origin=SourceManifestOrigin.COMMIT,
                    mode=item.mode,
                    size=item.size,
                    sha256=digest,
                    git_blob_id=item.object_id,
                    language=language,
                    classification=classification,
                    decision=SourceCaptureDecision.DEFERRED,
                    reason=reason,
                ),
                included_bytes,
            )
        if included_bytes + item.size > policy.max_repository_bytes:
            return (
                _entry(
                    path=path,
                    object_type=object_type,
                    origin=SourceManifestOrigin.COMMIT,
                    mode=item.mode,
                    size=item.size,
                    sha256=digest,
                    git_blob_id=item.object_id,
                    language=language,
                    classification=classification,
                    decision=SourceCaptureDecision.DEFERRED,
                    reason=SourceCaptureReason.REPOSITORY_BUDGET_EXCEEDED,
                ),
                included_bytes,
            )
        _write_staging_entry(root_fd, item.path, content, mode=item.mode, object_type=object_type)
        return (
            _entry(
                path=path,
                object_type=object_type,
                origin=SourceManifestOrigin.COMMIT,
                mode=item.mode,
                size=item.size,
                sha256=digest,
                git_blob_id=item.object_id,
                language=language,
                classification=(
                    SourceClassification.SYMLINK
                    if object_type is SourceManifestObjectType.SYMLINK
                    else classification
                ),
                decision=SourceCaptureDecision.INCLUDED,
                reason=SourceCaptureReason.INCLUDED,
            ),
            included_bytes + item.size,
        )

    def _materialize_working_tree(
        self,
        git: secure_git.SafeGitAdapter,
        staging_root: Path,
        *,
        source_descriptor: int,
        policy: SourceCapturePolicy,
    ) -> tuple[tuple[SourceManifestEntry, ...], bool, bool, bool]:
        initial = _working_tree_candidates(
            git,
            source_descriptor=source_descriptor,
            maximum_entries=policy.max_manifest_entries,
        )
        staged, unstaged, untracked_status, _ = secure_git._status(
            git,
            include_untracked=True,
        )
        candidates = initial[0]
        untracked_paths = initial[1]
        if untracked_status and not untracked_paths:
            raise SourceMaterializationError(SourceMaterializationFailure.REPOSITORY_CHANGED)
        self._enforce_manifest_limit(len(candidates), policy)
        observations: list[_WorktreeObservation] = []
        entries: list[SourceManifestEntry] = []
        included_bytes = 0
        root_fd = os.open(staging_root, os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY)
        source_fd = os.dup(source_descriptor)
        try:
            for candidate in candidates:
                entry, observation, included_bytes = self._worktree_manifest_entry(
                    source_fd,
                    root_fd,
                    candidate,
                    policy=policy,
                    included_bytes=included_bytes,
                )
                entries.append(entry)
                if observation is not None:
                    observations.append(observation)
                self._fault("after_entry", candidate.path)
        finally:
            os.close(source_fd)
            os.close(root_fd)
        _assert_worktree_observations(source_descriptor, observations)
        final = _working_tree_candidates(
            git,
            source_descriptor=source_descriptor,
            maximum_entries=policy.max_manifest_entries,
        )
        final_status = secure_git._status(git, include_untracked=True)
        if (
            final != initial
            or final_status[:2] != (staged, unstaged)
            or (final_status[2] and not untracked_paths)
        ):
            raise SourceMaterializationError(SourceMaterializationFailure.REPOSITORY_CHANGED)
        return tuple(entries), staged, unstaged, bool(untracked_paths)

    def _worktree_manifest_entry(
        self,
        source_fd: int,
        staging_fd: int,
        candidate: _WorktreeCandidate,
        *,
        policy: SourceCapturePolicy,
        included_bytes: int,
    ) -> tuple[SourceManifestEntry, _WorktreeObservation | None, int]:
        path = SourceManifestPath.from_bytes(candidate.path)
        language, classification = _classify(candidate.path)
        path_reason = _path_capture_reason(candidate.path)
        if path_reason is not None:
            return (
                _entry(
                    path=path,
                    object_type=_candidate_object_type(candidate),
                    origin=candidate.origin,
                    mode=candidate.mode,
                    size=None,
                    sha256=None,
                    git_blob_id=candidate.object_id,
                    language=language,
                    classification=classification,
                    decision=SourceCaptureDecision.DEFERRED,
                    reason=path_reason,
                ),
                None,
                included_bytes,
            )
        if candidate.origin is SourceManifestOrigin.IGNORED:
            return (
                _entry(
                    path=path,
                    object_type=SourceManifestObjectType.REGULAR_FILE,
                    origin=candidate.origin,
                    mode=0,
                    size=None,
                    sha256=None,
                    git_blob_id=None,
                    language=language,
                    classification=classification,
                    decision=SourceCaptureDecision.EXCLUDED,
                    reason=SourceCaptureReason.IGNORED,
                ),
                None,
                included_bytes,
            )
        policy_reason = _policy_exclusion(candidate.path, classification, policy)
        if policy_reason is not None:
            return (
                _entry(
                    path=path,
                    object_type=_candidate_object_type(candidate),
                    origin=candidate.origin,
                    mode=candidate.mode,
                    size=None,
                    sha256=None,
                    git_blob_id=candidate.object_id,
                    language=language,
                    classification=classification,
                    decision=SourceCaptureDecision.EXCLUDED,
                    reason=policy_reason,
                ),
                None,
                included_bytes,
            )
        if candidate.origin is SourceManifestOrigin.UNTRACKED and not policy.include_untracked:
            return (
                _entry(
                    path=path,
                    object_type=SourceManifestObjectType.REGULAR_FILE,
                    origin=candidate.origin,
                    mode=0,
                    size=None,
                    sha256=None,
                    git_blob_id=None,
                    language=language,
                    classification=classification,
                    decision=SourceCaptureDecision.EXCLUDED,
                    reason=SourceCaptureReason.UNTRACKED_EXCLUDED,
                ),
                None,
                included_bytes,
            )
        if candidate.mode == 0o160000:
            return (
                _entry(
                    path=path,
                    object_type=SourceManifestObjectType.SUBMODULE,
                    origin=candidate.origin,
                    mode=candidate.mode,
                    size=None,
                    sha256=None,
                    git_blob_id=candidate.object_id,
                    language=language,
                    classification=classification,
                    decision=SourceCaptureDecision.EXCLUDED,
                    reason=SourceCaptureReason.SUBMODULE,
                ),
                None,
                included_bytes,
            )
        observation = _read_worktree_entry(source_fd, candidate.path, policy.max_file_bytes)
        digest = (
            hashlib.sha256(observation.content).hexdigest()
            if observation.content is not None
            else None
        )
        if observation.object_type is SourceManifestObjectType.MISSING:
            decision = SourceCaptureDecision.EXCLUDED
            reason = SourceCaptureReason.MISSING_WORKTREE_ENTRY
        elif observation.object_type is SourceManifestObjectType.SPECIAL:
            decision = SourceCaptureDecision.DEFERRED
            reason = SourceCaptureReason.SPECIAL_FILE
        elif observation.fingerprint is not None and observation.fingerprint[3] != 1:
            decision = SourceCaptureDecision.DEFERRED
            reason = SourceCaptureReason.HARDLINK
        elif observation.size is not None and observation.size > policy.max_file_bytes:
            decision = SourceCaptureDecision.DEFERRED
            reason = SourceCaptureReason.OVERSIZED_FILE
        elif observation.content is None:
            raise SourceMaterializationError(SourceMaterializationFailure.OUTPUT_WRITE_FAILED)
        else:
            content_reason = _content_defer_reason(
                observation.content,
                observation.object_type,
            )
            if content_reason is not None:
                decision = SourceCaptureDecision.DEFERRED
                reason = content_reason
            elif included_bytes + len(observation.content) > policy.max_repository_bytes:
                decision = SourceCaptureDecision.DEFERRED
                reason = SourceCaptureReason.REPOSITORY_BUDGET_EXCEEDED
            else:
                decision = SourceCaptureDecision.INCLUDED
                reason = SourceCaptureReason.INCLUDED
                _write_staging_entry(
                    staging_fd,
                    candidate.path,
                    observation.content,
                    mode=observation.mode,
                    object_type=observation.object_type,
                )
                included_bytes += len(observation.content)
        return (
            _entry(
                path=path,
                object_type=observation.object_type,
                origin=candidate.origin,
                mode=observation.mode,
                size=observation.size,
                sha256=digest,
                git_blob_id=candidate.object_id,
                language=language,
                classification=(
                    SourceClassification.SYMLINK
                    if observation.object_type is SourceManifestObjectType.SYMLINK
                    else classification
                ),
                decision=decision,
                reason=reason,
            ),
            observation,
            included_bytes,
        )

    def _assert_output_disjoint_from_source(self, *, require_existing: bool) -> None:
        try:
            source = secure_git.SOURCE_ROOT.resolve(strict=True)
            staging = (
                self._staging_parent.resolve(strict=True)
                if require_existing
                else self._staging_parent
            )
            staging.relative_to(source)
        except ValueError:
            pass
        except OSError as exc:
            raise SourceMaterializationError(SourceMaterializationFailure.OUTPUT_INVALID) from exc
        else:
            raise SourceMaterializationError(SourceMaterializationFailure.OUTPUT_INVALID)
        try:
            source.relative_to(staging)
        except ValueError:
            return
        raise SourceMaterializationError(SourceMaterializationFailure.OUTPUT_INVALID)

    def _enforce_manifest_limit(self, count: int, policy: SourceCapturePolicy) -> None:
        if count > policy.max_manifest_entries:
            raise SourceMaterializationError(
                SourceMaterializationFailure.MANIFEST_LIMIT_EXCEEDED
            )

    def _cleanup_failed(self, root: Path) -> None:
        try:
            self._fault("before_cleanup", None)
            self._remove_owned_staging(root)
        except Exception as exc:
            raise SourceMaterializationError(
                SourceMaterializationFailure.CLEANUP_FAILED
            ) from exc

    def _remove_owned_staging(self, root: Path) -> None:
        absolute = Path(os.path.abspath(os.fspath(root)))
        if absolute.parent != self._staging_parent or not absolute.name.startswith(_STAGING_PREFIX):
            raise ValueError("materializer staging root is not owned")
        value = absolute.lstat()
        if not stat.S_ISDIR(value.st_mode) or stat.S_ISLNK(value.st_mode):
            raise ValueError("materializer staging root is invalid")
        shutil.rmtree(absolute, ignore_errors=False)

    def _fault(self, stage: str, path: bytes | None) -> None:
        if self._fault_injector is not None:
            self._fault_injector(stage, path)


def _working_tree_candidates(
    git: secure_git.SafeGitAdapter,
    *,
    source_descriptor: int,
    maximum_entries: int,
) -> tuple[tuple[_WorktreeCandidate, ...], frozenset[bytes], frozenset[bytes]]:
    index_entries: dict[bytes, _IndexEntry] = {}
    raw_index = git.run("ls-files", "-z", "--stage")
    seen_stages: dict[bytes, set[int]] = {}
    for record in raw_index.split(b"\0"):
        if not record:
            continue
        metadata, separator, path = record.partition(b"\t")
        fields = metadata.split()
        if not separator or len(fields) != 3:
            raise SourceMaterializationError(SourceMaterializationFailure.GIT_INDEX_INVALID)
        path = secure_git._validate_git_path(path)
        try:
            mode = int(fields[0], 8)
            object_id = fields[1].decode("ascii", errors="strict")
            stage = int(fields[2])
        except (ValueError, UnicodeDecodeError) as exc:
            raise SourceMaterializationError(
                SourceMaterializationFailure.GIT_INDEX_INVALID
            ) from exc
        seen_stages.setdefault(path, set()).add(stage)
        if stage == 0:
            index_entries[path] = _IndexEntry(path=path, mode=mode, object_id=object_id)
    if any(stages != {0} for stages in seen_stages.values()):
        raise SourceMaterializationError(SourceMaterializationFailure.UNMERGED_STATE)
    untracked = frozenset(
        secure_git._validate_git_path(path)
        for path in git.run("ls-files", "-z", "--others", "--exclude-standard").split(b"\0")
        if path
    )
    ignored = frozenset(
        secure_git._validate_git_path(path)
        for path in git.run(
            "ls-files",
            "-z",
            "--others",
            "--ignored",
            "--exclude-standard",
        ).split(b"\0")
        if path
    )
    filesystem_paths = _filesystem_leaf_paths(
        source_descriptor,
        maximum_entries=maximum_entries,
    )
    untracked = frozenset(
        set(untracked) | (set(filesystem_paths) - set(index_entries) - set(ignored))
    )
    candidates = [
        _WorktreeCandidate(
            path=entry.path,
            origin=SourceManifestOrigin.TRACKED_WORKTREE,
            mode=entry.mode,
            object_id=entry.object_id,
        )
        for entry in index_entries.values()
    ]
    candidates.extend(
        _WorktreeCandidate(
            path=path,
            origin=SourceManifestOrigin.UNTRACKED,
            mode=0,
            object_id=None,
        )
        for path in untracked
        if path not in index_entries
    )
    candidates.extend(
        _WorktreeCandidate(
            path=path,
            origin=SourceManifestOrigin.IGNORED,
            mode=0,
            object_id=None,
        )
        for path in ignored
        if path not in index_entries and path not in untracked
    )
    return tuple(sorted(candidates, key=lambda item: item.path)), untracked, ignored


def _filesystem_leaf_paths(root_fd: int, *, maximum_entries: int) -> frozenset[bytes]:
    paths: set[bytes] = set()
    pending: list[tuple[int, bytes]] = [(os.dup(root_fd), b"")]
    walked = 0
    maximum_walk_entries = min(2**63 - 1, maximum_entries * 2)
    try:
        while pending:
            directory_fd, prefix = pending.pop()
            try:
                with os.scandir(directory_fd) as entries:
                    for entry in entries:
                        name = os.fsencode(entry.name)
                        if name == b".git":
                            continue
                        walked += 1
                        if walked > maximum_walk_entries:
                            raise SourceMaterializationError(
                                SourceMaterializationFailure.MANIFEST_LIMIT_EXCEEDED
                            )
                        path = name if not prefix else prefix + b"/" + name
                        secure_git._validate_git_path(path)
                        value = entry.stat(follow_symlinks=False)
                        if stat.S_ISDIR(value.st_mode):
                            child_fd = os.open(
                                name,
                                os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW,
                                dir_fd=directory_fd,
                            )
                            pending.append((child_fd, path))
                        else:
                            paths.add(path)
                            if len(paths) > maximum_entries:
                                raise SourceMaterializationError(
                                    SourceMaterializationFailure.MANIFEST_LIMIT_EXCEEDED
                                )
            finally:
                os.close(directory_fd)
    except SourceMaterializationError:
        for directory_fd, _ in pending:
            os.close(directory_fd)
        raise
    except OSError as exc:
        for directory_fd, _ in pending:
            os.close(directory_fd)
        raise SourceMaterializationError(SourceMaterializationFailure.REPOSITORY_CHANGED) from exc
    return frozenset(paths)


def _read_worktree_entry(root_fd: int, path: bytes, max_file_bytes: int) -> _WorktreeObservation:
    components = path.split(b"/")
    parent_fd = os.dup(root_fd)
    final_fd = -1
    try:
        for component in components[:-1]:
            next_fd = os.open(
                component,
                os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=parent_fd,
            )
            os.close(parent_fd)
            parent_fd = next_fd
        final = components[-1]
        try:
            initial = os.stat(final, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return _WorktreeObservation(
                path=path,
                fingerprint=None,
                object_type=SourceManifestObjectType.MISSING,
                mode=0,
                size=None,
                content=None,
            )
        fingerprint = secure_git._stat_fingerprint(initial)
        if stat.S_ISLNK(initial.st_mode):
            target = os.readlink(final, dir_fd=parent_fd)
            content = os.fsencode(target)
            observed = os.stat(final, dir_fd=parent_fd, follow_symlinks=False)
            if secure_git._stat_fingerprint(observed) != fingerprint:
                raise SourceMaterializationError(SourceMaterializationFailure.REPOSITORY_CHANGED)
            return _WorktreeObservation(
                path=path,
                fingerprint=fingerprint,
                object_type=SourceManifestObjectType.SYMLINK,
                mode=0o120000,
                size=len(content),
                content=(content if len(content) <= max_file_bytes else None),
            )
        if not stat.S_ISREG(initial.st_mode):
            return _WorktreeObservation(
                path=path,
                fingerprint=fingerprint,
                object_type=SourceManifestObjectType.SPECIAL,
                mode=initial.st_mode,
                size=int(initial.st_size),
                content=None,
            )
        mode = 0o100755 if initial.st_mode & 0o111 else 0o100644
        if initial.st_size > max_file_bytes or initial.st_nlink != 1:
            return _WorktreeObservation(
                path=path,
                fingerprint=fingerprint,
                object_type=SourceManifestObjectType.REGULAR_FILE,
                mode=mode,
                size=int(initial.st_size),
                content=None,
            )
        final_fd = os.open(
            final,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
            dir_fd=parent_fd,
        )
        opened = os.fstat(final_fd)
        if secure_git._stat_fingerprint(opened) != fingerprint:
            raise SourceMaterializationError(SourceMaterializationFailure.REPOSITORY_CHANGED)
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(final_fd, _COPY_CHUNK_SIZE)
            if not chunk:
                break
            total += len(chunk)
            if total > max_file_bytes:
                raise SourceMaterializationError(SourceMaterializationFailure.REPOSITORY_CHANGED)
            chunks.append(chunk)
        completed = os.fstat(final_fd)
        if total != initial.st_size or secure_git._stat_fingerprint(completed) != fingerprint:
            raise SourceMaterializationError(SourceMaterializationFailure.REPOSITORY_CHANGED)
        return _WorktreeObservation(
            path=path,
            fingerprint=fingerprint,
            object_type=SourceManifestObjectType.REGULAR_FILE,
            mode=mode,
            size=total,
            content=b"".join(chunks),
        )
    except SourceMaterializationError:
        raise
    except OSError as exc:
        if exc.errno in {errno.ENOENT, errno.ENOTDIR, errno.ELOOP}:
            raise SourceMaterializationError(
                SourceMaterializationFailure.REPOSITORY_CHANGED
            ) from exc
        raise SourceMaterializationError(SourceMaterializationFailure.OUTPUT_WRITE_FAILED) from exc
    finally:
        if final_fd >= 0:
            os.close(final_fd)
        os.close(parent_fd)


def _assert_worktree_observations(
    root_fd: int,
    observations: list[_WorktreeObservation],
) -> None:
    for observation in observations:
        observed = _stat_path(root_fd, observation.path)
        if observed != observation.fingerprint:
            raise SourceMaterializationError(SourceMaterializationFailure.REPOSITORY_CHANGED)


def _stat_path(root_fd: int, path: bytes) -> tuple[int, ...] | None:
    components = path.split(b"/")
    parent_fd = os.dup(root_fd)
    try:
        for component in components[:-1]:
            next_fd = os.open(
                component,
                os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=parent_fd,
            )
            os.close(parent_fd)
            parent_fd = next_fd
        try:
            value = os.stat(components[-1], dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return None
        return secure_git._stat_fingerprint(value)
    except OSError as exc:
        if exc.errno in {errno.ENOENT, errno.ENOTDIR, errno.ELOOP}:
            return None
        raise SourceMaterializationError(SourceMaterializationFailure.REPOSITORY_CHANGED) from exc
    finally:
        os.close(parent_fd)


def _write_staging_entry(
    root_fd: int,
    path: bytes,
    content: bytes,
    *,
    mode: int,
    object_type: SourceManifestObjectType,
) -> None:
    components = path.split(b"/")
    parent_fd = os.dup(root_fd)
    descriptor = -1
    try:
        for component in components[:-1]:
            try:
                os.mkdir(component, mode=0o700, dir_fd=parent_fd)
            except FileExistsError:
                pass
            next_fd = os.open(
                component,
                os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=parent_fd,
            )
            os.close(parent_fd)
            parent_fd = next_fd
        final = components[-1]
        if object_type is SourceManifestObjectType.SYMLINK:
            os.symlink(content, final, dir_fd=parent_fd)
            return
        descriptor = os.open(
            final,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
            dir_fd=parent_fd,
        )
        written = 0
        while written < len(content):
            written += os.write(descriptor, content[written:])
        os.fsync(descriptor)
        os.fchmod(descriptor, 0o500 if mode == 0o100755 else 0o400)
        os.fsync(descriptor)
    except OSError as exc:
        raise SourceMaterializationError(SourceMaterializationFailure.OUTPUT_WRITE_FAILED) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(parent_fd)


def _path_capture_reason(path: bytes) -> SourceCaptureReason | None:
    try:
        decoded = path.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return SourceCaptureReason.INVALID_UTF8_PATH
    if (
        len(path) > 4096
        or unicodedata.normalize("NFC", decoded) != decoded
        or any(unicodedata.category(character).startswith("C") for character in decoded)
        or any(part == ".git" for part in decoded.split("/"))
    ):
        return SourceCaptureReason.NONCANONICAL_PATH
    return None


def _policy_exclusion(
    path: bytes,
    classification: SourceClassification,
    policy: SourceCapturePolicy,
) -> SourceCaptureReason | None:
    if not policy.selects(path):
        return SourceCaptureReason.PATH_EXCLUDED
    if classification is SourceClassification.GENERATED and not policy.include_generated:
        return SourceCaptureReason.GENERATED_EXCLUDED
    if classification is SourceClassification.VENDOR and not policy.include_vendor:
        return SourceCaptureReason.VENDOR_EXCLUDED
    return None


def _content_defer_reason(
    content: bytes,
    object_type: SourceManifestObjectType,
) -> SourceCaptureReason | None:
    if object_type is SourceManifestObjectType.SYMLINK:
        return None
    if _is_lfs_pointer(content):
        return SourceCaptureReason.LFS_POINTER
    try:
        content.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return SourceCaptureReason.INVALID_UTF8_CONTENT
    return None


def _is_lfs_pointer(content: bytes) -> bool:
    if not content.startswith(_LFS_POINTER_PREFIX) or len(content) > 1024:
        return False
    lines = content.rstrip(b"\n").split(b"\n")
    if len(lines) < 3:
        return False
    oid = lines[1].removeprefix(b"oid sha256:")
    size = lines[2].removeprefix(b"size ")
    return (
        lines[1].startswith(b"oid sha256:")
        and len(oid) == 64
        and all(value in b"0123456789abcdef" for value in oid)
        and lines[2].startswith(b"size ")
        and size.isdigit()
    )


def _classify(path: bytes) -> tuple[str, SourceClassification]:
    language = secure_git._language_for_path(path)
    try:
        decoded = path.decode("utf-8", errors="strict").lower()
    except UnicodeDecodeError:
        return language, SourceClassification.UNKNOWN
    parts = decoded.split("/")
    if any(part in {"vendor", "node_modules", ".venv", "third_party"} for part in parts):
        return language, SourceClassification.VENDOR
    if any(part in {"build", "dist", "generated", "target"} for part in parts):
        return language, SourceClassification.GENERATED
    suffix = Path(decoded).suffix
    if suffix in {".md", ".rst", ".txt"}:
        return language, SourceClassification.DOCUMENTATION
    if suffix in {".json", ".yaml", ".yml", ".toml", ".xml", ".csv"}:
        return language, SourceClassification.DATA
    if Path(decoded).name in {"dockerfile", "makefile", "containerfile"} or suffix in {
        ".ini",
        ".cfg",
        ".conf",
    }:
        return language, SourceClassification.CONFIGURATION
    if language != "unknown":
        return language, SourceClassification.SOURCE
    return language, SourceClassification.UNKNOWN


def _revision_object_type(item: _RevisionEntry) -> SourceManifestObjectType:
    if item.object_type == b"commit" or item.mode == 0o160000:
        return SourceManifestObjectType.SUBMODULE
    if item.mode == 0o120000:
        return SourceManifestObjectType.SYMLINK
    if item.object_type == b"blob":
        return SourceManifestObjectType.REGULAR_FILE
    return SourceManifestObjectType.SPECIAL


def _candidate_object_type(candidate: _WorktreeCandidate) -> SourceManifestObjectType:
    if candidate.mode == 0o160000:
        return SourceManifestObjectType.SUBMODULE
    if candidate.mode == 0o120000:
        return SourceManifestObjectType.SYMLINK
    return SourceManifestObjectType.REGULAR_FILE


def _entry(**values: object) -> SourceManifestEntry:
    return SourceManifestEntry(**values)  # type: ignore[arg-type]


__all__ = [
    "GitSourceMaterializer",
    "MaterializationCleanupReceipt",
    "MaterializedSource",
    "SourceMaterializationError",
    "SourceMaterializationFailure",
]
