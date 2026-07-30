package com.riftx.burp;

import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.Base64;
import java.util.function.Consumer;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

public final class RiftXConnectorClient {
    private static final Pattern RUN_ID = Pattern.compile("\\\"run_id\\\"\\s*:\\s*\\\"([^\\\"]+)\\\"");
    private static final Pattern WEBUI_URL = Pattern.compile("\\\"url\\\"\\s*:\\s*\\\"([^\\\"]+)\\\"");
    private final HttpClient http = HttpClient.newBuilder()
            .connectTimeout(Duration.ofSeconds(10))
            .followRedirects(HttpClient.Redirect.NORMAL)
            .build();
    private final String baseUrl;

    public RiftXConnectorClient(String baseUrl) {
        this.baseUrl = baseUrl.replaceAll("/+$", "");
    }

    public Receipt submitExisting(String runId, HttpCapture capture) throws Exception {
        return submit("{\"run_id\":\"" + json(runId) + "\",\"capture\":" + captureJson(capture) + "}");
    }

    public Receipt submitNew(String objective, String engagementName, HttpCapture capture) throws Exception {
        String target = "\"new_run\":{\"objective\":\"" + json(objective)
                + "\",\"engagement\":{\"name\":\"" + json(engagementName) + "\"}}";
        return submit("{" + target + ",\"capture\":" + captureJson(capture) + "}");
    }

    public void cancel(String runId) throws Exception {
        send(HttpRequest.newBuilder(uri("/api/v1/connectors/runs/" + runId + "/cancel"))
                .POST(HttpRequest.BodyPublishers.noBody()).build());
    }

    public String webuiUrl(String runId) throws Exception {
        String body = send(HttpRequest.newBuilder(uri("/api/v1/connectors/runs/" + runId + "/webui")).GET().build());
        Matcher matcher = WEBUI_URL.matcher(body);
        if (!matcher.find()) throw new IllegalStateException("RiftX omitted WebUI URL");
        return matcher.group(1).replace("\\/", "/");
    }

    public void streamEvents(String runId, Consumer<String> consumer) throws Exception {
        HttpRequest request = HttpRequest.newBuilder(uri("/api/v1/connectors/runs/" + runId + "/events"))
                .header("Accept", "text/event-stream").GET().build();
        HttpResponse<java.io.InputStream> response = http.send(request, HttpResponse.BodyHandlers.ofInputStream());
        requireSuccess(response.statusCode(), "SSE");
        try (BufferedReader reader = new BufferedReader(new InputStreamReader(response.body(), StandardCharsets.UTF_8))) {
            String line;
            while (!Thread.currentThread().isInterrupted() && (line = reader.readLine()) != null) {
                if (line.startsWith("event:") || line.startsWith("data:")) consumer.accept(line);
            }
        }
    }

    static String captureJson(HttpCapture capture) {
        return "{"
                + "\"capture_id\":\"" + json(capture.captureId()) + "\","
                + "\"source\":\"burp\","
                + "\"method\":\"" + json(capture.method()) + "\","
                + "\"url\":\"" + json(capture.url()) + "\","
                + "\"http_version\":\"" + json(capture.httpVersion()) + "\","
                + "\"request_headers\":" + headersJson(capture.requestHeaders()) + ","
                + "\"request_body_base64\":\"" + Base64.getEncoder().encodeToString(capture.requestBody()) + "\","
                + "\"raw_request_base64\":\"" + Base64.getEncoder().encodeToString(capture.rawRequest()) + "\","
                + "\"response_status\":" + (capture.responseStatus() == null ? "null" : capture.responseStatus()) + ","
                + "\"response_reason\":" + nullableJson(capture.responseReason()) + ","
                + "\"response_headers\":" + headersJson(capture.responseHeaders()) + ","
                + "\"response_body_base64\":" + nullableBase64(
                        capture.responseStatus() == null ? null : capture.responseBody()) + ","
                + "\"raw_response_base64\":" + nullableBase64(
                        capture.responseStatus() == null ? null : capture.rawResponse()) + ","
                + "\"observed_at\":\"" + capture.observedAt() + "\","
                + "\"metadata\":{\"captured_by\":\"burp_montoya\"}"
                + "}";
    }

    private Receipt submit(String body) throws Exception {
        String response = send(HttpRequest.newBuilder(uri("/api/v1/connectors/submissions"))
                .header("Content-Type", "application/json")
                .POST(HttpRequest.BodyPublishers.ofString(body, StandardCharsets.UTF_8)).build());
        Matcher matcher = RUN_ID.matcher(response);
        if (!matcher.find()) throw new IllegalStateException("RiftX omitted Run ID");
        return new Receipt(matcher.group(1), response);
    }

    private String send(HttpRequest request) throws Exception {
        HttpResponse<String> response = http.send(request, HttpResponse.BodyHandlers.ofString());
        requireSuccess(response.statusCode(), response.body());
        return response.body();
    }

    private URI uri(String path) { return URI.create(baseUrl + path); }

    private static void requireSuccess(int status, String body) {
        if (status < 200 || status >= 300) throw new IllegalStateException("RiftX API " + status + ": " + body);
    }

    private static String headersJson(java.util.List<HttpCapture.HeaderValue> headers) {
        return headers.stream().map(item -> "{\"name\":\"" + json(item.name())
                + "\",\"value\":\"" + json(item.value()) + "\"}")
                .collect(java.util.stream.Collectors.joining(",", "[", "]"));
    }

    private static String nullableJson(String value) { return value == null ? "null" : "\"" + json(value) + "\""; }

    private static String nullableBase64(byte[] value) {
        return value == null ? "null" : "\"" + Base64.getEncoder().encodeToString(value) + "\"";
    }

    static String json(String value) {
        StringBuilder output = new StringBuilder();
        for (char character : value.toCharArray()) {
            switch (character) {
                case '\\' -> output.append("\\\\");
                case '"' -> output.append("\\\"");
                case '\n' -> output.append("\\n");
                case '\r' -> output.append("\\r");
                case '\t' -> output.append("\\t");
                default -> output.append(character < 0x20 ? String.format("\\u%04x", (int) character) : character);
            }
        }
        return output.toString();
    }

    public record Receipt(String runId, String rawResponse) {}
}
