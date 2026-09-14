import assert from "node:assert/strict";
import test from "node:test";
import { randomUUID } from "node:crypto";
import { mkdtemp, rm } from "node:fs/promises";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { BenchmarkLedger, FIRST_ATTEMPT_LIMIT_MS } from "./ledger";
import { BenchmarkExecutionBinding, attemptContext, captureFence, type ExecutableTool } from "./fencing";
import { BenchmarkWorkspace } from "./workspace";
import { enqueuePendingSubmission, retryPendingSubmissions, hasPendingSubmissions } from "./pending-submissions";
import { BenchmarkController } from "./controller";
import { createBenchmarkControlTool } from "./tools/control-tool";
import type { BrowserManager } from "@/browser";
import type { FailureSource } from "./attempt-observation";

async function setup(t: test.TestContext, owner: "main" | `subagent:${string}` = "main") {
  const id = `fencing-${randomUUID()}`;
  t.after(() => BenchmarkLedger.destroy(id));
  let now = 1_000_000;
  const ledger = await new BenchmarkLedger(id, () => now).initialize();
  await ledger.syncFromPlatform([{ unique_code: "A", description: "synthetic", difficulty: "easy", level: 1, total_score: 100,
    flag_count: 2, correct_flag_count: 0, is_completed: false, container_status: "stopped", container_addr: [] }], true, "ip");
  const challenge = await ledger.acquire("A", owner, ["fixture"]);
  const binding = new BenchmarkExecutionBinding(ledger, owner, owner !== "main");
  t.after(() => binding.dispose());
  return { id, ledger, challenge, binding, advance: (ms: number) => { now += ms; } };
}
async function restart(ledger: BenchmarkLedger, owner: "main" | `subagent:${string}` = "main") {
  await ledger.defer("A", "unfinished", undefined, owner);
  await ledger.confirmClosed("A");
  await ledger.acquire("A", owner, ["fixture"]);
}

test("automatic control fence preserves explicit stale IDs and rejects a replacement generation", async (t) => {
  const { ledger, challenge, binding } = await setup(t);
  const fence = captureFence(challenge)!;
  assert.ok(fence.attemptId);
  assert.equal(fence.containerEpoch, 1);
  const controller = {} as BenchmarkController;
  const control = createBenchmarkControlTool(controller, ledger, {} as BrowserManager, () => "main") as unknown as ExecutableTool;
  binding.guard(control); binding.install(control);
  await control.execute!("valid", { action: "checkpoint", signal: "synthetic fact", evidenceRef: "synthetic:1" });
  assert.equal(challenge.blackboard.length, 1);
  await assert.rejects(control.execute!("old", { action: "checkpoint", signal: "bad", attemptId: "old" }), /STALE_ATTEMPT/);
  await assert.rejects(control.execute!("old-submit", { action: "submit", flag: "synthetic", evidenceRef: "synthetic:1", containerEpoch: 0 }), /STALE_ATTEMPT/);
  await restart(ledger);
  const before = JSON.stringify(challenge);
  await assert.rejects(control.execute!("obsolete", { action: "checkpoint", signal: "bad" }), /STALE_ATTEMPT/);
  assert.equal(JSON.stringify(challenge), before);
  assert.notEqual(challenge.attemptId, fence.attemptId);
  assert.equal(challenge.containerEpoch, 2);
});

test("a lock-queued workspace write is rejected when the container epoch changes", async (t) => {
  const { ledger, challenge, binding } = await setup(t);
  const root = await mkdtemp(join(tmpdir(), "riftx-fence-"));
  t.after(() => rm(root, { recursive: true, force: true }));
  const workspace = new BenchmarkWorkspace(root, "A", async () => {}, binding);
  let release!: () => void, started!: () => void;
  const running = new Promise<void>((resolve) => { started = resolve; });
  const lock = new Promise<void>((resolve) => { release = resolve; });
  let writes = 0;
  const write: ExecutableTool = { name: "write", execute: async () => { writes++; return {}; } };
  binding.guard(write);
  const guarded = write.execute!;
  write.execute = async (...args) => { started(); await lock; return guarded(...args); };
  workspace.install(write); binding.install(write);
  const queued = write.execute!("queued", {});
  await running;
  challenge.containerEpoch!++;
  await ledger.recordVpnCheck(true, "ip", true); // Persist/notify the generation change.
  release();
  await assert.rejects(queued, /STALE_ATTEMPT/);
  assert.equal(writes, 0);
});

