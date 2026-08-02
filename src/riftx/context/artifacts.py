"""Execution-output spill into immutable Artifact storage with logical URIs."""

from __future__ import annotations

import asyncio
import codecs
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, unquote

from riftx.application.errors import ApplicationConflictError, EntityNotFoundError
from riftx.application.services.artifacts import ArtifactApplicationService, RegisterArtifact
from riftx.domain import Artifact, Execution

from .models import ArtifactReadResult, OutputStream, RawArtifactReference

_ARTIFACT_URI = re.compile(
    r"^artifact://runs/(?P<run>[^/]+)/executions/(?P<execution>[^/]+)/"
    r"(?P<stream>stdout|stderr)$"
)


@dataclass(frozen=True, slots=True)
class SpilledArtifact:
    reference: RawArtifactReference
    content_path: Path | None


class ExecutionArtifactStore:
    """Map Runner output files to immutable artifacts without leaking local paths."""

    def __init__(self, service: ArtifactApplicationService) -> None:
        self._service = service

    async def spill(self, execution: Execution, stream: OutputStream) -> SpilledArtifact:
        uri = execution_artifact_uri(execution.run_id, execution.id, stream)
        name = f"{stream.value}.log"
        existing = await self._find(execution.run_id, execution.id, name)
        if existing is not None:
            return await self._existing(existing, uri, stream)

        source = Path(
            execution.stdout_path if stream is OutputStream.STDOUT else execution.stderr_path
        )
        mime_type = await asyncio.to_thread(_detect_mime_type, source)
        try:
            artifact = await self._service.register(
                execution.run_id,
                RegisterArtifact(
                    source_path=str(source),
                    name=name,
                    mime_type=mime_type,
                    description=f"Immutable {stream.value} for Execution {execution.id}",
                    execution_id=execution.id,
                ),
            )
        except ApplicationConflictError as exc:
            return SpilledArtifact(
                reference=RawArtifactReference(
                    uri=uri,
                    stream=stream,
                    mime_type=mime_type,
                    size=0,
                    available=False,
                    error=f"{exc.code}: {exc.message}",
                ),
                content_path=None,
            )
        return SpilledArtifact(
            reference=_reference(artifact, uri, stream),
            content_path=Path(artifact.path),
        )

    async def resolve(self, uri: str) -> RawArtifactReference:
        run_id, execution_id, stream = parse_execution_artifact_uri(uri)
        artifact = await self._find(run_id, execution_id, f"{stream.value}.log")
        if artifact is None:
            raise EntityNotFoundError("Artifact", uri)
        spilled = await self._existing(artifact, uri, stream)
        if not spilled.reference.available:
            raise ApplicationConflictError(
                "artifact_content_missing",
                f"Artifact content for {uri!r} is unavailable",
                details={"artifact_id": artifact.id, "uri": uri},
            )
        return spilled.reference

    async def read(
        self,
        uri: str,
        *,
        offset: int = 0,
        max_bytes: int = 64 * 1024,
    ) -> ArtifactReadResult:
        if offset < 0:
            raise ValueError("artifact offset must not be negative")
        if max_bytes < 1 or max_bytes > 1024 * 1024:
            raise ValueError("max_bytes must be between 1 and 1048576")
        run_id, execution_id, stream = parse_execution_artifact_uri(uri)
        artifact = await self._find(run_id, execution_id, f"{stream.value}.log")
        if artifact is None:
            raise EntityNotFoundError("Artifact", uri)
        _, path = await self._service.content_path(artifact.id)
        size = artifact.size
        if offset > size:
            raise ValueError(f"artifact offset {offset} is beyond content size {size}")
        data = await asyncio.to_thread(_read_slice, path, offset, max_bytes)
        next_offset = offset + len(data)
        return ArtifactReadResult(
            uri=uri,
            mime_type=artifact.mime_type,
            data=data,
            offset=offset,
            next_offset=next_offset,
            eof=next_offset >= size,
        )

    async def _find(self, run_id: str, execution_id: str, name: str) -> Artifact | None:
        artifacts = await self._service.list(run_id, execution_id=execution_id, limit=1000)
        return next((artifact for artifact in reversed(artifacts) if artifact.name == name), None)

    async def _existing(
        self,
        artifact: Artifact,
        uri: str,
        stream: OutputStream,
    ) -> SpilledArtifact:
        try:
            _, path = await self._service.content_path(artifact.id)
        except ApplicationConflictError as exc:
            return SpilledArtifact(
                reference=RawArtifactReference(
                    artifact_id=artifact.id,
                    uri=uri,
                    stream=stream,
                    mime_type=artifact.mime_type,
                    size=artifact.size,
                    sha256=artifact.sha256,
                    available=False,
                    error=f"{exc.code}: {exc.message}",
                ),
                content_path=None,
            )
        return SpilledArtifact(
            reference=_reference(artifact, uri, stream),
            content_path=path,
        )


def execution_artifact_uri(run_id: str, execution_id: str, stream: OutputStream) -> str:
    return (
        f"artifact://runs/{quote(run_id, safe='')}/executions/"
        f"{quote(execution_id, safe='')}/{stream.value}"
    )


def parse_execution_artifact_uri(uri: str) -> tuple[str, str, OutputStream]:
    match = _ARTIFACT_URI.fullmatch(uri)
    if match is None:
        raise ValueError(f"invalid execution Artifact URI: {uri!r}")
    run_id = unquote(match.group("run"))
    execution_id = unquote(match.group("execution"))
    if not run_id or not execution_id or "/" in run_id or "/" in execution_id:
        raise ValueError(f"invalid execution Artifact URI: {uri!r}")
    return run_id, execution_id, OutputStream(match.group("stream"))


def _reference(
    artifact: Artifact,
    uri: str,
    stream: OutputStream,
) -> RawArtifactReference:
    return RawArtifactReference(
        artifact_id=artifact.id,
        uri=uri,
        stream=stream,
        mime_type=artifact.mime_type,
        size=artifact.size,
        sha256=artifact.sha256,
    )


def _detect_mime_type(path: Path) -> str:
    try:
        with path.open("rb") as stream:
            sample = stream.read(4096)
    except OSError:
        return "application/octet-stream"
    if b"\x00" in sample:
        return "application/octet-stream"
    try:
        decoder = codecs.getincrementaldecoder("utf-8")()
        decoder.decode(sample, final=False)
    except UnicodeDecodeError:
        return "application/octet-stream"
    return "text/plain; charset=utf-8"


def _read_slice(path: Path, offset: int, max_bytes: int) -> bytes:
    with path.open("rb") as stream:
        stream.seek(offset)
        return stream.read(max_bytes)
