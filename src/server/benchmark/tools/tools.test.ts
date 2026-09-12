import { enqueuePendingSubmission, pendingSubmission, retryPendingSubmissions, hasPendingSubmissions } from "../pending-submissions";
import test from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, rm } from "node:fs/promises";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { createBenchmarkControlTool } from "./control-tool";
import { createAssignBenchmarkChallengeTool } from "./assign-tool";
import { BenchmarkController, BenchmarkError, type Challenge } from "../controller";
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

test("sync does not report a false leak when defer finishes before reconciliation acquires the challenge lock", async () => {
  const sessionId = `tools-sync-race-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
  const ledger = await new BenchmarkLedger(sessionId).initialize();
  const platform = platformChallenge("ch-1", { container_status: "available", container_addr: ["a"] });
  await ledger.syncFromPlatform([platform], true, "ip");
  await ledger.acquire("ch-1", "main", ["a"]);

  let releaseClose!: () => void;
  const closeGate = new Promise<void>((resolve) => { releaseClose = resolve; });
  let closeStarted!: () => void;
  const firstCloseStarted = new Promise<void>((resolve) => { closeStarted = resolve; });
  let closeCalls = 0;
  const controller = {
    checkVpn: async () => ({ status: "unchecked", client_ip: "", ok: false }),
    listChallenges: async () => [platform],
    closeChallenge: async () => {
      closeCalls += 1;
      if (closeCalls === 1) {
        closeStarted();
        await closeGate;
      }
      return { unique_code: "ch-1", closed: true };
    }
  } as unknown as BenchmarkController;
  const tool = createBenchmarkControlTool(controller, ledger, fakeBrowser(), () => "main");

  const deferResult = execute(tool, { action: "defer", uniqueCode: "ch-1", reason: "covered" });
  await firstCloseStarted;
  const syncResult = execute(tool, { action: "sync" });
  await new Promise((resolve) => setTimeout(resolve, 10));
  releaseClose();
  await Promise.all([deferResult, syncResult]);

  assert.equal(closeCalls, 1, "sync must re-check state instead of closing the same challenge twice");
  assert.equal(ledger.getChallenge("ch-1")?.status, "deferred");
  assert.equal(ledger.getChallenge("ch-1")?.containerStatus, "stopped");
  assert.equal(ledger.getChallenge("ch-1")?.closeFailureRecorded, false);
  assert.equal(ledger.getState().activeContainers, 0);
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

test("an ambiguous submit is reconciled and retried once instead of losing the flag", async () => {
  let submitCalls = 0;
  const browser = fakeBrowser();
  const sessionId = `tools-submit-retry-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
  const controller = new BenchmarkController({
    baseUrl: "https://bench.test",
    token: "test",
    fetchImpl: async (input, init) => {
      const url = new URL(input);
      const method = init?.method ?? "GET";
      if (url.pathname === "/openapi/v1/challenges" && method === "GET") {
        return new Response(JSON.stringify([platformChallenge("ch-1", { container_status: "available", container_addr: ["a"] })]), { status: 200 });
      }
      if (url.pathname === "/openapi/v1/challenges/submit") {
        submitCalls += 1;
        if (submitCalls === 1) {
          const aborted = new Error("connection dropped");
          aborted.name = "AbortError";
          throw aborted;
        }
        return new Response(JSON.stringify({ correct: true, awarded: 100, cumulative_score: 100, correct_flag_count: 1, total_flag_count: 1, matched_flag_index: 0 }), { status: 200 });
      }
      if (url.pathname === "/openapi/v1/challenges/close") {
        return new Response(JSON.stringify({ unique_code: "ch-1", closed: true }), { status: 200 });
      }
      return new Response(JSON.stringify({ message: "unexpected" }), { status: 500 });
    }
  });
  const ledger = await new BenchmarkLedger(sessionId).initialize();
  await ledger.syncFromPlatform([platformChallenge("ch-1", { container_status: "available", container_addr: ["a"] })], true, "ip");
  await ledger.acquire("ch-1", "main", ["a"]);
  const tool = createBenchmarkControlTool(controller, ledger, browser, () => "main");
  const result = await execute(tool, { action: "submit", uniqueCode: "ch-1", flag: "flag{network}" });
  assert.equal(submitCalls, 2);
  assert.match((result.content[0] as { text: string }).text, /CORRECT.*solved/);
  assert.equal(ledger.getChallenge("ch-1")?.status, "solved");
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

test("checkpoint updates the blackboard without extending the timer", async () => {
  const { tool } = await setupTool([
    ...VPN_ROUTES,
    { method: "GET", path: "/openapi/v1/challenges", body: [platformChallenge("ch-1")] },
    { method: "POST", path: "/openapi/v1/challenges/start", query: "ch-1", body: { unique_code: "ch-1", container_addr: ["a"] } }
  ]);
  await execute(tool, { action: "sync" });
  await execute(tool, { action: "acquire", uniqueCode: "ch-1" });
  const first = await execute(tool, { action: "checkpoint", uniqueCode: "ch-1", signal: "found login", signalKind: "new_surface" });
  assert.match((first.content[0] as { text: string }).text, /blackboard updated/);
  const strong = await execute(tool, { action: "checkpoint", uniqueCode: "ch-1", signal: "obtained admin access", signalKind: "privilege_change", evidenceRef: "request:req-1" });
  assert.match((strong.content[0] as { text: string }).text, /do not alter/);
  const repeat = await execute(tool, { action: "checkpoint", uniqueCode: "ch-1", signal: "admin access confirmed", signalKind: "privilege_change", evidenceRef: "request:req-1" });
  assert.match((repeat.content[0] as { text: string }).text, /blackboard updated/);
});

test("final-stage main and child handoffs preserve evidence without blindly inheriting plans", async () => {
  const ledger = await new BenchmarkLedger(`handoff-${Date.now()}`).initialize();
  await ledger.syncFromPlatform([platformChallenge("ch-1")], true, "ip");
  await ledger.acquire("ch-1", "main", ["old"]);
  await ledger.checkpoint("ch-1", "OBSERVED_FACT", ["TESTED_DIRECTION"], "OLD_NEXT_PROBE", "main", {
    currentApproach: "OLD_CURRENT_ROUTE", evidenceRef: "artifact:FACT_EVIDENCE", ruledOutFamilies: ["SUPPORTED_EXCLUSION"]
  });
  await ledger.defer("ch-1", "previous attempt unsuccessful", "OLD_NEXT_PROBE", "main");
  await ledger.confirmClosed("ch-1");
  const controller = {
    startChallenge: async () => ({ unique_code: "ch-1", container_addr: ["new"] }),
    closeChallenge: async () => ({ unique_code: "ch-1", closed: true })
  } as unknown as BenchmarkController;
  const tool = createBenchmarkControlTool(controller, ledger, fakeBrowser(), () => "main");
  assert.doesNotMatch(JSON.stringify(tool.parameters), /"nextProbe"|"currentApproach"/);
  const acquired = await execute(tool, { action: "acquire", uniqueCode: "ch-1" });
  const text = (acquired.content[0] as { text: string }).text;
  assert.match(text, /OBSERVED_FACT|FACT_EVIDENCE/);
  assert.match(text, /TESTED_DIRECTION/);
  assert.match(text, /SUPPORTED_EXCLUSION/);
  assert.match(text, /Preserve valid partial solutions/);
  assert.doesNotMatch(text, /OLD_NEXT_PROBE|OLD_CURRENT_ROUTE/);
  const recorded = await execute(tool, { action: "checkpoint", uniqueCode: "ch-1", signal: "new observation", nextProbe: "UNWANTED_NEXT", currentApproach: "UNWANTED_ROUTE" });
  assert.doesNotMatch(JSON.stringify(recorded), /OLD_NEXT_PROBE|UNWANTED_NEXT|UNWANTED_ROUTE/);
  assert.notEqual(ledger.getChallenge("ch-1")!.currentApproach, "UNWANTED_ROUTE");
  assert.notEqual(ledger.getChallenge("ch-1")!.nextProbe, "UNWANTED_NEXT");
  const deferred = await execute(tool, { action: "defer", uniqueCode: "ch-1", reason: "observation recorded", nextProbe: "UNWANTED_NEXT" });
  assert.doesNotMatch(JSON.stringify(deferred), /OLD_NEXT_PROBE|UNWANTED_NEXT/);
  let brief = "";
  const assign = createAssignBenchmarkChallengeTool(controller, ledger, async (task) => { brief = task; return { taskId: "fresh" }; });
  await execute(assign, { uniqueCode: "ch-1" });
  assert.match(brief, /OBSERVED_FACT/);
  assert.match(brief, /FACT_EVIDENCE/);
  assert.match(brief, /Preserve valid partial solutions/);
  assert.doesNotMatch(brief, /OLD_NEXT_PROBE|OLD_CURRENT_ROUTE|UNWANTED_NEXT|UNWANTED_ROUTE|NEXT:/);
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
  assert.match((result.content[0] as { text: string }).text, /unavailable on the first attempt/);
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
  assert.match((own.content[0] as { text: string }).text, /blackboard updated/);
});

test("assign tool dispatches and passes uniqueCode to spawnSubagent", async () => {
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
  const tool = createAssignBenchmarkChallengeTool(controller, ledger, async (task, uniqueCode, _containerAddrs, reservationOwner) => {
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
  assert.match(dispatched, /FLAG: exact captured flag string/);
  assert.equal(passedUniqueCode, "ch-1", "uniqueCode must be passed to spawnSubagent for child binding");
  assert.equal(ledger.getChallenge("ch-1")?.status, "running");
});

test("assign tool reports a SubAgent cancelled during dispatch as not assigned", async () => {
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
  const tool = createAssignBenchmarkChallengeTool(controller, ledger, async (_task, uniqueCode, _containerAddrs, reservationOwner) => {
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
  assert.match(text, /reassign/);
  assert.equal(ledger.getChallenge("ch-1")?.status, "deferred", "released challenge returns to the pool");
  assert.equal(ledger.getChallenge("ch-1")?.owner, null);
});


test("unknown submit is queued, survives release, and becomes accepted after connectivity recovers", async () => {
  const ledger = await new BenchmarkLedger(`pending-${Date.now()}`).initialize();
  await ledger.syncFromPlatform([platformChallenge("ch-1")], true, "ip");
  await ledger.acquire("ch-1", "main", ["a"]);
  let submits = 0;
  const controller = {
    listChallenges: async () => [platformChallenge("ch-1")],
    submitFlag: async () => {
      if (++submits <= 2) throw new BenchmarkError("connection_error", "fixture disconnected");
      return { unique_code: "ch-1", correct: true, awarded: 100, cumulative_score: 100, correct_flag_count: 1, total_flag_count: 1, matched_flag_index: 0 };
    },
    closeChallenge: async () => ({})
  } as unknown as BenchmarkController;
  const tool = createBenchmarkControlTool(controller, ledger, fakeBrowser(), () => "main");
  const first = await execute(tool, { action: "submit", uniqueCode: "ch-1", flag: "fixture-answer" });
  assert.match(JSON.stringify(first), /outcomeUnknown/);
  assert.equal(submits, 2);
  assert.equal(ledger.hasTriedFlag("ch-1", "fixture-answer"), false);
  await execute(tool, { action: "submit", uniqueCode: "ch-1", flag: "fixture-answer" });
  assert.equal(submits, 2, "manual duplicates must not bypass backoff");
  await ledger.defer("ch-1", "covered", undefined, "main");
  await ledger.confirmClosed("ch-1");
  await retryPendingSubmissions(controller, ledger, Date.now() + 31_000);
  assert.equal(submits, 3);
  assert.equal(ledger.getChallenge("ch-1")!.isCompleted, true);
  assert.equal(ledger.hasTriedFlag("ch-1", "fixture-answer"), true);
  assert.equal(hasPendingSubmissions(ledger), false);
  assert.ok(!ledger.getChallenge("ch-1")!.blackboard.some((entry) => entry.evidenceRef.startsWith("platform:pending:")));
});

test("pending confirmation is bounded without converting unknown outcomes into incorrect flags", async () => {
  const ledger = await new BenchmarkLedger(`pending-bounded-${Date.now()}`).initialize();
  await ledger.syncFromPlatform([platformChallenge("ch-1")], true, "ip");
  let checks = 0;
  const controller = { listChallenges: async () => { checks++; throw new BenchmarkError("connection_error", "fixture offline"); } } as unknown as BenchmarkController;
  enqueuePendingSubmission(ledger, "ch-1", "unknown-answer", "main", 0);
  for (const now of [30_000, 90_000, 210_000, 400_000]) await retryPendingSubmissions(controller, ledger, now);
  assert.equal(checks, 3);
  assert.equal(pendingSubmission(ledger, "ch-1", "unknown-answer")!.exhausted, true);
  assert.equal(ledger.hasTriedFlag("ch-1", "unknown-answer"), false);
  assert.equal(ledger.getMetrics().totalWrongSubmissions, 0);
});

test("a different accepted flag cannot falsely confirm a pending candidate", async () => {
  const ledger = await new BenchmarkLedger(`pending-multiple-${Date.now()}`).initialize();
  await ledger.syncFromPlatform([platformChallenge("ch-1", { flag_count: 2 })], true, "ip");
  await ledger.acquire("ch-1", "main", ["a"]);
  enqueuePendingSubmission(ledger, "ch-1", "pending-answer", "main", 0);
  let submits = 0;
  const controller = {
    listChallenges: async () => [platformChallenge("ch-1", { flag_count: 2, correct_flag_count: 1, container_status: "available", container_addr: ["a"] })],
    submitFlag: async () => { submits++; return { unique_code: "ch-1", correct: false, awarded: 0, cumulative_score: 100, correct_flag_count: 1, total_flag_count: 2, matched_flag_index: null }; }
  } as unknown as BenchmarkController;
  await retryPendingSubmissions(controller, ledger, 30_000);
  assert.equal(submits, 1, "an increased count cannot identify this candidate");
  assert.equal(ledger.getMetrics().totalWrongSubmissions, 1);
  assert.equal(pendingSubmission(ledger, "ch-1", "pending-answer"), undefined);
});

test("final three reuse one environment through repeated worker handoffs, including failed dispatch", async () => {
  const ledger = await new BenchmarkLedger(`endgame-reuse-${Date.now()}`).initialize();
  const codes = ["web", "portal", "binary"];
  await ledger.syncFromPlatform(codes.map((code) => platformChallenge(code)), true, "ip");
  for (const code of codes) {
    await ledger.acquire(code, "main", [code]);
    await ledger.defer(code, "coverage done", undefined, "main");
    await ledger.confirmClosed(code);
  }
  await ledger.acquire("web", "main", ["web-live"]);
  await ledger.acquire("portal", "subagent:slow", ["portal-live"]);
  let starts = 0;
  let closes = 0;
  const controller = {
    startChallenge: async () => { starts++; return { unique_code: "binary", container_addr: ["binary-live"] }; },
    closeChallenge: async () => { closes++; return { closed: true }; }
  } as unknown as BenchmarkController;
  let worker = 0;
  const assign = createAssignBenchmarkChallengeTool(controller, ledger, async (_task, code, addresses, reservation) => {
    const id = `worker-${++worker}`;
    assert.deepEqual(addresses, ["binary-live"]);
    await ledger.bindOwner(code, reservation, `subagent:${id}`);
    return { taskId: id };
  });
  for (let i = 0; i < 10; i++) {
    assert.deepEqual(ledger.candidates().map((challenge) => challenge.uniqueCode), ["binary"]);
    const assigned = await execute(assign, { uniqueCode: "binary" });
    assert.equal((assigned.details as { assigned: boolean }).assigned, true);
    const child = createBenchmarkControlTool(controller, ledger, fakeBrowser(), () => `subagent:worker-${worker}`, "binary");
    const released = await execute(child, { action: "defer", reason: "handoff evidence to next worker" });
    assert.equal((released.details as { environmentPreserved: boolean }).environmentPreserved, true);
    assert.equal(ledger.getState().activeContainers, 3, "preserved containers still occupy slots");
    assert.equal(ledger.getChallenge("binary")?.owner, null);
  }
  assert.equal(starts, 1);
  assert.equal(closes, 0);
  const failedAssign = createAssignBenchmarkChallengeTool(controller, ledger, async () => { throw new Error("synthetic dispatch failure"); });
  await execute(failedAssign, { uniqueCode: "binary" });
  assert.equal(ledger.getChallenge("binary")?.status, "orphaned");
  assert.equal(starts, 1);
  assert.equal(closes, 0);
  await execute(assign, { uniqueCode: "binary" });
  const ended = await ledger.releaseOnSubagentExit("binary", "subagent exited with status=completed", `subagent:worker-${worker}`);
  assert.equal(ended.status, "orphaned", "completion cleanup must preserve the environment too");
  assert.equal(ended.pendingStatus, undefined);
});

test("environment reset is explicit, owner-gated, records evidence, and starts fresh only on reacquire", async () => {
  const sessionId = `endgame-reset-${Date.now()}`;
  const ledger = await new BenchmarkLedger(sessionId).initialize();
  await ledger.syncFromPlatform([platformChallenge("binary")], true, "ip");
  await ledger.acquire("binary", "main", ["coverage"]);
  await ledger.defer("binary", "coverage done", undefined, "main");
  await ledger.confirmClosed("binary");
  await ledger.acquire("binary", "main", ["preserved"]);
  await ledger.defer("binary", "handoff", undefined, "main");
  const recovered = await new BenchmarkLedger(sessionId).initialize();
  assert.equal(recovered.getState().activeContainers, 1);
  let starts = 0;
  let closes = 0;
  let failClose = false;
  const controller = {
    startChallenge: async () => { starts++; return { unique_code: "binary", container_addr: ["fresh"] }; },
    closeChallenge: async () => { closes++; if (failClose) throw new Error("synthetic close error"); return { closed: true }; }
  } as unknown as BenchmarkController;
  const tool = createBenchmarkControlTool(controller, recovered, fakeBrowser(), () => "main");
  await execute(tool, { action: "acquire", uniqueCode: "binary" });
  assert.equal(starts, 0, "main also reuses a preserved container after runtime restart");
  for (const params of [{}, { reason: "service broken" }, { evidenceRef: "artifact:health.txt" }]) {
    await assert.rejects(() => execute(tool, { action: "reset_environment", uniqueCode: "binary", ...params }), /requires a concrete failure reason and evidenceRef/);
  }
  const stranger = createBenchmarkControlTool(controller, recovered, fakeBrowser(), () => "subagent:other", "binary");
  await assert.rejects(() => execute(stranger, { action: "reset_environment", reason: "broken", evidenceRef: "artifact:health.txt" }), /own/i);
  assert.equal(closes, 0);
  assert.equal(recovered.getChallenge("binary")?.owner, "main");
  await execute(tool, { action: "reset_environment", uniqueCode: "binary", reason: "process crashed; connection refused", evidenceRef: "artifact:health.txt" });
  assert.equal(closes, 1);
  assert.equal(starts, 0);
  assert.equal(recovered.getChallenge("binary")?.status, "deferred");
  assert.match(recovered.getChallenge("binary")!.approachHistory.at(-1)!.stopReason, /artifact:health.txt/);
  await execute(tool, { action: "acquire", uniqueCode: "binary" });
  assert.equal(starts, 1);
  assert.deepEqual(recovered.getChallenge("binary")?.containerAddrs, ["fresh"]);
  failClose = true;
  await execute(tool, { action: "reset_environment", uniqueCode: "binary", reason: "process failed again", evidenceRef: "artifact:health2.txt" });
  assert.equal(recovered.getChallenge("binary")?.status, "closing", "failed reset stays pending reconciliation");
  assert.equal(recovered.getChallenge("binary")?.closeFailureRecorded, true);
});
