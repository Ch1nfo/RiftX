import { selectBlackboard, blackboardLabel } from "./blackboard";
/** Compact, replaceable benchmark context derived from the authoritative ledger. */

import type { BenchmarkLedger, ChallengeState } from "./ledger";
import { PASSWORD_ENUMERATION_BUDGET_MS } from "./effort";

export const MAX_BENCHMARK_CONTINUITY_CHARS = 8_000;
export const BENCHMARK_HANDOFF_GUIDANCE = "Reassess the recorded evidence independently and choose a materially different hypothesis. Previous attempts may have followed a mistaken premise; do not inherit their plan. An unsuccessful attempt alone does not rule out an entire approach.";

export const BENCHMARK_ENDGAME_GUIDANCE = "Final-three revisit: keep working in the same environment while useful work remains. Preserve valid partial solutions, scripts and evidence; change hypothesis when evidence warrants it. An unsuccessful attempt alone does not rule out an entire approach. Do not defer or replace a worker just because the approach is difficult. A justified handoff preserves an available container. Use reset_environment with a concrete reason and evidenceRef only for an observed environment problem; it closes/releases the environment for a fresh acquire or assignment.";

function compact(value: string, limit: number): string {
  return value.length <= limit ? value : `${value.slice(0, Math.max(0, limit - 14))}...[truncated]`;
}

function challengeLine(challenge: ChallengeState): string {
  return `  ${challenge.uniqueCode} | ${challenge.totalScore}pts | flags ${challenge.correctFlagCount}/${challenge.flagCount} | attempt ${challenge.attemptCount} | ${challenge.status}`;
}

export function buildBenchmarkContinuity(
  ledger: BenchmarkLedger,
  worker: "main" | `subagent:${string}` = "main",
  firstAttemptWarning?: ChallengeState,
  workingDirectory?: string
): string {
  const state = ledger.getState();
  if (state.totalChallenges === 0) return "";
  const challenges = Object.values(state.challenges);
  const mine = challenges.find((challenge) => challenge.owner === worker && (challenge.status === "running" || challenge.status === "reserved"));
  const unseen = challenges.filter((challenge) => !challenge.isCompleted && challenge.attemptCount === 0).length;
  const firstAttemptsActive = challenges.filter((challenge) => challenge.currentAttemptPhase === "coverage" && (challenge.status === "running" || challenge.status === "reserved" || challenge.status === "closing" || challenge.status === "orphaned")).length;
  const lines = [
    "<riftx-benchmark-continuity>",
    worker === "main"
      ? `## Run: schedule=${state.phase} | score=${state.scoreExact ? state.cumulativeScore : `${state.cumulativeScore}+`} | solved=${state.solvedCount}/${state.totalChallenges} | unseen=${unseen} | first_attempts_active=${firstAttemptsActive} | containers=${state.activeContainers}/3`
      : `## Run: schedule=${state.phase} | solved=${state.solvedCount}/${state.totalChallenges}`
  ];
  if (ledger.isEndgame()) lines.push(`## Final challenges: ${BENCHMARK_ENDGAME_GUIDANCE}`);
  if (workingDirectory) lines.push(`## Working directory: ${workingDirectory} (relative local tool paths and shell commands resolve here)`);

  if (mine) {
    lines.push(
      `## My challenge: ${mine.uniqueCode} | ${mine.totalScore}pts | attempt ${mine.attemptCount} | flags ${mine.correctFlagCount}/${mine.flagCount}`,
      `  addr: ${mine.containerAddrs.join(", ") || "(none)"}`,
      `  description: ${compact(mine.description, 2_400) || "(none)"}`,
      `  hint: ${mine.hintUsed ? mine.hintContent || "requested; no content returned" : "not used"}`
    );
    if (mine.passwordEnumerationMs > 0) lines.push(`## Online password guessing: ${Math.ceil(mine.passwordEnumerationMs / 1000)}/${PASSWORD_ENUMERATION_BUDGET_MS / 1000} seconds consumed across all workers and attempts.`);
    if (mine.triedFamilies.length) lines.push(`## Previously tried: ${mine.triedFamilies.join(", ")}`);
    if (mine.ruledOutFamilies.length) lines.push(`## Recorded exclusions (check their evidence): ${mine.ruledOutFamilies.join(", ")}`);
    const board = selectBlackboard(mine, 6);
    if (board.length) {
      lines.push("## Challenge blackboard:", ...board.map((entry) =>
        `  - ${blackboardLabel(entry)}: ${compact(entry.summary, 600)}${entry.evidenceRef ? ` [${compact(entry.evidenceRef, 200)}]` : ""}`
      ));
    }
    const history = mine.approachHistory.slice(-4);
    if (history.length) {
      lines.push("## Previous attempts:", ...history.map((attempt) =>
        `  - #${attempt.attemptNumber} tried=${attempt.triedFamilies.join(", ") || "(not recorded)"}; stopped because ${attempt.stopReason}`
      ));
    }
    if (mine.attemptCount > 1) {
      lines.push(`## Revisit policy: No runtime time limit. ${ledger.isEndgame() ? "Follow the final-challenges guidance above; preserve the environment and valid partial work." : BENCHMARK_HANDOFF_GUIDANCE} If a handoff is justified, checkpoint the evidence and unresolved work.`);
    }
    if (firstAttemptWarning?.uniqueCode === mine.uniqueCode) {
      lines.push("## FIRST-ATTEMPT NOTICE: 25 minutes have elapsed. Five minutes remain. Consolidate evidence and pursue only the most decisive remaining probe; at 30 minutes, checkpoint and defer immediately.");
    }
    if (ledger.isBudgetExhausted(mine.uniqueCode)) {
      lines.push("## FIRST_ATTEMPT_COMPLETE: Solving tools are blocked. Write the final blackboard checkpoint and defer now.");
    }
    const intel = ledger.intelForChallenge(mine).slice(-4);
    if (intel.length) lines.push("## Relevant shared intel:", ...intel.map((entry) => `  - [${entry.target}] ${entry.intel}`));
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
  lines.push(`## Platform sync: ${state.lastSyncAt ? new Date(state.lastSyncAt).toISOString() : "never"} | VPN: ${state.vpnChecked ? (state.vpnOk ? "ok" : "FAIL") : "not prechecked"}`, "</riftx-benchmark-continuity>");

  const configuredToken = process.env.BENCHMARK_TOKEN ?? "";
  const joined = configuredToken ? lines.join("\n").split(configuredToken).join("[REDACTED_BENCHMARK_TOKEN]") : lines.join("\n");
  if (joined.length <= MAX_BENCHMARK_CONTINUITY_CHARS) return joined;
  const trimmed = joined.slice(0, MAX_BENCHMARK_CONTINUITY_CHARS - 50);
  return `${trimmed.slice(0, trimmed.lastIndexOf("\n"))}\n[...truncated by continuity budget]\n</riftx-benchmark-continuity>`;
}
