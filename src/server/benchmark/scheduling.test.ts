import assert from "node:assert/strict";
import test from "node:test";
import { EventEmitter } from "node:events";
import type { SessionRecord } from "@/server/pi/session-registry";
import { deliverSubagentCompletion, enqueueSessionAction } from "@/server/pi/session-join";
import type { SubagentTask } from "@/lib/types";
import type { BenchmarkLedger } from "./ledger";
import { benchmarkMainBusy, benchmarkMainHasWork, queueBenchmarkContinuation } from "./scheduling";

function fixture() {
  let calls = 0;
  const record = {
    session: { isStreaming: false, prompt: async () => { calls++; }, steer: async () => undefined },
    gate: { pendingRequests: () => [], beginTask: () => undefined },
    emitter: new EventEmitter(), toolStatuses: new Map(),
    subagents: { runningCount: 2, hasActiveTasks: () => true }, waitingForSubagents: true,
    deliveringSubagentResults: new Set(), deliveredSubagentResults: new Set()
  } as unknown as SessionRecord;
  const ledger = {
    getState: () => ({ phase: "coverage", activeContainers: 2, challenges: { a: { owner: "subagent:a", status: "running" }, b: { owner: "subagent:b", status: "running" } } }),
    budgetForOwner: () => undefined, candidates: () => [{ uniqueCode: "c" }]
  } as unknown as BenchmarkLedger;
  return { record, ledger, calls: () => calls };
}

test("idle main continues while two children run, with no duplicate queued prompts", async () => {
  const { record, ledger, calls } = fixture();
  assert.equal(benchmarkMainBusy(record), false);
  assert.ok(queueBenchmarkContinuation(record, ledger, "continue"));
  assert.equal(queueBenchmarkContinuation(record, ledger, "duplicate"), false);
  await record.promptChain;
  assert.equal(calls(), 1);
  assert.equal(record.pendingSessionActions, 0);
});

test("legitimate coverage waiting and compaction do not trigger a continuation", async () => {
  const { record, ledger } = fixture();
  ledger.candidates = () => [];
  assert.equal(benchmarkMainHasWork(ledger), false);
  assert.equal(queueBenchmarkContinuation(record, ledger, "wait"), false);
  ledger.budgetForOwner = () => ({ challenge: { uniqueCode: "main" } }) as ReturnType<BenchmarkLedger["budgetForOwner"]>;
  assert.ok(benchmarkMainHasWork(ledger));
  record.compacting = true;
  assert.equal(queueBenchmarkContinuation(record, ledger, "wait"), false);
});

test("child delivery arriving before dispatch supersedes a queued continuation", async () => {
  const { record, ledger, calls } = fixture();
  assert.ok(queueBenchmarkContinuation(record, ledger, "continue"));
  const task = { id: "child", name: "fixture", status: "completed", benchmarkChallenge: "a" } as SubagentTask;
  const delivered = deliverSubagentCompletion(record, task, "FINDINGS: fixture");
  assert.equal(await delivered, true);
  await record.promptChain;
  assert.equal(calls(), 1, "only the result delivery should start a model turn");
});

test("failed queued actions release the main busy marker", async () => {
  const { record } = fixture();
  await assert.rejects(enqueueSessionAction(record, async () => { throw new Error("fixture"); }));
  assert.equal(benchmarkMainBusy(record), false);
});
