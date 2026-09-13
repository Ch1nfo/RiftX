import assert from "node:assert/strict";
import test from "node:test";
import { abortBenchmarkAttempt, startBenchmarkAttemptWatchdog } from "./attempt-watchdog";
import { BenchmarkLedger, type ChallengeOwner } from "./ledger";
import type { Challenge } from "./controller";

const minute = 60_000;
function deferred() {
  let resolve!: () => void;
  const promise = new Promise<void>((done) => { resolve = done; });
  return { promise, resolve };
}

async function fixture(owner: Exclude<ChallengeOwner, null> = "main") {
  let now = 1000;
  const ledger = new BenchmarkLedger("watchdog-unit", () => now);
  // Exercise real ledger transitions without writing a user's runtime directory.
  Object.defineProperty(ledger, "writeStore", { value: async () => undefined });
  await ledger.syncFromPlatform(["a", "b"].map((code) => ({
    unique_code: code, description: "fixture", difficulty: "easy", level: 1,
    total_score: 100, flag_count: 4, correct_flag_count: 0, is_completed: false,
    container_status: "stopped", container_addr: []
  } satisfies Challenge)), true, "127.0.0.1");
  for (const code of ["a", "b"]) {
    await ledger.acquire(code, "main", ["127.0.0.1:80"]);
    await ledger.defer(code, "fixture coverage ended", undefined, "main", true);
    await ledger.confirmClosed(code);
  }
  await ledger.acquire("a", owner, ["127.0.0.1:80"]);
  return { ledger, owner, setMinute: (value: number) => { now = 1000 + value * minute; } };
}

for (const owner of ["main", "subagent:child"] as const) {
  test(`watchdog warns and forcibly rotates ${owner} without another model/tool call`, async (t) => {
    const f = await fixture(owner);
    const calls: string[] = [];
    const timeoutStarts: Array<number | null> = [];
    const watchdog = startBenchmarkAttemptWatchdog({
      ...f, controller: { closeChallenge: async (code) => {
        assert.equal(f.ledger.getChallenge(code)?.status, "closing");
        calls.push("close"); return { unique_code: code, closed: true };
      } },
      isStopping: () => false, warn: async () => { calls.push("warning"); },
      stopWorker: async () => { calls.push("stop"); },
      event: (event, attempt) => { if (event === "attempt_timeout") timeoutStarts.push(attempt!.currentAttemptStartedAt); }
    });
    t.after(() => watchdog.dispose());
    f.setMinute(25);
    await watchdog.check(); await watchdog.check();
    assert.deepEqual(calls, ["warning"]);
    f.setMinute(30);
    await Promise.all([watchdog.check(), watchdog.check()]);
    assert.deepEqual(calls, ["warning", "stop", "close"]);
    assert.deepEqual(timeoutStarts, [1000]);
    const state = f.ledger.getChallenge("a")!;
    assert.equal(state.status, "deferred");
    assert.equal(state.owner, null);
    assert.equal(f.ledger.getState().activeContainers, 0);
    assert.equal(state.approachHistory.at(-1)?.attemptNumber, 2);
    assert.equal(state.blackboard.at(-1)?.kind, "attempt_end");
    assert.equal(f.ledger.candidates(1)[0]?.uniqueCode, "b");
  });
}

test("late accepted progress extends a revisit once and watchdog stops it at 40 minutes", async (t) => {
  const f = await fixture();
  let stopped = 0;
  const watchdog = startBenchmarkAttemptWatchdog({
    ...f, controller: { closeChallenge: async (code) => ({ unique_code: code, closed: true }) },
    isStopping: () => false, warn: async () => undefined, stopWorker: async () => { stopped++; }
  });
  t.after(() => watchdog.dispose());
  f.setMinute(29);
  await f.ledger.recordSubmission("a", "fixture-flag-one", true, 25, 1, 0, "main");
  f.setMinute(30);
  await watchdog.check();
  assert.equal(stopped, 0);
  f.setMinute(39);
  await f.ledger.recordSubmission("a", "fixture-flag-two", true, 50, 2, 1, "main");
  f.setMinute(40);
  await watchdog.check();
  assert.equal(stopped, 1);
  assert.equal(f.ledger.getChallenge("a")?.correctFlagCount, 2);
});

