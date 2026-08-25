import assert from "node:assert/strict";
import test from "node:test";
import { extractLastAssistantResult, extractLastAssistantText } from "./subagent-result";

test("extracts the latest non-empty assistant text from a branch", () => {
  assert.equal(extractLastAssistantText([
    { type: "message", message: { role: "assistant", content: [{ type: "text", text: "first" }] } },
    { type: "message", message: { role: "assistant", content: [{ type: "toolCall", name: "bash" }] } },
    { type: "message", message: { role: "toolResult", content: [{ type: "text", text: "tool output" }] } },
    { type: "message", message: { role: "assistant", content: [{ type: "text", text: "latest" }] } }
  ]), "latest");
});

test("ignores assistant thinking and empty text messages", () => {
  assert.equal(extractLastAssistantText([
    { type: "message", message: { role: "assistant", content: [{ type: "thinking", thinking: "internal" }, { type: "text", text: "   " }] } },
    { type: "message", message: { role: "assistant", content: [{ type: "toolCall", name: "bash" }] } }
  ]), undefined);
});

test("ignores text from aborted or failed assistant messages", () => {
  assert.equal(extractLastAssistantText([
    { type: "message", message: { role: "assistant", stopReason: "aborted", content: [{ type: "text", text: "partial" }] } },
    { type: "message", message: { role: "assistant", stopReason: "error", content: [{ type: "text", text: "error detail" }] } }
  ]), undefined);
});

test("reports the provider error from the final assistant entry", () => {
  assert.deepEqual(extractLastAssistantResult([
    { type: "message", message: { role: "assistant", stopReason: "error", errorMessage: "429 rate limit", content: [] } }
  ]), { error: "429 rate limit" });
});

test("does not fall back to an earlier progress message", () => {
  assert.equal(extractLastAssistantText([
    { type: "message", message: { role: "assistant", stopReason: "stop", content: [{ type: "text", text: "Enumerating endpoints..." }] } },
    { type: "message", message: { role: "assistant", stopReason: "toolUse", content: [{ type: "toolCall", name: "bash" }] } }
  ]), undefined);
});
