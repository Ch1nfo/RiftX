import test from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, rm } from "node:fs/promises";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { createBenchmarkControlTool } from "./control-tool";
import { createAssignBenchmarkChallengeTool } from "./assign-tool";
import { BenchmarkController, type Challenge } from "../controller";
import { BenchmarkLedger } from "../ledger";
import type { BrowserManager } from "@/browser";

const realHome = process.env.HOME;
let tempDir: string;

test.before(async () => {
  tempDir = await mkdtemp(join(tmpdir(), "riftx-bench-tools-"));
  process.env.HOME = tempDir;
});

test.after(async () => {
  process.env.HOME = realHome;
  await rm(tempDir, { recursive: true, force: true }).catch(() => undefined);
});

function platformChallenge(code: string, overrides: Partial<Challenge> = {}): Challenge {
  return {
    unique_code: code, description: `Challenge ${code}`, difficulty: "easy", level: "L1",
    total_score: 100, flag_count: 1, correct_flag_count: 0, is_completed: false,
    container_status: "stopped", container_addr: [], ...overrides
  };
}

function fakeBrowser() {
  const grants: string[] = [];
  return { grants, grantScope: (url: string) => grants.push(url) } as unknown as BrowserManager;
}

type Route = { method: string; path: string; query?: string; status?: number; body: unknown };

