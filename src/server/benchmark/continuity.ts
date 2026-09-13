import { benchmarkMemoryLocator } from "./memory";
import { selectBlackboard, blackboardLabel, evidenceBackedRuleOuts, handoffAttempt, handoffCandidate } from "./blackboard";
/** Compact, replaceable benchmark context derived from the authoritative ledger. */

import type { BenchmarkLedger, ChallengeState } from "./ledger";
import { PASSWORD_ENUMERATION_BUDGET_MS } from "./effort";

export const MAX_BENCHMARK_CONTINUITY_CHARS = 8_000;
export const BENCHMARK_HANDOFF_GUIDANCE = "Preserve valid partial solutions, the inherited blackboard and evidence artifacts. Review the previous failure reason, approach and next probe before selecting a new route. Treat inherited plans as candidates to verify; an unsuccessful attempt alone does not rule out an approach.";

export const BENCHMARK_ENDGAME_GUIDANCE = "Every attempt has a 30-minute default deadline. A revisit can receive one verified progress extension to 40 minutes. Prepare a handoff at 25 minutes and retain valid partial solutions and evidence; inherited next probes are candidates to reassess.";

function compact(value: string, limit: number): string {
  return value.length <= limit ? value : `${value.slice(0, Math.max(0, limit - 14))}...[truncated]`;
}

function challengeLine(challenge: ChallengeState): string {
  return `  ${challenge.uniqueCode} | ${challenge.totalScore}pts | flags ${challenge.correctFlagCount}/${challenge.flagCount} | attempt ${challenge.attemptCount} | ${challenge.status}`;
}

