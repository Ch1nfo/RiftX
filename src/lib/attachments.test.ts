import test from "node:test";
import assert from "node:assert/strict";
import { attachmentExtension, composeAttachmentText, mergeRecoveredAttachments, promptAttachmentsError, promptImagesError, sessionAttachments, withSessionAttachments, type PromptAttachment } from "./attachments";

const PNG_BASE64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==";

test("image validation: count, mime whitelist, size ceiling", () => {
  const image = (mimeType = "image/png", data = PNG_BASE64) => ({ data, mimeType });
  assert.equal(promptImagesError(undefined), null);
  assert.equal(promptImagesError([image()]), null);
  assert.match(String(promptImagesError([image(), image(), image(), image(), image()])), /at most 4/);
  assert.match(String(promptImagesError([image("image/bmp")])), /image type/);
  assert.match(String(promptImagesError([{ data: "", mimeType: "image/png" }])), /base64 data/);
  assert.match(String(promptImagesError([image("image/png", "A".repeat(Math.ceil(8 * 1024 * 1024 / 3) * 4))])), /8 MB/);
  assert.match(String(promptImagesError("nope")), /array/);
});

test("image validation: malformed base64 and mismatched magic bytes are rejected", () => {
  assert.match(String(promptImagesError([{ data: "not-base64!!!!", mimeType: "image/png" }])), /valid base64/);
  assert.match(String(promptImagesError([{ data: "AAAA", mimeType: "image/png" }])), /does not match/);
  // Valid PNG bytes declared as JPEG must fail the magic check.
  assert.match(String(promptImagesError([{ data: PNG_BASE64, mimeType: "image/jpeg" }])), /does not match/);
});

test("attachment validation: whitelist, per-file and total caps, count", () => {
  const file = (name: string, content = "x") => ({ name, content });
  assert.equal(promptAttachmentsError(undefined), null);
  assert.equal(promptAttachmentsError([file("notes.md"), file("data.json")]), null);
  assert.match(String(promptAttachmentsError([file("report.pdf")])), /not supported/);
  assert.match(String(promptAttachmentsError([file("noext")])), /not supported/);
  assert.match(String(promptAttachmentsError([file("a.exe")])), /not supported/);
  assert.match(String(promptAttachmentsError([file("big.txt", "x".repeat(2 * 1024 * 1024 + 1))])), /2 MB/);
  const many: PromptAttachment[] = Array.from({ length: 6 }, (_, index) => file(`f${index}.txt`));
  assert.match(String(promptAttachmentsError(many)), /at most 5/);
  const wide = Array.from({ length: 4 }, () => file("w.txt", "y".repeat(1_100_000)));
  assert.match(String(promptAttachmentsError(wide)), /4 MB/);
  assert.match(String(promptAttachmentsError([{ name: " ", content: "x" }])), /name/);
});

test("byte caps count UTF-8 bytes, not code units", () => {
  // 5 files × ~700k CJK chars ≈ 2.1 MB code units but ~6.3 MB UTF-8 bytes:
  // a code-unit counter admits this; a byte counter must reject it.
  const cjk = "汉".repeat(700_000);
  const sneaky = Array.from({ length: 5 }, (_, index) => ({ name: `f${index}.txt`, content: cjk }));
  assert.match(String(promptAttachmentsError(sneaky)), /4 MB|2 MB/);
  // A single file at 2 MB of CJK (≈700k chars, 2.1 MB bytes) exceeds per-file cap.
  assert.match(String(promptAttachmentsError([{ name: "one.txt", content: "汉".repeat(700_000) }])), /2 MB/);
});

test("extension extraction is lowercase and extension-only", () => {
  assert.equal(attachmentExtension("A.PNG"), "png");
  assert.equal(attachmentExtension("archive.tar.gz"), "gz");
  assert.equal(attachmentExtension("noext"), "");
});

test("composeAttachmentText renders fenced blocks with names and languages", () => {
  const composed = composeAttachmentText([{ name: "notes.md", content: "# hi" }, { name: "data.json", content: "{}" }]);
  assert.match(composed, /--- attachment: notes\.md \(4 chars\) ---/);
  assert.match(composed, /```md\n# hi\n```/);
  assert.match(composed, /```json\n\{\}\n```/);
  assert.equal(composeAttachmentText([]), "");
});

test("content containing its own fence grows the delimiter instead of breaking out", () => {
  const composed = composeAttachmentText([{ name: "readme.md", content: "before\n```\ninner\n```\nafter" }]);
  // The block uses a 4-backtick fence; the inner 3-backtick run cannot close it.
  assert.ok(composed.includes("````md\nbefore\n```\ninner\n```\nafter\n````"), composed);
  assert.equal(composed.split("````").length, 3);
});

test("composer attachment state is isolated per session", () => {
  const first: PromptAttachment[] = [{ name: "a.txt", content: "x" }];
  let state = withSessionAttachments({}, "session-a", first);
  state = withSessionAttachments(state, "session-b", [{ name: "b.txt", content: "y" }]);
  assert.deepEqual(sessionAttachments(state, "session-a"), first);
  assert.deepEqual(sessionAttachments(state, "session-b"), [{ name: "b.txt", content: "y" }]);
  assert.deepEqual(sessionAttachments(state, ""), []);
  // Clearing one session leaves the other untouched.
  state = withSessionAttachments(state, "session-a", []);
  assert.deepEqual(sessionAttachments(state, "session-a"), []);
  assert.equal(sessionAttachments(state, "session-b").length, 1);
});

test("failed attachment recovery keeps overflow visible and lets newer duplicates win", () => {
  const existing = Array.from({ length: 4 }, (_, index) => ({ kind: "image" as const, name: `new-${index}.png`, value: `new-${index}` }));
  const restored = [
    { kind: "image" as const, name: "new-0.png", value: "old-duplicate" },
    ...Array.from({ length: 4 }, (_, index) => ({ kind: "image" as const, name: `old-${index}.png`, value: `old-${index}` }))
  ];
  const merged = mergeRecoveredAttachments(existing, restored);
  assert.equal(merged.length, 8, "recovery must not silently trim the failed batch");
  assert.equal(merged.find((item) => item.name === "new-0.png")?.value, "new-0");
  assert.ok(merged.some((item) => item.name === "old-3.png"));
});
