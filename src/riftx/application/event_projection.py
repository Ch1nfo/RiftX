"""Fail-closed projections for Event data exposed outside the runtime."""

from __future__ import annotations

import re
from collections.abc import Collection

from riftx.domain import RunEvent
from riftx.target_http.redaction import safe_url_metadata

_TARGET_HTTP_ARTIFACT_NAME = re.compile(r"target-http-.+-(?:request\.json|response\.bin)")


def target_http_artifact_candidates(events: Collection[RunEvent]) -> frozenset[str]:
    """Return Event Artifact IDs that require authority-backed classification."""

    return frozenset(
        artifact_id
        for event in events
        if event.event_type == "artifact.registered"
        and isinstance((artifact_id := event.payload.get("artifact_id")), str)
        and artifact_id
    )


def redact_sensitive_event(
    event: RunEvent,
    *,
    sensitive_artifact_ids: Collection[str] = (),
) -> RunEvent:
    """Project legacy Target HTTP Events without guessing whether values are secrets."""

    payload = event.payload
    if event.event_type == "target_http.request_started":
        raw_url = payload.get("url")
        url_summary = (
            safe_url_metadata(raw_url)
            if isinstance(raw_url, str)
            else _validated_url_summary(payload.get("url_summary"))
        )
        return event.model_copy(
            update={
                "payload": {
                    "url_redacted": True,
                    "url_summary": url_summary,
                }
            }
        )

    if event.event_type == "target_http.request_failed":
        return event.model_copy(
            update={"payload": {"failure_recorded": True, "category": "request_failed"}}
        )

    if event.event_type == "target_http.response_received":
        redacted_response: dict[str, object] = {"response_recorded": True}
        status_code = payload.get("status_code")
        if type(status_code) is int and 100 <= status_code <= 599:
            redacted_response["status_code"] = status_code
        return event.model_copy(update={"payload": redacted_response})

    if event.event_type == "target_http.request_cancelled":
        return event.model_copy(
            update={
                "payload": {
                    "cancellation_confirmed": True,
                    "category": "runner_confirmed",
                }
            }
        )

    if event.event_type.startswith("target_http."):
        return event.model_copy(
            update={"payload": {"content_restricted": True, "category": "unknown_event"}}
        )

    if event.event_type == "artifact.registered":
        name = payload.get("name")
        artifact_id = payload.get("artifact_id")
        marker_owned = isinstance(name, str) and bool(_TARGET_HTTP_ARTIFACT_NAME.fullmatch(name))
        association_owned = isinstance(artifact_id, str) and artifact_id in sensitive_artifact_ids
        if marker_owned or association_owned:
            return event.model_copy(
                update={
                    "payload": {
                        "artifact_class": "target_http_sensitive",
                        "content_restricted": True,
                    }
                }
            )
    return event


def _validated_url_summary(value: object) -> dict[str, object] | None:
    if not isinstance(value, dict):
        return None
    origin = value.get("origin")
    path_shape = value.get("path_shape")
    segment_count = value.get("path_segment_count")
    if (
        not isinstance(origin, str)
        or path_shape not in {"/", "/…"}
        or type(segment_count) is not int
        or not 0 <= segment_count <= 4096
    ):
        return None
    safe_origin = safe_url_metadata(origin)
    if (
        safe_origin is None
        or safe_origin["origin"] != origin
        or safe_origin["path_shape"] != "/"
        or safe_origin["path_segment_count"] != 0
    ):
        return None
    return {
        "scheme": safe_origin["scheme"],
        "origin": safe_origin["origin"],
        "path_shape": path_shape,
        "path_segment_count": segment_count,
    }


__all__ = ["redact_sensitive_event", "target_http_artifact_candidates"]
