from __future__ import annotations

import hashlib
from types import SimpleNamespace

import pytest

from riftx.application.errors import ApplicationConflictError
from riftx.application.services.artifacts import RegisterArtifactContent
from riftx.application.services.code_artifacts import ArtifactCodePublisher
from riftx.code import CodePatchReceipt
from riftx.domain import ArtifactAccessClass, ArtifactContentTrust


class _Artifacts:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None, RegisterArtifactContent]] = []
        self.content: bytes | None = None
        self.access_class = ArtifactAccessClass.PUBLIC_EXPORT
        self.mime_type = "application/vnd.riftx.code-patch-receipt+json"
        self.eof = True

    async def register_content(
        self,
        run_id: str,
        command: RegisterArtifactContent,
    ) -> object:
        self.calls.append((run_id, None, command))
        self.content = command.content
        return SimpleNamespace(id="artifact-general")

    async def register_audit_content(
        self,
        audit_id: str,
        run_id: str,
        command: RegisterArtifactContent,
    ) -> object:
        self.calls.append((run_id, audit_id, command))
        return SimpleNamespace(id="artifact-audit")

    async def read_content_slice(self, *_: object, **__: object) -> object:
        assert self.content is not None
        return SimpleNamespace(
            artifact=SimpleNamespace(
                access_class=self.access_class,
                mime_type=self.mime_type,
            ),
            data=self.content,
            eof=self.eof,
        )


async def test_code_artifact_publisher_routes_general_and_audit_owners() -> None:
    artifacts = _Artifacts()
    publisher = ArtifactCodePublisher(artifacts)  # type: ignore[arg-type]

    general_id = await publisher.publish(
        "run-general",
        audit_id=None,
        path="src/app.py",
        content=b"general",
        source_digest=None,
    )
    audit_id = await publisher.publish(
        "run-audit",
        audit_id="audit-1",
        path="src/app.py",
        content=b"audit",
        source_digest="1" * 64,
    )

    assert (general_id, audit_id) == ("artifact-general", "artifact-audit")
    assert [(run_id, owner) for run_id, owner, _ in artifacts.calls] == [
        ("run-general", None),
        ("run-audit", "audit-1"),
    ]
    for _, _, command in artifacts.calls:
        assert command.name.startswith("code-source-")
        assert command.name.endswith(".bin")
        assert command.mime_type == "application/octet-stream"
        assert command.content_trust is ArtifactContentTrust.UNTRUSTED_SOURCE


async def test_code_patch_receipt_round_trips_through_immutable_artifact() -> None:
    artifacts = _Artifacts()
    publisher = ArtifactCodePublisher(artifacts)  # type: ignore[arg-type]
    patch = (
        "*** Begin Patch\n"
        "*** Update File: src/app.py\n"
        "@@\n"
        "-old\n"
        "+new\n"
        "*** End Patch"
    )
    receipt = CodePatchReceipt(
        run_id="run-1",
        operation="update",
        path="src/app.py",
        original_sha256=hashlib.sha256(b"old").hexdigest(),
        result_sha256=hashlib.sha256(b"new").hexdigest(),
        original_mode=0o640,
        original_content_base64="b2xk",
        patch=patch,
        patch_sha256=hashlib.sha256(patch.encode()).hexdigest(),
    )

    artifact_id = await publisher.publish_patch_receipt("run-1", receipt)
    loaded = await publisher.load_patch_receipt("run-1", artifact_id)

    assert loaded == receipt
    command = artifacts.calls[0][2]
    assert command.mime_type == "application/vnd.riftx.code-patch-receipt+json"
    assert command.content_trust is ArtifactContentTrust.UNTRUSTED_SOURCE


async def test_code_patch_receipt_rejects_semantic_forgery_and_invalid_metadata() -> None:
    artifacts = _Artifacts()
    publisher = ArtifactCodePublisher(artifacts)  # type: ignore[arg-type]
    patch = "*** Begin Patch\n*** Add File: added.txt\n+hello\n*** End Patch"
    receipt = CodePatchReceipt(
        run_id="run-1",
        operation="add",
        path="added.txt",
        result_sha256="f" * 64,
        patch=patch,
        patch_sha256=hashlib.sha256(patch.encode()).hexdigest(),
    )
    artifact_id = await publisher.publish_patch_receipt("run-1", receipt)

    with pytest.raises(ApplicationConflictError) as semantic_error:
        await publisher.load_patch_receipt("run-1", artifact_id)
    assert semantic_error.value.code == "code_patch_receipt_invalid"

    artifacts.mime_type = "application/json"
    with pytest.raises(ApplicationConflictError) as metadata_error:
        await publisher.load_patch_receipt("run-1", artifact_id)
    assert metadata_error.value.code == "code_patch_receipt_invalid"
