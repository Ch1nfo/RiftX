import assert from "node:assert/strict";
import test from "node:test";
import { resolveConversationScroll, willCenterScrollMove } from "./conversation-scroll";

test("content growth does not disable an active conversation follow", () => {
  assert.deepEqual(resolveConversationScroll({
    wasFollowing: true,
    previousScrollTop: 500,
    scrollTop: 500,
    distanceFromBottom: 80
  }), { atLatest: false, shouldFollow: true });
});

test("content collapse at the bottom does not pause following", () => {
  assert.deepEqual(resolveConversationScroll({
    wasFollowing: true,
    previousScrollTop: 800,
    scrollTop: 640,
    distanceFromBottom: 0
  }), { atLatest: true, shouldFollow: true });

  assert.deepEqual(resolveConversationScroll({
    wasFollowing: true,
    previousScrollTop: 800,
    scrollTop: 640,
    distanceFromBottom: 12
  }), { atLatest: true, shouldFollow: true });
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

test("centering a target off the viewport center requires a scroll", () => {
  // 1920px content in a 720px viewport (max scrollTop 1200); the target
  // centered at content offset 900+20 needs scrollTop 560, not 300.
  assert.equal(willCenterScrollMove({
    scrollTop: 300,
    scrollHeight: 1920,
    clientHeight: 720,
    targetTop: 900,
    targetHeight: 40
  }), true);
});

test("bottom clamping makes an off-center target a no-op scroll", () => {
  // Already at the exact bottom (scrollTop = max = 1200); the desired
  // position 1240 clamps back to 1200, so following must not be paused.
  assert.equal(willCenterScrollMove({
    scrollTop: 1200,
    scrollHeight: 1920,
    clientHeight: 720,
    targetTop: 1580,
    targetHeight: 40
  }), false);
});

test("a container without overflow never scrolls", () => {
  assert.equal(willCenterScrollMove({
    scrollTop: 0,
    scrollHeight: 600,
    clientHeight: 720,
    targetTop: 100,
    targetHeight: 40
  }), false);
});

test("an already-centered target is a no-op even while paused near the threshold", () => {
  // Desired 840+20-360 = 500 equals scrollTop; skipping the call avoids a
  // sub-pixel nudge that could re-enable follow just inside the threshold.
  assert.equal(willCenterScrollMove({
    scrollTop: 500,
    scrollHeight: 1920,
    clientHeight: 720,
    targetTop: 840,
    targetHeight: 40
  }), false);
});

test("a taller-than-viewport target starting above the viewport scrolls", () => {
  // Target spans 100..2100 in content coordinates; centering wants 750.
  assert.equal(willCenterScrollMove({
    scrollTop: 800,
    scrollHeight: 3000,
    clientHeight: 700,
    targetTop: 100,
    targetHeight: 2000
  }), true);
});
