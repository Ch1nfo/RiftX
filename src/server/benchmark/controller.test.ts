import test from "node:test";
import assert from "node:assert/strict";
import { BenchmarkController, BenchmarkError } from "./controller";

type Route = { method: string; path: string; query?: string; respond: () => { status: number; body: unknown } };

function makeController(routes: Route[]) {
  const calls: Array<{ method: string; path: string; query: string; body?: unknown; headers?: Record<string, string> }> = [];
  const controller = new BenchmarkController({
    baseUrl: "https://bench.test",
    token: "test-token",
    fetchImpl: async (input, init) => {
      const url = new URL(input);
      const method = init?.method ?? "GET";
      const query = url.searchParams.get("unique_code") ?? "";
      const path = url.pathname;
      const headers = init?.headers as Record<string, string> | undefined;
      calls.push({ method, path, query, body: init?.body ? JSON.parse(String(init.body)) : undefined, headers });
      const route = routes.find((candidate) => candidate.method === method && candidate.path === path && (candidate.query === undefined || candidate.query === query));
      if (!route) return new Response(JSON.stringify({ error: "not found", message: `${method} ${path}?${query}` }), { status: 404, headers: { Connection: "close" } });
      const result = route.respond();
      return new Response(JSON.stringify(result.body), { status: result.status, headers: { "Content-Type": "application/json", Connection: "close" } });
    }
  });
  return { controller, calls };
}

test("checkVpn succeeds when real VPN endpoint returns status=ok", async () => {
  const controller = new BenchmarkController({
    baseUrl: "https://bench.test",
    token: "t",
    vpnUrl: "http://vpn.test",
    fetchImpl: async (_input, _init) => new Response(JSON.stringify({ status: "ok", client_ip: "10.0.0.1" }), { status: 200, headers: { Connection: "close" } })
  });
  const result = await controller.checkVpn();
  assert.equal(result.ok, true);
  assert.equal(result.client_ip, "10.0.0.1");
});

test("checkVpn is explicitly skipped when the raw API provides no health endpoint", async () => {
  let called = false;
  const controller = new BenchmarkController({
    baseUrl: "https://bench.test",
    token: "t",
    vpnUrl: "",
    fetchImpl: async () => {
      called = true;
      throw new Error("should not be called");
    }
  });
  const result = await controller.checkVpn();
  assert.equal(result.status, "unchecked");
  assert.equal(result.ok, false);
  assert.equal(called, false);
});

test("checkVpn throws when base URL is unreachable", async () => {
  const controller = new BenchmarkController({
    baseUrl: "https://bench.test",
    token: "t",
    vpnUrl: "http://vpn.test",
    fetchImpl: async () => { throw new Error("ECONNREFUSED"); }
  });
  await assert.rejects(() => controller.checkVpn(), (error: BenchmarkError) => error.kind === "vpn_check_failed");
});

test("listChallenges handles bare array response (real contract)", async () => {
  const { controller } = makeController([
    { method: "GET", path: "/openapi/v1/challenges", respond: () => ({ status: 200, body: [
      { unique_code: "ch-1", description: "SQL injection", difficulty: "easy", level: 1, total_score: 100, flag_count: 2, correct_flag_count: 1, is_completed: false, container_status: "stopped", container_addr: [] },
      { unique_code: "ch-2", description: null, difficulty: "hard", level: 3, total_score: 300, flag_count: 1, correct_flag_count: 1, is_completed: true, container_status: "available", container_addr: ["10.0.0.5:8080"] }
    ] }) }
  ]);
  const challenges = await controller.listChallenges();
  assert.equal(challenges.length, 2);
  assert.equal(challenges[0].unique_code, "ch-1");
  assert.equal(challenges[0].flag_count, 2);
  assert.equal(challenges[0].level, 1);
  assert.equal(challenges[1].description, "");
  assert.equal(challenges[1].container_addr[0], "10.0.0.5:8080");
});

test("startChallenge uses unique_code query parameter (real contract)", async () => {
  const { controller, calls } = makeController([
    { method: "POST", path: "/openapi/v1/challenges/start", query: "ch-1", respond: () => ({ status: 200, body: { unique_code: "ch-1", container_addr: ["10.0.0.1:80", "10.0.0.1:443"] } }) }
  ]);
  const result = await controller.startChallenge("ch-1");
  assert.deepEqual(result.container_addr, ["10.0.0.1:80", "10.0.0.1:443"]);
  assert.equal(calls[0].path, "/openapi/v1/challenges/start");
  assert.equal(calls[0].query, "ch-1");
});

test("submitFlag posts to /openapi/v1/challenges/submit with unique_code in body (real contract)", async () => {
  const { controller, calls } = makeController([
    { method: "POST", path: "/openapi/v1/challenges/submit", respond: () => ({ status: 200, body: { unique_code: "ch-1", correct: true, awarded: 50, cumulative_score: 150, correct_flag_count: 2, total_flag_count: 3, matched_flag_index: 1 } }) }
  ]);
  const result = await controller.submitFlag("ch-1", "flag{test}");
  assert.equal(result.correct, true);
  assert.equal(result.matched_flag_index, 1);
  assert.equal(calls[0].path, "/openapi/v1/challenges/submit");
  assert.deepEqual(calls[0].body, { unique_code: "ch-1", flag: "flag{test}" });
});

