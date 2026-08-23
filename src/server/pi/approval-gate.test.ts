import assert from "node:assert/strict";
import test from "node:test";
import { ApprovalGate } from "./approval-gate";

test("approval gate resolves an explicit decision", async () => {
  const gate = new ApprovalGate();
  const promise = gate.waitForApproval({ id: "a", toolName: "bash", input: { command: "pwd" }, createdAt: new Date().toISOString() });
  assert.equal(gate.decide("a", true), true);
  assert.deepEqual(await promise, { approved: true, task: false });
});

test("approval gate surfaces task-scope decisions to the awaiting caller", async () => {
  const gate = new ApprovalGate();
  const promise = gate.waitForApproval({ id: "task", toolName: "browser", input: { action: "navigate", url: "http://10.0.0.9/" }, createdAt: new Date().toISOString() });
  assert.equal(gate.decide("task", true, true), true);
  assert.deepEqual(await promise, { approved: true, task: true });
});

test("approval gate rejects unknown and timed out requests", async () => {
  const gate = new ApprovalGate();
  const decisions: Array<[string, boolean]> = [];
  gate.onDecision((request, approved) => decisions.push([request.id, approved]));
  assert.equal(gate.decide("missing", true), false);
  const promise = gate.waitForApproval({ id: "b", toolName: "write", input: { path: "x" }, createdAt: new Date().toISOString() }, 5);
  assert.deepEqual(await promise, { approved: false, task: false });
  assert.deepEqual(decisions, [["b", false]]);
});

test("changing approval mode keeps pending requests unless switching to full", async () => {
  const gate = new ApprovalGate();
  const pending = gate.waitForApproval({ id: "mode", toolName: "bash", input: { command: "id" }, createdAt: new Date().toISOString() }, 1000);
  gate.setMode("auto");
  assert.equal(gate.pendingRequests().length, 1);
  gate.setMode("full");
  assert.deepEqual(await pending, { approved: true, task: false });
  assert.deepEqual(gate.pendingRequests(), []);
});

test("approval gate exposes pending requests for stream reconnection", async () => {
  const gate = new ApprovalGate();
  const request = { id: "c", toolName: "bash" as const, input: { command: "id" }, createdAt: new Date().toISOString() };
  const pending = gate.waitForApproval(request, 1000);
  assert.deepEqual(gate.pendingRequests(), [request]);
  gate.rejectAll();
  assert.deepEqual(await pending, { approved: false, task: false });
  assert.deepEqual(gate.pendingRequests(), []);
});

test("approval gate rejects an outstanding request when the agent is aborted", async () => {
  const gate = new ApprovalGate();
  const controller = new AbortController();
  const pending = gate.waitForApproval({ id: "abort", toolName: "bash", input: { command: "sleep 30" }, createdAt: new Date().toISOString() }, 1000, controller.signal);
  controller.abort();
  assert.deepEqual(await pending, { approved: false, task: false });
  assert.deepEqual(gate.pendingRequests(), []);
});

test("task approval only bypasses the exact approved command", () => {
  const gate = new ApprovalGate();
  const cd = { toolName: "bash" as const, input: { command: "cd /tmp" } };
  gate.allowForTask(cd);
  assert.equal(gate.shouldBypass(cd), true);
  assert.equal(gate.shouldBypass({ toolName: "bash", input: { command: "npx http-server" } }), false);
  assert.equal(gate.shouldBypass({ toolName: "write", input: { path: "x" } }), false);
  gate.beginTask();
  assert.equal(gate.shouldBypass(cd), false);
});
