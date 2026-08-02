"""Shared fail-closed SQL predicates for generic Artifact read surfaces."""

from __future__ import annotations

from typing import Any

from sqlalchemy import and_, exists, or_, select

from .orm import ArtifactRecord, TargetHttpRequestRecord


def artifact_is_not_target_http_sensitive(
    artifact: Any = ArtifactRecord,
) -> Any:
    """Exclude Target HTTP bodies by durable association or server marker.

    The marker closes the create-before-request-row and failed-create windows.
    Association is deliberately not Run-scoped because historical records did
    not enforce same-Run Artifact ownership.
    """

    request_body = and_(
        artifact.name.like("target-http-%-request.json"),
        artifact.description == "Immutable Target HTTP request",
    )
    response_body = and_(
        artifact.name.like("target-http-%-response.bin"),
        artifact.description == "Immutable Target HTTP response body",
    )
    referenced = exists(
        select(TargetHttpRequestRecord.id).where(
            or_(
                TargetHttpRequestRecord.request_artifact_id == artifact.id,
                TargetHttpRequestRecord.response_artifact_id == artifact.id,
            )
        )
    )
    return and_(~(request_body | response_body), ~referenced)


__all__ = ["artifact_is_not_target_http_sensitive"]
