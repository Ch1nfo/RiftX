import type { ContextUsage } from "@/lib/types";

export function normalizeContextUsage(value: unknown, fallbackWindow = 128000): ContextUsage {
  const raw = (value ?? {}) as Record<string, unknown>;
  const tokens = Number(raw.tokens ?? raw.contextTokens ?? raw.totalTokens ?? 0);
  const contextWindow = Number(raw.contextWindow ?? fallbackWindow);
  const input = Number(raw.input ?? 0);
  const output = Number(raw.output ?? 0);
  const cacheRead = Number(raw.cacheRead ?? 0);
  const cacheWrite = Number(raw.cacheWrite ?? 0);
  const percent = Number.isFinite(contextWindow) && contextWindow > 0 ? Math.min(100, (tokens / contextWindow) * 100) : null;
  return {
    tokens: Math.max(0, tokens),
    contextWindow,
    percent,
    input,
    output,
    cacheRead,
    cacheWrite,
    remaining: Math.max(0, contextWindow - tokens)
  };
}
