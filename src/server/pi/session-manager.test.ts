import assert from "node:assert/strict";
import test from "node:test";
import type { SubagentTask } from "@/lib/types";
import { claimSubagentResult, deliverSubagentCompletion, dispatchSessionAction, enqueueSessionAction, finishSubagentResult, formatSubagentTerminalMessage, shouldDeliverSubagentCompletion, undeliveredTerminalTasks, waitForSubagentsBeforeConclusion } from "./session-join";

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

test("join retries a known terminal child whose result was never delivered", async () => {
  const task = makeTask("completed");
  task.summary = "Completed right before the server restarted.";
  task.delivered = false;
  const prompts: string[] = [];
  const record = makeRecord(task, () => false, async () => undefined, prompts, new Set());

  await waitForSubagentsBeforeConclusion(record, new Set([task.id]), new Set(), 0);

  assert.equal(prompts.length, 1);
  assert.match(prompts[0], /Completed right before the server restarted/);
});

test("finishSubagentResult persists the delivery mark on the task record", () => {
  const marks: Array<[string, boolean]> = [];
  const task = makeTask("completed");
  const record = {
    subagents: {
      list: () => [task],
      hasActiveTasks: () => false,
      waitForAll: async () => undefined,
      markDelivered: (id: string, delivered: boolean) => marks.push([id, delivered])
    },
    deliveredSubagentResults: new Set<string>(),
    deliveringSubagentResults: new Set<string>()
  };
  claimSubagentResult(record, task.id);
  finishSubagentResult(record, task.id, true);
  assert.deepEqual(marks, [[task.id, true]]);
});

test("a transient delivery failure is retried automatically", async () => {
  const task = makeTask("completed");
  task.summary = "Retry this delivery.";
  let calls = 0;
  const record = {
    session: {
      isStreaming: false,
      prompt: async () => {
        calls += 1;
        if (calls === 1) throw new Error("transient SDK rejection");
      },
      steer: async () => { throw new Error("steer must not be used while idle"); }
    },
    deliveredSubagentResults: new Set<string>(),
    deliveringSubagentResults: new Set<string>(),
    gate: { beginTask: () => undefined },
    promptChain: undefined as Promise<void> | undefined
  } as unknown as Parameters<typeof deliverSubagentCompletion>[0];

  assert.equal(await deliverSubagentCompletion(record, task, task.summary, { retries: 2, retryDelayMs: 1 }), true);
  assert.equal(calls, 2);
  assert.equal(record.deliveredSubagentResults.has(task.id), true);
});

