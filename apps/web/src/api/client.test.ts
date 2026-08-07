import { afterEach, describe, expect, it, vi } from "vitest";

import {
  MAX_AUTHENTICATED_DOWNLOAD_BYTES,
  api,
  clearLocalOperatorToken,
  localOperatorHeaders,
  setLocalOperatorToken,
} from "./client";

const originalFetch = globalThis.fetch;
const originalCreateObjectURL = URL.createObjectURL;
const originalRevokeObjectURL = URL.revokeObjectURL;
const TEST_DOWNLOAD_LIMIT_BYTES = 8;

afterEach(() => {
  clearLocalOperatorToken();
  globalThis.fetch = originalFetch;
  vi.restoreAllMocks();
  vi.unstubAllEnvs();
  restoreURLMethod("createObjectURL", originalCreateObjectURL);
  restoreURLMethod("revokeObjectURL", originalRevokeObjectURL);
});

describe("RiftX API client", () => {
  it("uses the typed cursor Action list and parent-scoped detail endpoints", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            items: [],
            limit: 25,
            sort: "created_at_desc",
            has_more: false,
            next_cursor: null,
          }),
          { status: 200, headers: { "content-type": "application/json" } },
        ),
      )
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            action_id: "action/1",
            run_id: "run/1",
            graph_ref: {
              view: "task",
              node_id: "action:run/1:action/1",
              projection_quality: "exact",
            },
          }),
          {
            status: 200,
            headers: { "content-type": "application/json" },
          },
        ),
      );
    globalThis.fetch = fetchMock;

    await api.listRunActions("run/1", "cursor+/=", 25);
    await api.getRunAction("run/1", "action/1");

    expect(String(fetchMock.mock.calls[0]?.[0])).toBe(
      "/api/v1/runs/run%2F1/actions?limit=25&sort=created_at_desc&cursor=cursor%2B%2F%3D",
    );
    expect(String(fetchMock.mock.calls[1]?.[0])).toBe(
      "/api/v1/runs/run%2F1/actions/action%2F1",
    );
  });

  it("encodes every bounded Graph view filter and forwards cancellation", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          scope: { engagement_id: "engagement-1", run_id: "run/1" },
          view: "evidence",
          nodes: [],
          edges: [],
          type_metadata: [],
          partial_reasons: [],
          truncated: false,
          has_more: false,
          next_cursor: null,
        }),
        { status: 200, headers: { "content-type": "application/json" } },
      ),
    );
    globalThis.fetch = fetchMock;
    const controller = new AbortController();

    await api.listRunGraph(
      "run/1",
      {
        view: "evidence",
        nodeType: "finding type",
        edgeType: "supports/edge",
        focus: "finding:1/2",
        search: "host=a&port=443",
        limit: 75,
        cursor: "cursor+/=",
      },
      controller.signal,
    );

    expect(String(fetchMock.mock.calls[0]?.[0])).toBe(
      "/api/v1/runs/run%2F1/graph?view=evidence&node_type=finding+type&edge_type=supports%2Fedge&focus=finding%3A1%2F2&search=host%3Da%26port%3D443&limit=75&cursor=cursor%2B%2F%3D",
    );
    expect(fetchMock).toHaveBeenCalledWith(
      expect.any(String),
      expect.objectContaining({ signal: controller.signal }),
    );
  });

  it("reads only Run-scoped Target HTTP metadata endpoints", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ items: [], has_more: false, next_cursor: null }), {
          status: 200,
          headers: { "content-type": "application/json" },
        }),
      )
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ exchange_id: "exchange/1" }), {
          status: 200,
          headers: { "content-type": "application/json" },
        }),
      );
    globalThis.fetch = fetchMock;
    const controller = new AbortController();

    await api.listRunTargetHttpExchanges(
      "run/1",
      {
        method: "GET",
        statusClass: "success",
        limit: 25,
        cursor: "cursor+/=",
      },
      controller.signal,
    );
    await api.getRunTargetHttpExchange("run/1", "exchange/1", controller.signal);

    expect(String(fetchMock.mock.calls[0]?.[0])).toBe(
      "/api/v1/runs/run%2F1/target-http/exchanges?method=GET&status_class=success&limit=25&cursor=cursor%2B%2F%3D",
    );
    expect(String(fetchMock.mock.calls[1]?.[0])).toBe(
      "/api/v1/runs/run%2F1/target-http/exchanges/exchange%2F1",
    );
    for (const [, init] of fetchMock.mock.calls) {
      expect(init).toEqual(expect.objectContaining({ signal: controller.signal }));
      expect(init).not.toHaveProperty("method", "POST");
      expect(init).not.toHaveProperty("body");
    }
    expect(fetchMock.mock.calls.map(([url]) => String(url)).join("\n")).not.toMatch(
      /(?:body|reveal|replay|artifact)/i,
    );
  });

  it("keeps the local token in memory and sends it on REST requests", async () => {
    const localStorageWrite = vi.spyOn(Storage.prototype, "setItem");
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          profile: "local_single_operator",
          principal_id: "local-principal:v1:test",
          capabilities: ["local.read"],
          features: {},
          tenant_safe: false,
        }),
        { status: 200, headers: { "content-type": "application/json" } },
      ),
    );
    globalThis.fetch = fetchMock;

    setLocalOperatorToken("  memory-only-secret  ");
    await api.getSecurityProfile();

    expect(localOperatorHeaders()).toEqual({
      Authorization: "Bearer memory-only-secret",
    });
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/security/profile",
      expect.objectContaining({
        headers: expect.objectContaining({
          Authorization: "Bearer memory-only-secret",
        }),
      }),
    );
    expect(localStorageWrite).not.toHaveBeenCalled();
  });

  it("keeps WebSocket credentials out of the URL and offers only protocol tokens", () => {
    setLocalOperatorToken("websocket-secret");

    const url = api.terminalWebSocketUrl("terminal-1", 42);
    const protocols = api.terminalWebSocketProtocols();

    expect(url).toMatch(/\/api\/v1\/terminals\/terminal-1\/ws\?cursor=42$/);
    expect(url).not.toContain("websocket-secret");
    expect(url).not.toContain("bearer");
    expect(protocols).toHaveLength(2);
    expect(protocols[0]).toBe("riftx.local-operator.v1");
    expect(protocols[1]).toMatch(/^riftx\.local-operator\.bearer\.v1\.[A-Za-z0-9_-]+$/);
    expect(protocols.join(",")).not.toContain("websocket-secret");
  });

  it("downloads through authenticated fetch, a Blob URL, and timely cleanup", async () => {
    setLocalOperatorToken("download-secret");
    const fetchMock = vi.fn().mockResolvedValue(
      new Response("evidence", {
        status: 200,
        headers: {
          "content-length": "8",
          "content-type": "text/plain",
        },
      }),
    );
    globalThis.fetch = fetchMock;
    const originalCreateObjectURL = URL.createObjectURL;
    const originalRevokeObjectURL = URL.revokeObjectURL;
    const createObjectURL = vi.fn().mockReturnValue("blob:riftx-evidence");
    const revokeObjectURL = vi.fn();
    Object.defineProperty(URL, "createObjectURL", {
      configurable: true,
      value: createObjectURL,
    });
    Object.defineProperty(URL, "revokeObjectURL", {
      configurable: true,
      value: revokeObjectURL,
    });
    const clicked: Array<{ download: string; href: string }> = [];
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (
      this: HTMLAnchorElement,
    ) {
      clicked.push({ download: this.download, href: this.href });
    });

    try {
      await api.downloadAuthenticatedUrl("/api/v1/artifacts/artifact-1/content", "scan.txt");
      await new Promise<void>((resolve) => window.setTimeout(resolve, 0));

      expect(fetchMock).toHaveBeenCalledWith(
        expect.stringMatching(/\/api\/v1\/artifacts\/artifact-1\/content$/),
        {
          cache: "no-store",
          headers: { Authorization: "Bearer download-secret" },
        },
      );
      expect(createObjectURL).toHaveBeenCalledWith(expect.any(Blob));
      const downloadedBlob = createObjectURL.mock.calls[0]?.[0] as Blob;
      expect(await downloadedBlob.text()).toBe("evidence");
      expect(clicked).toEqual([{ download: "scan.txt", href: "blob:riftx-evidence" }]);
      expect(revokeObjectURL).toHaveBeenCalledWith("blob:riftx-evidence");
      expect(document.querySelector('a[href="blob:riftx-evidence"]')).toBeNull();
    } finally {
      if (originalCreateObjectURL) {
        Object.defineProperty(URL, "createObjectURL", {
          configurable: true,
          value: originalCreateObjectURL,
        });
      } else {
        Reflect.deleteProperty(URL, "createObjectURL");
      }
      if (originalRevokeObjectURL) {
        Object.defineProperty(URL, "revokeObjectURL", {
          configurable: true,
          value: originalRevokeObjectURL,
        });
      } else {
        Reflect.deleteProperty(URL, "revokeObjectURL");
      }
    }
  });

  it("rejects a declared oversized download before buffering its body", async () => {
    const cancel = vi.fn();
    const response = new Response(
      new ReadableStream<Uint8Array>({ cancel }),
      {
        status: 200,
        headers: {
          "content-length": String(TEST_DOWNLOAD_LIMIT_BYTES + 1),
        },
      },
    );
    globalThis.fetch = vi.fn().mockResolvedValue(response);
    const createObjectURL = vi.fn();
    Object.defineProperty(URL, "createObjectURL", {
      configurable: true,
      value: createObjectURL,
    });

    await expect(
      api.downloadAuthenticatedUrl(
        "/api/v1/artifacts/artifact-oversized/content",
        "oversized.bin",
        { maxBytes: TEST_DOWNLOAD_LIMIT_BYTES },
      ),
    ).rejects.toMatchObject({
      status: 413,
      code: "download_too_large",
      message: "Download blocked because it exceeds the 8 B safety limit.",
      details: {
        limit_bytes: TEST_DOWNLOAD_LIMIT_BYTES,
        declared_bytes: String(TEST_DOWNLOAD_LIMIT_BYTES + 1),
      },
    });

    expect(cancel).toHaveBeenCalledOnce();
    expect(response.body?.locked).toBe(false);
    expect(createObjectURL).not.toHaveBeenCalled();
    expect(document.querySelector("a[download='oversized.bin']")).toBeNull();
  });

  it.each([
    ["missing", undefined],
    ["falsely small", "1"],
    ["invalid", "not-a-decimal-length"],
  ])(
    "enforces the cumulative stream limit when Content-Length is %s",
    async (_caseName, contentLength) => {
      const cancel = vi.fn();
      const chunk = new Uint8Array(4);
      const response = new Response(
        new ReadableStream<Uint8Array>({
          pull(controller) {
            controller.enqueue(chunk);
          },
          cancel,
        }),
        {
          status: 200,
          headers: contentLength === undefined
            ? undefined
            : { "content-length": contentLength },
        },
      );
      globalThis.fetch = vi.fn().mockResolvedValue(response);
      const createObjectURL = vi.fn();
      Object.defineProperty(URL, "createObjectURL", {
        configurable: true,
        value: createObjectURL,
      });

      await expect(
        api.downloadAuthenticatedUrl(
          "/api/v1/artifacts/artifact-streamed/content",
          "streamed.bin",
          { maxBytes: TEST_DOWNLOAD_LIMIT_BYTES },
        ),
      ).rejects.toMatchObject({
        status: 413,
        code: "download_too_large",
        details: {
          limit_bytes: TEST_DOWNLOAD_LIMIT_BYTES,
          received_bytes: TEST_DOWNLOAD_LIMIT_BYTES + chunk.byteLength,
        },
      });

      expect(cancel).toHaveBeenCalledOnce();
      expect(response.body?.locked).toBe(false);
      expect(createObjectURL).not.toHaveBeenCalled();
      expect(document.querySelector("a[download='streamed.bin']")).toBeNull();
    },
  );

  it("does not allow a caller to raise the authenticated download safety cap", async () => {
    const cancel = vi.fn();
    globalThis.fetch = vi.fn().mockResolvedValue(
      new Response(new ReadableStream<Uint8Array>({ cancel }), {
        status: 200,
        headers: {
          "content-length": String(MAX_AUTHENTICATED_DOWNLOAD_BYTES + 1),
        },
      }),
    );

    await expect(
      api.downloadAuthenticatedUrl(
        "/api/v1/artifacts/artifact-oversized/content",
        "oversized.bin",
        { maxBytes: MAX_AUTHENTICATED_DOWNLOAD_BYTES * 2 },
      ),
    ).rejects.toMatchObject({
      code: "download_too_large",
      details: { limit_bytes: MAX_AUTHENTICATED_DOWNLOAD_BYTES },
    });
    expect(cancel).toHaveBeenCalledOnce();
  });

  it("revokes the Blob URL and removes the link when triggering the download fails", async () => {
    setLocalOperatorToken("download-secret");
    globalThis.fetch = vi.fn().mockResolvedValue(
      new Response("evidence", {
        status: 200,
        headers: { "content-type": "text/plain" },
      }),
    );
    const originalCreateObjectURL = URL.createObjectURL;
    const originalRevokeObjectURL = URL.revokeObjectURL;
    const createObjectURL = vi.fn().mockReturnValue("blob:riftx-failed-download");
    const revokeObjectURL = vi.fn();
    Object.defineProperty(URL, "createObjectURL", {
      configurable: true,
      value: createObjectURL,
    });
    Object.defineProperty(URL, "revokeObjectURL", {
      configurable: true,
      value: revokeObjectURL,
    });
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {
      throw new Error("download click failed");
    });

    try {
      await expect(
        api.downloadAuthenticatedUrl(
          "/api/v1/artifacts/artifact-1/content",
          "scan.txt",
        ),
      ).rejects.toThrow("download click failed");
      await new Promise<void>((resolve) => window.setTimeout(resolve, 0));

      expect(createObjectURL).toHaveBeenCalledWith(expect.any(Blob));
      expect(revokeObjectURL).toHaveBeenCalledWith("blob:riftx-failed-download");
      expect(document.querySelector('a[href="blob:riftx-failed-download"]')).toBeNull();
    } finally {
      if (originalCreateObjectURL) {
        Object.defineProperty(URL, "createObjectURL", {
          configurable: true,
          value: originalCreateObjectURL,
        });
      } else {
        Reflect.deleteProperty(URL, "createObjectURL");
      }
      if (originalRevokeObjectURL) {
        Object.defineProperty(URL, "revokeObjectURL", {
          configurable: true,
          value: originalRevokeObjectURL,
        });
      } else {
        Reflect.deleteProperty(URL, "revokeObjectURL");
      }
    }
  });

  it("rejects authenticated downloads to a different or remote origin", async () => {
    setLocalOperatorToken("download-secret");
    globalThis.fetch = vi.fn();

    await expect(
      api.downloadAuthenticatedUrl("http://127.0.0.1:9999/private"),
    ).rejects.toThrow("Authenticated downloads must stay on the loopback Control Plane");
    await expect(
      api.downloadAuthenticatedUrl("https://remote.example.test/private"),
    ).rejects.toThrow("Authenticated downloads must stay on the loopback Control Plane");
    expect(globalThis.fetch).not.toHaveBeenCalled();
  });

  it("refuses to retain a token for a remotely configured API base", async () => {
    vi.stubEnv("VITE_RIFTX_API_URL", "https://remote.example.test");
    vi.resetModules();
    const remoteClient = await import("./client");

    expect(() => remoteClient.setLocalOperatorToken("must-not-leave-loopback")).toThrow(
      "The local operator token may only be sent to a loopback Control Plane",
    );
    expect(remoteClient.localOperatorHeaders()).toEqual({});
  });

  it("creates runs through the shared control-plane route", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          id: "run-1",
          objective: { description: "Inspect service" },
          status: "created",
        }),
        { status: 201, headers: { "content-type": "application/json" } },
      ),
    );
    globalThis.fetch = fetchMock;

    const created = await api.createRun({ objective: "Inspect service" });

    expect(created.id).toBe("run-1");
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/runs",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ objective: "Inspect service" }),
      }),
    );
  });

  it("filters Run lists by status and kind", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ items: [], limit: 100, offset: 0 }), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );
    globalThis.fetch = fetchMock;

    await api.listRuns("running", "general");

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/runs?status=running&kind=general",
      expect.any(Object),
    );
  });

  it("emergency-stops the entire Run through the Run cancellation route", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          accepted: true,
          run: { id: "run-1", status: "running" },
        }),
        { status: 202, headers: { "content-type": "application/json" } },
      ),
    );
    globalThis.fetch = fetchMock;

    await api.cancelRun("run 1");

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/runs/run%201/cancel",
      expect.objectContaining({ method: "POST" }),
    );
  });

  it("preserves the unified API error envelope", async () => {
    globalThis.fetch = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          error: {
            code: "run_not_found",
            message: "Run was not found",
            details: { run_id: "missing" },
          },
        }),
        { status: 404, headers: { "content-type": "application/json" } },
      ),
    );

    await expect(api.getRun("missing")).rejects.toMatchObject({
      status: 404,
      code: "run_not_found",
      details: { run_id: "missing" },
    });
  });

  it("updates model profiles without reading credentials back", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          name: "primary",
          model: "example-model",
          request_mode: "chat_completions",
          has_stored_api_key: true,
          api_key_configured: true,
        }),
        { status: 200, headers: { "content-type": "application/json" } },
      ),
    );
    globalThis.fetch = fetchMock;

    const profile = await api.updateModelProfile(
      "primary profile",
      {
        provider: "openai_compatible",
        model: "example-model",
        request_mode: "chat_completions",
        base_url: "https://llm.example.test/v1",
        api_key_env: "RIFTX_MODEL_API_KEY",
        requires_api_key: true,
        timeout_seconds: 120,
        max_retries: 2,
        api_key: "write-only-secret",
      },
      "admin-secret",
    );

    expect(profile).not.toHaveProperty("api_key");
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/model-profiles/primary%20profile",
      expect.objectContaining({
        method: "PUT",
        body: expect.stringContaining('"api_key":"write-only-secret"'),
        headers: expect.objectContaining({ Authorization: "Bearer admin-secret" }),
      }),
    );
  });

  it("sends durable approval decisions through the control plane", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ id: "approval-1", status: "approved" }), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );
    globalThis.fetch = fetchMock;

    await api.approve("approval-1", { approve_for_run: true });

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/approvals/approval-1/approve",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ approve_for_run: true }),
      }),
    );
  });

  it("registers and lists immutable artifacts through the control plane", async () => {
    const fetchMock = vi.fn().mockImplementation(() =>
      Promise.resolve(
        new Response(JSON.stringify({ id: "artifact-1", name: "scan.xml" }), {
          status: 200,
          headers: { "content-type": "application/json" },
        }),
      ),
    );
    globalThis.fetch = fetchMock;

    await api.registerArtifact("run-1", {
      source_path: "/tmp/run-1/scan.xml",
      description: "scan output",
    });
    await api.listArtifacts("run-1");

    expect(fetchMock).toHaveBeenNthCalledWith(
      1,
      "/api/v1/runs/run-1/artifacts",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({
          source_path: "/tmp/run-1/scan.xml",
          description: "scan output",
        }),
      }),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      "/api/v1/runs/run-1/artifacts",
      expect.any(Object),
    );
  });

  it("creates and edits structured findings through the control plane", async () => {
    const fetchMock = vi.fn().mockImplementation(() =>
      Promise.resolve(
        new Response(JSON.stringify({ id: "finding-1", title: "Exposed service" }), {
          status: 200,
          headers: { "content-type": "application/json" },
        }),
      ),
    );
    globalThis.fetch = fetchMock;

    await api.createFinding("run-1", {
      title: "Exposed service",
      severity: "high",
    });
    await api.updateFinding("finding-1", { status: "confirmed" });
    await api.getFinding("finding-1");

    expect(fetchMock).toHaveBeenNthCalledWith(
      1,
      "/api/v1/runs/run-1/findings",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ title: "Exposed service", severity: "high" }),
      }),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      "/api/v1/findings/finding-1",
      expect.objectContaining({
        method: "PATCH",
        body: JSON.stringify({ status: "confirmed" }),
      }),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      3,
      "/api/v1/findings/finding-1",
      expect.any(Object),
    );
  });


  it("generates and lists structured reports through the control plane", async () => {
    const fetchMock = vi.fn().mockImplementation(() =>
      Promise.resolve(
        new Response(JSON.stringify({ items: [] }), {
          status: 200,
          headers: { "content-type": "application/json" },
        }),
      ),
    );
    globalThis.fetch = fetchMock;

    await api.generateReports("run-1", { formats: ["markdown", "html", "json"] });
    await api.listReports("run-1");

    expect(fetchMock).toHaveBeenNthCalledWith(
      1,
      "/api/v1/runs/run-1/reports",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ formats: ["markdown", "html", "json"] }),
      }),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      "/api/v1/runs/run-1/reports",
      expect.any(Object),
    );
  });

  it("creates, fetches, and closes terminal sessions through the shared API", async () => {
    const fetchMock = vi.fn().mockImplementation(() =>
      Promise.resolve(
        new Response(JSON.stringify({ id: "terminal-1", status: "open" }), {
          status: 200,
          headers: { "content-type": "application/json" },
        }),
      ),
    );
    globalThis.fetch = fetchMock;

    await api.createTerminal("run-1", { argv: ["python", "-i"], owner: "agent" });
    await api.getTerminal("terminal-1");
    await api.closeTerminal("terminal-1");

    expect(fetchMock).toHaveBeenNthCalledWith(
      1,
      "/api/v1/runs/run-1/terminals",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ argv: ["python", "-i"], owner: "agent" }),
      }),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      "/api/v1/terminals/terminal-1",
      expect.any(Object),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      3,
      "/api/v1/terminals/terminal-1",
      expect.objectContaining({ method: "DELETE" }),
    );
    expect(api.terminalWebSocketUrl("terminal-1", 42)).toMatch(
      /^ws:\/\/localhost(?::\d+)?\/api\/v1\/terminals\/terminal-1\/ws\?cursor=42$/,
    );
  });
});

