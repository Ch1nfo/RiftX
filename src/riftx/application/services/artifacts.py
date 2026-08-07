"""Immutable Run Artifact registration and descriptor-safe retrieval."""

from __future__ import annotations

import asyncio
import mimetypes
from collections.abc import Callable, Coroutine, Sequence
from dataclasses import dataclass
from pathlib import Path

from riftx.application.errors import (
    ApplicationConflictError,
    EntityNotFoundError,
    RepositoryIntegrityError,
    RepositoryUnavailableError,
    ServiceUnavailableError,
    resource_not_accessible,
)
from riftx.application.ports import (
    ArtifactRepository,
    AuditAggregateReadRepository,
    AuditAuthorizationBinding,
    ExecutionRepository,
    RunEventRepository,
    RunRepository,
)
from riftx.application.ports.repositories import ArtifactOwnerBinding
from riftx.domain import (
    Artifact,
    ArtifactAccessClass,
    ArtifactContentTrust,
    ArtifactIngestMethod,
    ArtifactIngestProvenance,
    RunKind,
)
from riftx.domain.base import new_id
from riftx.runner import (
    ArtifactContentFailure,
    ArtifactContentStoreError,
    LocalArtifactContentStore,
    OpenedArtifactContent,
    RunnerPaths,
)

from .runs import require_interactive_run_operation

_DEFAULT_MAX_ARTIFACT_BYTES = 64 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class RegisterArtifact:
    source_path: str
    name: str | None = None
    mime_type: str | None = None
    description: str = ""
    execution_id: str | None = None
    content_trust: ArtifactContentTrust = ArtifactContentTrust.UNTRUSTED_TOOL_OUTPUT


@dataclass(frozen=True, slots=True)
class RegisterArtifactContent:
    content: bytes
    name: str
    mime_type: str
    description: str = ""
    content_trust: ArtifactContentTrust = ArtifactContentTrust.UNTRUSTED_TOOL_OUTPUT


@dataclass(frozen=True, slots=True)
class ArtifactContentSlice:
    artifact: Artifact
    data: bytes
    offset: int
    next_offset: int
    eof: bool


