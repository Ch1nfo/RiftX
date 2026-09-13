import test from "node:test";
import assert from "node:assert/strict";
import type { SubagentTask } from "@/lib/types";
import { deliverSubagentCompletion, waitForSubagentsBeforeConclusion } from "./session-join";
import { abortSessionRecord } from "./session-shutdown";

function fixture(ids = ["fixture-child"]) {
  const tasks: SubagentTask[] = ids.map((id) => ({
    id, parentSessionId: "fixture-parent", threadId: "fixture-thread", name: "fixture", task: "fixture", status: "completed",
    model: "fixture/model", createdAt: "2026-01-01T00:00:00Z", pendingApprovalCount: 0, logs: [],
    benchmarkChallenge: id, summary: "fixture-final-observation", delivered: false
  }));
  const events: string[] = [];
  const record: Parameters<typeof deliverSubagentCompletion>[0] = {
    abortEpoch: 0,
    deliveredSubagentResults: new Set(), deliveringSubagentResults: new Set(),
    gate: { beginTask: () => { events.push("begin"); } },
    session: { isStreaming: false, prompt: async () => { events.push("prompt"); }, steer: async () => { events.push("steer"); } },
    subagents: { list: () => tasks, hasActiveTasks: () => false, waitForAll: async () => undefined,
      markDelivered: (id, delivered) => { tasks.find((task) => task.id === id)!.delivered = delivered; events.push(`mark:${id}:${delivered}`); } }
  };
  return { tasks, events, record };
}

function stoppable(record: Parameters<typeof deliverSubagentCompletion>[0]) {
  return {
    ...record, id: "fixture-parent", unsubscribe: () => undefined,
    gate: { ...record.gate, rejectAll: () => undefined },
    subagents: { ...record.subagents!, abortAll: async () => undefined },
    session: { ...record.session, abortBash: () => undefined, abortCompaction: () => undefined,
      abort: async () => undefined, dispose: () => undefined }
  };
}

test("single completion retries persistence before prompting or marking delivered", async () => {
  const { tasks: [task], events, record } = fixture();
  let calls = 0;
  record.prepareSubagentCompletion = async () => {
    events.push("prepare");
    if (++calls === 1) throw new Error("fixture-write-failure");
  };
  assert.equal(await deliverSubagentCompletion(record, task, task.summary, { retries: 1, retryDelayMs: 1 }), true);
  assert.deepEqual(events, ["prepare", `mark:${task.id}:false`, "prepare", "begin", "prompt", `mark:${task.id}:true`]);
});

test("failed persistence never reaches steer and leaves the result retryable", async () => {
  const { tasks: [task], events, record } = fixture();
  record.session.isStreaming = true;
  record.prepareSubagentCompletion = async () => { events.push("prepare"); throw new Error("fixture-write-failure"); };
  assert.equal(await deliverSubagentCompletion(record, task, task.summary, { retries: 0 }), false);
  assert.deepEqual(events, ["prepare", `mark:${task.id}:false`]);
  assert.equal(record.deliveredSubagentResults.has(task.id), false);
  assert.equal(record.deliveringSubagentResults?.has(task.id), false);
  record.prepareSubagentCompletion = async () => { events.push("prepared"); };
  assert.equal(await deliverSubagentCompletion(record, task, task.summary, { retries: 0 }), true);
  assert.deepEqual(events.slice(-3), ["prepared", "steer", `mark:${task.id}:true`]);
});

test("batch delivery waits for every handoff and preserves retry marks on any failure", async () => {
  const { tasks, events, record } = fixture(["fixture-a", "fixture-b"]);
  let fail = true;
  record.prepareSubagentCompletion = async (task) => {
    events.push(`prepare:${task.id}`);
    if (task.id === "fixture-b" && fail) throw new Error("fixture-write-failure");
  };
  await assert.rejects(() => waitForSubagentsBeforeConclusion(record, new Set(), new Set(), 0), /fixture-write-failure/);
  assert.deepEqual(events, ["prepare:fixture-a", "prepare:fixture-b", "mark:fixture-a:false", "mark:fixture-b:false"]);
  assert.equal(record.deliveredSubagentResults.size, 0);
  assert.equal(record.deliveringSubagentResults?.size, 0);
  fail = false;
  await waitForSubagentsBeforeConclusion(record, new Set(tasks.map((task) => task.id)), new Set(), 0);
  assert.deepEqual(events.slice(4), ["prepare:fixture-a", "prepare:fixture-b", "begin", "prompt", "mark:fixture-a:true", "mark:fixture-b:true"]);
});

