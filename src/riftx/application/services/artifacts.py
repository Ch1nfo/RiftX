"""Immutable Run artifact registration and retrieval."""

from __future__ import annotations

import asyncio
import hashlib
import mimetypes
import os
import secrets
import shutil
from dataclasses import dataclass
from pathlib import Path

from riftx.application.errors import (
    ApplicationConflictError,
    EntityNotFoundError,
    resource_not_accessible,
)
from riftx.application.ports import (
    ArtifactRepository,
    ExecutionRepository,
    RunEventRepository,
    RunRepository,
)
from riftx.domain import Artifact
from riftx.domain.base import new_id
from riftx.runner import RunnerPaths

from .runs import require_general_run_operation

_COPY_CHUNK_SIZE = 1024 * 1024


@dataclass(frozen=True, slots=True)
class RegisterArtifact:
    source_path: str
    name: str | None = None
    mime_type: str | None = None
    description: str = ""
    execution_id: str | None = None


@dataclass(frozen=True, slots=True)
class RegisterArtifactContent:
    content: bytes
    name: str
    mime_type: str
    description: str = ""


class ArtifactApplicationService:
    def __init__(
        self,
        *,
        run_repository: RunRepository,
        execution_repository: ExecutionRepository,
        artifact_repository: ArtifactRepository,
        event_repository: RunEventRepository,
        paths: RunnerPaths,
    ) -> None:
        self._run_repository = run_repository
        self._execution_repository = execution_repository
        self._artifact_repository = artifact_repository
        self._event_repository = event_repository
        self._paths = paths

    async def register(self, run_id: str, command: RegisterArtifact) -> Artifact:
        run = await self._run_repository.get(run_id)
        if run is None:
            raise EntityNotFoundError("Run", run_id)
        require_general_run_operation(run)
        if command.execution_id is not None:
            execution = await self._execution_repository.get(command.execution_id)
            if execution is None:
                raise EntityNotFoundError("Execution", command.execution_id)
            if execution.run_id != run_id:
                raise ApplicationConflictError(
                    "artifact_execution_mismatch",
                    "The execution does not belong to the target Run",
                    details={
                        "run_id": run_id,
                        "execution_id": command.execution_id,
                    },
                )

        source = await self._resolve_source(run_id, run.workspace_path, command.source_path)
        name = _safe_artifact_name(command.name or source.name)
        mime_type = command.mime_type or mimetypes.guess_type(name)[0] or "application/octet-stream"
        mime_type = mime_type.strip()
        if not mime_type or len(mime_type) > 255:
            raise ApplicationConflictError(
                "invalid_artifact_mime_type",
                "Artifact MIME type must contain between 1 and 255 characters",
            )

        artifact_id = new_id()
        destination = self._paths.artifact(run_id, artifact_id, name)
        try:
            sha256, size = await asyncio.to_thread(
                _snapshot_file,
                source,
                destination.directory,
                destination.content,
            )
            artifact = Artifact(
                id=artifact_id,
                run_id=run_id,
                execution_id=command.execution_id,
                name=name,
                path=str(destination.content),
                mime_type=mime_type,
                sha256=sha256,
                size=size,
                description=command.description.strip(),
            )
            await self._artifact_repository.create(artifact)
        except Exception:
            await asyncio.to_thread(shutil.rmtree, destination.directory, True)
            raise

        await self._event_repository.append(
            run_id,
            "artifact.registered",
            {
                "artifact_id": artifact.id,
                "execution_id": artifact.execution_id,
                "name": artifact.name,
                "mime_type": artifact.mime_type,
                "sha256": artifact.sha256,
                "size": artifact.size,
            },
        )
        return artifact

    async def register_content(
        self,
        run_id: str,
        command: RegisterArtifactContent,
    ) -> Artifact:
        run = await self._run_repository.get(run_id)
        if run is None:
            raise EntityNotFoundError("Run", run_id)
        require_general_run_operation(run)
        name = _safe_artifact_name(command.name)
        mime_type = command.mime_type.strip()
        if not mime_type or len(mime_type) > 255:
            raise ApplicationConflictError(
                "invalid_artifact_mime_type",
                "Artifact MIME type must contain between 1 and 255 characters",
            )

        artifact_id = new_id()
        destination = self._paths.artifact(run_id, artifact_id, name)
        try:
            sha256, size = await asyncio.to_thread(
                _snapshot_bytes,
                command.content,
                destination.directory,
                destination.content,
            )
            artifact = Artifact(
                id=artifact_id,
                run_id=run_id,
                name=name,
                path=str(destination.content),
                mime_type=mime_type,
                sha256=sha256,
                size=size,
                description=command.description.strip(),
            )
            await self._artifact_repository.create(artifact)
        except Exception:
            await asyncio.to_thread(shutil.rmtree, destination.directory, True)
            raise

        await self._event_repository.append(
            run_id,
            "artifact.registered",
            {
                "artifact_id": artifact.id,
                "execution_id": None,
                "name": artifact.name,
                "mime_type": artifact.mime_type,
                "sha256": artifact.sha256,
                "size": artifact.size,
            },
        )
        return artifact

    async def get(self, artifact_id: str) -> Artifact:
        artifact = await self._artifact_repository.get(artifact_id)
        if artifact is None:
            raise EntityNotFoundError("Artifact", artifact_id)
        return artifact

    async def resolve_run_id(self, artifact_id: str) -> str:
        run_id = await self._artifact_repository.get_run_id(artifact_id)
        if run_id is None:
            raise resource_not_accessible()
        return run_id

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
        return list(
            await self._artifact_repository.list(
                run_id,
                execution_id=execution_id,
                limit=limit,
                offset=offset,
            )
        )

    async def content_path(
        self,
        artifact_id: str,
        *,
        expected_run_id: str | None = None,
    ) -> tuple[Artifact, Path]:
        artifact = await self.get(artifact_id)
        if expected_run_id is not None and not secrets.compare_digest(
            expected_run_id,
            artifact.run_id,
        ):
            raise resource_not_accessible()
        expected = self._paths.artifact(artifact.run_id, artifact.id, artifact.name).content
        try:
            actual = await asyncio.to_thread(Path(artifact.path).resolve, strict=True)
        except (OSError, RuntimeError) as exc:
            raise ApplicationConflictError(
                "artifact_content_missing",
                f"Artifact content for {artifact.id!r} is unavailable",
                details={"artifact_id": artifact.id},
            ) from exc
        if actual != expected.resolve():
            raise ApplicationConflictError(
                "artifact_path_mismatch",
                f"Artifact {artifact.id!r} does not point to its immutable storage location",
                details={"artifact_id": artifact.id},
            )
        sha256, size = await asyncio.to_thread(_hash_file, actual)
        if sha256 != artifact.sha256 or size != artifact.size:
            raise ApplicationConflictError(
                "artifact_integrity_mismatch",
                f"Artifact {artifact.id!r} no longer matches its registered digest",
                details={
                    "artifact_id": artifact.id,
                    "expected_sha256": artifact.sha256,
                    "actual_sha256": sha256,
                    "expected_size": artifact.size,
                    "actual_size": size,
                },
            )
        return artifact, actual

    async def _resolve_source(self, run_id: str, workspace_path: str, value: str) -> Path:
        try:
            source = await asyncio.to_thread(_resolve_regular_file, value)
        except ApplicationConflictError:
            raise
        except (OSError, RuntimeError) as exc:
            raise ApplicationConflictError(
                "artifact_source_unavailable",
                f"Artifact source {value!r} is unavailable",
                details={"source_path": value},
            ) from exc
        workspace, run_directory = await asyncio.to_thread(
            lambda: (
                Path(workspace_path).expanduser().resolve(),
                self._paths.run_directory(run_id).resolve(),
            )
        )
        if not source.is_relative_to(workspace) and not source.is_relative_to(run_directory):
            raise ApplicationConflictError(
                "artifact_source_outside_run",
                "Artifact sources must be inside the Run workspace or Runner state directory",
                details={
                    "source_path": str(source),
                    "workspace_path": str(workspace),
                    "runner_path": str(run_directory),
                },
            )
        return source