test("in-flight submission settles before timeout releases its owner", async (t) => {
  const f = await fixture();
  const entered = deferred();
  const reply = deferred();
  let stopped = false;
  const submission = f.ledger.runChallengeAction("a", async () => {
    entered.resolve();
    await reply.promise;
    await f.ledger.recordSubmission("a", "fixture-late-flag", true, 25, 1, 0, "main");
  });
  await entered.promise;
  const watchdog = startBenchmarkAttemptWatchdog({
    ...f, controller: { closeChallenge: async (code) => ({ unique_code: code, closed: true }) },
    isStopping: () => false, warn: async () => undefined, stopWorker: async () => { stopped = true; }
  });
  t.after(() => watchdog.dispose());
  f.setMinute(30);
  const expiry = watchdog.check();
  await Promise.resolve();
  assert.equal(stopped, false);
  reply.resolve();
  await Promise.all([submission, expiry]);
  assert.equal(stopped, true);
  assert.equal(f.ledger.getChallenge("a")?.correctFlagCount, 1);
  assert.equal(f.ledger.getChallenge("a")?.approachHistory.at(-1)?.flagsAfter, 1);
});

test("timeout releases the challenge action lock before draining queued control tools", { timeout: 2000 }, async () => {
  const f = await fixture();
  const events: string[] = [];
  const watchdog = startBenchmarkAttemptWatchdog({
    ...f, controller: { closeChallenge: async (code) => { events.push("closed"); return { unique_code: code, closed: true }; } },
    isStopping: () => false, warn: async () => undefined,
    stopWorker: () => f.ledger.runChallengeAction("a", async () => { events.push("queued-tool-drained"); })
  });
  f.setMinute(30);
  const keepAlive = setTimeout(() => undefined, 1900);
  try {
    await watchdog.check();
    assert.deepEqual(events, ["closed", "queued-tool-drained"]);
  } finally { clearTimeout(keepAlive); watchdog.dispose(); }
});

test("a stale timeout cannot stop the owner that replaced its worker", async (t) => {
  const f = await fixture("subagent:old");
  const entered = deferred();
  const reply = deferred();
  const held = f.ledger.runChallengeAction("a", async () => { entered.resolve(); await reply.promise; });
  await entered.promise;
  let stopped = false;
  const watchdog = startBenchmarkAttemptWatchdog({
    ...f, controller: { closeChallenge: async () => { throw new Error("must not close"); } },
    isStopping: () => false, warn: async () => undefined, stopWorker: async () => { stopped = true; }
  });
  t.after(() => watchdog.dispose());
  f.setMinute(30);
  const expiry = watchdog.check();
  await f.ledger.bindOwner("a", "subagent:old", "subagent:new");
  reply.resolve();
  await Promise.all([held, expiry]);
  assert.equal(stopped, false);
  assert.equal(f.ledger.getChallenge("a")?.owner, "subagent:new");
});