function restoreURLMethod(
  name: "createObjectURL" | "revokeObjectURL",
  original: unknown,
) {
  if (typeof original === "function") {
    Object.defineProperty(URL, name, {
      configurable: true,
      value: original,
    });
  } else {
    Reflect.deleteProperty(URL, name);
  }
}

it("persists tool edits through the node registry API", async () => {
  const fetchMock = vi.fn().mockImplementation(() =>
    Promise.resolve(
      new Response(JSON.stringify({ node_id: "local", generation: 2, tools: [] }), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    ),
  );
  globalThis.fetch = fetchMock;
  const payload = {
    enabled: true,
    command: ["nmap"],
    executor: "process" as const,
    capabilities: ["port_scan"],
    approval: "never" as const,
    timeout: 30,
  };

  await api.listTools("local");
  await api.listToolsForAdmin("local", "admin-secret");
  await api.refreshTools("local", "admin-secret");
  await api.updateTool("local", "nmap", payload, "admin-secret");

  expect(fetchMock).toHaveBeenNthCalledWith(
    1,
    "/api/v1/nodes/local/tools",
    expect.any(Object),
  );
  expect(fetchMock.mock.calls[0]?.[1]?.headers).not.toHaveProperty("Authorization");
  expect(fetchMock).toHaveBeenNthCalledWith(
    2,
    "/api/v1/nodes/local/tools/admin",
    expect.objectContaining({
      headers: expect.objectContaining({ Authorization: "Bearer admin-secret" }),
    }),
  );
  expect(fetchMock).toHaveBeenNthCalledWith(
    3,
    "/api/v1/nodes/local/refresh-tools",
    expect.objectContaining({
      method: "POST",
      headers: expect.objectContaining({ Authorization: "Bearer admin-secret" }),
    }),
  );
  expect(fetchMock).toHaveBeenNthCalledWith(
    4,
    "/api/v1/nodes/local/tools/nmap",
    expect.objectContaining({
      method: "PUT",
      body: JSON.stringify(payload),
      headers: expect.objectContaining({ Authorization: "Bearer admin-secret" }),
    }),
  );
});
