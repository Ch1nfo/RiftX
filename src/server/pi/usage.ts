import type { ContextUsage } from "@/lib/types";

export function emptyContextUsage(contextWindow?: number | null): ContextUsage {
  const safeWindow = Number.isFinite(contextWindow) && Number(contextWindow) > 0 ? Number(contextWindow) : 0;
  return {
    tokens: 0,
    contextWindow: safeWindow,
    percent: safeWindow > 0 ? 0 : null,
    input: null,
    output: null,
    cacheRead: null,
    cacheWrite: null,
    remaining: safeWindow
  };
}

function numericOrNull(value: unknown) {
  if (value === null) return null;
  const next = Number(value);
  return Number.isFinite(next) ? next : undefined;
}

export function normalizeContextUsage(value: unknown, fallbackWindow = 0): ContextUsage {
  const raw = (value ?? {}) as Record<string, unknown>;
  const tokenValue = numericOrNull(raw.tokens) ?? numericOrNull(raw.contextTokens) ?? numericOrNull(raw.totalTokens);
  const configuredWindow = Number(fallbackWindow);
  const rawWindow = Number(raw.contextWindow ?? 0);
  const contextWindow = Number.isFinite(configuredWindow) && configuredWindow > 0
    ? configuredWindow
    : Number.isFinite(rawWindow) && rawWindow > 0 ? rawWindow : 0;
  const input = numericOrNull(raw.input);
  const output = numericOrNull(raw.output);
  const cacheRead = numericOrNull(raw.cacheRead);
  const cacheWrite = numericOrNull(raw.cacheWrite);
  const explicitPercent = numericOrNull(raw.percent);
  const unknownUsage = tokenValue === null || explicitPercent === null;
  const tokens = unknownUsage ? 0 : Number(tokenValue ?? 0);
  const calculatedPercent = Number.isFinite(contextWindow) && contextWindow > 0 && tokenValue !== undefined
    ? Math.min(100, (tokens / contextWindow) * 100)
    : null;
  const percent = unknownUsage
    ? null
    : calculatedPercent ?? (explicitPercent === undefined ? null : Math.min(100, Math.max(0, explicitPercent)));
  return {
    tokens: Math.max(0, tokens),
    contextWindow,
    percent,
    input: input ?? null,
    output: output ?? null,
    cacheRead: cacheRead ?? null,
    cacheWrite: cacheWrite ?? null,
    remaining: Math.max(0, contextWindow - tokens)
  };
}