test("user Stop prevents automatic benchmark delivery until explicit user resume", async () => {
  const { tasks: [task], events, record } = fixture();
  const stopped = stoppable(record);
  await abortSessionRecord(stopped, () => undefined);
  assert.equal(stopped.aborting, false);
  assert.equal(stopped.benchmarkHandoffPaused, true);
  assert.equal(await deliverSubagentCompletion(stopped, task, task.summary, { retries: 0 }), false);
  await waitForSubagentsBeforeConclusion(stopped, new Set(), new Set(), stopped.abortEpoch!);
  assert.deepEqual(events, []);
  assert.equal(task.delivered, false);
  stopped.benchmarkHandoffPaused = false;
  assert.equal(await deliverSubagentCompletion(stopped, task, task.summary, { retries: 0 }), true);
  assert.deepEqual(events, ["begin", "prompt", `mark:${task.id}:true`]);
});

test("Stop while preparation is pending cannot launch a new turn after preparation resolves", async () => {
  const { tasks: [task], events, record } = fixture();
  const stopped = stoppable(record);
  let finishPreparation!: () => void;
  const prepared = new Promise<void>((resolve) => { finishPreparation = resolve; });
  stopped.prepareSubagentCompletion = () => prepared;
  const delivery = deliverSubagentCompletion(stopped, task, task.summary, { retries: 0 });
  await abortSessionRecord(stopped, () => undefined);
  finishPreparation();
  assert.equal(await delivery, false);
  assert.deepEqual(events, [`mark:${task.id}:false`]);
  assert.equal(stopped.deliveringSubagentResults?.size, 0);
});

test("queued deliveries recheck the abort epoch after an ordinary timeout abort finishes", async () => {
  const { tasks: [task], events, record } = fixture();
  let releaseQueue!: () => void;
  record.promptChain = new Promise<void>((resolve) => { releaseQueue = resolve; });
  const delivery = deliverSubagentCompletion(record, task, task.summary, { retries: 0 });
  record.abortEpoch = 1;
  releaseQueue();
  assert.equal(await delivery, false);
  assert.deepEqual(events, [`mark:${task.id}:false`]);
  assert.equal(record.benchmarkHandoffPaused, undefined);
  assert.equal(await deliverSubagentCompletion(record, task, task.summary, { retries: 0 }), true);
});

test("Stop still pauses delivery when another abort is already in progress", async () => {
  const { record } = fixture();
  const stopped = stoppable(record);
  stopped.abortPromise = Promise.resolve();
  await abortSessionRecord(stopped, () => undefined);
  assert.equal(stopped.benchmarkHandoffPaused, true);
});

test("batch preparation that overlaps Stop remains undelivered without prompting", async () => {
  const { tasks: [task], events, record } = fixture();
  const stopped = stoppable(record);
  let preparationStarted!: () => void;
  const started = new Promise<void>((resolve) => { preparationStarted = resolve; });
  let finishPreparation!: () => void;
  const prepared = new Promise<void>((resolve) => { finishPreparation = resolve; });
  stopped.prepareSubagentCompletion = () => { preparationStarted(); return prepared; };
  const delivery = waitForSubagentsBeforeConclusion(stopped, new Set(), new Set(), 0);
  await started;
  await abortSessionRecord(stopped, () => undefined);
  finishPreparation();
  await assert.rejects(() => delivery, /paused/);
  assert.deepEqual(events, [`mark:${task.id}:false`]);
  assert.equal(task.delivered, false);
});

test("benchmark handoff pause does not change ordinary non-benchmark completion behavior", async () => {
  const { tasks: [task], events, record } = fixture();
  task.benchmarkChallenge = undefined;
  record.benchmarkHandoffPaused = true;
  assert.equal(await deliverSubagentCompletion(record, task, task.summary, { retries: 0 }), true);
  assert.deepEqual(events, ["begin", "prompt", `mark:${task.id}:true`]);
});
