import assert from "node:assert/strict";
import test from "node:test";
import { DEFAULT_BASH_TIMEOUT_SECONDS, MAX_BASH_TIMEOUT_SECONDS, resolveBashTimeout } from "./bash-timeout-policy";

test("bash timeout always has a finite upper bound", () => {
  assert.equal(resolveBashTimeout(), DEFAULT_BASH_TIMEOUT_SECONDS);
  assert.equal(resolveBashTimeout(0), DEFAULT_BASH_TIMEOUT_SECONDS);
  assert.equal(resolveBashTimeout(10), 10);
  assert.equal(resolveBashTimeout(MAX_BASH_TIMEOUT_SECONDS + 1), MAX_BASH_TIMEOUT_SECONDS);
});
