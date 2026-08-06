"""Server-owned registration of replayable Evidence facts."""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from riftx.application.errors import ApplicationConflictError, EntityNotFoundError
from riftx.application.ports import RunRepository
from riftx.code import CodeLocationContent
from riftx.evidence import (
    ArtifactSpanLocator,
    CodeLocationLocator,
    CodeSource,
    Evidence,
    EvidenceCreatorType,
    EvidenceKind,
    EvidenceLedgerRepository,
    EvidenceRedactionStatus,
    EvidenceReplayMetadata,
    EvidenceReplayStrategy,
    EvidenceScope,
    EvidenceTrustClass,
)
from riftx.runtime.types import AgentSession
from riftx.tasks import TaskGraphRepository

from .artifacts import ArtifactContentSlice

_DIGEST = r"^[0-9a-f]{64}$"
_REPLAY_PARAMETERS_DOMAIN = b"riftx-evidence-replay-parameters-v1\0"


class AgentSessionReader(Protocol):
    async def get(self, session_id: str) -> AgentSession | None: ...


class ArtifactEvidenceSource(Protocol):
    async def read_content_slice(
        self,
        artifact_id: str,
        *,
        expected_run_id: str,
        offset: int,
        max_bytes: int,
    ) -> ArtifactContentSlice: ...

    async def read_audit_content_slice(
        self,
        artifact_id: str,
        *,
        audit_id: str,
        run_id: str,
        offset: int,
        max_bytes: int,
    ) -> ArtifactContentSlice: ...


class CodeEvidenceSource(Protocol):
    async def read_location(
        self,
        run_id: str,
        *,
        path: str,
        start_line: int,
        start_column: int,
        end_line: int,
        end_column: int,
    ) -> CodeLocationContent: ...


