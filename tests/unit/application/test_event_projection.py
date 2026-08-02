from __future__ import annotations

import pytest

from riftx.application.event_projection import redact_sensitive_event
from riftx.domain import RunEvent

CANARY = "RIFTX_TEST_SECRET_DO_NOT_LEAK_TARGET_HTTP_EVENT"


def _event(event_type: str, payload: dict[str, object]) -> RunEvent:
    return RunEvent(
        run_id="run-event-projection",
        sequence=1,
        event_type=event_type,
        payload=payload,
    )


@pytest.mark.parametrize(
    ("event_type", "payload", "expected"),
    [
        (
            "target_http.request_started",
            {
                "execution_key": f"execution:v1:{CANARY}",
                "method": f"GET-{CANARY}",
                "url": (
                    f"https://{CANARY}:password@target.example/private/{CANARY}"
                    f"?signature={CANARY}#{CANARY}"
                ),
            },
            {
                "url_redacted": True,
                "url_summary": {
                    "scheme": "https",
                    "origin": "https://target.example",
                    "path_shape": "/…",
                    "path_segment_count": 2,
                },
            },
        ),
        (
            "target_http.request_failed",
            {"execution_key": CANARY, "request_id": CANARY, "reason": CANARY},
            {"failure_recorded": True, "category": "request_failed"},
        ),
        (
            "target_http.response_received",
            {"execution_key": CANARY, "request_id": CANARY, "status_code": 204},
            {"response_recorded": True, "status_code": 204},
        ),
        (
            "target_http.request_cancelled",
            {
                "execution_key": CANARY,
                "intent_status": CANARY,
                "runner_reason": CANARY,
            },
            {"cancellation_confirmed": True, "category": "runner_confirmed"},
        ),
        (
            "target_http.future_event",
            {"arbitrary": CANARY},
            {"content_restricted": True, "category": "unknown_event"},
        ),
    ],
)
def test_target_http_event_projection_is_exact_and_value_free(
    event_type: str,
    payload: dict[str, object],
    expected: dict[str, object],
) -> None:
    projected = redact_sensitive_event(_event(event_type, payload))

    assert projected.payload == expected
    assert CANARY not in repr(projected.payload)


def test_target_http_artifact_association_hides_generic_legacy_metadata() -> None:
    event = _event(
        "artifact.registered",
        {
            "artifact_id": "artifact-associated",
            "name": f"generic-{CANARY}.bin",
            "mime_type": f"application/{CANARY}",
            "sha256": CANARY,
            "size": 42,
        },
    )

    projected = redact_sensitive_event(
        event,
        sensitive_artifact_ids={"artifact-associated"},
    )

    assert projected.payload == {
        "artifact_class": "target_http_sensitive",
        "content_restricted": True,
    }
    assert CANARY not in repr(projected.payload)


def test_ordinary_artifact_event_is_unchanged() -> None:
    event = _event(
        "artifact.registered",
        {
            "artifact_id": "artifact-ordinary",
            "name": "ordinary.txt",
            "mime_type": "text/plain",
            "sha256": "a" * 64,
            "size": 42,
        },
    )

    assert redact_sensitive_event(event) is event
