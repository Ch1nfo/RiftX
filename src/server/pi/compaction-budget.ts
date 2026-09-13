import { randomUUID } from "node:crypto";
import { buildSessionContext, type AgentSession, type CompactionEntry, type CompactionResult, type SessionBeforeCompactEvent } from "@mariozechner/pi-coding-agent";
import { contextModelKey, contextTokenRatio, estimateMessagesContextUsage, estimateStaticContextTokens } from "./context-usage";
import { upsertContinuityContext, type ContinuityContext } from "./continuity-context";
import { prepareCompactionWithBudget } from "./pi-internals";

export const COMPACTION_KEEP_RECENT_RATIO = 0.1;

export function keepRecentTokensForContext(contextWindow: number) {
  return Number.isFinite(contextWindow) && contextWindow > 0
    ? Math.max(1, Math.floor(contextWindow * COMPACTION_KEEP_RECENT_RATIO))
    : 0;
}

export function benchmarkReserveTokens(contextWindow: number, maxTokens: number, configuredReserve: number) {
  if (!Number.isFinite(contextWindow) || contextWindow <= 0 || !Number.isFinite(maxTokens) || maxTokens <= 0) return configuredReserve;
  const safety = Math.min(49_152, Math.max(128, Math.floor(contextWindow * 0.05)));
  return Math.max(Math.min(Math.max(0, configuredReserve), Math.floor(contextWindow / 2)), maxTokens + safety);
}

export class BenchmarkContextBudgetError extends Error {
  constructor(message: string) { super(message); this.name = "BenchmarkContextBudgetError"; }
}

const failures = new WeakMap<AgentSession, { modelKey: string; error: Error }>();

export function blockBenchmarkSampling(session: AgentSession, error: Error) {
  failures.set(session, { modelKey: contextModelKey(session), error });
}

export function clearBenchmarkCompactionFailure(session: AgentSession) { failures.delete(session); }

export function assertBenchmarkSamplingAllowed(session: AgentSession) {
  const failure = failures.get(session);
  if (!failure) return;
  if (failure.modelKey !== contextModelKey(session)) { failures.delete(session); return; }
  throw failure.error;
}

export function estimateBenchmarkInputTokens(session: AgentSession, messages: readonly unknown[], continuity?: ContinuityContext) {
  const prepared = [...messages];
  if (continuity) upsertContinuityContext(prepared, continuity);
  return Math.ceil((estimateStaticContextTokens(session) + estimateMessagesContextUsage(prepared, 0).tokens) * contextTokenRatio(session));
}

export function benchmarkInputLimit(session: AgentSession) {
  const contextWindow = session.model?.contextWindow ?? 0;
  const settings = session.settingsManager.getCompactionSettings();
  return contextWindow - benchmarkReserveTokens(contextWindow, session.model?.maxTokens ?? 0, settings.reserveTokens);
}

function previewMessages(event: SessionBeforeCompactEvent, result: CompactionResult) {
  if (!event.branchEntries.some((entry) => entry.id === result.firstKeptEntryId)) {
    throw new BenchmarkContextBudgetError("Benchmark compaction has no valid retained-history boundary");
  }
  const entry: CompactionEntry = {
    type: "compaction", id: randomUUID(), parentId: event.branchEntries.at(-1)?.id ?? null,
    timestamp: new Date().toISOString(), summary: result.summary, firstKeptEntryId: result.firstKeptEntryId,
    tokensBefore: result.tokensBefore, details: result.details
  };
  return buildSessionContext([...event.branchEntries, entry], entry.id).messages;
}

function record(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" ? value as Record<string, unknown> : {};
}

function strings(value: unknown): string[] { return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : []; }

