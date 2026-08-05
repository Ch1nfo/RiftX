"""Shared fail-closed SQL predicates for generic Artifact read surfaces."""

from __future__ import annotations

from typing import Any

from sqlalchemy import and_, exists, or_, select

from riftx.domain import ArtifactAccessClass, RunKind

from .orm import (
    ArtifactRecord,
    AuditScanRecord,
    ExecutionRecord,
    RunRecord,
    TargetHttpRequestRecord,
)


def artifact_is_publicly_visible(
    artifact: Any = ArtifactRecord,
) -> Any:
    """Return the complete SQL visibility gate for generic Artifact reads."""

    return and_(
        artifact.access_class == ArtifactAccessClass.PUBLIC_EXPORT.value,
        artifact_has_valid_owner(artifact),
        artifact_has_consistent_execution_owner(artifact),
        artifact_is_not_target_http_sensitive(artifact),
    )


def artifact_has_valid_owner(
    artifact: Any = ArtifactRecord,
) -> Any:
    """Require General ownership or a complete Code Audit ownership chain."""

    return or_(
        and_(
            artifact.audit_id.is_(None),
            exists(
                select(RunRecord.id).where(
                    RunRecord.id == artifact.run_id,
                    RunRecord.kind == RunKind.GENERAL.value,
                )
            ),
        ),
        and_(
            artifact.audit_id.is_not(None),
            artifact_has_consistent_audit_owner(artifact),
        ),
    )


def artifact_has_consistent_audit_owner(
    artifact: Any = ArtifactRecord,
) -> Any:
    """Require an Audit-owned Artifact to agree with its Audit Run binding."""

    return exists(
        select(AuditScanRecord.id)
        .join(RunRecord, RunRecord.id == AuditScanRecord.run_id)
        .where(
            AuditScanRecord.id == artifact.audit_id,
            AuditScanRecord.run_id == artifact.run_id,
            RunRecord.kind == RunKind.CODE_AUDIT.value,
        )
    )


def artifact_has_consistent_execution_owner(
    artifact: Any = ArtifactRecord,
) -> Any:
    """Require a referenced Execution to belong to the Artifact Run."""

    return or_(
        artifact.execution_id.is_(None),
        exists(
            select(ExecutionRecord.id).where(
                ExecutionRecord.id == artifact.execution_id,
                ExecutionRecord.run_id == artifact.run_id,
            )
        ),
    )


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


__all__ = [
    "artifact_has_consistent_audit_owner",
    "artifact_has_consistent_execution_owner",
    "artifact_has_valid_owner",
    "artifact_is_not_target_http_sensitive",
    "artifact_is_publicly_visible",
]
