export const COMPACTION_KEEP_RECENT_RATIO = 0.1;
export const COMPACTION_TARGET_RATIO = 0.13;

export function keepRecentTokensForContext(contextWindow: number) {
  return Number.isFinite(contextWindow) && contextWindow > 0
    ? Math.max(1, Math.floor(contextWindow * COMPACTION_KEEP_RECENT_RATIO))
    : 0;
}

/** Recent history is at most 10%; summary and fixed context share the 13% target.
 * Pi cuts history using chars/4, so translate its budget using observed usage.
 * Fixed prompts and the summary minimum can exceed the target on small windows. */
export function compactionBudget(contextWindow: number, fixedTokens = 0, tokenRatio = 1, actualSummaryTokens?: number) {
  const window = Number.isFinite(contextWindow) && contextWindow > 0 ? contextWindow : 0;
  const ratio = Number.isFinite(tokenRatio) ? Math.max(1, tokenRatio) : 1;
  const summaryTokens = Math.floor(Math.max(8192, window * 0.05));
  const targetTokens = Math.floor(window * COMPACTION_TARGET_RATIO);
  const recentTokens = Math.max(0, Math.min(
    keepRecentTokensForContext(window),
    targetTokens - Math.max(0, fixedTokens) - (actualSummaryTokens ?? summaryTokens)
  ));
  return {
    summaryTokens,
    keepRecentTokens: Math.max(1, Math.floor(recentTokens / ratio)),
    targetTokens
  };
}
