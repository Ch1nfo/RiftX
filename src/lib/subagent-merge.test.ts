import assert from "node:assert/strict";
import test from "node:test";
import { applySubagentTaskPatch, mergeSubagentTasks } from "./subagent-merge";
import { parseRiftxEvent, type SubagentTask } from "./types";

function task(overrides: Partial<SubagentTask> = {}): SubagentTask {
  return {
    id: "task-1", parentSessionId: "parent", threadId: "thread", name: "Subagent", task: "inspect",
    status: "running", model: "model", createdAt: "2026-01-01T00:00:00.000Z", pendingApprovalCount: 0, logs: [], ...overrides
  };
}

test("keeps a newer subagent task when a stale snapshot arrives", () => {
  const current = task({ logs: [{ id: "log", type: "text", content: "new", status: "done", createdAt: "2026-01-01T00:00:01.000Z" }] });
  const stale = task({ status: "queued" });
  const existing = [current];
  assert.strictEqual(mergeSubagentTasks(existing, [stale]), existing);
});

test("applies bounded log patches without mutating the source task", () => {
  const current = task();
  const updated = applySubagentTaskPatch(current, { id: current.id, appendLog: { id: "log", type: "tool", content: "output", status: "done", createdAt: "2026-01-01T00:00:01.000Z" } });
  assert.equal(current.logs.length, 0);
  assert.equal(updated.logs[0]?.content, "output");
});

test("rejects malformed or unknown SSE events at the protocol boundary", () => {
  assert.equal(parseRiftxEvent({ type: "unknown" }), null);
  assert.equal(parseRiftxEvent({ type: "connected" }), null);
  assert.equal(parseRiftxEvent({ type: "done" })?.type, "done");
});