test("old running mutation is aborted, while a started read can finish", async (t) => {
  const { ledger, binding } = await setup(t);
  let finishRead!: () => void;
  const read: ExecutableTool = { name: "read", execute: async () => new Promise<void>((resolve) => { finishRead = resolve; }) };
  let started!: () => void;
  const ready = new Promise<void>((resolve) => { started = resolve; });
  const mutation: ExecutableTool = { name: "browser", execute: async (_id, _params, signal) => {
    started(); return new Promise((_resolve, reject) => signal!.addEventListener("abort", () => reject(signal!.reason), { once: true }));
  } };
  binding.install(read); binding.install(mutation);
  const reading = read.execute!("r", {});
  const writing = mutation.execute!("w", {});
  const rejected = assert.rejects(writing, /STALE_ATTEMPT/);
  await ready;
  await ledger.defer("A", "done", undefined, "main");
  finishRead();
  await Promise.all([reading, rejected]);
});

test("pending submission cannot adopt a new attempt or reach platform after it changes", async (t) => {
  const { ledger } = await setup(t);
  enqueuePendingSubmission(ledger, "A", "synthetic", "main", 0);
  await restart(ledger);
  let calls = 0;
  const controller = { listChallenges: async () => { calls++; return []; }, submitFlag: async () => { calls++; } } as unknown as BenchmarkController;
  const before = JSON.stringify(ledger.getState());
  await retryPendingSubmissions(controller, ledger, 100_000);
  assert.equal(calls, 0);
  assert.equal(hasPendingSubmissions(ledger), false);
  assert.equal(JSON.stringify(ledger.getState()), before);
});

test("child scope stays fixed and a recovered live attempt preserves IDs", async (t) => {
  const { id, ledger, challenge, binding } = await setup(t, "subagent:test");
  const original = captureFence(challenge)!;
  const control: ExecutableTool = { name: "benchmark_control", execute: async (_id, input) => {
    const params = input as { uniqueCode: string; attemptId: string; containerEpoch: number };
    ledger.assertAttemptFence(params.uniqueCode, "subagent:test", params.attemptId, params.containerEpoch);
    return params;
  } };
  binding.install(control);
  const result = await control.execute!("child", { action: "checkpoint" });
  assert.equal((result as { attemptId: string }).attemptId, original.attemptId);
  await assert.rejects(control.execute!("other", { action: "checkpoint", uniqueCode: "B" }), /not found|STALE/);
  const recovered = await new BenchmarkLedger(id).initialize();
  await recovered.reserve("A", "subagent:test", { isSubagent: true });
  await recovered.confirmStarted("A", ["fixture"], "subagent:test");
  assert.equal(recovered.getChallenge("A")!.attemptId, original.attemptId);
  assert.equal(recovered.getChallenge("A")!.containerEpoch, original.containerEpoch);
  const restored = new BenchmarkExecutionBinding(recovered, "subagent:test", true);
  t.after(() => restored.dispose());
  restored.validate();
  await restart(recovered, "subagent:test");
  assert.throws(() => restored.validate(), /STALE_ATTEMPT/);
});

for (const source of ["model_failure", "platform_failure", "harness_stale_state", "harness_bad_handoff", "harness_bad_compaction", "harness_concurrency"] as FailureSource[]) {
  test(`attempt termination records ${source}`, async (t) => {
    const { ledger, challenge } = await setup(t);
    await ledger.recordAttemptIncident(captureFence(challenge), source, `synthetic ${source}`, "fixture_gate");
    await ledger.defer("A", "unfinished", undefined, "main");
    const ended = challenge.approachHistory.at(-1)!;
    assert.equal(ended.terminationSource, source);
    assert.equal(ended.activeGate, "fixture_gate");
    assert.equal(ended.attemptId, challenge.attemptId);
    assert.equal(ended.containerEpoch, challenge.containerEpoch);
    assert.equal(ended.flagsBefore, 0);
    assert.equal(ended.flagsAfter, 0);
    assert.equal(ledger.getMetrics().terminationCounts[source], 1);
  });
}

test("ordinary defer is a deliberate deferred outcome, first deadline is harness timeout, platform error takes priority", async (t) => {
  const a = await setup(t); await a.ledger.defer("A", "unfinished", undefined, "main");
  assert.equal(a.challenge.approachHistory.at(-1)?.terminationSource, "deferred");
  const b = await setup(t); b.advance(FIRST_ATTEMPT_LIMIT_MS);
  await b.ledger.defer("A", "deadline", undefined, "main");
  assert.equal(b.challenge.approachHistory.at(-1)?.terminationSource, "harness_timeout");
  const c = await setup(t); c.advance(FIRST_ATTEMPT_LIMIT_MS);
  await c.ledger.recordAttemptIncident(captureFence(c.challenge), "platform_failure", "platform unavailable");
  await c.ledger.recordAttemptIncident(captureFence(c.challenge), "harness_timeout", "deadline");
  await c.ledger.defer("A", "deadline", undefined, "main");
  assert.equal(c.challenge.approachHistory.at(-1)?.terminationSource, "platform_failure");
});