async function setupTool(routes: Route[]) {
  const browser = fakeBrowser();
  const sessionId = `tools-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
  const controller = new BenchmarkController({
    baseUrl: "https://bench.test",
    token: "test",
    fetchImpl: async (input, init) => {
      const url = new URL(input);
      const method = init?.method ?? "GET";
      const query = url.searchParams.get("unique_code") ?? "";
      const path = url.pathname;
      const route = routes.find((candidate) => candidate.method === method && candidate.path === path && (candidate.query === undefined || candidate.query === query));
      if (!route) return new Response(JSON.stringify({ error: "not found" }), { status: 404, headers: { Connection: "close" } });
      return new Response(JSON.stringify(route.body), { status: route.status ?? 200, headers: { "Content-Type": "application/json", Connection: "close" } });
    }
  });
  const ledger = await new BenchmarkLedger(sessionId).initialize();
  const tool = createBenchmarkControlTool(controller, ledger, browser, () => "main");
  return { tool, ledger, controller, browser, sessionId };
}

const VPN_ROUTES: Route[] = [
  { method: "GET", path: "/", body: { status: "ok" } },
  { method: "HEAD", path: "/", body: {} },
];

async function execute(tool: ReturnType<typeof createBenchmarkControlTool>, params: Record<string, unknown>) {
  const ctx = {} as Parameters<typeof tool.execute>[4];
  return tool.execute("test-call", params as Parameters<typeof tool.execute>[1], undefined, undefined, ctx);
}

test("sync pulls challenges (bare array) and updates ledger", async () => {
  const { tool, ledger } = await setupTool([
    ...VPN_ROUTES,
    { method: "GET", path: "/openapi/v1/challenges", body: [platformChallenge("ch-1"), platformChallenge("ch-2")] }
  ]);
  const result = await execute(tool, { action: "sync" });
  assert.match((result.content[0] as { text: string }).text, /VPN.*ok/);
  assert.equal(ledger.getState().totalChallenges, 2);
});

test("acquire starts container and grants browser scope", async () => {
  const { tool, ledger, browser } = await setupTool([
    ...VPN_ROUTES,
    { method: "GET", path: "/openapi/v1/challenges", body: [platformChallenge("ch-1")] },
    { method: "POST", path: "/openapi/v1/challenges/start", query: "ch-1", body: { unique_code: "ch-1", container_addr: ["10.0.0.5:8080"] } }
  ]);
  await execute(tool, { action: "sync" });
  const result = await execute(tool, { action: "acquire", uniqueCode: "ch-1" });
  assert.match((result.content[0] as { text: string }).text, /Acquired ch-1/);
  assert.equal(ledger.getChallenge("ch-1")?.status, "running");
  assert.deepEqual((browser as unknown as { grants: string[] }).grants, ["http://10.0.0.5:8080/"]);
});

test("submit correct flag on single-flag challenge marks solved and closes container", async () => {
  const { tool, ledger } = await setupTool([
    ...VPN_ROUTES,
    { method: "GET", path: "/openapi/v1/challenges", body: [platformChallenge("ch-1")] },
    { method: "POST", path: "/openapi/v1/challenges/start", query: "ch-1", body: { unique_code: "ch-1", container_addr: ["a"] } },
    { method: "POST", path: "/openapi/v1/challenges/submit", body: { correct: true, awarded: 100, cumulative_score: 100, correct_flag_count: 1, total_flag_count: 1, matched_flag_index: 0 } },
    { method: "POST", path: "/openapi/v1/challenges/close", query: "ch-1", body: { unique_code: "ch-1", closed: true } }
  ]);
  await execute(tool, { action: "sync" });
  await execute(tool, { action: "acquire", uniqueCode: "ch-1" });
  const result = await execute(tool, { action: "submit", uniqueCode: "ch-1", flag: "flag{test}" });
  assert.match((result.content[0] as { text: string }).text, /CORRECT.*solved/);
  assert.equal(ledger.getChallenge("ch-1")?.status, "solved");
});

test("submit partial flag on multi-flag challenge keeps running", async () => {
  const { tool, ledger } = await setupTool([
    ...VPN_ROUTES,
    { method: "GET", path: "/openapi/v1/challenges", body: [platformChallenge("ch-1", { flag_count: 3 })] },
    { method: "POST", path: "/openapi/v1/challenges/start", query: "ch-1", body: { unique_code: "ch-1", container_addr: ["a"] } },
    { method: "POST", path: "/openapi/v1/challenges/submit", body: { correct: true, awarded: 50, cumulative_score: 50, correct_flag_count: 1, total_flag_count: 3, matched_flag_index: 0 } }
  ]);
  await execute(tool, { action: "sync" });
  await execute(tool, { action: "acquire", uniqueCode: "ch-1" });
  const result = await execute(tool, { action: "submit", uniqueCode: "ch-1", flag: "flag{1}" });
  assert.match((result.content[0] as { text: string }).text, /1\/3/);
  assert.equal(ledger.getChallenge("ch-1")?.status, "running");
});

test("duplicate submit is treated as success (idempotent)", async () => {
  const { tool } = await setupTool([
    ...VPN_ROUTES,
    { method: "GET", path: "/openapi/v1/challenges", body: [platformChallenge("ch-1")] },
    { method: "POST", path: "/openapi/v1/challenges/start", query: "ch-1", body: { unique_code: "ch-1", container_addr: ["a"] } },
    { method: "POST", path: "/openapi/v1/challenges/submit", status: 409, body: { code: "duplicate", message: "already" } }
  ]);
  await execute(tool, { action: "sync" });
  await execute(tool, { action: "acquire", uniqueCode: "ch-1" });
  const result = await execute(tool, { action: "submit", uniqueCode: "ch-1", flag: "flag{dup}" });
  assert.match((result.content[0] as { text: string }).text, /already submitted.*no penalty/);
});

test("checkpoint with same signal does not reset budget", async () => {
  const { tool } = await setupTool([
    ...VPN_ROUTES,
    { method: "GET", path: "/openapi/v1/challenges", body: [platformChallenge("ch-1")] },
    { method: "POST", path: "/openapi/v1/challenges/start", query: "ch-1", body: { unique_code: "ch-1", container_addr: ["a"] } }
  ]);
  await execute(tool, { action: "sync" });
  await execute(tool, { action: "acquire", uniqueCode: "ch-1" });
  const first = await execute(tool, { action: "checkpoint", uniqueCode: "ch-1", signal: "found login" });
  assert.match((first.content[0] as { text: string }).text, /New signal recorded/);
  const repeat = await execute(tool, { action: "checkpoint", uniqueCode: "ch-1", signal: "found login" });
  assert.match((repeat.content[0] as { text: string }).text, /NOT reset/);
});

test("hint forbidden in pass 1", async () => {
  const { tool } = await setupTool([
    ...VPN_ROUTES,
    { method: "GET", path: "/openapi/v1/challenges", body: [platformChallenge("ch-1")] },
    { method: "POST", path: "/openapi/v1/challenges/start", query: "ch-1", body: { unique_code: "ch-1", container_addr: ["a"] } }
  ]);
  await execute(tool, { action: "sync" });
  await execute(tool, { action: "acquire", uniqueCode: "ch-1" });
  const result = await execute(tool, { action: "hint", uniqueCode: "ch-1" });
  assert.match((result.content[0] as { text: string }).text, /forbidden in pass 1/);
});

test("defer closes container and releases slot", async () => {
  const { tool, ledger } = await setupTool([
    ...VPN_ROUTES,
    { method: "GET", path: "/openapi/v1/challenges", body: [platformChallenge("ch-1")] },
    { method: "POST", path: "/openapi/v1/challenges/start", query: "ch-1", body: { unique_code: "ch-1", container_addr: ["a"] } },
    { method: "POST", path: "/openapi/v1/challenges/close", query: "ch-1", body: { unique_code: "ch-1", closed: true } }
  ]);
  await execute(tool, { action: "sync" });
  await execute(tool, { action: "acquire", uniqueCode: "ch-1" });
  await execute(tool, { action: "defer", uniqueCode: "ch-1", reason: "budget out" });
  assert.equal(ledger.getChallenge("ch-1")?.status, "deferred");
  assert.equal(ledger.getChallenge("ch-1")?.owner, null);
});

test("max-active error surfaces actionable message", async () => {
  const { tool } = await setupTool([
    ...VPN_ROUTES,
    { method: "GET", path: "/openapi/v1/challenges", body: [platformChallenge("ch-1")] },
    { method: "POST", path: "/openapi/v1/challenges/start", query: "ch-1", status: 409, body: { code: "invalid_state", message: "max active challenges reached" } }
  ]);
  await execute(tool, { action: "sync" });
  const result = await execute(tool, { action: "acquire", uniqueCode: "ch-1" });
  assert.match((result.content[0] as { text: string }).text, /Container limit reached.*defer/);
});

test("child session with assignedChallenge: only allowed actions, locked uniqueCode", async () => {
  const browser = fakeBrowser();
  const sessionId = `child-${Date.now()}`;
  const controller = new BenchmarkController({
    baseUrl: "https://b.test", token: "t",
    fetchImpl: async () => new Response("[]", { status: 200, headers: { Connection: "close" } })
  });
  const ledger = await new BenchmarkLedger(sessionId).initialize();
  await ledger.syncFromPlatform([platformChallenge("ch-1"), platformChallenge("ch-2")], 0, true, "ip");
  await ledger.acquire("ch-1", "subagent:t1", ["10.0.0.1:80"]);
  const tool = createBenchmarkControlTool(controller, ledger, browser, () => "subagent:t1", "ch-1");
  const ctx = {} as Parameters<typeof tool.execute>[4];

  const sync = await tool.execute("c1", { action: "sync" }, undefined, undefined, ctx);
  assert.match((sync.content[0] as { text: string }).text, /not available to SubAgents/);
  const acquire = await tool.execute("c2", { action: "acquire", uniqueCode: "ch-1" }, undefined, undefined, ctx);
  assert.match((acquire.content[0] as { text: string }).text, /not available to SubAgents/);
  const other = await tool.execute("c3", { action: "checkpoint", uniqueCode: "ch-2", signal: "x" }, undefined, undefined, ctx);
  assert.match((other.content[0] as { text: string }).text, /assigned to ch-1/);
  const own = await tool.execute("c4", { action: "checkpoint", signal: "found something" }, undefined, undefined, ctx);
  assert.match((own.content[0] as { text: string }).text, /New signal recorded/);
});

test("assign tool dispatches and passes uniqueCode to spawnSubagent", async () => {
  const browser = fakeBrowser();
  const sessionId = `assign-${Date.now()}`;
  const controller = new BenchmarkController({
    baseUrl: "https://b.test", token: "t",
    fetchImpl: async (input, init) => {
      const url = new URL(input);
      if (url.pathname === "/openapi/v1/challenges/start" && init?.method === "POST" && url.searchParams.get("unique_code") === "ch-1") {
        return new Response(JSON.stringify({ unique_code: "ch-1", container_addr: ["10.0.0.1:80"] }), { status: 200, headers: { Connection: "close" } });
      }
      return new Response(JSON.stringify([platformChallenge("ch-1")]), { status: 200, headers: { Connection: "close" } });
    }
  });
  const ledger = await new BenchmarkLedger(sessionId).initialize();
  await ledger.syncFromPlatform([platformChallenge("ch-1")], 0, true, "ip");
  let dispatched = "";
  let passedUniqueCode = "";
  const tool = createAssignBenchmarkChallengeTool(controller, ledger, browser, async (task, uniqueCode, _containerAddrs, reservationOwner) => {
    dispatched = task;
    passedUniqueCode = uniqueCode;
    await ledger.bindOwner(uniqueCode, reservationOwner, "subagent:task-1");
    return { taskId: "task-1" };
  });
  const ctx = {} as Parameters<typeof tool.execute>[4];
  const result = await tool.execute("call", { uniqueCode: "ch-1" }, undefined, undefined, ctx);
  assert.match((result.content[0] as { text: string }).text, /Assigned ch-1/);
  assert.match(dispatched, /Solve this TSec benchmark challenge/);
  assert.match(dispatched, /10\.0\.0\.1:80/);
  assert.match(dispatched, /SUBMIT_STATUS/);
  assert.equal(passedUniqueCode, "ch-1", "uniqueCode must be passed to spawnSubagent for child binding");
  assert.equal(ledger.getChallenge("ch-1")?.status, "running");
});

test("assign tool reports a SubAgent cancelled during dispatch as not assigned", async () => {
  const browser = fakeBrowser();
  const sessionId = `assign-cancel-${Date.now()}`;
  const controller = new BenchmarkController({
    baseUrl: "https://b.test", token: "t",
    fetchImpl: async (input, init) => {
      const url = new URL(input);
      if (url.pathname === "/openapi/v1/challenges/start" && init?.method === "POST" && url.searchParams.get("unique_code") === "ch-1") {
        return new Response(JSON.stringify({ unique_code: "ch-1", container_addr: ["10.0.0.1:80"] }), { status: 200, headers: { Connection: "close" } });
      }
      if (url.pathname === "/openapi/v1/challenges/close" && init?.method === "POST") {
        return new Response(JSON.stringify({ unique_code: "ch-1", closed: true }), { status: 200, headers: { Connection: "close" } });
      }
      return new Response(JSON.stringify([platformChallenge("ch-1")]), { status: 200, headers: { Connection: "close" } });
    }
  });
  const ledger = await new BenchmarkLedger(sessionId).initialize();
  await ledger.syncFromPlatform([platformChallenge("ch-1")], 0, true, "ip");
  const tool = createAssignBenchmarkChallengeTool(controller, ledger, browser, async (_task, uniqueCode, _containerAddrs, reservationOwner) => {
    // Mirror the real bridge: binding completes, then the task turns out to
    // have been cancelled during the binding window and the challenge was
    // released with a confirmed container close.
    await ledger.bindOwner(uniqueCode, reservationOwner, "subagent:task-cancelled");
    await ledger.releaseOnSubagentExit(uniqueCode, "subagent task cancelled during binding", "subagent:task-cancelled");
    await controller.closeChallenge(uniqueCode);
    await ledger.confirmClosed(uniqueCode);
    return { taskId: "task-cancelled", cancelled: true };
  });
  const ctx = {} as Parameters<typeof tool.execute>[4];
  const result = await tool.execute("call", { uniqueCode: "ch-1" }, undefined, undefined, ctx);
  const text = (result.content[0] as { text: string }).text;
  assert.match(text, /cancelled during dispatch/);
  assert.match(text, /re-assign/);
  assert.equal(ledger.getChallenge("ch-1")?.status, "deferred", "released challenge returns to the pool");
  assert.equal(ledger.getChallenge("ch-1")?.owner, null);
});
