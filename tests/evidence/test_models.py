from __future__ import annotations

import pytest
from pydantic import ValidationError

from riftx.evidence import (
    ArtifactSpanLocator,
    CodeLocationLocator,
    CodeSource,
    Evidence,
    EvidenceCreatorType,
    EvidenceKind,
    EvidenceRedactionStatus,
    EvidenceReplayMetadata,
    EvidenceReplayStrategy,
    EvidenceScope,
    EvidenceTrustClass,
)


def artifact_evidence(**updates: object) -> Evidence:
    locator = ArtifactSpanLocator(
        artifact_id="artifact-1",
        start_offset=10,
        end_offset=20,
        artifact_sha256="a" * 64,
    )
    payload: dict[str, object] = {
        "id": "evidence-1",
        "kind": EvidenceKind.ARTIFACT_SPAN,
        "source_uri": locator.source_uri,
        "digest": "b" * 64,
        "run_id": "run-1",
        "session_id": "session-1",
        "task_id": "task-1",
        "creator_type": EvidenceCreatorType.AGENT,
        "created_by": "primary",
        "trust_class": EvidenceTrustClass.UNTRUSTED_TOOL_OUTPUT,
        "scope": EvidenceScope(
            engagement_id="engagement-1",
            run_id="run-1",
            target_refs=("target://service/example",),
        ),
        "redaction_status": EvidenceRedactionStatus.RESTRICTED,
        "replay": EvidenceReplayMetadata(
            strategy=EvidenceReplayStrategy.ARTIFACT_SLICE,
            replayable=True,
            expected_digest="b" * 64,
            source_digest="a" * 64,
            parameters_digest="c" * 64,
        ),
        "locator": locator,
        "artifact_id": "artifact-1",
    }
    payload.update(updates)
    return Evidence.model_validate(payload)


def test_artifact_span_is_canonical_content_addressed_and_immutable() -> None:
    evidence = artifact_evidence()

    assert evidence.source_uri == "artifact://artifact-1#bytes=10-20"
    assert len(evidence.ledger_digest) == 64
    assert evidence.model_dump(mode="json")["ledger_digest"] == evidence.ledger_digest
    with pytest.raises(ValidationError, match="frozen"):
        evidence.digest = "d" * 64  # type: ignore[misc]

    payload = evidence.model_dump(mode="json")
    payload["ledger_digest"] = "d" * 64
    with pytest.raises(ValidationError, match="Ledger digest"):
        Evidence.model_validate(payload)


def test_code_location_requires_exact_normalized_range_and_replay_digest() -> None:
    locator = CodeLocationLocator(
        source=CodeSource.AUDIT_SNAPSHOT,
        path="src/auth handler.py",
        start_line=12,
        start_column=4,
        end_line=14,
        end_column=1,
        source_digest="a" * 64,
    )
    evidence = Evidence(
        id="code-evidence",
        kind=EvidenceKind.CODE_LOCATION,
        source_uri=locator.source_uri,
        digest="b" * 64,
        run_id="run-1",
        creator_type=EvidenceCreatorType.SCANNER,
        created_by="builtin-static",
        trust_class=EvidenceTrustClass.GENERATED,
        scope=EvidenceScope(
            engagement_id="engagement-1",
            run_id="run-1",
            audit_id="audit-1",
        ),
        redaction_status=EvidenceRedactionStatus.NOT_REQUIRED,
        replay=EvidenceReplayMetadata(
            strategy=EvidenceReplayStrategy.CODE_LOCATION,
            replayable=True,
            expected_digest="b" * 64,
            source_digest="a" * 64,
            parameters_digest="c" * 64,
        ),
        locator=locator,
    )

    assert evidence.source_uri == (
        "code://audit_snapshot/src/auth%20handler.py#L12:C4-L14:C1"
    )
    with pytest.raises(ValidationError, match="normalized relative POSIX"):
        CodeLocationLocator(
            source=CodeSource.WORKSPACE,
            path="../secret.py",
            start_line=1,
            end_line=2,
            end_column=0,
            source_digest="a" * 64,
        )


def test_replay_and_redaction_contracts_fail_closed() -> None:
    with pytest.raises(ValidationError, match="redaction policy"):
        artifact_evidence(
            redaction_status=EvidenceRedactionStatus.REDACTED,
            redaction_policy_ref=None,
        )
    with pytest.raises(ValidationError, match="replay digest"):
        artifact_evidence(
            replay=EvidenceReplayMetadata(
                strategy=EvidenceReplayStrategy.ARTIFACT_SLICE,
                replayable=True,
                expected_digest="d" * 64,
                source_digest="a" * 64,
                parameters_digest="c" * 64,
            )
        )
    with pytest.raises(ValidationError, match="Artifact Span locator"):
        artifact_evidence(
            locator={"locator_type": "source", "uri": "source://not-an-artifact"},
            source_uri="source://not-an-artifact",
        )
