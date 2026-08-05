from __future__ import annotations

from types import SimpleNamespace

from riftx.application.services.artifacts import RegisterArtifactContent
from riftx.application.services.code_artifacts import ArtifactCodePublisher
from riftx.domain import ArtifactContentTrust


class _Artifacts:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None, RegisterArtifactContent]] = []

    async def register_content(
        self,
        run_id: str,
        command: RegisterArtifactContent,
    ) -> object:
        self.calls.append((run_id, None, command))
        return SimpleNamespace(id="artifact-general")

    async def register_audit_content(
        self,
        audit_id: str,
        run_id: str,
        command: RegisterArtifactContent,
    ) -> object:
        self.calls.append((run_id, audit_id, command))
        return SimpleNamespace(id="artifact-audit")


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
