from __future__ import annotations

import hashlib
from types import SimpleNamespace

import pytest

from riftx.application.errors import ApplicationConflictError
from riftx.application.services import (
    EvidenceApplicationService,
    RegisterArtifactSpanEvidence,
    RegisterCodeLocationEvidence,
)
from riftx.code import CodeLocationContent
from riftx.domain import Objective, Run
from riftx.evidence import (
    CodeSource,
    Evidence,
    EvidenceCreatorType,
    EvidenceRedactionStatus,
    EvidenceTrustClass,
)


class _Runs:
    def __init__(self, run: Run) -> None:
        self.run = run

    async def get(self, run_id: str) -> Run | None:
        return self.run if self.run.id == run_id else None


class _Sessions:
    async def get(self, session_id: str) -> SimpleNamespace | None:
        return SimpleNamespace(id=session_id, run_id="run-1")


class _Tasks:
    async def get(self, run_id: str) -> SimpleNamespace | None:
        if run_id != "run-1":
            return None
        return SimpleNamespace(tasks=[SimpleNamespace(id="task-1")])


class _Artifacts:
    def __init__(self) -> None:
        self.data = b"selected"
        self.artifact = SimpleNamespace(
            id="artifact-1",
            run_id="run-1",
            audit_id=None,
            sha256="a" * 64,
        )

    async def read_content_slice(self, artifact_id: str, **kwargs: object) -> SimpleNamespace:
        assert artifact_id == self.artifact.id
        offset = int(kwargs["offset"])
        return SimpleNamespace(
            artifact=self.artifact,
            data=self.data,
            offset=offset,
            next_offset=offset + len(self.data),
        )

    async def read_audit_content_slice(
        self, artifact_id: str, **kwargs: object
    ) -> SimpleNamespace:
        return await self.read_content_slice(artifact_id, **kwargs)


class _Code:
    def __init__(self) -> None:
        self.content = CodeLocationContent(
            source="workspace",
            source_digest=None,
            audit_id=None,
            path="src/auth.py",
            content_digest="b" * 64,
            data=b"dangerous()",
        )

    async def read_location(self, run_id: str, **_: object) -> CodeLocationContent:
        assert run_id == "run-1"
        return self.content


class _Ledger:
    def __init__(self) -> None:
        self.items: list[Evidence] = []

    async def create(self, evidence: Evidence) -> Evidence:
        self.items.append(evidence)
        return evidence


def _run() -> Run:
    return Run(
        id="run-1",
        engagement_id="engagement-1",
        kind="general",
        node_id="node-1",
        objective=Objective(description="Evidence"),
        workspace_path="/workspace",
    )


def _service() -> tuple[EvidenceApplicationService, _Artifacts, _Code, _Ledger]:
    artifacts = _Artifacts()
    code = _Code()
    ledger = _Ledger()
    return (
        EvidenceApplicationService(
            runs=_Runs(_run()),  # type: ignore[arg-type]
            sessions=_Sessions(),  # type: ignore[arg-type]
            tasks=_Tasks(),  # type: ignore[arg-type]
            artifacts=artifacts,  # type: ignore[arg-type]
            code=code,
            ledger=ledger,  # type: ignore[arg-type]
        ),
        artifacts,
        code,
        ledger,
    )


async def test_register_artifact_span_hashes_verified_bytes_and_server_scope() -> None:
    service, artifacts, _, ledger = _service()

    evidence = await service.register_artifact_span(
        RegisterArtifactSpanEvidence(
            run_id="run-1",
            artifact_id="artifact-1",
            start_offset=10,
            end_offset=10 + len(artifacts.data),
            creator_type=EvidenceCreatorType.TOOL,
            created_by="read_artifact",
            trust_class=EvidenceTrustClass.UNTRUSTED_TOOL_OUTPUT,
            redaction_status=EvidenceRedactionStatus.METADATA_ONLY,
        )
    )

    assert ledger.items == [evidence]
    assert evidence.digest == hashlib.sha256(artifacts.data).hexdigest()
    assert evidence.scope.engagement_id == "engagement-1"
    assert evidence.locator.artifact_sha256 == "a" * 64  # type: ignore[union-attr]
    assert evidence.replay.source_digest == "a" * 64


async def test_register_code_location_binds_file_digest_session_and_task() -> None:
    service, _, code, _ = _service()

    evidence = await service.register_code_location(
        RegisterCodeLocationEvidence(
            run_id="run-1",
            session_id="session-1",
            task_id="task-1",
            source=CodeSource.WORKSPACE,
            path="src/auth.py",
            start_line=2,
            start_column=4,
            end_line=2,
            end_column=15,
            expected_content_digest=code.content.content_digest,
            creator_type=EvidenceCreatorType.SCANNER,
            created_by="builtin-static",
            trust_class=EvidenceTrustClass.GENERATED,
        )
    )

    assert evidence.digest == hashlib.sha256(code.content.data).hexdigest()
    assert evidence.session_id == "session-1"
    assert evidence.task_id == "task-1"
    assert evidence.locator.source_digest == "b" * 64  # type: ignore[union-attr]


async def test_register_code_location_rejects_stale_content_digest() -> None:
    service, _, _, _ = _service()

    with pytest.raises(ApplicationConflictError) as captured:
        await service.register_code_location(
            RegisterCodeLocationEvidence(
                run_id="run-1",
                source=CodeSource.WORKSPACE,
                path="src/auth.py",
                start_line=1,
                end_line=1,
                end_column=1,
                expected_content_digest="f" * 64,
                creator_type=EvidenceCreatorType.PARSER,
                created_by="parser",
                trust_class=EvidenceTrustClass.GENERATED,
            )
        )
    assert captured.value.code == "evidence_code_content_changed"
