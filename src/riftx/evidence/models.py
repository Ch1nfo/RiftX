"""Authoritative immutable Evidence Ledger domain contracts."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Sequence
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Annotated, Literal, Protocol, Self
from urllib.parse import quote

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from riftx.domain.base import new_id, utc_now

EVIDENCE_SCHEMA_VERSION: Literal["riftx.evidence/v1"] = "riftx.evidence/v1"
_DIGEST = r"^[0-9a-f]{64}$"
_URI = re.compile(r"[a-z][a-z0-9+.-]*://[^\s]+")
_LEDGER_DOMAIN = b"riftx-evidence-ledger-v1\0"
_MAX_ARTIFACT_SPAN_BYTES = 4 * 1024 * 1024


class _EvidenceModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, from_attributes=True)


class EvidenceKind(StrEnum):
    EXECUTION_OUTPUT = "execution_output"
    ARTIFACT_SPAN = "artifact_span"
    HTTP_REQUEST_RESPONSE = "http_request_response"
    BROWSER_OBSERVATION = "browser_observation"
    CODE_LOCATION = "code_location"
    CODE_FLOW = "code_flow"
    SCANNER_SIGNAL = "scanner_signal"
    USER_DECISION = "user_decision"
    DETERMINISTIC_PARSER_RESULT = "deterministic_parser_result"
    EXTERNAL_RESEARCH_SOURCE = "external_research_source"


class EvidenceCreatorType(StrEnum):
    AGENT = "agent"
    OPERATOR = "operator"
    SYSTEM = "system"
    TOOL = "tool"
    PARSER = "parser"
    SCANNER = "scanner"


class EvidenceTrustClass(StrEnum):
    GENERATED = "generated"
    USER_PROVIDED = "user_provided"
    UNTRUSTED_SOURCE = "untrusted_source"
    UNTRUSTED_TOOL_OUTPUT = "untrusted_tool_output"


class EvidenceRedactionStatus(StrEnum):
    NOT_REQUIRED = "not_required"
    REDACTED = "redacted"
    RESTRICTED = "restricted"
    METADATA_ONLY = "metadata_only"


class EvidenceReplayStrategy(StrEnum):
    ARTIFACT_SLICE = "artifact_slice"
    CODE_LOCATION = "code_location"
    SOURCE_LOOKUP = "source_lookup"
    NOT_REPLAYABLE = "not_replayable"


class CodeSource(StrEnum):
    WORKSPACE = "workspace"
    AUDIT_SNAPSHOT = "audit_snapshot"


class SourceLocator(_EvidenceModel):
    locator_type: Literal["source"] = "source"
    uri: str = Field(min_length=1, max_length=4096)

    @field_validator("uri")
    @classmethod
    def validate_uri(cls, value: str) -> str:
        return _validate_uri(value, "source URI")

    @property
    def source_uri(self) -> str:
        return self.uri


class ArtifactSpanLocator(_EvidenceModel):
    locator_type: Literal["artifact_span"] = "artifact_span"
    artifact_id: str = Field(min_length=1, max_length=64)
    start_offset: int = Field(ge=0)
    end_offset: int = Field(ge=1)
    artifact_sha256: str = Field(pattern=_DIGEST)

    @model_validator(mode="after")
    def validate_span(self) -> Self:
        if self.end_offset <= self.start_offset:
            raise ValueError("Artifact Span end offset must follow its start")
        if self.end_offset - self.start_offset > _MAX_ARTIFACT_SPAN_BYTES:
            raise ValueError("Artifact Span exceeds the maximum replayable size")
        return self

    @property
    def source_uri(self) -> str:
        return (
            f"artifact://{self.artifact_id}"
            f"#bytes={self.start_offset}-{self.end_offset}"
        )


class CodeLocationLocator(_EvidenceModel):
    locator_type: Literal["code_location"] = "code_location"
    source: CodeSource
    path: str = Field(min_length=1, max_length=4096)
    start_line: int = Field(ge=1)
    start_column: int = Field(default=0, ge=0)
    end_line: int = Field(ge=1)
    end_column: int = Field(ge=0)
    source_digest: str = Field(pattern=_DIGEST)

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        if value != value.strip() or "\\" in value or PurePosixPath(value).is_absolute():
            raise ValueError("Code Location path must be a normalized relative POSIX path")
        parts = PurePosixPath(value).parts
        if not parts or any(part in {"", ".", ".."} for part in parts):
            raise ValueError(
                "Code Location path must be a normalized relative POSIX path "
                "within its source root"
            )
        if _has_unsafe_unicode(value):
            raise ValueError("Code Location path contains unsafe Unicode")
        return value

    @model_validator(mode="after")
    def validate_range(self) -> Self:
        if (self.end_line, self.end_column) <= (self.start_line, self.start_column):
            raise ValueError("Code Location end position must follow its start")
        return self

    @property
    def source_uri(self) -> str:
        encoded_path = quote(self.path, safe="/._-~")
        return (
            f"code://{self.source.value}/{encoded_path}"
            f"#L{self.start_line}:C{self.start_column}"
            f"-L{self.end_line}:C{self.end_column}"
        )


EvidenceLocator = Annotated[
    SourceLocator | ArtifactSpanLocator | CodeLocationLocator,
    Field(discriminator="locator_type"),
]


class EvidenceScope(_EvidenceModel):
    engagement_id: str = Field(min_length=1, max_length=64)
    run_id: str = Field(min_length=1, max_length=64)
    audit_id: str | None = Field(default=None, min_length=1, max_length=128)
    target_refs: tuple[str, ...] = Field(default=(), max_length=100)

    @field_validator("target_refs")
    @classmethod
    def validate_target_refs(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(_validate_uri(value, "target reference") for value in values)
        if len(normalized) != len(set(normalized)):
            raise ValueError("Evidence target references must be unique")
        return normalized


class EvidenceReplayMetadata(_EvidenceModel):
    strategy: EvidenceReplayStrategy
    replayable: bool
    expected_digest: str = Field(pattern=_DIGEST)
    source_digest: str | None = Field(default=None, pattern=_DIGEST)
    parameters_digest: str | None = Field(default=None, pattern=_DIGEST)
    reason: str | None = Field(default=None, min_length=1, max_length=1000)

    @model_validator(mode="after")
    def validate_replay(self) -> Self:
        if self.replayable:
            if self.strategy is EvidenceReplayStrategy.NOT_REPLAYABLE:
                raise ValueError("replayable Evidence requires a replay strategy")
            if self.source_digest is None or self.parameters_digest is None:
                raise ValueError("replayable Evidence requires source and parameter digests")
            if self.reason is not None:
                raise ValueError("replayable Evidence cannot carry a non-replayable reason")
        elif self.strategy is not EvidenceReplayStrategy.NOT_REPLAYABLE or not self.reason:
            raise ValueError("non-replayable Evidence requires a durable reason")
        return self


class Evidence(_EvidenceModel):
    schema_version: Literal["riftx.evidence/v1"] = EVIDENCE_SCHEMA_VERSION
    id: str = Field(default_factory=new_id, min_length=1, max_length=64)
    kind: EvidenceKind
    source_uri: str = Field(min_length=1, max_length=4096)
    digest: str = Field(pattern=_DIGEST)
    ledger_digest: str = Field(default="", pattern=_DIGEST)
    run_id: str = Field(min_length=1, max_length=64)
    session_id: str | None = Field(default=None, min_length=1, max_length=64)
    task_id: str | None = Field(default=None, min_length=1, max_length=64)
    creator_type: EvidenceCreatorType
    created_by: str = Field(min_length=1, max_length=128)
    created_at: AwareDatetime = Field(default_factory=utc_now)
    trust_class: EvidenceTrustClass
    scope: EvidenceScope
    redaction_status: EvidenceRedactionStatus
    redaction_policy_ref: str | None = Field(default=None, min_length=1, max_length=4096)
    replay: EvidenceReplayMetadata
    locator: EvidenceLocator
    artifact_id: str | None = Field(default=None, min_length=1, max_length=64)

    @field_validator("source_uri")
    @classmethod
    def validate_source_uri(cls, value: str) -> str:
        return _validate_uri(value, "source URI")

    @field_validator("redaction_policy_ref")
    @classmethod
    def validate_policy_ref(cls, value: str | None) -> str | None:
        return _validate_uri(value, "redaction policy reference") if value else None

    @model_validator(mode="after")
    def validate_record(self) -> Self:
        if self.source_uri != self.locator.source_uri:
            raise ValueError("Evidence source URI must be canonical for its locator")
        if self.scope.run_id != self.run_id:
            raise ValueError("Evidence Scope must identify the Evidence Run")
        if self.replay.expected_digest != self.digest:
            raise ValueError("Evidence replay digest must match the Evidence digest")
        if isinstance(self.locator, ArtifactSpanLocator):
            if self.artifact_id != self.locator.artifact_id:
                raise ValueError("Artifact Span Evidence requires its exact Artifact reference")
        elif self.artifact_id is not None and self.kind is EvidenceKind.ARTIFACT_SPAN:
            raise ValueError("Artifact Span Evidence requires an Artifact Span locator")
        if self.kind is EvidenceKind.ARTIFACT_SPAN and not isinstance(
            self.locator, ArtifactSpanLocator
        ):
            raise ValueError("artifact_span Evidence requires an Artifact Span locator")
        if self.kind is EvidenceKind.CODE_LOCATION and not isinstance(
            self.locator, CodeLocationLocator
        ):
            raise ValueError("code_location Evidence requires a Code Location locator")
        expected_strategy = {
            "artifact_span": EvidenceReplayStrategy.ARTIFACT_SLICE,
            "code_location": EvidenceReplayStrategy.CODE_LOCATION,
            "source": EvidenceReplayStrategy.SOURCE_LOOKUP,
        }[self.locator.locator_type]
        if self.replay.replayable and self.replay.strategy is not expected_strategy:
            raise ValueError("Evidence replay strategy does not match its locator")
        if (
            self.redaction_status is EvidenceRedactionStatus.REDACTED
            and self.redaction_policy_ref is None
        ):
            raise ValueError("redacted Evidence requires a redaction policy reference")
        if (
            self.redaction_status is EvidenceRedactionStatus.NOT_REQUIRED
            and self.redaction_policy_ref is not None
        ):
            raise ValueError("unredacted Evidence cannot carry a redaction policy reference")

        expected_ledger_digest = canonical_ledger_digest(self)
        if "ledger_digest" not in self.model_fields_set:
            object.__setattr__(self, "ledger_digest", expected_ledger_digest)
        elif self.ledger_digest != expected_ledger_digest:
            raise ValueError("Evidence Ledger digest does not match its canonical record")
        return self


class EvidenceLedgerRepository(Protocol):
    async def create(self, evidence: Evidence) -> Evidence: ...

    async def get(self, evidence_id: str) -> Evidence | None: ...

    async def list_by_ids(
        self,
        run_id: str,
        evidence_ids: Sequence[str],
    ) -> tuple[Evidence, ...]: ...

    async def list(
        self,
        run_id: str,
        *,
        task_id: str | None = None,
        kind: EvidenceKind | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[Evidence, ...]: ...


def canonical_ledger_digest(evidence: Evidence) -> str:
    payload = evidence.model_dump(mode="json", exclude={"ledger_digest"})
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(_LEDGER_DOMAIN + canonical).hexdigest()


def _validate_uri(value: str, label: str) -> str:
    if value != value.strip() or len(value) > 4096 or _URI.fullmatch(value) is None:
        raise ValueError(f"{label} must be one normalized absolute URI")
    if _has_unsafe_unicode(value):
        raise ValueError(f"{label} contains unsafe Unicode")
    return value


def _has_unsafe_unicode(value: str) -> bool:
    return any(
        unicodedata.category(character) in {"Cc", "Cf", "Cs", "Co", "Cn"}
        for character in value
    )
