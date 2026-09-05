import test from "node:test";
import assert from "node:assert/strict";
import { promptRequestDisposition } from "./prompt-request-state";

test("prompt request reconciliation consumes only explicit terminal states", () => {
  assert.equal(promptRequestDisposition("failed", false), "restore");
  assert.equal(promptRequestDisposition("accepted", true), "clear");
  assert.equal(promptRequestDisposition("accepted", false), "keep", "wait for POST composition before clearing");
  assert.equal(promptRequestDisposition("pending", true), "keep");
  assert.equal(promptRequestDisposition(undefined, true), "keep", "unknown is not implicit success");
});

