import type { FindingConfidence, FindingEvidence } from "@/lib/types";
import { textFromContent } from "./text-content";

export type ToolEvidenceSnapshot = { type: "tool"; toolCallId: string; toolName: string; content: string };

export function isHardEvidence(item: FindingEvidence) {
  if (item.type !== "tool") return item.type === "request" || item.type === "screenshot";
  // These tools only record or schedule claims; their acknowledgements are
  // not observations of the target and must not bootstrap circular evidence.
  return item.toolName !== "record_finding" && item.toolName !== "spawn_subagent";
}

export function requireFindingEvidence(confidence: FindingConfidence, evidence: readonly FindingEvidence[]) {
  if (confidence === "confirmed" && !evidence.some(isHardEvidence)) {
    throw new Error("A confirmed finding requires at least one resolvable tool, request, or screenshot evidence reference");
  }
}

/** Resolve tool identity and output from the actual session transcript. */
export function resolveToolEvidence(messages: readonly unknown[], requestedId: string): ToolEvidenceSnapshot | undefined {
  const transcript = messages as Array<{ role?: string; content?: unknown; toolCallId?: string }>;
  const calls = transcript.flatMap((message) => {
    const parts = Array.isArray(message.content) ? message.content : [];
    return parts.filter((part): part is { type?: string; id?: string; name?: string } => Boolean(part && typeof part === "object" && (part as { type?: string }).type === "toolCall"))
      .map((part) => ({ id: String(part.id ?? ""), name: String(part.name ?? "tool") }));
  });
  const selected = calls.find((call) => call.id === requestedId);
  if (!selected) return undefined;
  const result = transcript.find((message) => message.role === "toolResult" && message.toolCallId === selected.id);
  const content = textFromContent(result?.content);
  return content ? { type: "tool", toolCallId: selected.id, toolName: selected.name, content } : undefined;
}

/** Confirmed findings must cite a live transcript result. Likely/suspected may
 * keep a compacted or unknown tool id so the hypothesis is not dropped. */
export function bindToolEvidence(
  item: Extract<FindingEvidence, { type: "tool" }>,
  messages: readonly unknown[],
  confidence: FindingConfidence
): FindingEvidence {
  const snapshot = resolveToolEvidence(messages, item.toolCallId);
  if (snapshot) return snapshot;
  if (confidence === "confirmed") throw new Error(`Unknown or empty tool evidence ${item.toolCallId}`);
  return item;
}
