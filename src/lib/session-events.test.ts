import assert from "node:assert/strict";
import test from "node:test";
import type { SessionEventContext } from "./session-events";
import { applyRiftxEvent } from "./session-events";

function context() {
  const calls: string[] = [];
  const noop = () => undefined;
  const ctx: SessionEventContext = {
    activeId: "session-1",
    t: (key) => key,
    queueMessageDelta: noop,
    flushMessageDeltas: () => calls.push("flush"),
    setMessages: (update) => { update([]); },
    setFindings: noop,
    queueSubagentTask: noop,
    queueSubagentTaskPatch: noop,
    setApprovalQueue: noop,
    setUsage: noop,
    setSessions: noop,
    setSessionRunning: noop,
    setMainAgentRunning: noop,
    setContextCompacting: noop,
    setStreamReady: (ready) => calls.push(`ready:${ready}`),
    reconcileMessages: () => calls.push("reconcile"),
    setError: noop
  };
  return { calls, ctx };
}

test("connected marks the stream ready and reconciles the cold-start gap", () => {
  const { calls, ctx } = context();
  applyRiftxEvent({ type: "connected", sessionId: "session-1" }, ctx);
  assert.deepEqual(calls, ["ready:true", "reconcile"]);
});

test("the authoritative turn message reconciles missed text deltas", () => {
  const { calls, ctx } = context();
  applyRiftxEvent({ type: "message", turnEnd: true, message: { role: "assistant", content: [{ type: "text", text: "complete" }] } }, ctx);
  assert.deepEqual(calls, ["flush", "reconcile"]);
});

test("intermediate Pi message lifecycle events do not refetch the snapshot", () => {
  const { calls, ctx } = context();
  applyRiftxEvent({ type: "message", message: { type: "message_end" } }, ctx);
  assert.deepEqual(calls, ["flush"]);
});

test("done performs a final reconciliation when the turn message was also missed", () => {
  const { calls, ctx } = context();
  applyRiftxEvent({ type: "done" }, ctx);
  assert.deepEqual(calls, ["flush", "reconcile"]);
});

test("compaction failure diagnostics do not mark a still-running agent as stopped", () => {
  const { calls, ctx } = context();
  ctx.setError = (message) => calls.push(`error:${message}`);
  ctx.setMainAgentRunning = (running) => calls.push(`running:${running}`);
  ctx.setContextCompacting = (compacting) => calls.push(`compacting:${compacting}`);
  applyRiftxEvent({ type: "session_state", state: "running", reason: "threshold", error: "Checkpoint validation failed" }, ctx);
  assert.deepEqual(calls, ["flush", "running:true", "compacting:false", "error:Checkpoint validation failed"]);
});
