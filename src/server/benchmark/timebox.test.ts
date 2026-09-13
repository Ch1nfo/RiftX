import assert from "node:assert/strict";
import test from "node:test";
import type { BenchmarkLedger, ChallengeState } from "./ledger";
import { checkBenchmarkToolExecutionGuard, installBenchmarkTimeboxGate } from "./timebox";
import { BrowserManager } from "@/browser/runtime/browser-manager";
import { createCrawlTool } from "@/browser/tools/crawl";

const MINUTE = 60_000;

function fixture(attemptCount = 1, clock = () => Date.now()) {
  const startedAt = clock();
  const challenge = {
    uniqueCode: "fixture", owner: "main", status: "running", currentAttemptStartedAt: startedAt, attemptCount
  } as ChallengeState;
  let deadlineAt = startedAt + 30 * MINUTE;
  const ledger = {
    getChallenge: () => challenge,
    budgetForOwner: (owner: string) => challenge.owner === owner && ["running", "reserved"].includes(challenge.status) ? {
      challenge,
      budget: {
        firstAttempt: attemptCount === 1, elapsedMs: clock() - startedAt,
        warningDue: clock() >= startedAt + 25 * MINUTE, expired: clock() >= deadlineAt,
        deadlineAt, remainingMs: Math.max(0, deadlineAt - clock()), limitMs: deadlineAt - startedAt,
        extensionUsed: deadlineAt > startedAt + 30 * MINUTE
      }
    } : undefined
  } as unknown as BenchmarkLedger;
  return { ledger, challenge, get deadlineAt() { return deadlineAt; }, extend() { deadlineAt = startedAt + 40 * MINUTE; } };
}

for (const attemptCount of [1, 2, 4]) {
  test(`solving tools are blocked at the deadline in attempt ${attemptCount}`, async () => {
    let now = 1_000;
    const { ledger } = fixture(attemptCount, () => now);
    let executed = 0;
    const tool = { name: "read", execute: async (): Promise<unknown> => { executed++; return { content: [] }; } };
    installBenchmarkTimeboxGate(tool, ledger, "main");
    await tool.execute();
    now += 30 * MINUTE;
    const denied = await tool.execute() as { details: { timeboxExpired: boolean } };
    assert.equal(denied.details.timeboxExpired, true);
    assert.equal(executed, 1);
  });
}

for (const name of ["bash", "browser", "crawl", "read", "edit", "write"]) {
  test(`${name} receives a cancellation signal when a revisit deadline expires`, async (t) => {
    t.mock.timers.enable({ apis: ["Date", "setTimeout"], now: 1_000 });
    const { ledger } = fixture(2);
    t.mock.timers.tick(30 * MINUTE - 100);
    let aborted = false;
    const tool = { name, execute: async (_id: string, _params: unknown, signal?: AbortSignal): Promise<unknown> =>
      new Promise((_resolve, reject) => signal!.addEventListener("abort", () => { aborted = true; reject(signal!.reason); }, { once: true })) };
    installBenchmarkTimeboxGate(tool, ledger, "main");
    const pending = tool.execute("fixture-call", {});
    t.mock.timers.tick(100);
    const result = await pending as { details: { timeboxExpired: boolean }; isError: boolean };
    assert.equal(aborted, true);
    assert.equal(result.details.timeboxExpired, true);
    assert.equal(result.isError, true);
  });
}

test("a long bash call honors a granted extension instead of the original thirty-minute timer", async (t) => {
  t.mock.timers.enable({ apis: ["Date", "setTimeout"], now: 1_000 });
  const run = fixture(2);
  t.mock.timers.tick(25 * MINUTE);
  let aborted = false;
  const tool = { name: "bash", execute: async (_id: string, params: unknown, signal?: AbortSignal): Promise<unknown> => {
    assert.deepEqual(params, { timeout: 900 }, "the tool timeout must not be frozen to the old attempt remainder");
    return new Promise((_resolve, reject) => signal!.addEventListener("abort", () => { aborted = true; reject(signal!.reason); }, { once: true }));
  } };
  installBenchmarkTimeboxGate(tool, run.ledger, "main");
  const pending = tool.execute("fixture-call", { timeout: 900 });
  t.mock.timers.tick(2 * MINUTE);
  run.extend();
  t.mock.timers.tick(3 * MINUTE);
  assert.equal(aborted, false, "the original deadline must re-read the extended ledger budget");
  t.mock.timers.tick(10 * MINUTE);
  const result = await pending as { details: { timeboxExpired: boolean } };
  assert.equal(aborted, true);
  assert.equal(result.details.timeboxExpired, true);
});