def _safe_artifact_name(value: str) -> str:
    name = value.strip()
    if (
        not name
        or len(name) > 255
        or name in {".", ".."}
        or Path(name).name != name
        or "\x00" in name
    ):
        raise ApplicationConflictError(
            "invalid_artifact_name",
            "Artifact name must be a safe single path component of at most 255 characters",
            details={"name": value},
        )
    return name


def _resolve_regular_file(value: str) -> Path:
    source = Path(value).expanduser().resolve(strict=True)
    if not source.is_file():
        raise ApplicationConflictError(
            "artifact_source_not_file",
            f"Artifact source {str(source)!r} is not a regular file",
            details={"source_path": str(source)},
        )
    return source


def _snapshot_file(source: Path, directory: Path, destination: Path) -> tuple[str, int]:
    directory.mkdir(parents=True, exist_ok=False)
    temporary = directory / ".content.partial"
    digest = hashlib.sha256()
    size = 0
    try:
        with source.open("rb") as reader, temporary.open("xb") as writer:
            while chunk := reader.read(_COPY_CHUNK_SIZE):
                writer.write(chunk)
                digest.update(chunk)
                size += len(chunk)
            writer.flush()
            os.fsync(writer.fileno())
        os.replace(temporary, destination)
        destination.chmod(0o444)
    finally:
        temporary.unlink(missing_ok=True)
    return digest.hexdigest(), size


def _snapshot_bytes(content: bytes, directory: Path, destination: Path) -> tuple[str, int]:
    directory.mkdir(parents=True, exist_ok=False)
    temporary = directory / ".content.partial"
    try:
        with temporary.open("xb") as writer:
            writer.write(content)
            writer.flush()
            os.fsync(writer.fileno())
        os.replace(temporary, destination)
        destination.chmod(0o444)
    finally:
        temporary.unlink(missing_ok=True)
    return hashlib.sha256(content).hexdigest(), len(content)


def _hash_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as reader:
        while chunk := reader.read(_COPY_CHUNK_SIZE):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size
