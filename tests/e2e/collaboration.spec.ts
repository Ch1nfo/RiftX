import { expect, test } from "@playwright/test";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { BoardStore } from "../../src/server/collaboration/store";
import { BoardRuntime } from "../../src/server/collaboration/runtime";
import { collaborationResponse, type CollaborationRequest } from "../../src/server/collaboration/http";
import { MockApi } from "./mock-api";

test("board controls, durable messages, budget edits, cancellation and duplicate SSE", async ({ page, context }, testInfo) => {
  const mock = new MockApi(); const port = await mock.start();
  const dir = mkdtempSync(join(tmpdir(), "riftx-board-ui-"));
  const store = new BoardStore(join(dir, "board.sqlite"), "e2e-second", { create: true });
  const runtime = new BoardRuntime(store, { actor: async () => { throw new Error("UI fixture must not run a model"); }, release: async () => {}, emit: (event) => mock.collaborationEvent(event) }, 60000);
  store.apply("main", "create", "create", { objective: "Inspect fixture endpoint", acceptance: "Record reproducible evidence" }, { epoch: 1 });
  store.apply("system", "register", "register_agent");
  store.apply("user", "pause", "control", { action: "pause" });
  const mutations: string[] = [];
  try {
    await page.addInitScript(() => window.localStorage.setItem("riftx-language", "en"));
    await context.route("**/api/**", (route) => { const url = new URL(route.request().url()); return route.continue({ url: `http://127.0.0.1:${port}${url.pathname}${url.search}` }); });
    await page.route("**/api/sessions/e2e-second/collaboration**", async (route) => {
      const url = new URL(route.request().url());
      const suffix = url.pathname.split("/collaboration")[1];
      let kind: CollaborationRequest = "read"; let entity: string | undefined;
      if (suffix === "/messages") kind = "messages";
      else if (suffix === "/control") kind = "control";
      else if (suffix.startsWith("/tasks/")) { kind = "task"; entity = suffix.split("/")[2]; }
      else if (suffix.startsWith("/agents/")) { kind = "agent"; entity = suffix.split("/")[2]; }
      if (route.request().method() === "POST") mutations.push(kind);
      const response = await collaborationResponse(new Request(url, { method: route.request().method(), ...(route.request().postData() ? { body: route.request().postData()! } : {}) }), "e2e-second", kind, entity, async () => runtime);
      await route.fulfill({ status: response.status, contentType: "application/json", body: await response.text() });
    });
    await page.goto("/");
    await page.locator(".session-item", { hasText: "E2E second session" }).click();
    const panel = page.getByRole("region", { name: "Shared collaboration" });
    await expect(panel).toContainText("Inspect fixture endpoint");
    expect(mutations).toHaveLength(0);
    await panel.getByRole("button", { name: "Messages", exact: true }).click();
    await panel.getByLabel("Message", { exact: true }).fill("Keep this while paused");
    await panel.getByRole("button", { name: "Send", exact: true }).click();
    await expect(panel).toContainText("Pending delivery");
    expect(store.read().messages[0].status).toBe("queued"); expect(store.read().used.wakes).toBe(0);
    await panel.getByRole("button", { name: "Resume", exact: true }).click();
    await expect(panel.locator("header")).toContainText("Running");
    await panel.getByRole("button", { name: "Adjust budgets" }).click();
    await panel.getByLabel("wakes", { exact: true }).fill("90"); await panel.getByRole("button", { name: "Save", exact: true }).click();
    await expect(panel).toContainText("0/90");
    await panel.getByRole("button", { name: "Task board", exact: true }).click();
    const task = panel.locator("article", { hasText: "Inspect fixture endpoint" });
    await task.getByRole("button", { name: "Cancel", exact: true }).click();
    await expect(task).toContainText("Cancelled"); await expect(task.getByRole("button", { name: "Retry", exact: true })).toHaveCount(0);
    const seq = store.read().revision;
    mock.collaborationEvent({ seq, type: "manage", actor: "user", createdAt: Date.now() });
    mock.collaborationEvent({ seq: seq - 1, type: "create", actor: "main", createdAt: Date.now() });
    await expect(task).toContainText("Cancelled");
    await panel.getByRole("button", { name: "Pause", exact: true }).click();
    await page.screenshot({ path: testInfo.outputPath("collaboration-panel.png"), fullPage: true });
    const before = mutations.length;
    await page.reload(); await page.locator(".session-item", { hasText: "E2E second session" }).click();
    await expect(panel.locator("header")).toContainText("Paused"); expect(mutations.length).toBe(before);
    expect(store.read().used.wakes).toBe(0);
    await panel.getByRole("button", { name: "Messages", exact: true }).click();
    await expect(panel).toContainText("Keep this while paused");
  } finally { await runtime.close(); mock.close(); rmSync(dir, { recursive: true, force: true }); }
});