for (const name of ["bash", "edit"]) {
  test(`a queued ${name} rechecks the deadline after its lock is acquired`, async () => {
    let now = 1_000;
    const run = fixture(2, () => now);
    let release!: () => void;
    const lock = new Promise<void>((resolve) => { release = resolve; });
    let executed = 0;
    const tool = { name, execute: async (): Promise<unknown> => {
      await lock;
      checkBenchmarkToolExecutionGuard();
      executed++;
      return { content: [] };
    } };
    installBenchmarkTimeboxGate(tool, run.ledger, "main");
    const pending = tool.execute();
    now += 30 * MINUTE;
    release();
    const result = await pending as { details: { timeboxExpired: boolean } };
    assert.equal(result.details.timeboxExpired, true);
    assert.equal(executed, 0);
  });
}

test("the browser queue checks the live deadline at operation execution", async () => {
  let now = 1_000;
  const run = fixture(2, () => now);
  const browser = new BrowserManager({});
  let release!: () => void;
  const lock = new Promise<void>((resolve) => { release = resolve; });
  const blocker = browser.run(() => lock);
  let executed = 0;
  const tool = { name: "browser", execute: (_id: string, _params: unknown, signal?: AbortSignal): Promise<unknown> =>
    browser.run(async () => { executed++; return { content: [] }; }, signal) };
  installBenchmarkTimeboxGate(tool, run.ledger, "main");
  const pending = tool.execute("fixture-call", {});
  now += 30 * MINUTE;
  release();
  await blocker;
  const result = await pending as { details: { timeboxExpired: boolean } };
  assert.equal(result.details.timeboxExpired, true);
  assert.equal(executed, 0);
  assert.equal(await browser.run(async () => "cleanup-allowed"), "cleanup-allowed");
});

test("an old queued operation cannot execute in a fresh attempt owned by the same worker", async () => {
  const run = fixture(2);
  let release!: () => void;
  const lock = new Promise<void>((resolve) => { release = resolve; });
  let executed = 0;
  const tool = { name: "write", execute: async (): Promise<unknown> => {
    await lock;
    checkBenchmarkToolExecutionGuard();
    executed++;
    return { content: [] };
  } };
  installBenchmarkTimeboxGate(tool, run.ledger, "main");
  const pending = tool.execute();
  run.challenge.currentAttemptStartedAt!++;
  release();
  const result = await pending as { details: { challengeReleased: boolean } };
  assert.equal(result.details.challengeReleased, true);
  assert.equal(executed, 0);
});

test("crawl preserves completed page evidence when the next queued page is denied", async () => {
  let now = 1_000;
  const run = fixture(2, () => now);
  now += 30 * MINUTE - 1;
  const browser = new BrowserManager({});
  let navigations = 0;
  let probes = 0;
  Object.assign(browser, {
    navigateWithDeadline: async () => { navigations++; return { text: "fixture" }; },
    evaluateWithDeadline: async () => {
      if (++probes === 1) return JSON.stringify({ url: "http://fixture.invalid/", title: "fixture", links: [{ href: "http://fixture.invalid/next", text: "next" }], forms: [], metaGenerator: "", routes: [] });
      now += 10;
      return "[]";
    }
  });
  const tool = createCrawlTool(browser);
  installBenchmarkTimeboxGate(tool as never, run.ledger, "main");
  const result = await (tool.execute as unknown as (id: string, params: unknown) => Promise<{ content: unknown[]; details: { pages: number; timeboxExpired?: boolean } }>)("fixture-call", { entry: "http://fixture.invalid/", maxPages: 2 });
  assert.equal(navigations, 1);
  assert.equal(result.details.pages, 1);
  assert.equal(result.details.timeboxExpired, true);
  assert.ok(result.content.length >= 2);
});

test("checkpoint, submission and cleanup control paths remain callable after the deadline", async () => {
  let now = 1_000;
  const run = fixture(2, () => now);
  now += 30 * MINUTE;
  for (const name of ["benchmark_control", "checkpoint_progress", "assign_benchmark_challenge"]) {
    let executed = false;
    const tool = { name, execute: async () => { executed = true; return { content: [] }; } };
    installBenchmarkTimeboxGate(tool, run.ledger, "main", "fixture");
    await tool.execute();
    assert.equal(executed, true);
  }
});

test("a released assigned challenge cannot start another solving operation", async () => {
  const run = fixture(2);
  const tool = { name: "bash", execute: async (): Promise<unknown> => { throw new Error("must not execute"); } };
  installBenchmarkTimeboxGate(tool, run.ledger, "main", "fixture");
  run.challenge.owner = null;
  const result = await tool.execute() as { details: { challengeReleased: boolean } };
  assert.equal(result.details.challengeReleased, true);
});

test("caller cancellation stays distinct from an attempt timeout", async () => {
  const run = fixture(2);
  const caller = new AbortController();
  const tool = { name: "bash", execute: async (_id: string, _params: unknown, signal?: AbortSignal): Promise<unknown> =>
    new Promise((_resolve, reject) => signal!.addEventListener("abort", () => reject(signal!.reason), { once: true })) };
  installBenchmarkTimeboxGate(tool, run.ledger, "main");
  const pending = tool.execute("fixture-call", {}, caller.signal);
  caller.abort(new Error("fixture-caller-cancelled"));
  await assert.rejects(pending, /fixture-caller-cancelled/);
});
