import assert from "node:assert/strict";
import test from "node:test";
import { McpCallGuard } from "./call-guard";

test("limits calls per server and starts queued work when a slot is released", async () => {
  const guard = new McpCallGuard("demo", { maxConcurrent: 1, timeoutMs: 1_000 });
  let releaseFirst!: () => void;
  const first = guard.run(() => new Promise<void>((resolve) => { releaseFirst = resolve; }));
  let secondStarted = false;
  const second = guard.run(async () => { secondStarted = true; return "ok"; });
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(secondStarted, false);
  releaseFirst();
  await first;
  assert.equal(await second, "ok");
});

test("times out a call but keeps its semaphore slot until ignored work settles", async () => {
  const guard = new McpCallGuard("demo", { maxConcurrent: 1, timeoutMs: 10, failureThreshold: 10 });
  let settle!: () => void;
  const stuck = guard.run(() => new Promise<void>((resolve) => { settle = resolve; }));
  await assert.rejects(stuck, /timed out/);
  let nextStarted = false;
  const next = guard.run(async () => { nextStarted = true; });
  await new Promise((resolve) => setTimeout(resolve, 5));
  assert.equal(nextStarted, false);
  settle();
  await next;
  assert.equal(nextStarted, true);
});

test("opens after consecutive failures, cools down, and resets on success", async () => {
  let now = 100;
  const guard = new McpCallGuard("demo", { failureThreshold: 2, cooldownMs: 50, timeoutMs: 1_000, now: () => now });
  await assert.rejects(guard.run(async () => { throw new Error("one"); }), /one/);
  await assert.rejects(guard.run(async () => { throw new Error("two"); }), /two/);
  await assert.rejects(guard.run(async () => "blocked"), /circuit is open/);
  now = 151;
  assert.equal(await guard.run(async () => "recovered"), "recovered");
});

test("caller cancellation does not count as a server failure", async () => {
  let now = 100;
  const guard = new McpCallGuard("demo", { failureThreshold: 1, cooldownMs: 50, timeoutMs: 1_000, now: () => now });
  const controller = new AbortController();
  const cancelled = guard.run((signal) => new Promise((_resolve, reject) => {
    signal.addEventListener("abort", () => reject(new Error("cancelled")), { once: true });
  }), controller.signal);
  controller.abort(new Error("stop"));
  await assert.rejects(cancelled, /stop|cancelled/);
  assert.equal(await guard.run(async () => "still available"), "still available");
  now += 1;
});

test("attempts during the cooldown do not extend the circuit", async () => {
  let clock = 1_000;
  const guard = new McpCallGuard("demo", { now: () => clock, timeoutMs: 50, cooldownMs: 30_000 });
  const fail = () => guard.run(async () => { throw new Error("boom"); });
  for (let attempt = 0; attempt < 3; attempt++) await assert.rejects(fail());
  clock += 100;
  for (let attempt = 0; attempt < 5; attempt++) await assert.rejects(fail(), /circuit is open/);
  clock += 29_999;
  assert.equal(await guard.run(async () => "ok"), "ok", "original cooldown window must hold");
});

test("a failing half-open probe re-trips the circuit immediately", async () => {
  let clock = 1_000;
  const guard = new McpCallGuard("demo", { now: () => clock, timeoutMs: 50, cooldownMs: 30_000 });
  const fail = () => guard.run(async () => { throw new Error("boom"); });
  for (let attempt = 0; attempt < 3; attempt++) await assert.rejects(fail());
  clock += 30_001;
  await assert.rejects(fail(), /boom/);
  clock += 1;
  await assert.rejects(fail(), /circuit is open/);
  clock += 30_000;
  assert.equal(await guard.run(async () => "ok"), "ok", "recovers after the second cooldown");
});