class ArtifactApplicationService:
    def __init__(
        self,
        *,
        run_repository: RunRepository,
        execution_repository: ExecutionRepository,
        artifact_repository: ArtifactRepository,
        event_repository: RunEventRepository,
        paths: RunnerPaths,
        max_artifact_bytes: int = _DEFAULT_MAX_ARTIFACT_BYTES,
        content_store: LocalArtifactContentStore | None = None,
        audit_repository: AuditAggregateReadRepository | None = None,
    ) -> None:
        self._run_repository = run_repository
        self._execution_repository = execution_repository
        self._artifact_repository = artifact_repository
        self._event_repository = event_repository
        self._paths = paths
        self._audit_repository = audit_repository
        self._content_store = content_store or LocalArtifactContentStore(
            paths,
            max_artifact_bytes=max_artifact_bytes,
        )

    async def register(self, run_id: str, command: RegisterArtifact) -> Artifact:
        run = await self._run_repository.get(run_id)
        if run is None:
            raise EntityNotFoundError("Run", run_id)
        require_interactive_run_operation(run)
        if command.execution_id is not None:
            execution = await self._execution_repository.get(command.execution_id)
            if execution is None:
                raise EntityNotFoundError("Execution", command.execution_id)
            if execution.run_id != run_id:
                raise ApplicationConflictError(
                    "artifact_execution_mismatch",
                    "The execution does not belong to the target Run",
                )

        source_name = Path(command.source_path).name
        name = _safe_artifact_name(command.name or source_name)
        mime_type = _safe_mime_type(
            command.mime_type or mimetypes.guess_type(name)[0] or "application/octet-stream"
        )
        artifact_id = new_id()
        destination = self._paths.artifact(run_id, artifact_id, name)
        try:
            stored = await _complete_blocking_operation(
                lambda: self._content_store.snapshot_file(
                    command.source_path,
                    allowed_roots=(
                        Path(run.workspace_path).expanduser(),
                        self._paths.run_directory(run_id),
                    ),
                    storage_key=destination.storage_key,
                ),
                on_cancel=lambda _: self._content_store.discard(destination.storage_key),
            )
        except ArtifactContentStoreError as exc:
            raise _registration_store_error(exc) from None

        artifact = Artifact(
            id=artifact_id,
            run_id=run_id,
            execution_id=command.execution_id,
            audit_id=None,
            access_class=ArtifactAccessClass.PUBLIC_EXPORT,
            content_trust=command.content_trust,
            name=name,
            path=str(destination.content),
            storage_key=destination.storage_key,
            ingest_provenance=ArtifactIngestProvenance(
                method=ArtifactIngestMethod.LOCAL_NOFOLLOW_FD,
                producer_node_id=run.node_id,
                producer_execution_id=command.execution_id,
            ),
            mime_type=mime_type,
            sha256=stored.sha256,
            size=stored.size,
            description=command.description.strip(),
        )
        await self._persist_or_discard(artifact)
        await self._append_registered_event(artifact)
        return artifact

    async def register_content(
        self,
        run_id: str,
        command: RegisterArtifactContent,
    ) -> Artifact:
        run = await self._run_repository.get(run_id)
        if run is None:
            raise EntityNotFoundError("Run", run_id)
        require_interactive_run_operation(run)
        return await self._register_owned_content(
            run_id=run.id,
            node_id=run.node_id,
            audit_id=None,
            access_class=ArtifactAccessClass.PUBLIC_EXPORT,
            command=command,
        )

    async def register_audit_content(
        self,
        audit_id: str,
        run_id: str,
        command: RegisterArtifactContent,
    ) -> Artifact:
        run = await self._run_repository.get(run_id)
        if run is None:
            raise EntityNotFoundError("Run", run_id)
        if run.kind is not RunKind.CODE_AUDIT or self._audit_repository is None:
            raise resource_not_accessible()

        def authorize(binding: AuditAuthorizationBinding) -> None:
            if (
                binding.requested_audit_id != audit_id
                or binding.audit_id != audit_id
                or binding.scan_run_id != run_id
                or binding.run_id != run_id
                or binding.run_kind != RunKind.CODE_AUDIT.value
            ):
                raise resource_not_accessible()

        try:
            aggregate = await self._audit_repository.get_by_run_authorized(
                run_id,
                authorize=authorize,
            )
        except (RepositoryIntegrityError, RepositoryUnavailableError):
            raise _audit_persistence_unavailable() from None
        if aggregate is None or aggregate.audit.value.id != audit_id:
            raise resource_not_accessible()
        return await self._register_owned_content(
            run_id=run.id,
            node_id=run.node_id,
            audit_id=audit_id,
            access_class=ArtifactAccessClass.AUDIT_INTERNAL,
            command=command,
        )

    async def _register_owned_content(
        self,
        *,
        run_id: str,
        node_id: str,
        audit_id: str | None,
        access_class: ArtifactAccessClass,
        command: RegisterArtifactContent,
    ) -> Artifact:
        name = _safe_artifact_name(command.name)
        mime_type = _safe_mime_type(command.mime_type)
        artifact_id = new_id()
        destination = self._paths.artifact(run_id, artifact_id, name)
        try:
            stored = await _complete_blocking_operation(
                lambda: self._content_store.snapshot_bytes(
                    command.content,
                    storage_key=destination.storage_key,
                ),
                on_cancel=lambda _: self._content_store.discard(destination.storage_key),
            )
        except ArtifactContentStoreError as exc:
            raise _registration_store_error(exc) from None

        artifact = Artifact(
            id=artifact_id,
            run_id=run_id,
            audit_id=audit_id,
            access_class=access_class,
            content_trust=command.content_trust,
            name=name,
            path=str(destination.content),
            storage_key=destination.storage_key,
            ingest_provenance=ArtifactIngestProvenance(
                method=ArtifactIngestMethod.CONTROL_PLANE_BYTES,
                producer_node_id=node_id,
            ),
            mime_type=mime_type,
            sha256=stored.sha256,
            size=stored.size,
            description=command.description.strip(),
        )
        await self._persist_or_discard(artifact)
        await self._append_registered_event(artifact)
        return artifact

    async def get(self, artifact_id: str) -> Artifact:
        try:
            artifact = await self._artifact_repository.get(artifact_id)
        except (RepositoryIntegrityError, RepositoryUnavailableError):
            raise _artifact_persistence_unavailable() from None
        if artifact is None:
            raise EntityNotFoundError("Artifact", artifact_id)
        return artifact

    async def get_target_http_for_evidence(
        self,
        artifact_id: str,
        *,
        expected_run_id: str,
    ) -> Artifact:
        try:
            artifact = await self._artifact_repository.get_target_http_for_evidence(
                artifact_id,
                expected_run_id,
            )
        except (RepositoryIntegrityError, RepositoryUnavailableError):
            raise _artifact_persistence_unavailable() from None
        if artifact is None:
            raise EntityNotFoundError("Artifact", artifact_id)
        _require_exact_owner(
            artifact,
            artifact_id=artifact_id,
            run_id=expected_run_id,
            audit_id=None,
        )
        return artifact

    async def resolve_run_id(self, artifact_id: str) -> str:
        try:
            run_id = await self._artifact_repository.get_run_id(artifact_id)
        except (RepositoryIntegrityError, RepositoryUnavailableError):
            raise _artifact_persistence_unavailable() from None
        if run_id is None:
            raise resource_not_accessible()
        return run_id

    async def resolve_owner(self, artifact_id: str) -> ArtifactOwnerBinding:
        try:
            binding = await self._artifact_repository.resolve_owner(artifact_id)
        except (RepositoryIntegrityError, RepositoryUnavailableError):
            raise _artifact_persistence_unavailable() from None
        if binding is None:
            raise resource_not_accessible()
        return binding

    async def get_for_audit(
        self,
        artifact_id: str,
        *,
        audit_id: str,
        run_id: str,
    ) -> Artifact:
        try:
            artifact = await self._artifact_repository.get_for_audit(
                artifact_id,
                audit_id,
                run_id,
            )
        except (RepositoryIntegrityError, RepositoryUnavailableError):
            raise _artifact_persistence_unavailable() from None
        if artifact is None:
            raise resource_not_accessible()
        _require_exact_owner(
            artifact,
            artifact_id=artifact_id,
            run_id=run_id,
            audit_id=audit_id,
        )
        return artifact

    async def list(
        self,
        run_id: str,
        *,
        execution_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Artifact]:
        if await self._run_repository.get(run_id) is None:
            raise EntityNotFoundError("Run", run_id)
        try:
            artifacts = await self._artifact_repository.list(
                run_id,
                execution_id=execution_id,
                limit=limit,
                offset=offset,
            )
        except (RepositoryIntegrityError, RepositoryUnavailableError):
            raise _artifact_persistence_unavailable() from None
        return list(artifacts)

    async def list_for_audit(
        self,
        audit_id: str,
        run_id: str,
        *,
        execution_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> Sequence[Artifact]:
        try:
            artifacts = await self._artifact_repository.list_for_audit(
                audit_id,
                run_id,
                execution_id=execution_id,
                limit=limit,
                offset=offset,
            )
        except (RepositoryIntegrityError, RepositoryUnavailableError):
            raise _artifact_persistence_unavailable() from None
        result = list(artifacts)
        for artifact in result:
            _require_exact_owner(
                artifact,
                artifact_id=artifact.id,
                run_id=run_id,
                audit_id=audit_id,
            )
        return result

    async def open_public_content(
        self,
        artifact_id: str,
        *,
        expected_run_id: str,
    ) -> tuple[Artifact, OpenedArtifactContent]:
        artifact = await self.get(artifact_id)
        _require_exact_owner(
            artifact,
            artifact_id=artifact_id,
            run_id=expected_run_id,
            audit_id=artifact.audit_id,
        )
        if artifact.access_class is not ArtifactAccessClass.PUBLIC_EXPORT:
            raise resource_not_accessible()
        return artifact, await self._open_verified(artifact)

    async def open_audit_content(
        self,
        artifact_id: str,
        *,
        audit_id: str,
        run_id: str,
    ) -> tuple[Artifact, OpenedArtifactContent]:
        artifact = await self.get_for_audit(
            artifact_id,
            audit_id=audit_id,
            run_id=run_id,
        )
        return artifact, await self._open_verified(artifact)

    async def read_content_slice(
        self,
        artifact_id: str,
        *,
        expected_run_id: str,
        offset: int = 0,
        max_bytes: int = 64 * 1024,
    ) -> ArtifactContentSlice:
        if offset < 0:
            raise ValueError("Artifact offset must not be negative")
        if max_bytes < 1 or max_bytes > self._content_store.max_artifact_bytes:
            raise ValueError("Artifact read size is outside the configured bounds")
        artifact, lease = await self.open_public_content(
            artifact_id,
            expected_run_id=expected_run_id,
        )
        return await self._read_open_content_slice(
            artifact,
            lease,
            offset=offset,
            max_bytes=max_bytes,
        )

    async def read_target_http_content_slice(
        self,
        artifact_id: str,
        *,
        expected_run_id: str,
        offset: int = 0,
        max_bytes: int = 64 * 1024,
    ) -> ArtifactContentSlice:
        if offset < 0:
            raise ValueError("Artifact offset must not be negative")
        if max_bytes < 1 or max_bytes > self._content_store.max_artifact_bytes:
            raise ValueError("Artifact read size is outside the configured bounds")
        artifact = await self.get_target_http_for_evidence(
            artifact_id,
            expected_run_id=expected_run_id,
        )
        return await self._read_open_content_slice(
            artifact,
            await self._open_verified(artifact),
            offset=offset,
            max_bytes=max_bytes,
        )

    async def read_audit_content_slice(
        self,
        artifact_id: str,
        *,
        audit_id: str,
        run_id: str,
        offset: int = 0,
        max_bytes: int = 64 * 1024,
    ) -> ArtifactContentSlice:
        if offset < 0:
            raise ValueError("Artifact offset must not be negative")
        if max_bytes < 1 or max_bytes > self._content_store.max_artifact_bytes:
            raise ValueError("Artifact read size is outside the configured bounds")
        artifact, lease = await self.open_audit_content(
            artifact_id,
            audit_id=audit_id,
            run_id=run_id,
        )
        return await self._read_open_content_slice(
            artifact,
            lease,
            offset=offset,
            max_bytes=max_bytes,
        )

    async def _read_open_content_slice(
        self,
        artifact: Artifact,
        lease: OpenedArtifactContent,
        *,
        offset: int,
        max_bytes: int,
    ) -> ArtifactContentSlice:
        try:
            if offset > artifact.size:
                raise ValueError("Artifact offset is beyond content size")
            lease.seek(offset)
            data = await _complete_blocking_operation(
                lambda: lease.read(max_bytes) if offset < artifact.size else b"",
                on_cancel=lambda _: lease.close(),
            )
            await _complete_blocking_operation(
                lease.verify_unchanged,
                on_cancel=lambda _: lease.close(),
            )
        except ArtifactContentStoreError as exc:
            raise _download_store_error(exc) from None
        finally:
            lease.close()
        next_offset = offset + len(data)
        return ArtifactContentSlice(
            artifact=artifact,
            data=data,
            offset=offset,
            next_offset=next_offset,
            eof=next_offset >= artifact.size,
        )

    async def _open_verified(self, artifact: Artifact) -> OpenedArtifactContent:
        expected = self._paths.artifact(
            artifact.run_id,
            artifact.id,
            artifact.name,
        )
        if expected.storage_key != artifact.storage_key:
            raise ApplicationConflictError(
                "artifact_integrity_mismatch",
                "Artifact content failed immutable storage verification",
            )
        try:
            return await _complete_blocking_operation(
                lambda: self._content_store.open_verified(
                    storage_key=artifact.storage_key,
                    expected_sha256=artifact.sha256,
                    expected_size=artifact.size,
                ),
                on_cancel=lambda lease: lease.close(),
            )
        except ArtifactContentStoreError as exc:
            raise _download_store_error(exc) from None

    async def _persist_or_discard(self, artifact: Artifact) -> None:
        try:
            await self._artifact_repository.create(artifact)
        except asyncio.CancelledError:
            await _finish_async_cleanup_after_cancellation(
                lambda: self._discard_unless_persisted(artifact)
            )
            raise
        except Exception:
            await self._discard_unless_persisted(artifact)
            raise

    async def _discard_unless_persisted(self, artifact: Artifact) -> None:
        try:
            current = await self._artifact_repository.get_for_reconciliation(artifact.id)
        except Exception:
            return
        if current == artifact:
            return
        await _complete_blocking_operation(
            lambda: self._content_store.discard(artifact.storage_key)
        )

    async def _append_registered_event(self, artifact: Artifact) -> None:
        payload: dict[str, object] = {
            "artifact_id": artifact.id,
            "execution_id": artifact.execution_id,
            "name": artifact.name,
            "mime_type": artifact.mime_type,
            "sha256": artifact.sha256,
            "size": artifact.size,
            "access_class": artifact.access_class.value,
            "content_trust": artifact.content_trust.value,
        }
        if artifact.audit_id is not None:
            payload["audit_id"] = artifact.audit_id
        await self._event_repository.append(
            artifact.run_id,
            "artifact.registered",
            payload,
        )


async def _complete_blocking_operation[T](
    operation: Callable[[], T],
    *,
    on_cancel: Callable[[T], None] | None = None,
) -> T:
    """Keep ownership until a blocking operation and cancellation cleanup settle."""

    worker = asyncio.create_task(asyncio.to_thread(operation))
    cancelled = await _wait_for_task_completion(worker)
    try:
        result = worker.result()
    except asyncio.CancelledError:
        raise
    except Exception:
        if cancelled:
            raise asyncio.CancelledError() from None
        raise

    if not cancelled:
        return result
    if on_cancel is not None:
        cleanup = asyncio.create_task(asyncio.to_thread(on_cancel, result))
        await _wait_for_task_completion(cleanup)
        if not cleanup.cancelled():
            try:
                cleanup.result()
            except Exception:
                pass
    raise asyncio.CancelledError()


async def _finish_async_cleanup_after_cancellation(
    operation: Callable[[], Coroutine[object, object, None]],
) -> None:
    """Finish an ownership cleanup even if the caller is cancelled again."""

    cleanup: asyncio.Task[None] = asyncio.create_task(operation())
    await _wait_for_task_completion(cleanup)
    if not cleanup.cancelled():
        try:
            cleanup.result()
        except Exception:
            pass


async def _wait_for_task_completion[T](task: asyncio.Task[T]) -> bool:
    """Wait for one owned Task while remembering any number of cancellations."""

    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
        except Exception:
            # The outcome is consumed by the owner after the Task is settled.
            pass
    return cancelled


def _safe_artifact_name(value: str) -> str:
    name = value.strip()
    if (
        not name
        or len(name) > 255
        or name in {".", ".."}
        or Path(name).name != name
        or "/" in name
        or "\\" in name
        or any(not 0x20 <= ord(character) <= 0x7E for character in name)
    ):
        raise ApplicationConflictError(
            "invalid_artifact_name",
            "Artifact name must be a safe single path component of at most 255 characters",
        )
    return name


def _safe_mime_type(value: str) -> str:
    mime_type = value
    if (
        not mime_type
        or len(mime_type) > 255
        or mime_type != mime_type.strip()
        or any(not 0x20 <= ord(character) <= 0x7E for character in mime_type)
    ):
        raise ApplicationConflictError(
            "invalid_artifact_mime_type",
            "Artifact MIME type must contain between 1 and 255 safe characters",
        )
    return mime_type


def _require_exact_owner(
    artifact: Artifact,
    *,
    artifact_id: str,
    run_id: str,
    audit_id: str | None,
) -> None:
    pairs: Sequence[tuple[str | None, str | None]] = (
        (artifact.id, artifact_id),
        (artifact.run_id, run_id),
        (artifact.audit_id, audit_id),
    )
    if any(actual != expected for actual, expected in pairs):
        raise resource_not_accessible()


def _registration_store_error(
    error: ArtifactContentStoreError,
) -> ApplicationConflictError | ServiceUnavailableError:
    if error.failure is ArtifactContentFailure.SOURCE_OUTSIDE_ROOT:
        return ApplicationConflictError(
            "artifact_source_outside_run",
            "Artifact sources must be inside the Run workspace or Runner state directory",
        )
    if error.failure is ArtifactContentFailure.SOURCE_NOT_REGULAR:
        return ApplicationConflictError(
            "artifact_source_not_file",
            "Artifact source must be a regular file",
        )
    if error.failure is ArtifactContentFailure.SOURCE_LINKED:
        return ApplicationConflictError(
            "artifact_source_linked",
            "Artifact source has an unsafe additional filesystem link",
        )
    if error.failure is ArtifactContentFailure.SOURCE_CHANGED:
        return ApplicationConflictError(
            "artifact_source_changed",
            "Artifact source changed while it was being ingested",
        )
    if error.failure is ArtifactContentFailure.SIZE_LIMIT_EXCEEDED:
        return ApplicationConflictError(
            "artifact_size_limit_exceeded",
            "Artifact content exceeds the configured byte limit",
        )
    if error.failure in {
        ArtifactContentFailure.SOURCE_UNAVAILABLE,
        ArtifactContentFailure.DECLARED_CONTENT_MISMATCH,
    }:
        return ApplicationConflictError(
            "artifact_source_unavailable",
            "Artifact source is unavailable or invalid",
        )
    return ServiceUnavailableError(
        "artifact_storage_unavailable",
        "Artifact storage is temporarily unavailable",
    )


def _download_store_error(
    error: ArtifactContentStoreError,
) -> ApplicationConflictError | ServiceUnavailableError:
    if error.failure is ArtifactContentFailure.STORAGE_MISSING:
        return ApplicationConflictError(
            "artifact_content_missing",
            "Artifact content is unavailable",
        )
    if error.failure is ArtifactContentFailure.STORAGE_INTEGRITY:
        return ApplicationConflictError(
            "artifact_integrity_mismatch",
            "Artifact content failed immutable storage verification",
        )
    return ServiceUnavailableError(
        "artifact_storage_unavailable",
        "Artifact storage is temporarily unavailable",
    )


def _artifact_persistence_unavailable() -> ServiceUnavailableError:
    return ServiceUnavailableError(
        "artifact_persistence_unavailable",
        "Artifact metadata is temporarily unavailable",
    )


def _audit_persistence_unavailable() -> ServiceUnavailableError:
    return ServiceUnavailableError(
        "audit_persistence_unavailable",
        "Audit metadata is temporarily unavailable",
    )


__all__ = [
    "ArtifactApplicationService",
    "ArtifactContentSlice",
    "RegisterArtifact",
    "RegisterArtifactContent",
]
