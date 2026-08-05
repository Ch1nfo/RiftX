"""Publish bounded code-source reads into immutable Run Artifacts."""

from __future__ import annotations

import hashlib

from riftx.domain import ArtifactContentTrust

from .artifacts import ArtifactApplicationService, RegisterArtifactContent


class ArtifactCodePublisher:
    def __init__(self, artifacts: ArtifactApplicationService) -> None:
        self._artifacts = artifacts

    async def publish(
        self,
        run_id: str,
        *,
        audit_id: str | None,
        path: str,
        content: bytes,
        source_digest: str | None,
    ) -> str:
        path_digest = hashlib.sha256(path.encode("utf-8")).hexdigest()[:24]
        description = f"Immutable full code source for {path}"
        if source_digest is not None:
            description += f" from Snapshot {source_digest}"
        command = RegisterArtifactContent(
            content=content,
            name=f"code-source-{path_digest}.bin",
            mime_type="application/octet-stream",
            description=description,
            content_trust=ArtifactContentTrust.UNTRUSTED_SOURCE,
        )
        artifact = (
            await self._artifacts.register_audit_content(audit_id, run_id, command)
            if audit_id is not None
            else await self._artifacts.register_content(run_id, command)
        )
        return artifact.id


__all__ = ["ArtifactCodePublisher"]
