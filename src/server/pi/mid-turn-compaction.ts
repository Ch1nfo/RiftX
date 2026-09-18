import type { AgentSession } from "@mariozechner/pi-coding-agent";
export { estimateCompactedUsage, estimateMessagesContextUsage } from "./context-usage";

import { installAutoCompactionRetryPolicy, replaceAgentMessages, runAutoCompaction, waitForAgentEvents } from "./pi-internals";
import { refreshContinuityContext, upsertContinuityContext, type ContinuityContext } from "./continuity-context";
import { keepRecentTokensForContext } from "./compaction-budget";
import { compactionBlocked } from "./compaction-retry";

export { COMPACTION_KEEP_RECENT_RATIO, keepRecentTokensForContext } from "./compaction-budget";

const budgetInstalled = new WeakSet<object>();

/** Pi computes its cut point from SettingsManager, so apply the 10% target there. */
function installCompactionBudget(session: AgentSession) {
  const manager = session.settingsManager;
  if (budgetInstalled.has(manager)) return;
  budgetInstalled.add(manager);
  const original = manager.getCompactionSettings.bind(manager);
  manager.getCompactionSettings = () => {
    const settings = original();
    const keepRecentTokens = keepRecentTokensForContext(session.model?.contextWindow ?? 0);
    return keepRecentTokens ? { ...settings, keepRecentTokens } : settings;
  };
}

export function shouldCompactBeforeSampling(tokens: number | null | undefined, contextWindow: number, reserveTokens: number) {
  return Number.isFinite(tokens)
    && Number(tokens) > 0
    && Number.isFinite(contextWindow)
    && contextWindow > 0
    && Number(tokens) > Math.max(0, contextWindow - Math.max(0, reserveTokens));
}

async function runMidTurnCompaction(session: AgentSession, signal?: AbortSignal) {
  if (signal?.aborted) throw new Error("Mid-turn compaction was cancelled");

  let result: unknown;
  const unsubscribe = session.subscribe((event) => {
    if (event.type === "compaction_end" && event.reason === "threshold") {
      result = event.result;
    }
  });
  const abortCompaction = () => session.abortCompaction();
  if (signal) {
    signal.addEventListener("abort", abortCompaction, { once: true });
  }
  try {
    await runAutoCompaction(session);
  } finally {
    unsubscribe();
    signal?.removeEventListener("abort", abortCompaction);
  }
  if (!result) {
    if (signal?.aborted) throw new Error("Mid-turn compaction was cancelled");
    return false;
  }
  return true;
}

/**
 * Keep the Agent's active loop alive while replacing its detached context after a
 * tool turn. The public compact() API aborts the active run, so this uses the
 * SDK's auto-compaction path and keeps the current message array in place.
 */
export function installMidTurnCompaction(session: AgentSession, getContinuityContext?: () => Promise<ContinuityContext>) {
  installCompactionBudget(session);
  installAutoCompactionRetryPolicy(session);
  const agent = session.agent;
  const originalTransform = agent.transformContext;
  let compacting = false;

  agent.transformContext = async (messages, signal) => {
    const transformed = originalTransform ? await originalTransform(messages, signal) : messages;
    if (compacting || signal?.aborted || compactionBlocked(session)) return transformed;

    const settings = session.settingsManager.getCompactionSettings();
    const contextWindow = session.model?.contextWindow ?? 0;
    const usage = session.getContextUsage();
    if (!settings.enabled || !shouldCompactBeforeSampling(usage?.percent === null ? null : usage?.tokens, contextWindow, settings.reserveTokens)) {
      return transformed;
    }

    // The session file must be settled before compaction reads its branch, but
    // avoid paying this await on ordinary sampling turns far below the limit.
    await waitForAgentEvents(session);
    const settledUsage = session.getContextUsage();
    if (!shouldCompactBeforeSampling(settledUsage?.percent === null ? null : settledUsage?.tokens, contextWindow, settings.reserveTokens)) {
      return transformed;
    }

    compacting = true;
    try {
      const compacted = await runMidTurnCompaction(session, signal);
      if (compacted) {
        replaceAgentMessages(session, messages, session.agent.state.messages);
        if (getContinuityContext) {
          try {
            const continuity = await getContinuityContext();
            // `messages` is the detached array currently being sampled while
            // auto-compaction replaces agent.state.messages with a new array.
            // Refresh both so this very request and every later turn see the
            // same durable continuity state.
            upsertContinuityContext(messages as unknown[], continuity);
            refreshContinuityContext(session, continuity);
          } catch (error) {
            console.warn("RiftX could not refresh continuity context after compaction:", error);
          }
        }
      }
      return compacted ? messages : transformed;
    } finally {
      compacting = false;
    }
  };
}
