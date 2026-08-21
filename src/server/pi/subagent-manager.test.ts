import assert from "node:assert/strict";
import { mkdtemp, mkdir, writeFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { SubagentManager } from "./subagent-manager";
import type { RiftxEvent } from "@/lib/types";

async function waitFor(check: () => boolean, timeoutMs = 1_000) {
  const started = Date.now();
  while (!check()) {
    if (Date.now() - started > timeoutMs) throw new Error("Timed out waiting for subagent state");
    await new Promise((resolve) => setTimeout(resolve, 5));
  }
}

test("limits concurrent subagents and starts queued work after a slot is released", async () => {
  const root = await mkdtemp(join(tmpdir(), "riftx-subagents-"));
  const events: RiftxEvent[] = [];
  const active = new Map<string, () => void>();
  const manager = new SubagentManager("parent", root, (event) => events.push(event), 1, "request");
  await manager.initialize(async ({ task }) => new Promise((resolve) => active.set(task.id, () => resolve({ summary: task.task }))));
  try {
    const first = manager.submit("first");
    const second = manager.submit("second");
    await waitFor(() => manager.list().filter((task) => task.status === "running").length === 1);
    assert.equal(manager.list().filter((task) => task.status === "queued").length, 1);
    const firstTask = manager.list().find((task) => task.task === "first")!;
    await waitFor(() => active.has(firstTask.id));
    active.get(firstTask.id)!();
    await first;
    await waitFor(() => manager.list().find((task) => task.task === "second")?.status === "running");
    const secondTask = manager.list().find((task) => task.task === "second")!;
    await waitFor(() => active.has(secondTask.id));
    active.get(secondTask.id)!();
    await second;
    await waitFor(() => manager.runningCount === 0);
    assert.equal(manager.runningCount, 0);
    assert.equal(events.filter((event) => event.type === "subagent_start").length, 2);
  } finally {
    await new Promise((resolve) => setTimeout(resolve, 25));
    await rm(root, { recursive: true, force: true });
  }
});

test("cancels queued work without starting it", async () => {
  const root = await mkdtemp(join(tmpdir(), "riftx-subagents-"));
  const manager = new SubagentManager("parent", root, () => undefined, 1, "request");
  const active = new Map<string, () => void>();
  await manager.initialize(async ({ task }) => new Promise((resolve) => active.set(task.id, () => resolve({ summary: task.task }))));
  try {
    const first = manager.submit("first");
    const second = manager.submit("second");
    await waitFor(() => manager.list().some((task) => task.task === "first" && task.status === "running"));
    const secondTask = manager.list().find((task) => task.task === "second")!;
    assert.equal(manager.cancel(secondTask.id), true);
    await assert.rejects(second, /Cancelled before/);
    assert.equal(manager.list().find((task) => task.id === secondTask.id)?.status, "cancelled");
    const firstTask = manager.list().find((task) => task.task === "first")!;
    await waitFor(() => active.has(firstTask.id));
    active.get(firstTask.id)!();
    await first;
  } finally {
    await new Promise((resolve) => setTimeout(resolve, 25));
    await rm(root, { recursive: true, force: true });
  }
});

test("abortAll cancels every running subagent and waits for shutdown", async () => {
  const root = await mkdtemp(join(tmpdir(), "riftx-subagents-"));
  const manager = new SubagentManager("parent", root, () => undefined, 3, "request");
  await manager.initialize(async ({ signal }) => new Promise((_resolve, reject) => {
    signal.addEventListener("abort", () => reject(new Error("aborted")), { once: true });
  }));
  try {
    const tasks = [manager.submit("one"), manager.submit("two"), manager.submit("three")];
    const settled = Promise.allSettled(tasks);
    await waitFor(() => manager.runningCount === 3);
    await manager.abortAll();
    await settled;
    await waitFor(() => manager.runningCount === 0);
    assert.deepEqual(manager.list().map((task) => task.status), ["cancelled", "cancelled", "cancelled"]);
  } finally {
    await new Promise((resolve) => setTimeout(resolve, 25));
    await rm(root, { recursive: true, force: true });
  }
});

test("queues every submitted task immediately and lets maxConcurrent control execution", async () => {
  const root = await mkdtemp(join(tmpdir(), "riftx-subagents-"));
  const manager = new SubagentManager("parent", root, () => undefined, 2, "request");
  const active = new Map<string, () => void>();
  await manager.initialize(async ({ task }) => new Promise((resolve) => active.set(task.id, () => resolve({ summary: task.task }))));
  try {
    const promises = ["one", "two", "three", "four"].map((task) => manager.submit(task));
    assert.equal(manager.list().length, 4);
    const [first, second, third, fourth] = manager.list();
    await waitFor(() => active.has(first.id) && active.has(second.id));
    assert.equal(manager.list().filter((task) => task.status === "queued").length, 2);
    active.get(first.id)?.();
    active.get(second.id)?.();
    await waitFor(() => active.has(third.id) && active.has(fourth.id)
      && third.status === "running" && fourth.status === "running");
    active.get(third.id)?.();
    active.get(fourth.id)?.();
    await Promise.all(promises);
    await waitFor(() => manager.runningCount === 0);
  } finally {
    await new Promise((resolve) => setTimeout(resolve, 25));
    await rm(root, { recursive: true, force: true });
  }
});

test("deduplicates normalized queued and running tasks", async () => {
  const root = await mkdtemp(join(tmpdir(), "riftx-subagents-"));
  const manager = new SubagentManager("parent", root, () => undefined, 1, "request");
  let finish!: () => void;
  await manager.initialize(async () => new Promise((resolve) => { finish = () => resolve({ summary: "done" }); }));
  try {
    const first = manager.submit("  Inspect   API routes  ");
    const duplicate = manager.submit("inspect api routes");
    assert.strictEqual(first, duplicate);
    assert.equal(manager.list().length, 1);
    await waitFor(() => typeof finish === "function");
    finish();
    await first;
  } finally {
    await new Promise((resolve) => setTimeout(resolve, 25));
    await rm(root, { recursive: true, force: true });
  }
});

test("returns a background task immediately and reports its result on completion", async () => {
  const root = await mkdtemp(join(tmpdir(), "riftx-subagents-"));
  const manager = new SubagentManager("parent", root, () => undefined, 1, "request");
  let finish!: () => void;
  let completion: { id: string; summary: string } | undefined;
  manager.setCompletionHandler((task, result) => { completion = { id: task.id, summary: result.summary }; });
  await manager.initialize(async () => new Promise((resolve) => { finish = () => resolve({ summary: "background result" }); }));
  try {
    const submitted = manager.submitTask("background task");
    assert.ok(submitted.task);
    assert.ok(submitted.task.status === "queued" || submitted.task.status === "running");
    await waitFor(() => manager.list()[0]?.status === "running");
    await waitFor(() => typeof finish === "function");
    finish();
    assert.deepEqual(await submitted.promise, { summary: "background result" });
    await waitFor(() => completion?.id === submitted.task?.id);
    assert.equal(completion?.summary, "background result");
  } finally {
    await new Promise((resolve) => setTimeout(resolve, 25));
    await rm(root, { recursive: true, force: true });
  }
});

test("waitForAll resolves only after every queued and running task reaches a terminal state", async () => {
  const root = await mkdtemp(join(tmpdir(), "riftx-subagents-"));
  const manager = new SubagentManager("parent", root, () => undefined, 1, "request");
  let finish!: () => void;
  await manager.initialize(async () => new Promise((resolve) => { finish = () => resolve({ summary: "done" }); }));
  try {
    const first = manager.submitTask("first");
    const second = manager.submitTask("second");
    let joined = false;
    const join = manager.waitForAll().then(() => { joined = true; });
    await new Promise((resolve) => setTimeout(resolve, 10));
    assert.equal(joined, false);
    assert.equal(manager.cancel(second.task!.id), true);
    await assert.rejects(second.promise, /Cancelled before/);
    finish();
    await first.promise;
    await join;
    assert.equal(joined, true);
  } finally {
    await new Promise((resolve) => setTimeout(resolve, 25));
    await rm(root, { recursive: true, force: true });
  }
});

test("uses the generated child-agent title instead of the prompt prefix", async () => {
  const root = await mkdtemp(join(tmpdir(), "riftx-subagents-"));
  const events: RiftxEvent[] = [];
  const manager = new SubagentManager("parent", root, (event) => events.push(event), 1, "request", async () => "API access review");
  await manager.initialize(async () => ({ summary: "done" }));
  try {
    const submitted = manager.submit("Inspect every API route for authorization bypasses and report evidence.");
    await waitFor(() => manager.list()[0]?.name === "API access review");
    assert.equal(manager.list()[0]?.name, "API access review");
    assert.equal(events.some((event) => event.type === "subagent_update" && (event.taskPatch as { name?: string } | undefined)?.name === "API access review"), true);
    await submitted;
  } finally {
    await new Promise((resolve) => setTimeout(resolve, 25));
    await rm(root, { recursive: true, force: true });
  }
});

test("waits for the real result of a queued task restored after restart", async () => {
  const root = await mkdtemp(join(tmpdir(), "riftx-subagents-"));
  const parentDir = join(root, "parent");
  await mkdir(parentDir, { recursive: true });
  const createdAt = new Date().toISOString();
  await writeFile(join(parentDir, "tasks.json"), JSON.stringify({ tasks: [
    { id: "queued", parentSessionId: "parent", threadId: "", name: "queued", task: "queued task", status: "queued", model: "m", createdAt, pendingApprovalCount: 0, logs: [] }
  ] }));
  const manager = new SubagentManager("parent", root, () => undefined, 1, "request");
  let finish!: () => void;
  await manager.initialize(async ({ task }) => new Promise((resolve) => {
    finish = () => resolve({ summary: `real result: ${task.task}` });
  }));
  try {
    await waitFor(() => manager.list()[0]?.status === "running");
    const submitted = manager.submitTask(" QUEUED   TASK ");
    let settled = false;
    void submitted.promise.finally(() => { settled = true; });
    await new Promise((resolve) => setTimeout(resolve, 10));
    assert.equal(settled, false);
    finish();
    assert.deepEqual(await submitted.promise, { summary: "real result: queued task" });
  } finally {
    await new Promise((resolve) => setTimeout(resolve, 25));
    await rm(root, { recursive: true, force: true });
  }
});

test("rejects a cancelled subagent when cancellation races with a successful runner result", async () => {
  const root = await mkdtemp(join(tmpdir(), "riftx-subagents-"));
  const manager = new SubagentManager("parent", root, () => undefined, 1, "request");
  await manager.initialize(async ({ signal }) => new Promise((resolve) => {
    signal.addEventListener("abort", () => setTimeout(() => resolve({ summary: "late result" }), 0), { once: true });
  }));
  try {
    const submitted = manager.submitTask("cancelled required task");
    await waitFor(() => manager.list()[0]?.status === "running");
    assert.equal(manager.cancel(submitted.task!.id), true);
    await assert.rejects(submitted.promise, /cancelled/i);
    assert.equal(manager.list()[0]?.status, "cancelled");
  } finally {
    await new Promise((resolve) => setTimeout(resolve, 25));
    await rm(root, { recursive: true, force: true });
  }
});

test("emits approval decisions when a child approval times out", async () => {
  const root = await mkdtemp(join(tmpdir(), "riftx-subagents-"));
  const events: RiftxEvent[] = [];
  const manager = new SubagentManager("parent", root, (event) => events.push(event), 1, "request");
  await manager.initialize(async ({ gate, emit }) => {
    const request = { id: "approval-timeout", toolName: "bash" as const, input: { command: "id" }, createdAt: new Date().toISOString() };
    emit({ type: "approval_required", approval: request });
    assert.equal(await gate.waitForApproval(request, 5), false);
    throw new Error("approval timed out");
  });
  try {
    await assert.rejects(manager.submit("approval timeout"), /approval timed out/);
    const decision = events.find((event) => event.type === "approval_decided");
    assert.equal(decision?.approvalId, "approval-timeout");
    assert.deepEqual(decision?.taskPatch, { id: manager.list()[0].id, pendingApprovalCount: 0 });
  } finally {
    await new Promise((resolve) => setTimeout(resolve, 25));
    await rm(root, { recursive: true, force: true });
  }
});

test("retry creates a new task record", async () => {
  const root = await mkdtemp(join(tmpdir(), "riftx-subagents-"));
  const manager = new SubagentManager("parent", root, () => undefined, 1, "request");
  await manager.initialize(async ({ task }) => ({ summary: task.task }));
  try {
    const originalPromise = manager.submit("retry me");
    const original = manager.list()[0];
    await originalPromise;
    const retried = await manager.retry(original.id);
    assert.notEqual(retried.id, original.id);
    assert.equal(retried.task, original.task);
    assert.equal(manager.list().length, 2);
    await waitFor(() => manager.list().find((task) => task.id === retried.id)?.status === "completed");
  } finally {
    await new Promise((resolve) => setTimeout(resolve, 25));
    await rm(root, { recursive: true, force: true });
  }
});

test("marks persisted running tasks interrupted and requeues queued tasks", async () => {
  const root = await mkdtemp(join(tmpdir(), "riftx-subagents-"));
  const parentDir = join(root, "parent");
  await mkdir(parentDir, { recursive: true });
  const now = new Date().toISOString();
  await writeFile(join(parentDir, "tasks.json"), JSON.stringify({ tasks: [
    { id: "running", parentSessionId: "parent", threadId: "thread-1", name: "running", task: "running", status: "running", model: "m", createdAt: now, pendingApprovalCount: 2, logs: [] },
    { id: "queued", parentSessionId: "parent", threadId: "", name: "queued", task: "queued", status: "queued", model: "m", createdAt: now, pendingApprovalCount: 0, logs: [] }
  ] }));
  const manager = new SubagentManager("parent", root, () => undefined, 1, "request");
  const started: string[] = [];
  await manager.initialize(async ({ task }) => { started.push(task.id); return { summary: task.task }; });
  try {
    assert.equal(manager.list().find((task) => task.id === "running")?.status, "interrupted");
    await waitFor(() => manager.list().find((task) => task.id === "queued")?.status === "completed");
    assert.deepEqual(started, ["queued"]);
  } finally {
    await new Promise((resolve) => setTimeout(resolve, 25));
    await rm(root, { recursive: true, force: true });
  }
});
