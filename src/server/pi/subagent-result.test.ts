import assert from "node:assert/strict";
import test from "node:test";
import { extractLastAssistantResult, buildSummaryTranscript } from "./subagent-result";

test("reports the provider error from the final assistant entry", () => {
  assert.deepEqual(extractLastAssistantResult([
    { type: "message", message: { role: "assistant", stopReason: "error", errorMessage: "429 rate limit", content: [] } }
  ]), { error: "429 rate limit" });
});

test("skips toolUse preamble text and finds the actual final response", () => {
  assert.deepEqual(extractLastAssistantResult([
    { type: "message", message: { role: "assistant", stopReason: "toolUse", content: [{ type: "text", text: "I will inspect this." }, { type: "toolCall", name: "bash" }] } },
    { type: "message", message: { role: "toolResult", content: [{ type: "text", text: "output" }] } },
    { type: "message", message: { role: "assistant", stopReason: "stop", content: [{ type: "text", text: "Found SQL injection in /api/users" }] } }
  ]), { summary: "Found SQL injection in /api/users" });
});

test("scans past toolUse-only entries to find an earlier text response", () => {
  assert.deepEqual(extractLastAssistantResult([
    { type: "message", message: { role: "assistant", stopReason: "stop", content: [{ type: "text", text: "Earlier final answer" }] } },
    { type: "message", message: { role: "assistant", stopReason: "toolUse", content: [{ type: "toolCall", name: "browser" }] } }
  ]), { summary: "Earlier final answer" });
});

test("does not return toolUse preamble as summary when no final text exists", () => {
  assert.deepEqual(extractLastAssistantResult([
    { type: "message", message: { role: "assistant", stopReason: "toolUse", content: [{ type: "text", text: "I will check this." }, { type: "toolCall", name: "bash" }] } }
  ]), {});
});

test("extracts partial text from a length-truncated response", () => {
  assert.deepEqual(extractLastAssistantResult([
    { type: "message", message: { role: "assistant", stopReason: "length", content: [{ type: "text", text: "Partial findings that got cut" }] } }
  ]), { summary: "Partial findings that got cut" });
});

test("aborted is terminal even when earlier text exists", () => {
  assert.deepEqual(extractLastAssistantResult([
    { type: "message", message: { role: "assistant", stopReason: "stop", content: [{ type: "text", text: "done" }] } },
    { type: "message", message: { role: "assistant", stopReason: "aborted", content: [] } }
  ]), {});
});

test("skips unknown or missing stopReason entries", () => {
  assert.deepEqual(extractLastAssistantResult([
    { type: "message", message: { role: "assistant", stopReason: "unknown", content: [{ type: "text", text: "not a result" }] } },
    { type: "message", message: { role: "assistant", content: [{ type: "text", text: "no stopReason either" }] } },
    { type: "message", message: { role: "assistant", stopReason: "stop", content: [{ type: "text", text: "actual result" }] } }
  ]), { summary: "actual result" });
});

test("returns empty when only unknown stopReason entries have text", () => {
  assert.deepEqual(extractLastAssistantResult([
    { type: "message", message: { role: "assistant", stopReason: "weird", content: [{ type: "text", text: "some text" }] } }
  ]), {});
});

test("buildSummaryTranscript includes tool results with tool names", () => {
  const transcript = buildSummaryTranscript([
    { type: "message", message: { role: "assistant", content: [{ type: "text", text: "Checking SQL injection" }] } },
    { type: "message", message: { role: "toolResult", toolName: "bash", content: [{ type: "text", text: "Found vulnerability in /api/users" }] } },
    { type: "message", message: { role: "assistant", content: [{ type: "text", text: "Confirmed SQLi" }] } }
  ]);
  assert.match(transcript, /Checking SQL injection/);
  assert.match(transcript, /\[bash\] Found vulnerability/);
  assert.match(transcript, /Confirmed SQLi/);
});

test("buildSummaryTranscript truncates long tool results", () => {
  const long = "x".repeat(1000);
  const transcript = buildSummaryTranscript([
    { type: "message", message: { role: "toolResult", toolName: "bash", content: [{ type: "text", text: long }] } }
  ]);
  assert.ok(transcript.length < 600, `tool result should be truncated, got ${transcript.length}`);
  assert.match(transcript, /\[bash\]/);
});

test("buildSummaryTranscript skips empty and non-message entries", () => {
  const transcript = buildSummaryTranscript([
    null,
    { type: "compaction" },
    { type: "message", message: { role: "user", content: "task text" } },
    { type: "message", message: { role: "toolResult", toolName: "browser", content: [] } }
  ]);
  assert.equal(transcript, "");
});