export function buildBenchmarkContinuity(
  ledger: BenchmarkLedger,
  worker: "main" | `subagent:${string}` = "main",
  attemptWarning?: ChallengeState,
  workingDirectory?: string
): string {
  const state = ledger.getState();
  if (state.totalChallenges === 0) return "";
  const challenges = Object.values(state.challenges);
  const mine = challenges.find((challenge) => challenge.owner === worker && (challenge.status === "running" || challenge.status === "reserved"));
  const unseen = challenges.filter((challenge) => !challenge.isCompleted && challenge.attemptCount === 0).length;
  const firstAttemptsActive = challenges.filter((challenge) => challenge.currentAttemptPhase === "coverage" && (challenge.status === "running" || challenge.status === "reserved" || challenge.status === "closing" || challenge.status === "orphaned")).length;
  // Keep live control state and evidence ahead of descriptive detail so the
  // bounded packet cannot lose a timebox or worker slot to a long hint.
  const evidenceLines: string[] = [];
  const evidenceWithoutReferences = new Map<string, string>();
  const detailLines: string[] = [];
  const lines = [
    "<riftx-benchmark-continuity>",
    JSON.stringify(benchmarkMemoryLocator(worker === "main" ? mine?.uniqueCode : undefined)),
    worker === "main"
      ? `## Run: schedule=${state.phase} | score=${state.scoreExact ? state.cumulativeScore : `${state.cumulativeScore}+`} | solved=${state.solvedCount}/${state.totalChallenges} | unseen=${unseen} | first_attempts_active=${firstAttemptsActive} | containers=${state.activeContainers}/3`
      : `## Run: schedule=${state.phase} | solved=${state.solvedCount}/${state.totalChallenges}`
  ];
  lines.push("## Persistence: Unfinished challenges are never permanently abandoned. Preserve partial progress and continue solving until all flags are accepted, the platform ends the task, or the operator stops the run. Defer only requeues a challenge for continued work.");
  if (ledger.isEndgame()) lines.push(`## Final challenges: ${BENCHMARK_ENDGAME_GUIDANCE}`);
  if (workingDirectory) lines.push(`## Working directory: ${workingDirectory} (relative local tool paths and shell commands resolve here)`);

  if (mine) {
    const supportedRuleOuts = evidenceBackedRuleOuts(mine);
    const budget = ledger.budgetFor(mine.uniqueCode);
    if (budget) lines.push(`Attempt deadline: ${budget.deadlineAt === null ? "pending start" : new Date(budget.deadlineAt).toISOString()}; limit=${Math.round(budget.limitMs / 60_000)} minutes; extension=${budget.extensionUsed ? "used" : mine.attemptCount > 1 ? "available once for verified progress in the final five minutes" : "not available on the first attempt"}.`);
    lines.push(
      `## My challenge: ${mine.uniqueCode} | ${mine.totalScore}pts | attempt ${mine.attemptCount} | flags ${mine.correctFlagCount}/${mine.flagCount}`,
      `  addr: ${mine.containerAddrs.join(", ") || "(none)"}`
    );
    detailLines.push(
      `  description: ${compact(mine.description, 2_400) || "(none)"}`,
      `  hint: ${mine.hintUsed ? compact(mine.hintContent || "", 1_200) || "requested; no content returned" : "not used"}`
    );
    if (mine.passwordEnumerationMs > 0) lines.push(`## Online password guessing: ${Math.ceil(mine.passwordEnumerationMs / 1000)}/${PASSWORD_ENUMERATION_BUDGET_MS / 1000} seconds consumed across all workers and attempts.`);
    if (mine.triedFamilies.length) detailLines.push(`## Previously tried: ${mine.triedFamilies.join(", ")}`);
    if (supportedRuleOuts.length) detailLines.push(`Evidence-backed ruled-out families: ${supportedRuleOuts.join(", ")}`);
    const previousAttempt = mine.approachHistory.at(-1);
    const previous = previousAttempt ? handoffAttempt(previousAttempt, supportedRuleOuts) : undefined;
    if (previous) {
      lines.push(`Previous attempt to review: ${JSON.stringify({
        attemptNumber: previous.attemptNumber, phase: previous.phase, worker: previous.worker,
        startedAt: previous.startedAt, endedAt: previous.endedAt,
        flagsBefore: previous.flagsBefore, flagsAfter: previous.flagsAfter, flagsDelta: previous.flagsDelta,
        stopReason: previous.stopReason
      })}`);
      if (previous.previousCandidate) lines.push(`Previous attempt candidate to verify: ${JSON.stringify(previous.previousCandidate)}`);
    }
    const candidate = handoffCandidate(mine.currentApproach, mine.nextProbe);
    if (candidate) lines.push(`Candidates to verify against the blackboard: ${JSON.stringify(candidate)}`);
    const board = selectBlackboard(mine, 6);
    if (board.length) {
      evidenceLines.push("Inherited blackboard evidence:", ...board.map((entry) => {
        const evidence = { kind: blackboardLabel(entry), summary: compact(entry.summary, 600) };
        const line = JSON.stringify({ ...evidence, ...(entry.evidenceRef ? { evidenceRef: entry.evidenceRef } : {}) });
        if (entry.evidenceRef) evidenceWithoutReferences.set(line, JSON.stringify({ ...evidence, evidenceRefOmitted: true }));
        return line;
      }));
    }
    const history = mine.approachHistory.slice(-4).reverse();
    if (history.length) detailLines.push("Previous attempts and candidates to reassess:",
      ...history.map((attempt) => JSON.stringify(handoffAttempt(attempt, supportedRuleOuts)))
    );
    if (mine.attemptCount > 1) {
      lines.push(`Revisit handoff: ${BENCHMARK_HANDOFF_GUIDANCE}`);
    }
    if (attemptWarning?.uniqueCode === mine.uniqueCode) {
      lines.push("ATTEMPT_WARNING: 25 minutes reached. Save verified findings, evidence artifacts, failure reasons and a candidate nextProbe now. The recorded deadline still applies; a revisit may extend once only for verifiable progress.");
    }
    if (ledger.isBudgetExhausted(mine.uniqueCode)) {
      lines.push("ATTEMPT_TIMEBOX_COMPLETE: Solving tools are blocked. Preserve your checkpoint; submission and cleanup remain available while the runtime closes this attempt.");
    }
    const intel = ledger.intelForChallenge(mine).slice(-4);
    if (intel.length) detailLines.push("## Relevant shared intel:", ...intel.map((entry) => `  - [${entry.target}] ${entry.intel}`));
  } else {
    lines.push('## My challenge: (none — acquire one from the eligible candidates)');
  }

  if (worker === "main") {
    const children = challenges.filter((challenge) => challenge.owner !== null && challenge.owner !== "main" && (challenge.status === "running" || challenge.status === "reserved"));
    lines.push(`## SubAgent challenges (${children.length}/2):`, ...(children.length ? children.map(challengeLine) : ["  (none active)"]));
    const candidates = ledger.candidates(5);
    if (candidates.length) lines.push("## Eligible candidates (already ordered; choose from the lowest score tier during coverage):", ...candidates.map(challengeLine));
    else if (state.phase === "coverage") lines.push("## Eligible candidates: none until the remaining active first attempts finish.");
  }
  // Use a stable timestamp instead of an ever-changing "N minutes ago" value.
  // This packet is sampled frequently; stable text preserves provider prompt
  // cache prefixes and avoids turning silent timing into attention noise.
  lines.push(`## Platform sync: ${state.lastSyncAt ? new Date(state.lastSyncAt).toISOString() : "never"} | VPN: ${state.vpnChecked ? (state.vpnOk ? "ok" : "FAIL") : "not prechecked"}`, ...evidenceLines, ...detailLines, "</riftx-benchmark-continuity>");

  const configuredToken = process.env.BENCHMARK_TOKEN ?? "";
  const scrub = (line: string) => configuredToken ? line.split(configuredToken).join("[REDACTED_BENCHMARK_TOKEN]") : line;
  const closing = "</riftx-benchmark-continuity>";
  const retained: string[] = [];
  let used = closing.length + 1;
  let omitted = false;
  for (const original of lines.slice(0, -1)) {
    let line = scrub(original);
    const separator = retained.length ? 1 : 0;
    if (used + separator + line.length > MAX_BENCHMARK_CONTINUITY_CHARS) {
      const fallback = evidenceWithoutReferences.get(original);
      if (fallback) line = scrub(fallback);
      omitted = true;
    }
    if (used + separator + line.length <= MAX_BENCHMARK_CONTINUITY_CHARS) {
      retained.push(line);
      used += separator + line.length;
    }
  }
  const notice = "Some lower-priority details were omitted to fit the continuity budget.";
  if (omitted && used + notice.length + 1 <= MAX_BENCHMARK_CONTINUITY_CHARS) retained.push(notice);
  return retained.join("\n") + "\n" + closing;
}
