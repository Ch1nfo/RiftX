import type { BlackboardEntry, ChallengeState } from "./ledger";

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
    const key = JSON.stringify([entry.kind, entry.summary, entry.evidenceRef]);
    unique.delete(key);
    unique.set(key, entry);
  }
  const all = [...unique.values()];
  const recent = new Set(all.filter((entry) => !priority(entry)).slice(-30));
  return all.filter((entry) => priority(entry) || recent.has(entry));
}

/** Reserve space for recent uncertainty as well as lasting observations. */
export function selectBlackboard(challenge: Pick<ChallengeState, "blackboard">, limit: number): BlackboardEntry[] {
  const facts = challenge.blackboard.filter((entry) => priority(entry))
    .sort((a, b) => priority(b) - priority(a) || b.at - a.at).slice(0, Math.ceil(limit * 2 / 3));
  const recent = challenge.blackboard.filter((entry) => !facts.includes(entry)).slice(-(limit - facts.length));
  return [...facts, ...recent].slice(0, limit);
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