test("an async stale checkpoint cannot modify a new attempt even after initial authorization", async (t) => {
  const { ledger, challenge } = await setup(t);
  const old = captureFence(challenge)!;
  await restart(ledger);
  const before = JSON.stringify(challenge);
  await assert.rejects(attemptContext.run(old, () => ledger.checkpoint("A", "obsolete", [], undefined, "main")), /STALE_ATTEMPT/);
  assert.equal(JSON.stringify(challenge), before);
});

test("acquire binds runtime automatically; checkpoint, partial submit and defer need no IDs", async (t) => {
  const id = `automatic-${randomUUID()}`;
  t.after(() => BenchmarkLedger.destroy(id));
  const ledger = await new BenchmarkLedger(id).initialize();
  await ledger.syncFromPlatform([{ unique_code: "A", description: "synthetic", difficulty: "easy", level: 1, total_score: 100,
    flag_count: 2, correct_flag_count: 0, is_completed: false, container_status: "stopped", container_addr: [] }], true, "ip");
  const binding = new BenchmarkExecutionBinding(ledger, "main"); t.after(() => binding.dispose());
  const root = await mkdtemp(join(tmpdir(), "riftx-auto-")); t.after(() => rm(root, { recursive: true, force: true }));
  const workspace = new BenchmarkWorkspace(root, undefined, async () => {}, binding);
  let submits = 0;
  const controller = { startChallenge: async () => ({ container_addr: ["fixture"] }), closeChallenge: async () => ({}),
    submitFlag: async () => { submits++; return { unique_code: "A", correct: true, awarded: 50, cumulative_score: 50, correct_flag_count: 1, total_flag_count: 2, matched_flag_index: 0 }; }
  } as unknown as BenchmarkController;
  const control = createBenchmarkControlTool(controller, ledger, { grantScope: () => {} } as unknown as BrowserManager, () => "main", undefined,
    async (challenge) => { binding.bind(challenge); await workspace.activate(challenge.uniqueCode); },
    async () => { binding.bind(); await workspace.activate(); }) as unknown as ExecutableTool;
  binding.guard(control); workspace.install(control); binding.install(control);
  const acquired = await control.execute!("a", { action: "acquire", uniqueCode: "A" }) as { details: { attemptId: string; containerEpoch: number } };
  assert.equal(acquired.details.attemptId, binding.snapshot()?.attemptId);
  assert.equal(acquired.details.containerEpoch, 1);
  await control.execute!("c", { action: "checkpoint", signal: "synthetic flag", evidenceRef: "synthetic:evidence" });
  await control.execute!("s", { action: "submit", flag: "synthetic flag", evidenceRef: "synthetic:evidence" });
  assert.equal(submits, 1);
  await control.execute!("d", { action: "defer", reason: "unfinished" });
  assert.equal(binding.snapshot(), undefined);
  assert.equal(ledger.getChallenge("A")?.status, "deferred");
});

test("pending retry rechecks its generation after the platform read returns", async (t) => {
  const { ledger } = await setup(t);
  enqueuePendingSubmission(ledger, "A", "synthetic", "main", 0);
  let submits = 0;
  let replacement = "";
  const controller = {
    listChallenges: async () => {
      await attemptContext.exit(() => restart(ledger));
      replacement = JSON.stringify(ledger.getState());
      return [{ unique_code: "A", description: "obsolete", correct_flag_count: 1, is_completed: false }];
    },
    submitFlag: async () => { submits++; }
  } as unknown as BenchmarkController;
  await retryPendingSubmissions(controller, ledger, 100_000);
  assert.equal(submits, 0);
  assert.equal(JSON.stringify(ledger.getState()), replacement);
});

test("delayed handoff from a prior child cannot write into the replacement attempt", async (t) => {
  const { ledger, challenge } = await setup(t, "subagent:old");
  await ledger.defer("A", "unfinished", undefined, "subagent:old");
  await ledger.confirmClosed("A");
  await ledger.acquire("A", "main", ["fixture"]);
  const before = JSON.stringify(challenge);
  await ledger.recordChildHandoff("A", "subagent:old", "old report");
  assert.equal(JSON.stringify(challenge), before);
});
