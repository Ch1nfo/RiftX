import assert from "node:assert/strict";
import test from "node:test";
import { extractLastAssistantResult } from "./subagent-result";

test("reports the provider error from the final assistant entry", () => {
  assert.deepEqual(extractLastAssistantResult([
    { type: "message", message: { role: "assistant", stopReason: "error", errorMessage: "429 rate limit", content: [] } }
  ]), { error: "429 rate limit" });
});