test("delivery retries are bounded and leave the result undelivered", async () => {
  const task = makeTask("failed");
  task.error = "Keeps failing.";
  let calls = 0;
  const record = {
    session: {
      isStreaming: false,
      prompt: async () => {
        calls += 1;
        throw new Error("persistent SDK rejection");
      },
      steer: async () => undefined
    },
    deliveredSubagentResults: new Set<string>(),
    deliveringSubagentResults: new Set<string>(),
    gate: { beginTask: () => undefined },
    promptChain: undefined as Promise<void> | undefined
  } as unknown as Parameters<typeof deliverSubagentCompletion>[0];

  assert.equal(await deliverSubagentCompletion(record, task, task.summary, { retries: 1, retryDelayMs: 1 }), false);
  assert.equal(calls, 2);
  assert.equal(record.deliveredSubagentResults.has(task.id), false);
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
  task.error = "SubAgent completed without a final text response.";
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

test("streaming sessions deliver partial results while idle sessions batch active siblings", () => {
  const streaming = { isStreaming: true, steer: async () => undefined, prompt: async () => undefined };
  const idle = { isStreaming: false, steer: async () => undefined, prompt: async () => undefined };
  const noSubagents = { hasActiveTasks: () => false };
  const otherRunning = { hasActiveTasks: () => true };
  // A stale waiting flag never suppresses a running parent's steer delivery.
  assert.equal(shouldDeliverSubagentCompletion({ session: streaming, subagents: noSubagents }), true);
  assert.equal(shouldDeliverSubagentCompletion({ session: streaming, subagents: noSubagents, waitingForSubagents: true }), true, "stale waiting flag must not suppress when no other subagent is running");
  assert.equal(shouldDeliverSubagentCompletion({ session: streaming, subagents: noSubagents, aborting: true }), false);
  assert.equal(shouldDeliverSubagentCompletion({ session: streaming, subagents: noSubagents, abortPromise: Promise.resolve() }), false);
  // Idle + no other subagents running: deliver immediately.
  assert.equal(shouldDeliverSubagentCompletion({ session: idle, subagents: noSubagents, waitingForSubagents: true }), true);
  assert.equal(shouldDeliverSubagentCompletion({ session: idle, subagents: noSubagents }), true);
  // An idle parent batches while a sibling is active, but a running parent can
  // consume the completed result immediately through steer.
  assert.equal(shouldDeliverSubagentCompletion({ session: idle, subagents: otherRunning, waitingForSubagents: true }), false);
  assert.equal(shouldDeliverSubagentCompletion({ session: idle, subagents: otherRunning, waitingForSubagents: false }), false, "stale waiting=false must not bypass batch deference");
  assert.equal(shouldDeliverSubagentCompletion({ session: streaming, subagents: otherRunning, waitingForSubagents: false }), true);
  assert.equal(shouldDeliverSubagentCompletion({ session: streaming, subagents: otherRunning, waitingForSubagents: true }), true, "a stale waiting state must not hold a streaming result");
  // No subagents manager at all: deliver.
  assert.equal(shouldDeliverSubagentCompletion({ session: idle }), true);
});

test("session prompt actions are serialized", async () => {
  const record = { promptChain: undefined as Promise<void> | undefined };
  const order: string[] = [];
  let releaseFirst!: () => void;
  const firstGate = new Promise<void>((resolve) => { releaseFirst = resolve; });
  const first = enqueueSessionAction(record, async () => {
    order.push("first-start");
    await firstGate;
    order.push("first-end");
  });
  const second = enqueueSessionAction(record, async () => { order.push("second"); });
  await new Promise<void>((resolve) => setImmediate(resolve));
  assert.deepEqual(order, ["first-start"]);
  releaseFirst();
  await Promise.all([first, second]);
  assert.deepEqual(order, ["first-start", "first-end", "second"]);
});

test("steering and follow-up actions bypass the prompt run queue", async () => {
  const record = { promptChain: undefined as Promise<void> | undefined };
  const order: string[] = [];
  let releaseQueue!: () => void;
  const queueGate = new Promise<void>((resolve) => { releaseQueue = resolve; });
  record.promptChain = queueGate;
  const prompt = enqueueSessionAction(record, async () => {
    order.push("prompt-start");
    order.push("prompt-end");
  });
  const steer = dispatchSessionAction(record, "steer", async () => { order.push("steer"); });
  const followUp = dispatchSessionAction(record, "followUp", async () => { order.push("follow-up"); });
  await Promise.all([steer, followUp]);
  assert.deepEqual(order, ["steer", "follow-up"]);
  releaseQueue();
  await prompt;
  assert.deepEqual(order, ["steer", "follow-up", "prompt-start", "prompt-end"]);
});

test("a completed child steers an active parent immediately despite an active sibling and pending prompt queue", async () => {
  const task = makeTask("completed");
  task.summary = "Child found a useful result.";
  const order: string[] = [];
  let releaseQueue!: () => void;
  const queueGate = new Promise<void>((resolve) => { releaseQueue = resolve; });
  const record = {
    promptChain: queueGate,
    waitingForSubagents: false,
    deliveredSubagentResults: new Set<string>(),
    deliveringSubagentResults: new Set<string>(),
    subagents: { hasActiveTasks: () => true },
    gate: { beginTask: () => order.push("begin-task") },
    session: {
      isStreaming: true,
      steer: async () => { order.push("steer-result"); },
      prompt: async () => { order.push("prompt-result"); }
    }
  } as unknown as Parameters<typeof deliverSubagentCompletion>[0];
  const existingPrompt = enqueueSessionAction(record, async () => { order.push("existing-prompt-start"); });

  assert.equal(await deliverSubagentCompletion(record, task, task.summary), true);
  assert.deepEqual(order, ["steer-result"]);
  assert.equal(record.deliveredSubagentResults.has(task.id), true);
  releaseQueue();
  await existingPrompt;
  assert.deepEqual(order, ["steer-result", "existing-prompt-start"]);
});

test("streaming child results arrive individually and the final scan does not redeliver the first", async () => {
  const first = makeTask("completed");
  first.id = "child-a";
  first.name = "Fast review";
  first.summary = "Fast result.";
  first.delivered = false;
  const second = makeTask("running");
  second.id = "child-b";
  second.name = "Slow review";
  second.delivered = false;
  const tasks = [first, second];
  const steered: string[] = [];
  const record = {
    waitingForSubagents: false,
    deliveredSubagentResults: new Set<string>(),
    deliveringSubagentResults: new Set<string>(),
    gate: { beginTask: () => undefined },
    subagents: {
      list: () => tasks,
      hasActiveTasks: () => tasks.some((task) => task.status === "queued" || task.status === "running"),
      waitForAll: async () => undefined,
      markDelivered: (taskId: string, delivered: boolean) => {
        const task = tasks.find((candidate) => candidate.id === taskId);
        if (task) task.delivered = delivered;
      }
    },
    session: {
      isStreaming: true,
      steer: async (message: string) => { steered.push(message); },
      prompt: async () => { throw new Error("streaming completion must use steer"); }
    }
  } as unknown as Parameters<typeof deliverSubagentCompletion>[0];

  assert.equal(await deliverSubagentCompletion(record, first, first.summary), true);
  assert.equal(steered.length, 1);
  assert.match(steered[0], /Subagent: Fast review/);
  assert.equal(first.delivered, true);

  second.status = "completed";
  second.summary = "Slow result.";
  assert.equal(await deliverSubagentCompletion(record, second, second.summary), true);
  assert.equal(steered.length, 2);
  assert.match(steered[1], /Subagent: Slow review/);
  assert.equal(second.delivered, true);
  assert.deepEqual(undeliveredTerminalTasks(record, tasks), []);
});