test("a queued timeout cannot expire a later attempt by the same owner", async (t) => {
  const f = await fixture();
  const entered = deferred(); const reply = deferred();
  const held = f.ledger.runChallengeAction("a", async () => { entered.resolve(); await reply.promise; });
  await entered.promise;
  let stopped = false;
  const watchdog = startBenchmarkAttemptWatchdog({
    ...f, controller: { closeChallenge: async () => { throw new Error("must not close"); } },
    isStopping: () => false, warn: async () => undefined, stopWorker: async () => { stopped = true; }
  });
  t.after(() => watchdog.dispose());
  f.setMinute(30);
  const expiry = watchdog.check();
  await f.ledger.defer("a", "old attempt ended", undefined, "main", true);
  await f.ledger.confirmClosed("a");
  await f.ledger.acquire("b", "main", ["127.0.0.1:80"]);
  await f.ledger.defer("b", "fixture retry", undefined, "main", true);
  await f.ledger.confirmClosed("b");
  f.setMinute(31);
  await f.ledger.acquire("a", "main", ["127.0.0.1:80"]);
  f.setMinute(62);
  reply.resolve();
  await Promise.all([held, expiry]);
  assert.equal(stopped, false);
  assert.equal(f.ledger.getChallenge("a")?.status, "running");
  assert.equal(f.ledger.getChallenge("a")?.attemptCount, 3);
});

test("timer autonomously expires an idle model without an explicit check", { timeout: 3000 }, async () => {
  const f = await fixture();
  const closed = deferred();
  f.setMinute(30);
  const watchdog = startBenchmarkAttemptWatchdog({
    ...f, pollMs: 5,
    controller: { closeChallenge: async (code) => ({ unique_code: code, closed: true }) },
    isStopping: () => false, warn: async () => undefined, stopWorker: async () => undefined,
    event: (event) => { if (event === "attempt_closed") closed.resolve(); }
  });
  // Keep the test alive; production already has the runner's keepAlive timer.
  const keepAlive = setTimeout(() => undefined, 2500);
  try { await closed.promise; assert.equal(f.ledger.getChallenge("a")?.status, "deferred"); }
  finally { clearTimeout(keepAlive); watchdog.dispose(); }
});

test("parent reconciles a failed close after the timed-out child was disposed", async () => {
  const f = await fixture("subagent:child");
  const child = startBenchmarkAttemptWatchdog({
    ...f, controller: { closeChallenge: async () => { throw new Error("temporary fixture failure"); } },
    isStopping: () => false, warn: async () => undefined, stopWorker: async () => undefined
  });
  f.setMinute(30);
  await child.check();
  child.dispose();
  assert.equal(f.ledger.getChallenge("a")?.status, "closing");
  assert.equal(f.ledger.getState().activeContainers, 1);
  const closed = deferred();
  const parent = startBenchmarkAttemptWatchdog({
    ledger: f.ledger, owner: "main",
    controller: { closeChallenge: async (code) => ({ unique_code: code, closed: true }) },
    isStopping: () => false, warn: async () => undefined, stopWorker: async () => undefined,
    event: (event) => { if (event === "attempt_closed") closed.resolve(); }
  });
  try {
    await parent.check();
    await closed.promise;
    assert.equal(f.ledger.getChallenge("a")?.status, "deferred");
    assert.equal(f.ledger.getState().activeContainers, 0);
  } finally { parent.dispose(); }
});

test("main timeout cancels its own model, tools and browser without aborting children", async () => {
  const draining = deferred();
  const calls: string[] = [];
  const record = {
    gate: { rejectAll: () => { calls.push("gate"); } },
    session: {
      abortBash: () => { calls.push("bash"); }, abortCompaction: () => { calls.push("compaction"); },
      abort: async () => { calls.push("model"); await draining.promise; }
    },
    browser: { close: async () => { calls.push("browser"); } },
    subagents: { abortAll: async () => { calls.push("children"); } },
    aborting: false, abortEpoch: 0, benchmarkAttemptTimeoutEpoch: 0, abortPromise: undefined as Promise<void> | undefined
  };
  const stopping = abortBenchmarkAttempt(record);
  assert.equal(record.aborting, true);
  assert.equal(record.benchmarkAttemptTimeoutEpoch, 1);
  assert.ok(record.abortPromise);
  assert.deepEqual(calls, ["gate", "bash", "compaction", "model", "browser"]);
  draining.resolve();
  await stopping;
  assert.equal(record.aborting, false);
  assert.equal(record.abortPromise, undefined);
});
