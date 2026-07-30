import { afterEach, describe, expect, it, vi } from "vitest";

import { harEntryToCapture, RiftXConnectorClient, shouldCapture } from "./connector";

afterEach(() => vi.unstubAllGlobals());

describe("browser connector", () => {
  it("converts selected XHR HAR entries without losing bodies", async () => {
    vi.stubGlobal("crypto", { randomUUID: () => "capture-1" });
    const entry = {
      _resourceType: "xhr",
      request: {
        method: "POST",
        url: "https://example.com/api",
        headers: [{ name: "Content-Type", value: "application/json" }],
        postData: { text: '{"hello":"world"}' },
      },
      response: { status: 200, headers: [], content: { mimeType: "application/json" } },
      getContent(callback: (content: string, encoding: string) => void) {
        callback('{"ok":true}', "");
      },
    };
    expect(shouldCapture(entry)).toBe(true);
    const capture = await harEntryToCapture(entry);
    expect(capture.capture_id).toBe("capture-1");
    expect(atob(capture.request_body_base64 || "")).toContain("hello");
    expect(atob(capture.response_body_base64 || "")).toContain("ok");
  });

  it("uses one unified submission endpoint for existing Runs", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({ receipt: { submission: { run_id: "run-1" }, created_run: false } }),
        { status: 201, headers: { "Content-Type": "application/json" } },
      ),
    );
    vi.stubGlobal("fetch", fetchMock);
    const client = new RiftXConnectorClient("http://127.0.0.1:8787/");
    await client.submit(
      {
        capture_id: "capture-1",
        source: "browser",
        method: "GET",
        url: "https://example.com/",
        http_version: "HTTP/1.1",
        request_headers: [],
        request_body_base64: null,
        response_status: 200,
        response_reason: null,
        response_headers: [],
        response_body_base64: null,
        observed_at: new Date().toISOString(),
        metadata: {},
      },
      { runId: "run-1" },
    );
    expect(fetchMock).toHaveBeenCalledWith(
      "http://127.0.0.1:8787/api/v1/connectors/submissions",
      expect.objectContaining({ body: expect.stringContaining('"run_id":"run-1"') }),
    );
  });
});