function mergeRetainedFiles(result: CompactionResult, preparation: SessionBeforeCompactEvent["preparation"]) {
  const details = record(result.details);
  const previousReads = strings(details.readFiles);
  const previousWrites = strings(details.modifiedFiles);
  const modifiedFiles = [...new Set([...previousWrites, ...preparation.fileOps.written, ...preparation.fileOps.edited])].sort();
  const modified = new Set(modifiedFiles);
  const readFiles = [...new Set([...previousReads, ...preparation.fileOps.read])].filter((file) => !modified.has(file)).sort();
  const known = new Set([...previousReads, ...previousWrites]);
  const extra = [...readFiles, ...modifiedFiles].filter((file) => !known.has(file));
  const token = process.env.BENCHMARK_TOKEN;
  const references = extra.length ? "\nAdditional file references from retained-history reduction:\n" + extra.join("\n") : "";
  return {
    ...result, firstKeptEntryId: preparation.firstKeptEntryId,
    summary: result.summary + (token ? references.split(token).join("[REDACTED_BENCHMARK_TOKEN]") : references),
    details: { ...details, readFiles, modifiedFiles }
  };
}

/** Validate the actual replay shape before Pi appends anything to the session. */
export async function fitBenchmarkCompaction(
  session: AgentSession,
  event: SessionBeforeCompactEvent,
  result: CompactionResult,
  continuity: ContinuityContext,
  rebuildSummary?: (maxChars: number) => string | Promise<string>
): Promise<CompactionResult> {
  const limit = benchmarkInputLimit(session);
  if (limit <= 0) throw new BenchmarkContextBudgetError("Benchmark context window cannot fit the configured output reserve");
  // The SDK may carry usage from an assistant retained across the previous
  // compaction. Compare both sides with the same current estimator instead.
  const before = estimateBenchmarkInputTokens(session, session.messages ?? buildSessionContext(event.branchEntries).messages, continuity);
  let candidate = result;
  let recut = false;
  const estimate = () => estimateBenchmarkInputTokens(session, previewMessages(event, candidate), continuity);
  let inputTokens = estimate();
  if ((inputTokens > limit || inputTokens >= before) && rebuildSummary) {
    candidate = { ...candidate, summary: await rebuildSummary(4_000) };
    inputTokens = estimate();
  }
  if (inputTokens > limit || inputTokens >= before) {
    // One SDK-selected retry only: never slice message arrays or split a tool pair.
    const emptyInput = estimateBenchmarkInputTokens(session, [], continuity);
    const summaryTokens = estimateMessagesContextUsage([{ role: "compactionSummary", summary: candidate.summary }], 0).tokens * contextTokenRatio(session);
    const available = Math.min(limit, before - 1) - emptyInput - summaryTokens - 256;
    if (available > 0) {
      const settings = event.preparation.settings;
      const keepRecentTokens = Math.max(1, Math.floor(Math.min(settings.keepRecentTokens / 2, available / contextTokenRatio(session) * 0.8)));
      const preparation = await prepareCompactionWithBudget(event.branchEntries, { ...settings, keepRecentTokens });
      if (preparation && preparation.firstKeptEntryId !== candidate.firstKeptEntryId) {
        // A model summary never described the newly discarded tail. Require a
        // deterministic recovery snapshot before advancing beyond its original cut.
        if (!rebuildSummary) throw new BenchmarkContextBudgetError("Benchmark summary requires a smaller retained-history boundary");
        const previousIndex = event.branchEntries.findIndex((entry) => entry.id === candidate.firstKeptEntryId);
        const nextIndex = event.branchEntries.findIndex((entry) => entry.id === preparation.firstKeptEntryId);
        if (nextIndex > previousIndex) { candidate = mergeRetainedFiles(candidate, preparation); recut = true; inputTokens = estimate(); }
      }
    }
  }
  if (!candidate.summary.trim() || inputTokens > limit || inputTokens >= before) {
    throw new BenchmarkContextBudgetError(`Benchmark compaction cannot reduce context to a usable size (estimated input ${inputTokens}, limit ${limit}, before ${before})`);
  }
  const details = record(candidate.details);
  const riftx = record(details.riftx);
  clearBenchmarkCompactionFailure(session);
  return { ...candidate, details: { ...details, riftx: { ...riftx, budget: {
    modelKey: contextModelKey(session), tokenRatio: contextTokenRatio(session),
    continuityTokens: estimateMessagesContextUsage((() => { const messages: unknown[] = []; upsertContinuityContext(messages, continuity); return messages; })(), 0).tokens * contextTokenRatio(session),
    inputTokensBefore: before, inputTokensAfter: inputTokens, inputLimit: limit, recut
  } } } };
}
