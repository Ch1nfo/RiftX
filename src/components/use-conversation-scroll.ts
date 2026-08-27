"use client";

import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { resolveConversationScroll, willCenterScrollMove } from "@/lib/conversation-scroll";
import type { MergeableMessage } from "@/lib/message-merge";

/**
 * The conversation auto-follow machinery: refs, effects, and handlers that
 * keep the viewport pinned to the latest message while the user is idle, and
 * pause/resume follow on intentional scrolls. This cluster went through seven
 * rounds of fixes; isolating it makes the invariants local and testable.
 *
 * Owns: conversation refs, follow flag, jump button state, and the visible
 * message window (load-earlier + batched tool reveals).
 */
export function useConversationScroll(messages: MergeableMessage[], visibleTotal: number, batchSize: number) {
  const conversationRef = useRef<HTMLElement>(null);
  const conversationInnerRef = useRef<HTMLDivElement>(null);
  const shouldAutoScrollRef = useRef(true);
  const lastScrollTopRef = useRef(0);
  const historyScrollRef = useRef<{ height: number; top: number } | null>(null);
  const pendingToolScrollRef = useRef<string | null>(null);
  const [showJumpToLatest, setShowJumpToLatest] = useState(false);
  const [visibleMessageCount, setVisibleMessageCount] = useState(batchSize);

  // Pin synchronously, never through a rAF hop. A deferred pin leaves one
  // frame where the viewport trails the streamed content (distance can grow
  // past the latest threshold), and a native scroll-anchoring adjustment in
  // that window — a thinking/tool block auto-collapsing above the viewport,
  // or the message window sliding when a long conversation grows — arrives
  // as a scroll event that preserves the distance to the bottom while moving
  // scrollTop up: indistinguishable from an intentional upward scroll, it
  // permanently killed auto-follow until the next user action. Pinning inside
  // the commit (layout effect) and directly in the ResizeObserver callback
  // (post-layout, pre-paint) keeps the viewport bottomed before the next
  // frame's scroll events are dispatched — scroll steps run before rAF
  // callbacks, so a deferred pin always lost that race. Whatever residual
  // window late layout (fonts, images) leaves open is covered by the
  // atLatest rule.
  const pinToLatest = () => {
    const conversation = conversationRef.current;
    if (!conversation || !shouldAutoScrollRef.current) return;
    conversation.scrollTop = conversation.scrollHeight;
    lastScrollTopRef.current = conversation.scrollTop;
  };

  useLayoutEffect(() => {
    pinToLatest();
  }, [messages]);

  useEffect(() => {
    const content = conversationInnerRef.current;
    if (!content || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(() => {
      pinToLatest();
    });
    observer.observe(content);
    return () => observer.disconnect();
  }, []);

  // Pausing auto-follow deliberately does not touch the jump button: the
  // button derives from shouldFollow on the next scroll event, so a
  // navigation that ends up not scrolling never flashes the button nor
  // strands follow off with no event to reconcile it.
  const pauseAutoFollow = () => {
    shouldAutoScrollRef.current = false;
  };

  const resumeAutoFollow = () => {
    shouldAutoScrollRef.current = true;
    setShowJumpToLatest(false);
  };

  // Reconcile follow state from real geometry two frames later: if a path
  // paused optimistically (the batched-away tool reveal) and nothing actually
  // scrolled — or its target vanished before rendering, which a refetch or
  // compaction can do — this restores or keeps follow correctly instead of
  // leaving it frozen with no scroll event to re-evaluate it.
  const scheduleFollowReconcile = () => {
    requestAnimationFrame(() => requestAnimationFrame(() => handleConversationScroll()));
  };

  const handleConversationScroll = () => {
    const conversation = conversationRef.current;
    if (!conversation) return;
    const { shouldFollow } = resolveConversationScroll({
      wasFollowing: shouldAutoScrollRef.current,
      previousScrollTop: lastScrollTopRef.current,
      scrollTop: conversation.scrollTop,
      distanceFromBottom: conversation.scrollHeight - conversation.clientHeight - conversation.scrollTop
    });
    lastScrollTopRef.current = conversation.scrollTop;
    shouldAutoScrollRef.current = shouldFollow;
    setShowJumpToLatest((current) => {
      const next = !shouldFollow && messages.length > 0;
      return current === next ? current : next;
    });
  };

  // Shared by the direct evidence click and the deferred (batched-away tool)
  // reveal so both pause follow under the same rule: only when the centering
  // scroll will actually move the viewport, decided with the browser's own
  // clamping applied — a fully-visible or edge-clamped target is a true no-op
  // (no pause, no sub-pixel nudge that could flip follow near the threshold).
  // A missing target returns false with no side effects: the direct probe
  // must not touch follow state (subagent tools legitimately have no main-
  // conversation card), and callers that paused before rendering own the
  // reconciliation themselves.
  const revealToolCard = (toolCallId: string) => {
    const target = document.getElementById(`tool-${encodeURIComponent(toolCallId)}`);
    if (!(target instanceof HTMLDetailsElement)) return false;
    target.open = true;
    const conversation = conversationRef.current;
    if (!conversation) {
      target.scrollIntoView({ behavior: "smooth", block: "center" });
      return true;
    }
    const viewportTop = conversation.getBoundingClientRect().top;
    const rect = target.getBoundingClientRect();
    if (willCenterScrollMove({
      scrollTop: conversation.scrollTop,
      scrollHeight: conversation.scrollHeight,
      clientHeight: conversation.clientHeight,
      targetTop: conversation.scrollTop + rect.top - viewportTop,
      targetHeight: rect.height
    })) {
      pauseAutoFollow();
      target.scrollIntoView({ behavior: "smooth", block: "center" });
    }
    scheduleFollowReconcile();
    return true;
  };

  const requestToolReveal = (toolCallId: string) => {
    pendingToolScrollRef.current = toolCallId;
  };

  /** Grow the visible window to include the given number of newest messages. */
  const expandVisibleMessages = (count: number) => {
    setVisibleMessageCount((current) => Math.max(current, count));
  };

  const loadEarlierMessages = () => {
    const conversation = conversationRef.current;
    if (conversation) historyScrollRef.current = { height: conversation.scrollHeight, top: conversation.scrollTop };
    pauseAutoFollow();
    setVisibleMessageCount((current) => Math.min(visibleTotal, current + batchSize));
  };

  const jumpToLatest = () => {
    resumeAutoFollow();
    const conversation = conversationRef.current;
    if (!conversation) return;
    conversation.scrollTop = conversation.scrollHeight;
    lastScrollTopRef.current = conversation.scrollTop;
    requestAnimationFrame(() => {
      conversation.scrollTop = conversation.scrollHeight;
      handleConversationScroll();
    });
  };

  useLayoutEffect(() => {
    const conversation = conversationRef.current;
    const historyScroll = historyScrollRef.current;
    if (conversation && historyScroll) conversation.scrollTop = historyScroll.top + conversation.scrollHeight - historyScroll.height;
    historyScrollRef.current = null;
    const toolCallId = pendingToolScrollRef.current;
    if (!toolCallId) return;
    pendingToolScrollRef.current = null;
    // Only the deferred reveal pauses follow before its target exists, so
    // only its failure needs reconciling: a target that vanished before
    // rendering (refetch/compaction) must not strand that pause with no
    // scroll event coming.
    if (!revealToolCard(toolCallId)) scheduleFollowReconcile();
    // The count effect intentionally re-runs for history restores too; the
    // reveal closure above is stable enough for this purpose.
  }, [visibleMessageCount]);

  /** Wholesale reset when the active session changes. */
  const resetConversationView = () => {
    shouldAutoScrollRef.current = true;
    lastScrollTopRef.current = 0;
    setVisibleMessageCount(batchSize);
    setShowJumpToLatest(false);
  };

  return {
    conversationRef,
    conversationInnerRef,
    showJumpToLatest,
    visibleMessageCount,
    handleConversationScroll,
    revealToolCard,
    requestToolReveal,
    expandVisibleMessages,
    loadEarlierMessages,
    jumpToLatest,
    pauseAutoFollow,
    resumeAutoFollow,
    resetConversationView
  };
}
