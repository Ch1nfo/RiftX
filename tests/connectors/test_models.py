import base64

import pytest
from pydantic import ValidationError

from riftx.connectors import ConnectorHttpCapture, ConnectorSource, HttpHeader


def test_capture_preserves_complete_request_and_response_bytes() -> None:
    capture = ConnectorHttpCapture(
        capture_id="capture-1",
        source=ConnectorSource.BROWSER,
        method="post",
        url="https://example.com/api",
        request_headers=[HttpHeader(name="Content-Type", value="application/octet-stream")],
        request_body_base64=base64.b64encode(b"\x00request").decode(),
        raw_request_base64=base64.b64encode(b"RAW REQUEST").decode(),
        response_status=201,
        response_reason="Created",
        response_headers=[HttpHeader(name="X-Trace", value="abc")],
        response_body_base64=base64.b64encode(b"\xffresponse").decode(),
        raw_response_base64=base64.b64encode(b"RAW RESPONSE").decode(),
    )
    assert capture.method == "POST"
    assert capture.request_body == b"\x00request"
    assert capture.request_bytes == b"RAW REQUEST"
    assert capture.response_bytes == b"RAW RESPONSE"
    assert capture.safe_summary()["request_header_names"] == ["Content-Type"]
    assert "application/octet-stream" not in capture.safe_summary().values()


def test_capture_rejects_header_injection_and_invalid_base64() -> None:
    with pytest.raises(ValidationError, match="line breaks"):
        HttpHeader(name="X-Test\r\nInjected", value="yes")
    with pytest.raises(ValidationError, match="valid base64"):
        ConnectorHttpCapture(
            capture_id="bad",
            source=ConnectorSource.BURP,
            method="GET",
            url="https://example.com/",
            request_body_base64="not base64",
        )