test("getHint uses unique_code query parameter (real contract)", async () => {
  const { controller, calls } = makeController([
    { method: "GET", path: "/openapi/v1/challenges/hint", query: "ch-1", respond: () => ({ status: 200, body: { unique_code: "ch-1", hint: "check /backup" } }) }
  ]);
  const result = await controller.getHint("ch-1");
  assert.equal(result.hint, "check /backup");
  assert.equal(calls[0].query, "ch-1");
});

test("closeChallenge uses unique_code query parameter (real contract)", async () => {
  const { controller, calls } = makeController([
    { method: "POST", path: "/openapi/v1/challenges/close", query: "ch-1", respond: () => ({ status: 200, body: { unique_code: "ch-1", closed: true } }) }
  ]);
  const result = await controller.closeChallenge("ch-1");
  assert.equal(result.closed, true);
  assert.equal(calls[0].query, "ch-1");
});

test("auth uses BENCHMARK_TOKEN header, NOT Bearer (real contract)", async () => {
  let capturedHeaders: Record<string, string> = {};
  const controller = new BenchmarkController({
    baseUrl: "https://bench.test",
    token: "secret-token",
    fetchImpl: async (_input, init) => {
      capturedHeaders = init?.headers as Record<string, string>;
      return new Response("[]", { status: 200, headers: { "Content-Type": "application/json", Connection: "close" } });
    }
  });
  await controller.listChallenges();
  assert.equal(capturedHeaders.BENCHMARK_TOKEN, "secret-token");
  assert.equal(capturedHeaders.Authorization, undefined, "must NOT use Authorization Bearer");
});

test("classifies duplicate_submit from 409 + code=duplicate", async () => {
  const { controller } = makeController([
    { method: "POST", path: "/openapi/v1/challenges/submit", respond: () => ({ status: 409, body: { code: "duplicate", message: "already submitted" } }) }
  ]);
  await assert.rejects(() => controller.submitFlag("ch-1", "flag{dup}"), (error: BenchmarkError) => error.kind === "duplicate_submit");
});

test("classifies max-active from 409 + message", async () => {
  const { controller } = makeController([
    { method: "POST", path: "/openapi/v1/challenges/start", query: "ch-1", respond: () => ({ status: 409, body: { code: "invalid_state", message: "max active challenges reached" } }) }
  ]);
  await assert.rejects(() => controller.startChallenge("ch-1"), (error: BenchmarkError) => error.kind === "invalid_state_max_active");
});

test("classifies max-active from the API doc's Chinese phrasing (上限)", async () => {
  const { controller } = makeController([
    { method: "POST", path: "/openapi/v1/challenges/start", query: "ch-1", respond: () => ({ status: 409, body: { code: "invalid_state", message: "当前活跃的题目实例数已达到上限" } }) }
  ]);
  await assert.rejects(() => controller.startChallenge("ch-1"), (error: BenchmarkError) => error.kind === "invalid_state_max_active");
});

test("classifies task-ended from the API doc's Chinese phrasing (已结束)", async () => {
  const { controller } = makeController([
    { method: "POST", path: "/openapi/v1/challenges/start", query: "ch-1", respond: () => ({ status: 409, body: { code: "invalid_state", message: "任务已结束（超时过期或手动停止）" } }) }
  ]);
  await assert.rejects(() => controller.startChallenge("ch-1"), (error: BenchmarkError) => error.kind === "invalid_state_task_ended");
});

test("classifies invalid/missing token as not_found (doc: 404 task_not_found)", async () => {
  const { controller } = makeController([
    { method: "GET", path: "/openapi/v1/challenges", respond: () => ({ status: 404, body: { code: "task_not_found", message: "task does not exist" } }) }
  ]);
  await assert.rejects(() => controller.listChallenges(), (error: BenchmarkError) => error.kind === "not_found");
});

test("classifies task-ended from 409 + message", async () => {
  const { controller } = makeController([
    { method: "POST", path: "/openapi/v1/challenges/start", query: "ch-1", respond: () => ({ status: 409, body: { code: "invalid_state", message: "task has ended" } }) }
  ]);
  await assert.rejects(() => controller.startChallenge("ch-1"), (error: BenchmarkError) => error.kind === "invalid_state_task_ended");
});

test("classifies resource_unavailable from 503", async () => {
  const { controller } = makeController([
    { method: "POST", path: "/openapi/v1/challenges/start", query: "ch-1", respond: () => ({ status: 503, body: { code: "resource_unavailable", message: "no capacity" } }) }
  ]);
  await assert.rejects(() => controller.startChallenge("ch-1"), (error: BenchmarkError) => error.kind === "resource_unavailable");
});

test("connection error is classified", async () => {
  const controller = new BenchmarkController({
    baseUrl: "https://bench.test",
    token: "t",
    fetchImpl: async () => { throw new Error("ECONNREFUSED"); }
  });
  await assert.rejects(() => controller.listChallenges(), (error: BenchmarkError) => error.kind === "connection_error");
});
