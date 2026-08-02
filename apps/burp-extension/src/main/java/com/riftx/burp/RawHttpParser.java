package com.riftx.burp;

import java.nio.charset.StandardCharsets;
import java.time.Instant;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.UUID;

public final class RawHttpParser {
    private RawHttpParser() {}

    public static HttpCapture parse(String url, byte[] rawRequest, byte[] rawResponse) {
        Message request = split(rawRequest);
        String[] requestLine = request.startLine().split(" ", 3);
        if (requestLine.length < 3) throw new IllegalArgumentException("Invalid HTTP request line");
        Message response = rawResponse == null ? null : split(rawResponse);
        Integer status = null;
        String reason = null;
        String responseVersion = requestLine[2];
        if (response != null) {
            String[] responseLine = response.startLine().split(" ", 3);
            if (responseLine.length < 2) throw new IllegalArgumentException("Invalid HTTP response line");
            responseVersion = responseLine[0];
            status = Integer.parseInt(responseLine[1]);
            reason = responseLine.length == 3 ? responseLine[2] : null;
        }
        Map<String, String> metadata = new LinkedHashMap<>();
        metadata.put("captured_by", "burp_montoya");
        return new HttpCapture(
                UUID.randomUUID().toString(),
                requestLine[0],
                url,
                responseVersion,
                request.headers(),
                request.body(),
                rawRequest.clone(),
                status,
                reason,
                response == null ? List.of() : response.headers(),
                response == null ? new byte[0] : response.body(),
                rawResponse == null ? new byte[0] : rawResponse.clone(),
                Instant.now(),
                metadata);
    }

    static Message split(byte[] raw) {
        int boundary = findBoundary(raw);
        byte[] head = boundary < 0 ? raw : Arrays.copyOf(raw, boundary);
        byte[] body = boundary < 0 ? new byte[0] : Arrays.copyOfRange(raw, boundary + 4, raw.length);
        String[] lines = new String(head, StandardCharsets.ISO_8859_1).split("\\r\\n");
        if (lines.length == 0 || lines[0].isBlank()) throw new IllegalArgumentException("Empty HTTP message");
        List<HttpCapture.HeaderValue> headers = new ArrayList<>();
        for (int index = 1; index < lines.length; index++) {
            int separator = lines[index].indexOf(':');
            if (separator <= 0) continue;
            headers.add(new HttpCapture.HeaderValue(
                    lines[index].substring(0, separator).trim(),
                    lines[index].substring(separator + 1).trim()));
        }
        return new Message(lines[0], List.copyOf(headers), body);
    }

    private static int findBoundary(byte[] raw) {
        for (int i = 0; i <= raw.length - 4; i++) {
            if (raw[i] == '\r' && raw[i + 1] == '\n' && raw[i + 2] == '\r' && raw[i + 3] == '\n') {
                return i;
            }
        }
        return -1;
    }

    record Message(String startLine, List<HttpCapture.HeaderValue> headers, byte[] body) {}
}
