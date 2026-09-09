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
  return `  ${challenge.uniqueCode} | ${challenge.difficulty} | ${challenge.totalScore}pts | flags ${challenge.correctFlagCount}/${challenge.flagCount} | ${challenge.status}`;
}

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
    myLines.push(
      `## My challenge: ${challenge.uniqueCode} | ${challenge.difficulty} | ${challenge.totalScore}pts`,
      `  flags: ${challenge.correctFlagCount}/${challenge.flagCount} | addr: ${challenge.containerAddrs.join(", ") || "(none)"}`,
      `  description: ${challenge.description.slice(0, 3_000) || "(none)"}`,
      `  last_signal: ${challenge.lastSignalContent || "(none)"} (${fmtElapsed(elapsed)} ago)`,
      `  tried: ${challenge.triedFamilies.slice(-5).join(", ") || "(none)"}`,
      `  next_probe: ${challenge.nextProbe || "(not set)"}`,
      `  hint: ${challenge.hintUsed ? challenge.hintContent || "requested; no content returned" : "not used"}`
    );
    const relatedIntel = ledger.intelForChallenge(challenge).slice(-4);
    if (relatedIntel.length) myLines.push(`  shared_intel: ${relatedIntel.map((entry) => `[${entry.target}] ${entry.intel}`).join(" | ")}`);
    if (ledger.isBudgetExhausted(challenge.uniqueCode)) {
      myLines.push(
        "",
        `  ⚠ 8-MINUTE BUDGET EXHAUSTED (${fmtElapsed(elapsed)} since last new signal).`,
        `  benchmark_control(action="defer", uniqueCode="${challenge.uniqueCode}") NOW,`,
        `  or checkpoint with a genuinely NEW signal if one was just found.`
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
  const runLine = isMain
    ? `## Run: phase=${state.phase} | score=${score} | solved=${state.solvedCount}/${state.totalChallenges} | deferred=${Object.values(state.challenges).filter((challenge) => challenge.status === "deferred").length} | exhausted=${state.exhaustedCount} | containers=${state.activeContainers}/3`
    : `## Run: phase=${state.phase} | score=${score} | solved=${state.solvedCount}/${state.totalChallenges}`;

  const lines = [
    "<riftx-benchmark-continuity>",
    runLine,
    ...myLines,
    ...(isMain ? [`## SubAgent challenges (${subagentChallenges.length}/2):`, ...subagentLines] : []),
    ...(candidates.length ? ["## Next candidates:", ...candidates.map(challengeLine)] : []),
    `## Platform sync: ${state.lastSyncAt ? fmtElapsed(Date.now() - state.lastSyncAt) : "never"} ago | VPN: ${state.vpnOk ? "ok" : "FAIL"}`,
    "</riftx-benchmark-continuity>"
  ];

  const joined = lines.join("\n");
  if (joined.length <= MAX_BENCHMARK_CONTINUITY_CHARS) return joined;
  // Shed candidate lines first, then subagent detail, preserving the header and timeout warning.
  const trimmed = joined.slice(0, MAX_BENCHMARK_CONTINUITY_CHARS - 40);
  const lastNewline = trimmed.lastIndexOf("\n");
  return `${trimmed.slice(0, lastNewline)}\n[...truncated by continuity budget]\n</riftx-benchmark-continuity>`;
}
