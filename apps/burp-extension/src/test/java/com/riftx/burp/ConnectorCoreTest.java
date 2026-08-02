package com.riftx.burp;

import java.nio.charset.StandardCharsets;

public final class ConnectorCoreTest {
    public static void main(String[] args) {
        byte[] request = "POST /api HTTP/1.1\r\nHost: example.com\r\nX-Test: yes\r\n\r\nbody"
                .getBytes(StandardCharsets.ISO_8859_1);
        byte[] response = "HTTP/1.1 201 Created\r\nContent-Type: text/plain\r\n\r\nok"
                .getBytes(StandardCharsets.ISO_8859_1);
        HttpCapture capture = RawHttpParser.parse("https://example.com/api", request, response);
        assert capture.method().equals("POST");
        assert new String(capture.requestBody(), StandardCharsets.ISO_8859_1).equals("body");
        assert capture.responseStatus() == 201;
        String json = RiftXConnectorClient.captureJson(capture);
        assert json.contains("\"source\":\"burp\"");
        assert json.contains("Ym9keQ==");
        assert RiftXConnectorClient.json("a\"b\n").equals("a\\\"b\\n");
    }
}
