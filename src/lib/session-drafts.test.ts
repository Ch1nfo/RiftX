import assert from "node:assert/strict";
import test from "node:test";
import { sessionDraft, withSessionDraft } from "./session-drafts";

test("composer drafts are isolated by session", () => {
  let drafts = withSessionDraft({}, "session-a", "draft for A");
  drafts = withSessionDraft(drafts, "session-b", "draft for B");

  assert.equal(sessionDraft(drafts, "session-a"), "draft for A");
  assert.equal(sessionDraft(drafts, "session-b"), "draft for B");
  assert.equal(sessionDraft(drafts, "session-c"), "");
});

test("sending or archiving clears only the target session draft", () => {
  const drafts = withSessionDraft(withSessionDraft({}, "session-a", "A"), "session-b", "B");
  const cleared = withSessionDraft(drafts, "session-a", "");

  assert.equal(sessionDraft(cleared, "session-a"), "");
  assert.equal(sessionDraft(cleared, "session-b"), "B");
  assert.equal(sessionDraft(drafts, "session-a"), "A", "the previous state remains immutable");
});

test("the no-session composer cannot own or expose a draft", () => {
  const drafts = { "session-a": "A" };
  assert.equal(withSessionDraft(drafts, "", "orphan"), drafts);
  assert.equal(sessionDraft(drafts, ""), "");
});
