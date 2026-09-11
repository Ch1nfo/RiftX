import { textFromContent } from "./text-content";

const normalized = (text: string) => text.replace(/\s+/g, " ").trim();
const empty = (text: string) => /^(?:\(?none\)?|n\/a|[-–—]|\.{3})[.!]?$/i.test(text.trim());

/** Check exact anchors we can verify without asking a second model to judge
 * itself. This is deliberately bounded; it is not a semantic fact checker. */
export function compactionFacts(messages: readonly unknown[]): string[] {
  const records = messages as readonly { role?: string; content?: unknown; summary?: string }[];
  const user = [...records].reverse().find((message) => message.role === "user");
  const request = user ? textFromContent(user.content).trim() : "";
  const text = records.map((message) => message.summary ?? textFromContent(message.content)).join("\n");
  const references = [...text.matchAll(/\b(?:request|screenshot|artifact):[\w./:-]+|https?:\/\/[^\s<>"'`]+/g)]
    .map(([reference]) => reference.replace(/[),.;]+$/, "")).filter((reference) => reference.length <= 500);
  return [...new Set([...(request && request.length <= 1200 ? [request] : []), ...[...new Set(references)].slice(-8)])];
}

export function summaryIssues(summary: string, headings: readonly string[], facts: readonly string[] = [], survivingContext = "") {
  const issues: string[] = [];
  const sections = summary.split(/(?=^## )/m);
  for (const heading of headings) {
    const matches = sections.filter((section) => section.split("\n", 1)[0].trim() === heading);
    if (matches.length !== 1 || !matches[0].slice(heading.length).trim()) issues.push(`Missing or empty section: ${heading}`);
  }
  const bodies = sections.filter((section) => section.startsWith("## ")).map((section) => section.slice(section.indexOf("\n") + 1).trim());
  if (!bodies.some((body) => body && !empty(body))) issues.push("The checkpoint contains no substantive facts");
  const covered = normalized(`${summary}\n${survivingContext}`);
  for (const fact of facts) {
    if (!covered.includes(normalized(fact))) issues.push(`Missing protected fact: ${fact}`);
  }
  return issues;
}

/** Only remove exact nontrivial lines also supplied by the fresh runtime packet.
 * Historical evidence absent from that selected packet must remain recoverable. */
export function deduplicateSummaryState(summary: string, currentState: string) {
  const lineKey = (line: string) => normalized(line.replace(/^\s*[-*]\s+/, ""));
  const known = new Set(currentState.split("\n").map(lineKey).filter((line) => line.length >= 40 && !line.startsWith("##")));
  let referenced = false;
  return summary.split("\n").flatMap((line) => {
    if (line.startsWith("## ")) referenced = false;
    if (!known.has(lineKey(line))) return [line];
    if (referenced) return [];
    referenced = true;
    return ["See the current runtime state for these facts; its latest values take precedence."];
  }).join("\n");
}
