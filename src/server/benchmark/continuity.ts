/** Benchmark continuity context: dynamic, replaceable, injected at sampling and compaction. */

import type { BenchmarkLedger, ChallengeState } from "./ledger";

export const MAX_BENCHMARK_CONTINUITY_CHARS = 12_000;

function fmtElapsed(ms: number): string {
  if (ms <= 0) return "0m";
  const minutes = Math.floor(ms / 60_000);
  if (minutes < 60) return `${minutes}m`;
  return `${Math.floor(minutes / 60)}h${minutes % 60}m`;
}

function challengeLine(challenge: ChallengeState): string {
  return `  ${challenge.uniqueCode} | ${challenge.difficulty} | ${challenge.totalScore}pts | flags ${challenge.correctFlagCount}/${challenge.flagCount} | attempt ${challenge.attemptCount} | ${challenge.status}`;
}

const APPROACH_FAMILIES = ["source audit", "authorization/IDOR", "SSRF", "injection", "protocol abuse", "version-specific CVE", "alternate role/state", "algorithm recovery"];

/**
 * Builds the benchmark continuity block from the live ledger state, scoped to
 * the current worker. Main Agent sees the run overview + its challenge + subagent
 * challenges; a SubAgent sees only its own challenge and timing — not the parent
 * or other subagents' state. Called on every model sampling — must be cheap.
 */
