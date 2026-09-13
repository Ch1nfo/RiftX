import { benchmarkMemoryLocator } from "./memory";
import { selectBlackboard, handoffAttempt, handoffCandidate, evidenceBackedRuleOuts } from "./blackboard";
import type { BenchmarkLedger, BlackboardEntry, ChallengeOwner } from "./ledger";

export type BenchmarkFallbackSource = {
  ledger: BenchmarkLedger;
  worker: Exclude<ChallengeOwner, null>;
  workingDirectory: string;
  sessionFile?: string;
  ledgerFile?: string;
  assignedChallenge?: string;
};

/** A deliberately partial historical snapshot, independent of models and files. */
export function buildBenchmarkCompactionFallback(source: BenchmarkFallbackSource, maxChars = 16_000): string {
  if (!Number.isSafeInteger(maxChars) || maxChars <= 0) throw new RangeError("Fallback character budget must be a positive safe integer");
  const limit = Math.min(maxChars, 16_000);
  const bound = Object.values(source.ledger.getState().challenges).find((challenge) =>
    challenge.owner === source.worker && (challenge.status === "running" || challenge.status === "reserved")
  );
  const challenge = bound && (!source.assignedChallenge || source.assignedChallenge === bound.uniqueCode) ? bound : undefined;
  const secrets = [...new Set([process.env.BENCHMARK_TOKEN, ...(challenge?.triedFlags ?? [])].filter((value): value is string => Boolean(value)))];
  const clean = (value: string): string => {
    let text = value;
    for (const secret of secrets) text = text.split(secret).join("[REDACTED]");
    return text.replace(/[\u0000-\u001f\u007f]/g, " ").trim();
  };
  const clip = (value: string, length: number): string => {
    const text = clean(value);
    if (text.length <= length) return text;
    let end = length - 1;
    if (/[\uD800-\uDBFF]/.test(text[end - 1] ?? "")) end--;
    return `${text.slice(0, end)}…`;
  };
  const packet: Record<string, unknown> = {
    ...benchmarkMemoryLocator(challenge?.uniqueCode ?? source.assignedChallenge),
    type: "benchmark_compaction_fallback",
    version: 1,
    snapshot: "partial_historical",
    binding: {
      worker: clean(source.worker),
      challenge: challenge ? clean(challenge.uniqueCode) : null,
      ...(!challenge && source.assignedChallenge ? { assignedChallenge: clean(source.assignedChallenge) } : {})
    }
  };
  let serialized = JSON.stringify(packet);
  if (serialized.length > limit) throw new RangeError(`Fallback binding requires at least ${serialized.length} characters`);
  const put = (key: string, value: unknown): boolean => {
    const previous = packet[key];
    packet[key] = value;
    const candidate = JSON.stringify(packet);
    if (candidate.length <= limit) { serialized = candidate; return true; }
    if (previous === undefined) delete packet[key];
    else packet[key] = previous;
    return false;
  };
  const append = (key: string, value: unknown) => put(key, [...(packet[key] as unknown[] | undefined ?? []), value]);
  const observation = (key: string, value: Record<string, unknown>, multiple = false): void => {
    const save = (item: Record<string, unknown>) => multiple ? append(key, item) : put(key, item);
    if (save(value) || !value.evidenceRef) return;
    const { evidenceRef: _reference, ...withoutReference } = value;
    save(withoutReference);
  };

  // Exact file references are omitted as whole fields if they do not fit.
  // A clipped pathname would look usable while pointing to the wrong file.
  let recovery: Record<string, string> = {};
  for (const [key, value] of Object.entries({ ledgerFile: source.ledgerFile, sessionFile: source.sessionFile, workingDirectory: source.workingDirectory })) {
    if (!value) continue;
    const next = { ...recovery, [key]: clean(value) };
    if (put("recovery", next)) recovery = next;
  }
  if (!challenge) return serialized;

  if (challenge.lastMeaningfulSignalContent) observation("reportedProgress", {
    kind: challenge.lastSignalKind,
    summary: clip(challenge.lastMeaningfulSignalContent, 800),
    evidenceRef: clean(challenge.lastEvidenceRef)
  });
  const selected = selectBlackboard(challenge, 16);
  const facts = selected.filter((entry) => entry.kind !== "note" && entry.kind !== "attempt_end"
    && (entry.kind !== "handoff" || /^(FINDINGS|EVIDENCE):/.test(entry.summary)));
  const fact = (entry: BlackboardEntry) => ({
    kind: entry.kind,
    summary: clip(entry.summary, 600),
    ...(entry.evidenceRef ? { evidenceRef: clean(entry.evidenceRef) } : {})
  });
  // One important observation precedes lists; repeated low-value notes and a
  // long task description cannot displace the core evidence at small budgets.
  if (facts[0]) observation("reportedFacts", fact(facts[0]), true);
  const supportedRuleOuts = evidenceBackedRuleOuts(challenge);
  const previous = challenge.approachHistory.at(-1);
  if (previous) {
    const { previousCandidate: _candidate, triedFamilies: _tried, ruledOutFamilies: _ruledOut, ...outcome } = handoffAttempt(previous, supportedRuleOuts);
    put("lastAttempt", { ...outcome, worker: clean(outcome.worker), stopReason: clip(outcome.stopReason, 500) });
  }
  const candidate = handoffCandidate(challenge.currentApproach, challenge.nextProbe)
    ?? handoffCandidate(previous?.approach, previous?.nextDistinctApproach);
  if (candidate) put("previousCandidate", {
    requiresRevalidation: true, approach: clip(candidate.approach, 300), nextProbe: clip(candidate.nextProbe, 1_000)
  });
  for (const [key, families] of [["tried", challenge.triedFamilies], ["ruledOut", supportedRuleOuts]] as const) {
    for (const family of families.slice(-10)) append(key, clip(family, 100));
  }
  for (const entry of facts.slice(1)) observation("reportedFacts", fact(entry), true);
  for (const entry of selected.filter((entry) => entry.kind === "note" || /^UNCERTAINTIES:/.test(entry.summary)).slice(-4)) {
    observation("reportedUncertainties", fact(entry), true);
  }
  for (const entry of selected.filter((entry) => entry.kind === "handoff" && /^ARTIFACTS:/.test(entry.summary))) {
    observation("reportedArtifacts", fact(entry), true);
  }
  for (const attempt of challenge.approachHistory.slice(-3)) append("pastAttempts", {
    attemptNumber: attempt.attemptNumber, phase: attempt.phase, worker: clean(attempt.worker),
    startedAt: attempt.startedAt, endedAt: attempt.endedAt,
    flagsBefore: attempt.flagsBefore, flagsAfter: attempt.flagsAfter, flagsDelta: attempt.flagsAfter - attempt.flagsBefore,
    tried: attempt.triedFamilies.slice(-6).map((value) => clip(value, 100)),
    ruledOut: attempt.ruledOutFamilies.filter((value) => supportedRuleOuts.includes(value)).slice(-6).map((value) => clip(value, 100)),
    stopReason: clip(attempt.stopReason, 500)
  });
  if (challenge.description) put("taskDescription", clip(challenge.description, 1_200));
  return serialized;
}
