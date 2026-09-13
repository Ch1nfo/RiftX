import type { AgentSession } from "@mariozechner/pi-coding-agent";
export { estimateCompactedUsage, estimateMessagesContextUsage } from "./context-usage";

import { replaceAgentMessages, runAutoCompaction, waitForAgentEvents } from "./pi-internals";
import { refreshContinuityContext, upsertContinuityContext, type ContinuityContext } from "./continuity-context";
import { assertBenchmarkSamplingAllowed, benchmarkInputLimit, benchmarkReserveTokens, BenchmarkContextBudgetError, estimateBenchmarkInputTokens, keepRecentTokensForContext } from "./compaction-budget";

export { COMPACTION_KEEP_RECENT_RATIO, keepRecentTokensForContext } from "./compaction-budget";

const budgetInstalled = new WeakSet<object>();

/** Pi computes its cut point from SettingsManager, so apply the 10% ceiling at that source. */
function installCompactionBudget(session: AgentSession, benchmark = false) {
  const manager = session.settingsManager;
  if (budgetInstalled.has(manager)) return;
  budgetInstalled.add(manager);
  const original = manager.getCompactionSettings.bind(manager);
  manager.getCompactionSettings = () => {
    const settings = original();
    const keepRecentTokens = keepRecentTokensForContext(session.model?.contextWindow ?? 0);
    const reserveTokens = benchmark
      ? benchmarkReserveTokens(session.model?.contextWindow ?? 0, session.model?.maxTokens ?? 0, settings.reserveTokens)
      : settings.reserveTokens;
    return keepRecentTokens ? { ...settings, keepRecentTokens, reserveTokens } : settings;
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
 *
 * `samplingRefresh` re-injects the continuity packet on EVERY sampling call —
 * a benchmark need (dynamic attempt budgets and live ownership change constantly). It is
 * opt-in: ordinary sessions pay the refresh only after a real compaction.
 */
export function installMidTurnCompaction(session: AgentSession, getContinuityContext?: () => Promise<ContinuityContext>, options?: { samplingRefresh?: boolean }) {
  const benchmark = Boolean(options?.samplingRefresh);
  installCompactionBudget(session, benchmark);
  const agent = session.agent;
  const originalTransform = agent.transformContext;
  let compacting = false;

  agent.transformContext = async (messages, signal) => {
    if (benchmark) assertBenchmarkSamplingAllowed(session);
    let transformed = originalTransform ? await originalTransform(messages, signal) : messages;
    if (compacting || signal?.aborted) return transformed;

    if (options?.samplingRefresh && getContinuityContext) {
      try {
        const continuity = await getContinuityContext();
        // The SDK's base transformContext returns a structured clone. Update
        // the array that will actually be returned to the provider, not the
        // pre-transform input that is discarded after this hook.
        upsertContinuityContext(transformed as unknown[], continuity);
      } catch {
        // Continuity refresh is best-effort; sampling must proceed.
      }
    }

    const settings = session.settingsManager.getCompactionSettings();
    const contextWindow = session.model?.contextWindow ?? 0;
    const usage = session.getContextUsage();
    const samplingTokens = () => benchmark ? estimateBenchmarkInputTokens(session, transformed) : 0;
    const checkBudget = () => {
      if (!benchmark) return;
      assertBenchmarkSamplingAllowed(session);
      const tokens = samplingTokens();
      const limit = benchmarkInputLimit(session);
      if (tokens > limit) throw new BenchmarkContextBudgetError(`Benchmark request exceeds its context budget (estimated input ${tokens}, limit ${limit})`);
    };
    const initialTokens = Math.max(usage?.percent === null ? 0 : usage?.tokens ?? 0, samplingTokens());
    if (!settings.enabled || !shouldCompactBeforeSampling(initialTokens, contextWindow, settings.reserveTokens)) {
      checkBudget();
      return transformed;
    }

    // The session file must be settled before compaction reads its branch, but
    // avoid paying this await on ordinary sampling turns far below the limit.
    const previousState = session.agent.state.messages;
    await waitForAgentEvents(session);
    if (previousState !== session.agent.state.messages) {
      replaceAgentMessages(session, messages, session.agent.state.messages);
      transformed = originalTransform ? await originalTransform(messages, signal) : messages;
      if (benchmark && getContinuityContext) upsertContinuityContext(transformed as unknown[], await getContinuityContext());
    }
    const settledUsage = session.getContextUsage();
    const settledTokens = Math.max(settledUsage?.percent === null ? 0 : settledUsage?.tokens ?? 0, samplingTokens());
    if (!shouldCompactBeforeSampling(settledTokens, contextWindow, settings.reserveTokens)) {
      checkBudget();
      return transformed;
    }

    compacting = true;
    try {
      const compacted = await runMidTurnCompaction(session, signal);
      if (!compacted) { checkBudget(); return transformed; }
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
      transformed = originalTransform ? await originalTransform(messages, signal) : messages;
      checkBudget();
      return transformed;
    } finally {
      compacting = false;
    }
  };
}
