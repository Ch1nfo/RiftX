import test from "node:test";
import assert from "node:assert/strict";
import { getSessionMessages } from "./session-snapshot";
import type { SessionRecord } from "./session-registry";

type BranchEntry = { type: "message"; message: unknown };

function makeRecord(branch: BranchEntry[]): SessionRecord {
  // Real SDK getBranch() returns a FRESH array on every call: identity-based
  // incrementality would silently degrade to full rescans. Mirror that here.
  return {
    id: "sess-1",
    toolStatuses: new Map(),
    sessionManager: { getBranch: () => [...branch] }
  } as unknown as SessionRecord;
}

test("user image parts come back as hash-referenced route URLs on the first text part", async () => {
  const record = makeRecord([
    { type: "message", message: { role: "user", content: [
      { type: "text", text: "look at this" },
      { type: "image", data: "QUJD", mimeType: "image/png" }
    ] } }
  ]);
  const messages = await getSessionMessages(() => Promise.resolve(record));
  assert.equal(messages.length, 1);
  assert.equal(messages[0].role, "user");
  assert.equal(messages[0].content, "look at this");
  const image = messages[0].images?.[0];
  assert.ok(image, "expected an image entry");
  assert.equal(image.mimeType, "image/png");
  assert.match(image.src, new RegExp(`^/api/sessions/sess-1/messages/image/[a-f0-9]{24}$`));
});

test("findTranscriptImage resolves the referenced bytes back from the branch", async () => {
  const { imageRefFor, findTranscriptImage } = await import("./session-snapshot");
  const record = makeRecord([
    { type: "message", message: { role: "user", content: [
      { type: "text", text: "pic" },
      { type: "image", data: "iVBORw0KGgo=", mimeType: "image/png" }
    ] } }
  ]);
  const ref = imageRefFor("iVBORw0KGgo=");
  const image = findTranscriptImage(record, ref);
  assert.ok(image);
  assert.equal(image.mimeType, "image/png");
  assert.equal(image.bytes.toString("base64"), "iVBORw0KGgo=");
  assert.equal(findTranscriptImage(record, "nothex!!"), undefined);
  assert.equal(findTranscriptImage(record, "aaaaaaaaaaaaaaaaaaaaaaaa"), undefined);
});

test("tool results surface their screenshot id for URL-based rendering", async () => {
  const record = makeRecord([
    { type: "message", message: { role: "assistant", content: [{ type: "toolCall", id: "call-1", name: "browser", arguments: { action: "screenshot" } }] } },
    { type: "message", message: { role: "toolResult", toolCallId: "call-1", toolName: "browser", content: [{ type: "text", text: "Screenshot captured: s-1" }, { type: "image", data: "QUJD", mimeType: "image/png" }], details: { screenshotId: "s-abc" } } }
  ]);
  const messages = await getSessionMessages(() => Promise.resolve(record));
  const tool = messages.find((message) => message.role === "tool");
  assert.ok(tool);
  assert.equal(tool.screenshotId, "s-abc");
  assert.equal(tool.toolName, "browser");
});

test("tool results without a screenshot id carry no field", async () => {
  const record = makeRecord([
    { type: "message", message: { role: "assistant", content: [{ type: "toolCall", id: "call-2", name: "bash", arguments: { command: "ls" } }] } },
    { type: "message", message: { role: "toolResult", toolCallId: "call-2", toolName: "bash", content: [{ type: "text", text: "file-a" }] } }
  ]);
  const messages = await getSessionMessages(() => Promise.resolve(record));
  const tool = messages.find((message) => message.role === "tool");
  assert.ok(tool);
  assert.equal("screenshotId" in tool, false);
});

test("SSE tool_end extraction: extractScreenshotId finds nested details", async () => {
  const { extractScreenshotId } = await import("@/lib/tool-result");
  assert.equal(extractScreenshotId({ details: { screenshotId: "s-x" } }), "s-x");
  assert.equal(extractScreenshotId({ content: [{ type: "text", text: "x" }] }), undefined);
  assert.equal(extractScreenshotId(undefined), undefined);
  assert.equal(extractScreenshotId("plain"), undefined);
});

test("image index is incremental across fresh-array getBranch calls", async () => {
  const { imageRefFor, findTranscriptImage } = await import("./session-snapshot");
  const record = makeRecord([
    { type: "message", message: { role: "user", content: [
      { type: "text", text: "one" },
      { type: "image", data: "iVBORw0KGgo=", mimeType: "image/png" }
    ] } }
  ]);
  const first = findTranscriptImage(record, imageRefFor("iVBORw0KGgo="));
  assert.ok(first);
  const indexAfterFirst = record.imageRefIndex;
  assert.ok(indexAfterFirst);
  // A second lookup (fresh branch array, same entry objects) must reuse the
  // same Map — no rebuild, no re-hash of history.
  const second = findTranscriptImage(record, imageRefFor("iVBORw0KGgo="));
  assert.ok(second);
  assert.equal(record.imageRefIndex, indexAfterFirst, "index must not be rebuilt when only the array identity changes");
  // New stable entry objects land in the same index even though getBranch()
  // returns a fresh array each time, matching the real SessionManager.
  const existingEntry = (record.sessionManager as unknown as { getBranch: () => BranchEntry[] }).getBranch()[0]!;
  const newEntry = { type: "message", message: { role: "user", content: [{ type: "text", text: "two" }, { type: "image", data: "R0lGODlh", mimeType: "image/gif" }] } } as BranchEntry;
  (record.sessionManager as unknown as { getBranch: () => BranchEntry[] }).getBranch = () => [existingEntry, newEntry];
  const third = findTranscriptImage(record, imageRefFor("R0lGODlh"));
  assert.equal(third?.mimeType, "image/gif");
  assert.equal(record.imageRefIndex, indexAfterFirst);
  const fourth = findTranscriptImage(record, imageRefFor("R0lGODlh"));
  assert.equal(fourth?.mimeType, "image/gif");
  assert.equal(record.imageRefIndex, indexAfterFirst);
});

test("image index drops refs that leave the current branch", async () => {
  const { imageRefFor, findTranscriptImage } = await import("./session-snapshot");
  const oldEntry = { type: "message", message: { role: "user", content: [{ type: "image", data: "b2xk", mimeType: "image/png" }] } } as BranchEntry;
  let branch: BranchEntry[] = [oldEntry];
  const record = makeRecord([]);
  (record.sessionManager as unknown as { getBranch: () => BranchEntry[] }).getBranch = () => [...branch];
  const oldRef = imageRefFor("b2xk");
  assert.ok(findTranscriptImage(record, oldRef));
  const oldIndex = record.imageRefIndex;

  branch = [];
  assert.equal(findTranscriptImage(record, oldRef), undefined);
  assert.notEqual(record.imageRefIndex, oldIndex, "a rewritten branch must release its old strong-reference index");
  assert.equal(record.imageRefIndex?.size, 0);
});
