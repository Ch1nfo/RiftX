import { Type } from "@sinclair/typebox";
import { defineTool, type AgentSession, type ToolDefinition } from "@mariozechner/pi-coding-agent";
import type { FindingInput, FindingSource } from "@/lib/types";
import type { EvidenceStore } from "../evidence-store";
import { textFromContent } from "../text-content";
import type { BrowserManager } from "@/browser";

/** The record_finding tool: writes evidence-backed conclusions to the parent session's store. */

export type FindingSourceInfo = { source: FindingSource; subagentId?: string };

export type ToolEvidenceSnapshot = { toolCallId: string; toolName: string; content: string };

export function resolveToolEvidence(session: AgentSession | undefined, requestedId: string): ToolEvidenceSnapshot | undefined {
  if (!session) return undefined;
  const messages = session.messages as unknown as Array<{ role?: string; content?: unknown; toolCallId?: string }>;
  const calls = messages.flatMap((message) => {
    const parts = Array.isArray(message.content) ? message.content : [];
    return parts.filter((part): part is { type?: string; id?: string; name?: string } => Boolean(part && typeof part === "object" && (part as { type?: string }).type === "toolCall"))
      .map((part) => ({ id: String(part.id ?? ""), name: String(part.name ?? "tool") }));
  });
  const selected = calls.find((call) => call.id === requestedId);
  if (!selected) return undefined;
  const result = messages.find((message) => message.role === "toolResult" && message.toolCallId === selected.id);
  const content = textFromContent(result?.content);
  return content ? { toolCallId: selected.id, toolName: selected.name, content } : undefined;
}

export function createFindingTool(store: EvidenceStore, source: FindingSourceInfo, browser: BrowserManager, getSession: () => AgentSession | undefined): ToolDefinition {
  return defineTool({
    name: "record_finding",
    label: "Record finding",
    description: "Record one evidence-backed finding in the parent session. Use only when there is a concrete, reviewable conclusion; use likely or suspected when validation is incomplete and never create findings to fill a quota.",
    promptSnippet: "record_finding(title, asset, confidence, impact, reproduction, evidence)",
    parameters: Type.Object({
      title: Type.String({ description: "Short finding title." }),
      asset: Type.String({ description: "Affected URL, host, route, or other asset." }),
      confidence: Type.Union([Type.Literal("confirmed"), Type.Literal("likely"), Type.Literal("suspected"), Type.Literal("not_reproducible")]),
      impact: Type.String({ description: "Short description of the actual or expected impact." }),
      reproduction: Type.String({ description: "Short reproducible validation steps or the reason reproduction is incomplete." }),
      evidence: Type.Array(Type.Union([
        Type.Object({ type: Type.Literal("quote"), quote: Type.String() }),
        Type.Object({ type: Type.Literal("tool"), toolCallId: Type.String(), toolName: Type.String() }),
        Type.Object({ type: Type.Literal("request"), requestRef: Type.String() }),
        Type.Object({ type: Type.Literal("screenshot"), screenshotId: Type.String() })
      ]), { minItems: 1 })
    }),
    async execute(_toolCallId, params) {
      const input = params as FindingInput;
      const evidence = await Promise.all(input.evidence.map(async (item) => {
        if (item.type === "tool") {
          const snapshot = resolveToolEvidence(getSession(), item.toolCallId);
          return snapshot ? { ...item, ...snapshot } : item;
        }
        if (item.type === "request") return browser.requestEvidence(item.requestRef);
        if (item.type === "screenshot") return browser.screenshotEvidence(item.screenshotId);
        return item;
      }));
      const finding = await store.upsert({ ...input, evidence }, source.source, source.subagentId);
      return { content: [{ type: "text", text: `Finding recorded: ${finding.title}` }], details: { findingId: finding.id } };
    }
  });
}
