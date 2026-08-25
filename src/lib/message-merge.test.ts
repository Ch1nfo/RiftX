import assert from "node:assert/strict";
import test from "node:test";
import { mergeFetchedMessages, type MergeableMessage } from "./message-merge";

function message(overrides: Partial<MergeableMessage> = {}): MergeableMessage {
  return { id: "message-1", role: "tool", content: "", toolCallId: "call-1", status: "running", ...overrides };
}

test("tool snapshots keep the longer content but use remote terminal state", () => {
  const local = message({ content: "{\"command\":\"longer\"}\\nStopped", status: "cancelled", isError: true });
  const remote = message({ content: "ok", status: "done", isError: false });
  const [merged] = mergeFetchedMessages([local], [remote]);
  assert.equal(merged.content, local.content);
  assert.equal(merged.status, "done");
  assert.equal(merged.isError, false);
});

test("text snapshots preserve the longer local stream", () => {
  const local = message({ id: "text", role: "assistant", toolCallId: undefined, content: "complete response" });
  const remote = message({ id: "text", role: "assistant", toolCallId: undefined, content: "complete" });
  const [merged] = mergeFetchedMessages([local], [remote]);
  assert.equal(merged.content, "complete response");
});
