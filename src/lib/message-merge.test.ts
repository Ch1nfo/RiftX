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

test("a reconnect refetch does not duplicate an optimistically echoed user message", () => {
  const local = [
    message({ id: "client-uuid", role: "user", toolCallId: undefined, status: undefined, content: "summarize the repo" }),
    message({ id: "client-uuid-2", role: "assistant", toolCallId: undefined, status: undefined, content: "Here is the summary." })
  ];
  const fetched = [
    message({ id: "record-0-0", role: "user", toolCallId: undefined, status: undefined, content: "summarize the repo" }),
    message({ id: "record-0-1", role: "assistant", toolCallId: undefined, status: undefined, content: "Here is the summary." })
  ];
  const merged = mergeFetchedMessages(local, fetched);
  assert.equal(merged.length, 2);
  assert.equal(merged.filter((item) => item.role === "user").length, 1);
});

test("an in-flight assistant tail survives a refetch that predates it", () => {
  const local = [
    message({ id: "record-0-0", role: "user", toolCallId: undefined, status: undefined, content: "go" }),
    message({ id: "client-uuid", role: "assistant", toolCallId: undefined, status: undefined, content: "partial answ" })
  ];
  const fetched = [
    message({ id: "record-0-0", role: "user", toolCallId: undefined, status: undefined, content: "go" })
  ];
  const merged = mergeFetchedMessages(local, fetched);
  assert.equal(merged.length, 2);
  assert.equal(merged[1].content, "partial answ");
});

test("messages dropped from the server snapshot are not re-appended", () => {
  const local = [
    message({ id: "old-1", role: "assistant", toolCallId: undefined, status: undefined, content: "compacted away" }),
    message({ id: "old-2", role: "assistant", toolCallId: undefined, status: undefined, content: "still present" }),
    message({ id: "client-uuid", role: "assistant", toolCallId: undefined, status: undefined, content: "streaming tail" })
  ];
  const fetched = [
    message({ id: "record-0-0", role: "assistant", toolCallId: undefined, status: undefined, content: "still present" })
  ];
  const merged = mergeFetchedMessages(local, fetched);
  assert.equal(merged.filter((item) => item.content === "compacted away").length, 0);
  assert.equal(merged.filter((item) => item.content === "streaming tail").length, 1);
});

test("an older running snapshot does not resurrect a settled tool card", () => {
  const local = message({ content: "final summary", status: "done" });
  const remote = message({ content: "{\"command\":\"ls\"}", status: "running" });
  const [merged] = mergeFetchedMessages([local], [remote]);
  assert.equal(merged.status, "done");
  assert.equal(merged.content, "final summary");
});

test("a locally settled card stored without a status key still overrides a running snapshot", () => {
  const local: MergeableMessage = { id: "local-1", role: "tool", content: "final summary", toolCallId: "call-1" };
  const remote = message({ content: "{\"command\":\"ls\"}", status: "running" });
  const [merged] = mergeFetchedMessages([local], [remote]);
  assert.notEqual(merged.status, "running");
});

test("a completed snapshot still updates a locally running card", () => {
  const local = message({ content: "{\"command\":\"ls\"}", status: "running" });
  const remote = message({ content: "final summary", status: "done" });
  const [merged] = mergeFetchedMessages([local], [remote]);
  assert.equal(merged.status, "done");
});

test("remote canonical identity wins: hash-image URLs and positional id replace the optimistic echo", () => {
  const local = [message({ id: "client-uuid", role: "user", content: "look", images: [{ src: "data:image/png;base64,QUJD", mimeType: "image/png" }] })];
  const fetched = [message({ id: "sess-1-7-1", role: "user", content: "look", images: [{ src: "/api/sessions/sess-1/messages/image/abc", mimeType: "image/png" }] })];
  const merged = mergeFetchedMessages(local, fetched);
  assert.equal(merged.length, 1);
  assert.equal(merged[0].id, "sess-1-7-1");
  assert.deepEqual(merged[0].images, [{ src: "/api/sessions/sess-1/messages/image/abc", mimeType: "image/png" }], "data URI must not survive once the snapshot's route URL exists");
});

test("same-text duplicate locals: one matches the snapshot, the other stays trailing without stealing it", () => {
  const local = [
    message({ id: "u1", role: "user", content: "continue", images: [{ src: "data:image/png;base64,QQ==", mimeType: "image/png" }] }),
    message({ id: "u2", role: "user", content: "continue", images: [{ src: "data:image/png;base64,V1o=", mimeType: "image/png" }] })
  ];
  const fetched = [message({ id: "srv-latest", role: "user", content: "continue", images: [{ src: "/api/sessions/s/messages/image/xyz", mimeType: "image/png" }] })];
  const merged = mergeFetchedMessages(local, fetched);
  // The snapshot's single message keeps the canonical id + route URL; the
  // unmatched trailing optimistic echo survives (it predates the snapshot).
  assert.equal(merged.length, 2);
  assert.ok(merged.some((message) => message.id === "srv-latest" && message.images?.[0]?.src === "/api/sessions/s/messages/image/xyz"));
});

test("local images are kept when the snapshot predates them (no remote images)", () => {
  const local: MergeableMessage[] = [{ id: "u9", role: "user", content: "pic", images: [{ src: "data:image/png;base64,QUJD", mimeType: "image/png" }] }];
  const fetched: MergeableMessage[] = [{ id: "srv-1", role: "user", content: "pic" }];
  const merged = mergeFetchedMessages(local, fetched);
  assert.equal(merged.length, 1);
  assert.equal(merged[0].id, "srv-1");
  assert.deepEqual(merged[0].images, [{ src: "data:image/png;base64,QUJD", mimeType: "image/png" }]);
});
