import assert from "node:assert/strict";
import test from "node:test";
import { bindToolEvidence, requireFindingEvidence, resolveToolEvidence } from "./finding-evidence";

test("confirmed findings reject quote-only evidence", () => {
  assert.throws(
    () => requireFindingEvidence("confirmed", [{ type: "quote", quote: "model observation" }]),
    /requires at least one resolvable/
  );
  assert.doesNotThrow(() => requireFindingEvidence("suspected", [{ type: "quote", quote: "observable response difference" }]));
  assert.doesNotThrow(() => requireFindingEvidence("confirmed", [{ type: "request", requestRef: "r-1" }]));
  assert.throws(
    () => requireFindingEvidence("confirmed", [{ type: "tool", toolCallId: "record-1", toolName: "record_finding", content: "Finding recorded" }]),
    /requires at least one resolvable/
  );
  assert.throws(
    () => requireFindingEvidence("confirmed", [{ type: "tool", toolCallId: "spawn-1", toolName: "spawn_subagent", content: "Child reported XSS" }]),
    /requires at least one resolvable/
  );
});

test("unresolved or empty tool references cannot become evidence", () => {
  assert.equal(resolveToolEvidence([], "call-1"), undefined);
  assert.equal(resolveToolEvidence([
    { role: "assistant", content: [{ type: "toolCall", id: "call-1", name: "bash", arguments: {} }] },
    { role: "toolResult", toolCallId: "call-1", content: [] }
  ], "call-1"), undefined);
});

test("tool identity and content are derived from the real transcript", () => {
  const evidence = resolveToolEvidence([
    { role: "assistant", content: [{ type: "toolCall", id: "call-1", name: "bash", arguments: { command: "probe" } }] },
    { role: "toolResult", toolCallId: "call-1", content: [{ type: "text", text: "HTTP 200 with cross-tenant marker" }] }
  ], "call-1");
  assert.deepEqual(evidence, {
    type: "tool",
    toolCallId: "call-1",
    toolName: "bash",
    content: "HTTP 200 with cross-tenant marker"
  });
});

test("suspected findings keep unresolved tool references after compaction", () => {
  const requested = { type: "tool" as const, toolCallId: "gone-1", toolName: "bash" };
  assert.deepEqual(bindToolEvidence(requested, [], "suspected"), requested);
  assert.deepEqual(bindToolEvidence(requested, [], "likely"), requested);
});

test("confirmed findings reject unresolved or empty tool references", () => {
  const requested = { type: "tool" as const, toolCallId: "gone-1", toolName: "bash" };
  assert.throws(() => bindToolEvidence(requested, [], "confirmed"), /Unknown or empty tool evidence gone-1/);
  assert.throws(() => bindToolEvidence(requested, [
    { role: "assistant", content: [{ type: "toolCall", id: "gone-1", name: "bash" }] },
    { role: "toolResult", toolCallId: "gone-1", content: [] }
  ], "confirmed"), /Unknown or empty tool evidence gone-1/);
});
