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
});
