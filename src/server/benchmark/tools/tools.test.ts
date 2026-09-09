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
    unique_code: code, description: `Challenge ${code}`, difficulty: "easy", level: 1,
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
    vpnUrl: "https://bench.test",
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

test("sync persists a failed VPN preflight instead of retaining stale success", async () => {
  let vpnAvailable = true;
  const browser = fakeBrowser();
  const sessionId = `tools-vpn-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
  const controller = new BenchmarkController({
    baseUrl: "https://bench.test",
    token: "test",
    vpnUrl: "https://vpn.test",
    fetchImpl: async (input) => {
      const url = new URL(input);
      if (url.hostname === "vpn.test") {
        return vpnAvailable
          ? new Response(JSON.stringify({ status: "ok", client_ip: "10.0.0.1" }), { status: 200 })
          : new Response(JSON.stringify({ status: "fail" }), { status: 503 });
      }
      return new Response(JSON.stringify([platformChallenge("ch-1")]), { status: 200 });
    }
  });
  const ledger = await new BenchmarkLedger(sessionId).initialize();
  const tool = createBenchmarkControlTool(controller, ledger, browser, () => "main");

  await execute(tool, { action: "sync" });
  assert.equal(ledger.getState().vpnOk, true);
  vpnAvailable = false;

  const result = await execute(tool, { action: "sync" });
  assert.match((result.content[0] as { text: string }).text, /VPN check failed/);
  assert.equal(ledger.getState().vpnChecked, true);
  assert.equal(ledger.getState().vpnOk, false);
  assert.equal(ledger.getState().vpnClientIp, "");
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

test("acquire reuses a live orphan after Runtime restart without calling start again", async () => {
  let startCalls = 0;
  const browser = fakeBrowser();
  const sessionId = `tools-live-orphan-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
  const controller = new BenchmarkController({
    baseUrl: "https://bench.test",
    token: "test",
    vpnUrl: "https://bench.test",
    fetchImpl: async (input, init) => {
      const url = new URL(input);
      if (url.pathname === "/") return new Response(JSON.stringify({ status: "ok" }), { status: 200 });
      if (url.pathname === "/openapi/v1/challenges" && (init?.method ?? "GET") === "GET") {
        return new Response(JSON.stringify([platformChallenge("ch-live", {
          container_status: "available", container_addr: ["10.0.0.9:8080"]
        })]), { status: 200 });
      }
      if (url.pathname === "/openapi/v1/challenges/start") startCalls += 1;
      return new Response(JSON.stringify({ message: "unexpected" }), { status: 500 });
    }
  });
  const ledger = await new BenchmarkLedger(sessionId).initialize();
  const tool = createBenchmarkControlTool(controller, ledger, browser, () => "main");
  await execute(tool, { action: "sync" });
  assert.equal(ledger.getChallenge("ch-live")?.status, "orphaned");

  const result = await execute(tool, { action: "acquire", uniqueCode: "ch-live" });
  assert.match((result.content[0] as { text: string }).text, /Acquired ch-live/);
  assert.equal(startCalls, 0);
  assert.equal(ledger.getChallenge("ch-live")?.status, "running");
  assert.deepEqual((browser as unknown as { grants: string[] }).grants, ["http://10.0.0.9:8080/"]);
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
  const first = await execute(tool, { action: "checkpoint", uniqueCode: "ch-1", signal: "found login", signalKind: "new_surface" });
  assert.match((first.content[0] as { text: string }).text, /NOT extended/);
  const strong = await execute(tool, { action: "checkpoint", uniqueCode: "ch-1", signal: "obtained admin access", signalKind: "privilege_change", evidenceRef: "request:req-1" });
  assert.match((strong.content[0] as { text: string }).text, /clock extended/);
  const repeat = await execute(tool, { action: "checkpoint", uniqueCode: "ch-1", signal: "admin access confirmed", signalKind: "privilege_change", evidenceRef: "request:req-1" });
  assert.match((repeat.content[0] as { text: string }).text, /NOT extended/);
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
  await ledger.syncFromPlatform([platformChallenge("ch-1"), platformChallenge("ch-2")], true, "ip");
  await ledger.acquire("ch-1", "subagent:t1", ["10.0.0.1:80"]);
  const tool = createBenchmarkControlTool(controller, ledger, browser, () => "subagent:t1", "ch-1");
  const ctx = {} as Parameters<typeof tool.execute>[4];

  const sync = await tool.execute("c1", { action: "sync" }, undefined, undefined, ctx);
  assert.match((sync.content[0] as { text: string }).text, /not available to SubAgents/);
  const acquire = await tool.execute("c2", { action: "acquire", uniqueCode: "ch-1" }, undefined, undefined, ctx);
  assert.match((acquire.content[0] as { text: string }).text, /not available to SubAgents/);
  const other = await tool.execute("c3", { action: "checkpoint", uniqueCode: "ch-2", signal: "x" }, undefined, undefined, ctx);
  assert.match((other.content[0] as { text: string }).text, /assigned to ch-1/);
  const own = await tool.execute("c4", { action: "checkpoint", signal: "found something", signalKind: "foothold", evidenceRef: "artifact:scan-1" }, undefined, undefined, ctx);
  assert.match((own.content[0] as { text: string }).text, /clock extended/);
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
  await ledger.syncFromPlatform([platformChallenge("ch-1")], true, "ip");
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
  await ledger.syncFromPlatform([platformChallenge("ch-1")], true, "ip");
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

test("assign reuses a warm-handoff container and briefs a different recovery approach", async () => {
  const browser = fakeBrowser();
  const sessionId = `assign-handoff-${Date.now()}`;
  let startCalls = 0;
  const controller = new BenchmarkController({
    baseUrl: "https://b.test", token: "t",
    fetchImpl: async (input, init) => {
      const url = new URL(input);
      if (url.pathname === "/openapi/v1/challenges/start" && init?.method === "POST") startCalls += 1;
      return new Response(JSON.stringify({ code: "unexpected" }), { status: 500, headers: { Connection: "close" } });
    }
  });
  const ledger = await new BenchmarkLedger(sessionId).initialize();
  await ledger.syncFromPlatform([platformChallenge("ch-1", { flag_count: 2 })], true, "ip");
  await ledger.acquire("ch-1", "main", ["old"]);
  await ledger.checkpoint("ch-1", "sqlmap ruled out generic injection", ["SQLi"], "audit access control", "main", {
    signalKind: "decisive_rule_out", currentApproach: "generic SQLi", ruledOutFamilies: ["SQLi"]
  });
  await ledger.recordSubmission("ch-1", "flag{one}", true, 50, 1, 0, "main");
  await ledger.defer("ch-1", "first pass complete", "audit access control", "main");
  await ledger.confirmClosed("ch-1");
  await ledger.maybeAdvancePhase();
  await ledger.acquire("ch-1", "subagent:old", ["live:8080"]);
  await ledger.defer("ch-1", "timebox", "try source audit", "subagent:old", { preserveContainer: true });

  let brief = "";
  const tool = createAssignBenchmarkChallengeTool(controller, ledger, browser, async (task, uniqueCode, _addrs, reservationOwner) => {
    brief = task;
    await ledger.bindOwner(uniqueCode, reservationOwner, "subagent:fresh");
    return { taskId: "fresh" };
  });
  const result = await tool.execute("handoff", { uniqueCode: "ch-1" }, undefined, undefined, {} as Parameters<typeof tool.execute>[4]);
  assert.match((result.content[0] as { text: string }).text, /warm handoff/);
  assert.equal(startCalls, 0, "a live handoff must not start a replacement container");
  assert.match(brief, /Mandatory strategy reset/);
  assert.match(brief, /generic SQLi/);
  assert.match(brief, /first three probes/);
  assert.equal(ledger.getChallenge("ch-1")?.owner, "subagent:fresh");
});

test("failed warm-handoff dispatch restores the live handoff instead of closing it", async () => {
  const browser = fakeBrowser();
  const sessionId = `assign-handoff-fail-${Date.now()}`;
  let startCalls = 0;
  let closeCalls = 0;
  const controller = new BenchmarkController({
    baseUrl: "https://b.test", token: "t",
    fetchImpl: async (input, init) => {
      const url = new URL(input);
      if (url.pathname.endsWith("/start") && init?.method === "POST") startCalls += 1;
      if (url.pathname.endsWith("/close") && init?.method === "POST") closeCalls += 1;
      return new Response(JSON.stringify({ code: "unexpected" }), { status: 500, headers: { Connection: "close" } });
    }
  });
  const ledger = await new BenchmarkLedger(sessionId).initialize();
  await ledger.syncFromPlatform([platformChallenge("ch-1", { flag_count: 2 })], true, "ip");
  await ledger.acquire("ch-1", "main", ["old"]);
  await ledger.recordSubmission("ch-1", "flag{one}", true, 50, 1, 0, "main");
  await ledger.defer("ch-1", "covered", "try source audit", "main");
  await ledger.confirmClosed("ch-1");
  await ledger.maybeAdvancePhase();
  await ledger.acquire("ch-1", "subagent:old", ["live:8080"]);
  await ledger.defer("ch-1", "rotate", "try protocol abuse", "subagent:old", { preserveContainer: true });
  const attemptsBefore = ledger.getChallenge("ch-1")!.attemptCount;

  const tool = createAssignBenchmarkChallengeTool(controller, ledger, browser, async () => {
    throw new Error("child runtime failed to initialize");
  });
  const result = await tool.execute("handoff-fail", { uniqueCode: "ch-1" }, undefined, undefined, {} as Parameters<typeof tool.execute>[4]);

  assert.match((result.content[0] as { text: string }).text, /returned to warm-handoff state/);
  assert.equal(startCalls, 0);
  assert.equal(closeCalls, 0);
  assert.equal(ledger.getChallenge("ch-1")?.status, "handoff_waiting");
  assert.deepEqual(ledger.getChallenge("ch-1")?.containerAddrs, ["live:8080"]);
  assert.equal(ledger.getChallenge("ch-1")?.attemptCount, attemptsBefore, "failed dispatch must not count as a real attempt");
});
