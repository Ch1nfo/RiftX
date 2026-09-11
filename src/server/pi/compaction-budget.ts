export const COMPACTION_KEEP_RECENT_RATIO = 0.1;

export function keepRecentTokensForContext(contextWindow: number) {
  return Number.isFinite(contextWindow) && contextWindow > 0
    ? Math.max(1, Math.floor(contextWindow * COMPACTION_KEEP_RECENT_RATIO))
    : 0;
}