class _Registration(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str = Field(min_length=1, max_length=64)
    session_id: str | None = Field(default=None, min_length=1, max_length=64)
    task_id: str | None = Field(default=None, min_length=1, max_length=64)
    audit_id: str | None = Field(default=None, min_length=1, max_length=128)
    creator_type: EvidenceCreatorType
    created_by: str = Field(min_length=1, max_length=128)
    trust_class: EvidenceTrustClass
    target_refs: tuple[str, ...] = Field(default=(), max_length=100)
    redaction_status: EvidenceRedactionStatus = EvidenceRedactionStatus.NOT_REQUIRED
    redaction_policy_ref: str | None = Field(default=None, min_length=1, max_length=4096)


class RegisterArtifactSpanEvidence(_Registration):
    artifact_id: str = Field(min_length=1, max_length=64)
    start_offset: int = Field(ge=0)
    end_offset: int = Field(ge=1)

    @model_validator(mode="after")
    def validate_span(self) -> RegisterArtifactSpanEvidence:
        if self.end_offset <= self.start_offset:
            raise ValueError("Artifact Span end offset must follow its start")
        if self.end_offset - self.start_offset > 4 * 1024 * 1024:
            raise ValueError("Artifact Span exceeds the maximum replayable size")
        return self


class RegisterCodeLocationEvidence(_Registration):
    source: CodeSource
    path: str = Field(min_length=1, max_length=4096)
    start_line: int = Field(ge=1)
    start_column: int = Field(default=0, ge=0)
    end_line: int = Field(ge=1)
    end_column: int = Field(ge=0)
    expected_content_digest: str = Field(pattern=_DIGEST)
    expected_source_digest: str | None = Field(default=None, pattern=_DIGEST)

    @model_validator(mode="after")
    def require_immutable_snapshot_identity(self) -> RegisterCodeLocationEvidence:
        if self.source is CodeSource.AUDIT_SNAPSHOT and self.expected_source_digest is None:
            raise ValueError("Audit Snapshot Evidence requires its source digest")
        return self


class EvidenceApplicationService:
    def __init__(
        self,
        *,
        runs: RunRepository,
        sessions: AgentSessionReader,
        tasks: TaskGraphRepository,
        artifacts: ArtifactEvidenceSource,
        code: CodeEvidenceSource,
        ledger: EvidenceLedgerRepository,
    ) -> None:
        self._runs = runs
        self._sessions = sessions
        self._tasks = tasks
        self._artifacts = artifacts
        self._code = code
        self._ledger = ledger

    async def register_artifact_span(
        self,
        command: RegisterArtifactSpanEvidence,
    ) -> Evidence:
        scope = await self._scope(command, resolved_audit_id=command.audit_id)
        size = command.end_offset - command.start_offset
        if command.audit_id is None:
            content = await self._artifacts.read_content_slice(
                command.artifact_id,
                expected_run_id=command.run_id,
                offset=command.start_offset,
                max_bytes=size,
            )
        else:
            content = await self._artifacts.read_audit_content_slice(
                command.artifact_id,
                audit_id=command.audit_id,
                run_id=command.run_id,
                offset=command.start_offset,
                max_bytes=size,
            )
        if (
            content.artifact.id != command.artifact_id
            or content.artifact.run_id != command.run_id
            or content.artifact.audit_id != command.audit_id
            or content.offset != command.start_offset
            or content.next_offset != command.end_offset
        ):
            raise _conflict(
                "evidence_artifact_span_invalid",
                "Artifact Span is outside the immutable Artifact content",
            )
        locator = ArtifactSpanLocator(
            artifact_id=content.artifact.id,
            start_offset=command.start_offset,
            end_offset=command.end_offset,
            artifact_sha256=content.artifact.sha256,
        )
        digest = hashlib.sha256(content.data).hexdigest()
        return await self._ledger.create(
            Evidence(
                kind=EvidenceKind.ARTIFACT_SPAN,
                source_uri=locator.source_uri,
                digest=digest,
                run_id=command.run_id,
                session_id=command.session_id,
                task_id=command.task_id,
                creator_type=command.creator_type,
                created_by=command.created_by,
                trust_class=command.trust_class,
                scope=scope,
                redaction_status=command.redaction_status,
                redaction_policy_ref=command.redaction_policy_ref,
                replay=EvidenceReplayMetadata(
                    strategy=EvidenceReplayStrategy.ARTIFACT_SLICE,
                    replayable=True,
                    expected_digest=digest,
                    source_digest=content.artifact.sha256,
                    parameters_digest=_parameters_digest(
                        {
                            "artifact_id": command.artifact_id,
                            "end_offset": command.end_offset,
                            "start_offset": command.start_offset,
                        }
                    ),
                ),
                locator=locator,
                artifact_id=command.artifact_id,
            )
        )

    async def register_code_location(
        self,
        command: RegisterCodeLocationEvidence,
    ) -> Evidence:
        content = await self._code.read_location(
            command.run_id,
            path=command.path,
            start_line=command.start_line,
            start_column=command.start_column,
            end_line=command.end_line,
            end_column=command.end_column,
        )
        expected_source = command.source.value
        if content.source != expected_source:
            raise _conflict(
                "evidence_code_source_mismatch",
                "Code Location source does not match the Run-owned source",
            )
        if content.path != command.path:
            raise _conflict(
                "evidence_code_path_mismatch",
                "Code Location path does not match the Run-owned source",
            )
        if not hmac.compare_digest(content.content_digest, command.expected_content_digest):
            raise _conflict(
                "evidence_code_content_changed",
                "Code Location file changed after it was observed",
            )
        if command.expected_source_digest is not None and (
            content.source_digest is None
            or not hmac.compare_digest(
                content.source_digest,
                command.expected_source_digest,
            )
        ):
            raise _conflict(
                "evidence_code_source_changed",
                "Code Location source changed after it was observed",
            )
        scope = await self._scope(command, resolved_audit_id=content.audit_id)
        locator = CodeLocationLocator(
            source=command.source,
            path=content.path,
            start_line=command.start_line,
            start_column=command.start_column,
            end_line=command.end_line,
            end_column=command.end_column,
            source_digest=content.content_digest,
        )
        digest = hashlib.sha256(content.data).hexdigest()
        return await self._ledger.create(
            Evidence(
                kind=EvidenceKind.CODE_LOCATION,
                source_uri=locator.source_uri,
                digest=digest,
                run_id=command.run_id,
                session_id=command.session_id,
                task_id=command.task_id,
                creator_type=command.creator_type,
                created_by=command.created_by,
                trust_class=command.trust_class,
                scope=scope,
                redaction_status=command.redaction_status,
                redaction_policy_ref=command.redaction_policy_ref,
                replay=EvidenceReplayMetadata(
                    strategy=EvidenceReplayStrategy.CODE_LOCATION,
                    replayable=True,
                    expected_digest=digest,
                    source_digest=content.source_digest or content.content_digest,
                    parameters_digest=_parameters_digest(
                        {
                            "end_column": command.end_column,
                            "end_line": command.end_line,
                            "path": content.path,
                            "start_column": command.start_column,
                            "start_line": command.start_line,
                        }
                    ),
                ),
                locator=locator,
            )
        )

    async def _scope(
        self,
        command: _Registration,
        *,
        resolved_audit_id: str | None,
    ) -> EvidenceScope:
        run = await self._runs.get(command.run_id)
        if run is None:
            raise EntityNotFoundError("Run", command.run_id)
        if command.audit_id != resolved_audit_id:
            raise _conflict(
                "evidence_audit_owner_mismatch",
                "Evidence source does not belong to the requested Audit",
            )
        if command.session_id is not None:
            session = await self._sessions.get(command.session_id)
            if session is None or session.run_id != command.run_id:
                raise _conflict(
                    "evidence_session_owner_mismatch",
                    "Evidence Session does not belong to the Run",
                )
        if command.task_id is not None:
            graph = await self._tasks.get(command.run_id)
            if graph is None or command.task_id not in {task.id for task in graph.tasks}:
                raise _conflict(
                    "evidence_task_owner_mismatch",
                    "Evidence Task does not belong to the Run",
                )
        return EvidenceScope(
            engagement_id=run.engagement_id,
            run_id=run.id,
            audit_id=resolved_audit_id,
            target_refs=command.target_refs,
        )


def _parameters_digest(parameters: dict[str, object]) -> str:
    canonical = json.dumps(
        parameters,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(_REPLAY_PARAMETERS_DOMAIN + canonical).hexdigest()


def _conflict(code: str, message: str) -> ApplicationConflictError:
    return ApplicationConflictError(code, message)
