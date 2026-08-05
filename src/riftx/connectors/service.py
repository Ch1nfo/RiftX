"""Unified ingestion service used by browser and Burp extensions."""

from __future__ import annotations

import asyncio
import json

from riftx.application.errors import ApplicationConflictError, EntityNotFoundError
from riftx.application.services import ArtifactApplicationService, RunApplicationService
from riftx.application.services.artifacts import RegisterArtifactContent
from riftx.application.services.runs import require_general_run_operation
from riftx.domain import ArtifactContentTrust, Run
from riftx.scope import ScopeGuard, ScopeTargetKind

from .models import (
    ConnectorHttpCapture,
    ConnectorReceipt,
    ConnectorSubmission,
)
from .repository import ConnectorSubmissionRepository


class ConnectorApplicationService:
    def __init__(
        self,
        *,
        runs: RunApplicationService,
        submissions: ConnectorSubmissionRepository,
        artifacts: ArtifactApplicationService,
    ) -> None:
        self._runs = runs
        self._submissions = submissions
        self._artifacts = artifacts
        self._locks: dict[str, asyncio.Lock] = {}

    async def ingest(
        self,
        run_id: str,
        capture: ConnectorHttpCapture,
        *,
        created_run: bool = False,
    ) -> ConnectorReceipt:
        key = f"{capture.source.value}:{capture.capture_id}"
        lock = self._locks.setdefault(key, asyncio.Lock())
        try:
            async with lock:
                # Admission precedes replay so a historical generic receipt
                # cannot authorize effects for an Audit-owned Run.
                run = await self._require_run(run_id)
                require_general_run_operation(run)
                existing = await self._submissions.get(capture.source, capture.capture_id)
                if existing is not None:
                    if existing.run_id != run_id or existing.fingerprint != capture.fingerprint:
                        raise ApplicationConflictError(
                            "connector_capture_id_conflict",
                            "Connector capture ID was reused with different content or Run",
                        )
                    return _receipt(existing, created_run=False)
                ScopeGuard(run.scope).require(capture.url, kind=ScopeTargetKind.URL)
                request_artifact = await self._artifacts.register_content(
                    run.id,
                    RegisterArtifactContent(
                        content=capture.request_bytes,
                        name=f"connector-{capture.source.value}-{capture.capture_id}-request.http",
                        mime_type="message/http",
                        description="Complete HTTP request imported by external connector",
                        content_trust=ArtifactContentTrust.UNTRUSTED_SOURCE,
                    ),
                )
                response_artifact = None
                if capture.response_bytes is not None:
                    response_artifact = await self._artifacts.register_content(
                        run.id,
                        RegisterArtifactContent(
                            content=capture.response_bytes,
                            name=(
                                f"connector-{capture.source.value}-{capture.capture_id}-"
                                "response.http"
                            ),
                            mime_type="message/http",
                            description="Complete HTTP response imported by external connector",
                            content_trust=ArtifactContentTrust.UNTRUSTED_SOURCE,
                        ),
                    )
                summary = capture.safe_summary()
                manifest_artifact = await self._artifacts.register_content(
                    run.id,
                    RegisterArtifactContent(
                        content=json.dumps(
                            {
                                **summary,
                                "request_artifact_id": request_artifact.id,
                                "response_artifact_id": (
                                    response_artifact.id if response_artifact else None
                                ),
                                "content_trust": "UNTRUSTED_EXTERNAL_CONTENT",
                            },
                            ensure_ascii=False,
                            indent=2,
                        ).encode(),
                        name=(
                            f"connector-{capture.source.value}-{capture.capture_id}-manifest.json"
                        ),
                        mime_type="application/json",
                        description="Sanitized connector capture manifest",
                        content_trust=ArtifactContentTrust.GENERATED,
                    ),
                )
                submission = await self._submissions.create(
                    ConnectorSubmission(
                        run_id=run.id,
                        capture_id=capture.capture_id,
                        source=capture.source,
                        fingerprint=capture.fingerprint,
                        request_artifact_id=request_artifact.id,
                        response_artifact_id=(response_artifact.id if response_artifact else None),
                        manifest_artifact_id=manifest_artifact.id,
                        summary=summary,
                    )
                )
                return _receipt(submission, created_run=created_run)
        finally:
            if not lock.locked():
                self._locks.pop(key, None)

    async def _require_run(self, run_id: str) -> Run:
        try:
            return await self._runs.get_run(run_id)
        except EntityNotFoundError:
            raise


def _receipt(item: ConnectorSubmission, *, created_run: bool) -> ConnectorReceipt:
    return ConnectorReceipt(
        submission=item,
        created_run=created_run,
        webui_path=f"/runs/{item.run_id}",
        events_path=f"/api/v1/connectors/runs/{item.run_id}/events",
        cancel_path=f"/api/v1/connectors/runs/{item.run_id}/cancel",
    )
