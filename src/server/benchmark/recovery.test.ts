import assert from "node:assert/strict";
import test from "node:test";
import { ModelRecovery, recoverableModelError } from "./recovery";

test("recover only transient errors; auth, context and cancellation remain terminal", () => {
  for (const message of ["429 rate limit", "503 overloaded", "ECONNRESET", "fetch failed", "request timed out"]) assert.ok(recoverableModelError(message));
  for (const message of ["401 invalid api key", "insufficient_quota", "context length exceeded", "Model stopped: aborted", "unknown fixture bug"]) assert.equal(recoverableModelError(message), false);
});

test("bounded backoff preserves active children when main recovery is exhausted", () => {
  let now = 1;
  const recovery = new ModelRecovery(() => now, [5, 15, 30]);
  for (const delay of [5, 15, 30]) {
    recovery.failed("ECONNRESET");
    assert.equal(recovery.decision(true).action, "wait");
    now += delay;
    assert.equal(recovery.decision(true).action, "retry");
    recovery.dispatched();
  }
  recovery.failed("ECONNRESET");
  assert.equal(recovery.decision(true).action, "wait");
  assert.equal(recovery.decision(false).action, "fail");
  recovery.succeeded();
  assert.equal(recovery.decision(false).action, "continue");
  recovery.failed("ECONNRESET");
  assert.equal(recovery.decision(false).attempt, 0);
});
