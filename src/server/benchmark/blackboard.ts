import type { AttemptSummary, BlackboardEntry, ChallengeState } from "./ledger";

function priority(entry: BlackboardEntry): number {
  if (entry.kind === "handoff") return entry.summary.startsWith("FINDINGS:") || entry.summary.startsWith("EVIDENCE:") ? 1 : 0;
  if (!entry.evidenceRef) return 0;
  if (entry.kind === "note") return 1;
  if (entry.kind === "credential" || entry.kind === "foothold") return 3;
  return ["privilege_change", "exploit_primitive", "stage_transition", "decisive_rule_out", "new_surface"].includes(entry.kind) ? 2 : 0;
}

/** Durable observations survive routine activity; identical entries are replaced. */
export function retainBlackboard(entries: BlackboardEntry[]): BlackboardEntry[] {
  const unique = new Map<string, BlackboardEntry>();
  for (const entry of entries) {
    // Identical wording in different attempts is still a separate observation.
    const key = JSON.stringify(entry);
    unique.delete(key);
    unique.set(key, entry);
  }
  // This is durable memory, not a prompt budget. Context projections select a
  // bounded view; old unsuccessful attempts must remain available on demand.
  return [...unique.values()];
}

/** Reserve space for recent uncertainty as well as lasting observations. */
export function selectBlackboard(challenge: Pick<ChallengeState, "blackboard">, limit: number): BlackboardEntry[] {
  const latest = new Map<string, BlackboardEntry>();
  for (const entry of challenge.blackboard) {
    const key = JSON.stringify([entry.kind, entry.summary, entry.evidenceRef]);
    latest.delete(key);
    latest.set(key, entry);
  }
  // Repeated observations stay in durable history but use only one preview slot.
  const entries = [...latest.values()];
  const ranked = entries.filter((entry) => priority(entry))
    .sort((a, b) => priority(b) - priority(a) || b.at - a.at);
  // Repeated credentials must not displace every later stage or exclusion.
  const kinds = new Set<BlackboardEntry["kind"]>();
  const diverse = ranked.filter((entry) => kinds.has(entry.kind) ? false : (kinds.add(entry.kind), true));
  const facts = [...diverse, ...ranked.filter((entry) => !diverse.includes(entry))].slice(0, Math.ceil(limit * 2 / 3));
  const recent = entries.filter((entry) => !facts.includes(entry)).slice(-(limit - facts.length));
  return [...facts, ...recent].slice(0, limit);
}

export function handoffCandidate(approach = "", nextProbe = "") {
  if (!approach && !nextProbe) return undefined;
  return { requiresRevalidation: true, approach: approach.slice(0, 300), nextProbe: nextProbe.slice(0, 1_000) };
}

export function evidenceBackedRuleOuts(challenge: Pick<ChallengeState, "blackboard" | "ruledOutFamilies">): string[] {
  const supported = new Set(challenge.blackboard.filter((entry) => entry.kind === "decisive_rule_out" && entry.evidenceRef)
    .flatMap((entry) => entry.ruledOutFamilies));
  return challenge.ruledOutFamilies.filter((family) => supported.has(family)).slice(-20);
}

/** Historical observations and a separate, unverified proposal for revisiting. */
export function handoffAttempt(attempt: AttemptSummary, supportedRuleOuts: readonly string[]) {
  return {
    attemptNumber: attempt.attemptNumber, phase: attempt.phase, worker: attempt.worker,
    startedAt: attempt.startedAt, endedAt: attempt.endedAt,
    flagsBefore: attempt.flagsBefore, flagsAfter: attempt.flagsAfter,
    flagsDelta: attempt.flagsAfter - attempt.flagsBefore,
    triedFamilies: attempt.triedFamilies.slice(-20).map((value) => value.slice(0, 100)),
    ruledOutFamilies: attempt.ruledOutFamilies.filter((value) => supportedRuleOuts.includes(value)).slice(-20).map((value) => value.slice(0, 100)),
    stopReason: (attempt.stopReason ?? "").slice(0, 1_000),
    previousCandidate: handoffCandidate(attempt.approach, attempt.nextDistinctApproach)
  };
}

export function blackboardLabel(entry: BlackboardEntry): string {
  return entry.kind === "handoff" ? `child report from ${entry.worker}; not independently verified` : entry.kind;
}

/** Parse the existing return format without generating or inheriting a plan. */
export function childHandoffSections(summary: string): string[] {
  const aliases: Record<string, string> = {
    findings: "FINDINGS", observations: "FINDINGS", "发现": "FINDINGS", "观察": "FINDINGS", "关键发现": "FINDINGS",
    evidence: "EVIDENCE", "证据": "EVIDENCE", artifacts: "ARTIFACTS", "产物": "ARTIFACTS", "文件": "ARTIFACTS",
    tried: "TRIED", "已尝试": "TRIED", ruled_out: "RULED_OUT", "ruled out": "RULED_OUT", "已排除": "RULED_OUT",
    uncertainties: "UNCERTAINTIES", "不确定性": "UNCERTAINTIES", "未解决问题": "UNCERTAINTIES", "疑问": "UNCERTAINTIES"
  };
  const sections = new Map<string, string[]>();
  let section = "";
  for (const raw of summary.split("\n")) {
    const line = raw.trim().replace(/^(?:#{1,6}\s*|[-*]\s+|\d+[.)]\s+)/, "").replace(/\*\*/g, "");
    const heading = /^([^:：]{1,40})[:：]\s*(.*)$/.exec(line);
    const name = aliases[(heading?.[1] ?? line).trim().toLowerCase()];
    if (name) {
      section = name;
      sections.set(section, [...(sections.get(section) ?? []), heading?.[2] ?? ""]);
    } else if (/^#{1,6}\s/.test(raw.trim()) || (heading && /^[A-Z][A-Z_ ]+$/.test(heading[1]))) {
      section = "";
    } else if (/^(?:next\b|plan\b|strategy\b|current (?:route|approach)\b|下一步|当前路线|当前思路|后续建议|计划)/i.test(line)) {
      section = "";
    } else if (section && line) sections.get(section)!.push(line);
  }
  return [...sections].filter(([, lines]) => lines.some((line) => line.trim()))
    .map(([name, lines]) => `${name}: ${lines.join("\n").trim()}`);
}
