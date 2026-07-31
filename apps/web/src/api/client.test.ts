import { afterEach, describe, expect, it, vi } from "vitest";

import { api } from "./client";

const originalFetch = globalThis.fetch;

afterEach(() => {
  globalThis.fetch = originalFetch;
  vi.restoreAllMocks();
});

describe("RiftX API client", () => {
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

it("persists tool edits through the node registry API", async () => {
  const fetchMock = vi.fn().mockResolvedValue(
    new Response(JSON.stringify({ node_id: "local", generation: 2, tools: [] }), {
      status: 200,
      headers: { "content-type": "application/json" },
    }),
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

  await api.updateTool("local", "nmap", payload);

  expect(fetchMock).toHaveBeenCalledWith(
    "/api/v1/nodes/local/tools/nmap",
    expect.objectContaining({ method: "PUT", body: JSON.stringify(payload) }),
  );
});
