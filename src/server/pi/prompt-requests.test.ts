import test from "node:test";
import assert from "node:assert/strict";
import { beginPromptRequest, promptRequestStates, settlePromptRequest } from "./prompt-requests";

test("prompt request lifecycle moves pending to its first terminal state", () => {
  const record: { promptRequests?: Map<string, { state: "pending" | "accepted" | "failed"; error?: string }> } = {};
  beginPromptRequest(record, "accepted");
  beginPromptRequest(record, "rejected");
  settlePromptRequest(record, "accepted", "accepted");
  settlePromptRequest(record, "rejected", "failed", "no key");
  // A later runtime error cannot turn an already accepted request into a
  // retryable failure and duplicate its attachments.
  settlePromptRequest(record, "accepted", "failed", "late provider error");

  assert.deepEqual(promptRequestStates(record), { accepted: "accepted", rejected: "failed" });
  assert.equal(record.promptRequests?.get("accepted")?.error, undefined);
  assert.equal(record.promptRequests?.get("rejected")?.error, "no key");
});

test("terminal pruning never evicts unresolved requests", () => {
  const record: { promptRequests?: Map<string, { state: "pending" | "accepted" | "failed"; error?: string }> } = {};
  beginPromptRequest(record, "still-pending");
  for (let index = 0; index < 70; index += 1) {
    const requestId = `done-${index}`;
    beginPromptRequest(record, requestId);
    settlePromptRequest(record, requestId, "accepted");
  }

  const states = promptRequestStates(record);
  assert.equal(states["still-pending"], "pending");
  assert.equal(states["done-0"], undefined);
  assert.equal(states["done-69"], "accepted");
  assert.equal(Object.values(states).filter((state) => state !== "pending").length, 64);
});

test("beginPromptRequest is an idempotency key: a known id reports duplicate", () => {
  const record: { promptRequests?: Map<string, { state: "pending" | "accepted" | "failed"; error?: string }> } = {};
  assert.equal(beginPromptRequest(record, "req-1"), true);
  assert.equal(beginPromptRequest(record, "req-1"), false, "replay must not re-dispatch");
  assert.equal(beginPromptRequest(record, "req-2"), true);
  assert.equal(beginPromptRequest(record, undefined), true, "no id is never a duplicate");
});