export function buildBenchmarkContinuity(ledger: BenchmarkLedger, worker: "main" | `subagent:${string}` = "main"): string {
  const state = ledger.getState();
  if (state.totalChallenges === 0) return "";

  // My active challenge — scoped to THIS worker.
  const mine = Object.values(state.challenges).filter((challenge) => challenge.owner === worker && (challenge.status === "running" || challenge.status === "reserved"));
  const myLines: string[] = [];
  if (mine[0]) {
    const challenge = mine[0];
    const elapsed = ledger.signalElapsedMs(challenge.uniqueCode);
    const budget = ledger.budgetFor(challenge.uniqueCode);
    const previousApproaches = challenge.approachHistory.slice(-4).map((attempt) => `${attempt.attemptNumber}:${attempt.approach}`).join(" | ") || "(none)";
    const ruledOut = [...new Set(challenge.approachHistory.flatMap((attempt) => attempt.ruledOutFamilies))];
    const used = new Set([...challenge.approachHistory.flatMap((attempt) => [...attempt.triedFamilies, ...attempt.ruledOutFamilies]), ...challenge.triedFamilies, ...challenge.ruledOutFamilies].map((item) => item.toLocaleLowerCase()));
    const unused = APPROACH_FAMILIES.filter((item) => !used.has(item.toLocaleLowerCase())).slice(0, 4);
    myLines.push(
      `## My challenge: ${challenge.uniqueCode} | ${challenge.difficulty} | ${challenge.totalScore}pts | ATTEMPT ${challenge.attemptCount}`,
      `  flags: ${challenge.correctFlagCount}/${challenge.flagCount} | addr: ${challenge.containerAddrs.join(", ") || "(none)"}`,
      `  description: ${challenge.description.slice(0, 3_000) || "(none)"}`,
      `  timebox: ${budget?.policy.label ?? "unknown"} | elapsed=${fmtElapsed(budget?.elapsedMs ?? 0)} | since_progress=${fmtElapsed(budget?.sinceProgressMs ?? elapsed)} | hard_remaining=${budget?.hardRemainingMs === null ? "unbounded" : fmtElapsed(Math.max(0, budget?.hardRemainingMs ?? 0))}`,
      `  last_progress: ${challenge.lastMeaningfulSignalContent || challenge.lastSignalContent || "(none)"} (${fmtElapsed(elapsed)} ago; kind=${challenge.lastSignalKind ?? "none"}; evidence=${challenge.lastEvidenceRef || "none"})`,
      `  current_approach: ${challenge.currentApproach || "(declare one in the next checkpoint)"}`,
      `  PREVIOUS_APPROACHES: ${previousApproaches}`,
      `  RULED_OUT: ${ruledOut.join(", ") || "(none recorded)"}`,
      `  STRATEGY_RESET: ${challenge.attemptCount > 1 ? "This is a recovery attempt. Start from a materially different hypothesis; do not rerun the prior tools with cosmetic parameter changes." : "not required on the first attempt"}`,
      `  suggested_unused: ${unused.join(", ") || "derive a new hypothesis from the challenge evidence"}`,
      `  tried: ${challenge.triedFamilies.slice(-5).join(", ") || "(none)"}`,
      `  next_probe: ${challenge.nextProbe || "(not set)"}`,
      `  hint: ${challenge.hintUsed ? challenge.hintContent || "requested; no content returned" : "not used"}`
    );
    const relatedIntel = ledger.intelForChallenge(challenge).slice(-4);
    if (relatedIntel.length) myLines.push(`  shared_intel: ${relatedIntel.map((entry) => `[${entry.target}] ${entry.intel}`).join(" | ")}`);
    if (ledger.isBudgetExhausted(challenge.uniqueCode)) {
      myLines.push(
        "",
        `  ⚠ TIMEBOX_EXPIRED (${budget?.policy.label}; ${fmtElapsed(budget?.sinceProgressMs ?? elapsed)} since meaningful progress).`,
        `  Solving tools are blocked. Submit a confirmed flag, checkpoint NEW evidence, or defer for a different approach.`,
        `  ${budget?.workerRotationDue ? "A fresh worker is required; preserve the live container through warm handoff." : "Do not extend the same failed hypothesis by rewording it."}`
      );
    }
  } else {
    myLines.push("## My challenge: (none — benchmark_control(action=\"acquire\") one)");
  }

  // SubAgent challenges and candidate queue are only relevant to the MAIN
  // Agent — a SubAgent sees only its own challenge to stay focused.
  const isMain = worker === "main";
  const subagentChallenges = isMain
    ? Object.values(state.challenges).filter((challenge) =>
        challenge.owner !== null && challenge.owner !== "main" && (challenge.status === "running" || challenge.status === "reserved")
      )
    : [];
  const subagentLines = subagentChallenges.length
    ? subagentChallenges.map((challenge) => `  ${challenge.uniqueCode} (${challenge.status}, flags ${challenge.correctFlagCount}/${challenge.flagCount})`)
    : ["  (none active)"];

  // Candidate queue (next 5) — main Agent only.
  const candidates = isMain ? ledger.candidates(5) : [];

  // Run overview — abbreviated for SubAgents (score/solved totals only).
  const score = state.scoreExact ? String(state.cumulativeScore) : `${state.cumulativeScore}+ (not exact)`;
  const remainingFlags = Object.values(state.challenges).reduce((total, challenge) => total + Math.max(0, challenge.flagCount - challenge.correctFlagCount), 0);
  const runLine = isMain
    ? `## Run: phase=${state.phase} | elapsed=${fmtElapsed(ledger.runElapsedMs())} | score=${score} | solved=${state.solvedCount}/${state.totalChallenges} | remaining_flags=${remainingFlags} | deferred=${Object.values(state.challenges).filter((challenge) => challenge.status === "deferred").length} | handoff=${Object.values(state.challenges).filter((challenge) => challenge.status === "handoff_waiting").length} | exhausted=${state.exhaustedCount} | containers=${state.activeContainers}/3`
    : `## Run: phase=${state.phase} | elapsed=${fmtElapsed(ledger.runElapsedMs())} | score=${score} | solved=${state.solvedCount}/${state.totalChallenges}`;

  const lines = [
    "<riftx-benchmark-continuity>",
    runLine,
    ...myLines,
    ...(isMain ? [`## SubAgent challenges (${subagentChallenges.length}/2):`, ...subagentLines] : []),
    ...(candidates.length ? ["## Next candidates:", ...candidates.map(challengeLine)] : []),
    `## Platform sync: ${state.lastSyncAt ? fmtElapsed(Date.now() - state.lastSyncAt) : "never"} ago | VPN: ${state.vpnChecked ? (state.vpnOk ? "ok" : "FAIL") : "not prechecked"}`,
    "</riftx-benchmark-continuity>"
  ];

  const configuredToken = process.env.BENCHMARK_TOKEN ?? "";
  const joined = configuredToken
    ? lines.join("\n").split(configuredToken).join("[REDACTED_BENCHMARK_TOKEN]")
    : lines.join("\n");
  if (joined.length <= MAX_BENCHMARK_CONTINUITY_CHARS) return joined;
  // Shed candidate lines first, then subagent detail, preserving the header and timeout warning.
  const trimmed = joined.slice(0, MAX_BENCHMARK_CONTINUITY_CHARS - 40);
  const lastNewline = trimmed.lastIndexOf("\n");
  return `${trimmed.slice(0, lastNewline)}\n[...truncated by continuity budget]\n</riftx-benchmark-continuity>`;
}
