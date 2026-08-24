import assert from "node:assert/strict";
import test from "node:test";
import { resolveConversationScroll } from "./conversation-scroll";

test("content growth does not disable an active conversation follow", () => {
  assert.deepEqual(resolveConversationScroll({
    wasFollowing: true,
    previousScrollTop: 500,
    scrollTop: 500,
    distanceFromBottom: 80
  }), { atLatest: false, shouldFollow: true });
});

test("moving upward pauses following until the user returns to the bottom", () => {
  assert.deepEqual(resolveConversationScroll({
    wasFollowing: true,
    previousScrollTop: 500,
    scrollTop: 420,
    distanceFromBottom: 100
  }), { atLatest: false, shouldFollow: false });

  assert.deepEqual(resolveConversationScroll({
    wasFollowing: false,
    previousScrollTop: 420,
    scrollTop: 500,
    distanceFromBottom: 0
  }), { atLatest: true, shouldFollow: true });
});
