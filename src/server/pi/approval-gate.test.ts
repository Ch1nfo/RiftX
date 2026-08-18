import assert from "node:assert/strict";
import test from "node:test";
import { ApprovalGate } from "./approval-gate";

test("approval gate resolves an explicit decision", async () => {
  const gate = new ApprovalGate();
  const promise = gate.waitForApproval({ id: "a", toolName: "bash", input: { command: "pwd" }, createdAt: new Date().toISOString() });
  assert.equal(gate.decide("a", true), true);
  assert.equal(await promise, true);
});

test("approval gate rejects unknown and timed out requests", async () => {
  const gate = new ApprovalGate();
  assert.equal(gate.decide("missing", true), false);
  const promise = gate.waitForApproval({ id: "b", toolName: "write", input: { path: "x" }, createdAt: new Date().toISOString() }, 5);
  assert.equal(await promise, false);
});

test("approval gate exposes pending requests for stream reconnection", async () => {
  const gate = new ApprovalGate();
  const request = { id: "c", toolName: "bash" as const, input: { command: "id" }, createdAt: new Date().toISOString() };
  const pending = gate.waitForApproval(request, 1000);
  assert.deepEqual(gate.pendingRequests(), [request]);
  gate.rejectAll();
  assert.equal(await pending, false);
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
