import assert from "node:assert/strict";
import test from "node:test";
import type { SubagentTask } from "@/lib/types";
import { claimSubagentResult, finishSubagentResult, formatSubagentTerminalMessage, shouldDeliverSubagentCompletion, waitForSubagentsBeforeConclusion } from "./session-join";

function makeTask(status: SubagentTask["status"]): SubagentTask {
  return {
    id: "child-1",
    parentSessionId: "parent",
    threadId: "thread-1",
    name: "API review",
    task: "Review API authorization",
    status,
    model: "test/model",
    createdAt: new Date().toISOString(),
    pendingApprovalCount: 0,
    logs: []
  };
}

function makeRecord(task: SubagentTask, active: () => boolean, waitForAll: () => Promise<void>, prompts: string[], delivered = new Set<string>()) {
  const manager = {
    list: () => [task],
    hasActiveTasks: active,
    waitForAll
  };
  return {
    subagents: manager,
    abortEpoch: 0,
    waitingForSubagents: true,
    deliveredSubagentResults: delivered,
    gate: { beginTask: () => undefined },
    session: { prompt: async (message: string) => { prompts.push(message); } }
  } as unknown as Parameters<typeof waitForSubagentsBeforeConclusion>[0];
}

test("join preserves an undelivered terminal child when no tasks remain active", async () => {
  const task = makeTask("completed");
  task.summary = "Found an authorization gap.";
  const prompts: string[] = [];
  const record = makeRecord(task, () => false, async () => undefined, prompts);

  await waitForSubagentsBeforeConclusion(record, new Set(), new Set(), 0);

  assert.equal(prompts.length, 1);
  assert.match(prompts[0], /Found an authorization gap/);
});

test("join does not re-synthesize a child already delivered during the parent turn", async () => {
  const task = makeTask("completed");
  task.summary = "Already delivered result.";
  const prompts: string[] = [];
  const record = makeRecord(task, () => false, async () => undefined, prompts, new Set([task.id]));

  await waitForSubagentsBeforeConclusion(record, new Set(), new Set(), 0);

  assert.equal(prompts.length, 0);
});

test("join waits for a child spawned during the current turn", async () => {
  const task = makeTask("running");
  task.summary = "Late child result.";
  const prompts: string[] = [];
  let active = true;
  const record = makeRecord(task, () => active, async () => {
    task.status = "completed";
    active = false;
  }, prompts, new Set());

  await waitForSubagentsBeforeConclusion(record, new Set(), new Set(), 0);

  assert.equal(prompts.length, 1);
  assert.match(prompts[0], /Late child result/);
});

test("join does not re-deliver historical terminal children after restart", async () => {
  const task = makeTask("completed");
  task.summary = "Historical result.";
  const prompts: string[] = [];
  const record = makeRecord(task, () => false, async () => undefined, prompts, new Set());

  await waitForSubagentsBeforeConclusion(record, new Set([task.id]), new Set(), 0);

  assert.equal(prompts.length, 0);
});

test("a failed result delivery can be claimed again", () => {
  const record = { deliveredSubagentResults: new Set<string>(), deliveringSubagentResults: new Set<string>() };
  assert.equal(claimSubagentResult(record, "child-1"), true);
  finishSubagentResult(record, "child-1", false);
  assert.equal(claimSubagentResult(record, "child-1"), true);
  finishSubagentResult(record, "child-1", true);
  assert.equal(claimSubagentResult(record, "child-1"), false);
});

test("join reports an empty child as a status, not a fake result", () => {
  const task = makeTask("empty");
  task.error = "Child Agent completed without a final text response.";
  const message = formatSubagentTerminalMessage(task, "");

  assert.match(message, /^\[RiftX subagent status\]/);
  assert.match(message, /Status: empty/);
  assert.doesNotMatch(message, /No result/);
  assert.doesNotMatch(message, /Summary:/);
});

test("join exits before terminal cancellation results when Stop advanced the abort epoch", async () => {
  const task = makeTask("cancelled");
  task.error = "Cancelled by the user.";
  const prompts: string[] = [];
  const record = makeRecord(task, () => false, async () => undefined, prompts);
  record.abortEpoch = 1;

  await waitForSubagentsBeforeConclusion(record, new Set(), new Set([task.id]), 0);

  assert.equal(prompts.length, 0);
});

test("completion delivery is suppressed while a session is stopping or joining", () => {
  assert.equal(shouldDeliverSubagentCompletion({}), true);
  assert.equal(shouldDeliverSubagentCompletion({ waitingForSubagents: true }), false);
  assert.equal(shouldDeliverSubagentCompletion({ aborting: true }), false);
  assert.equal(shouldDeliverSubagentCompletion({ abortPromise: Promise.resolve() }), false);
});
