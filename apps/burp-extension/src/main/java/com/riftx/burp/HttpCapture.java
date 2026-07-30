package com.riftx.burp;

import java.time.Instant;
import java.util.List;
import java.util.Map;

public record HttpCapture(
        String captureId,
        String method,
        String url,
        String httpVersion,
        List<HeaderValue> requestHeaders,
        byte[] requestBody,
        byte[] rawRequest,
        Integer responseStatus,
        String responseReason,
        List<HeaderValue> responseHeaders,
        byte[] responseBody,
        byte[] rawResponse,
        Instant observedAt,
        Map<String, String> metadata) {

    public record HeaderValue(String name, String value) {}
}
