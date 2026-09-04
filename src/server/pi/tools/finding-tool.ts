import { Type } from "@sinclair/typebox";
import { defineTool, type AgentSession, type ToolDefinition } from "@mariozechner/pi-coding-agent";
import type { FindingInput, FindingSource } from "@/lib/types";
import type { EvidenceStore } from "../evidence-store";
import { bindToolEvidence, requireFindingEvidence } from "../finding-evidence";
import type { BrowserManager } from "@/browser";

/** The record_finding tool: writes evidence-backed conclusions to the parent session's store. */

export type FindingSourceInfo = { source: FindingSource; subagentId?: string };

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
          // Tool identity and captured content come from the transcript when
          // present. Confirmed findings reject unknown/empty refs; likely and
          // suspected keep the original pointer after compaction.
          return bindToolEvidence(item, getSession()?.messages ?? [], input.confidence);
        }
        if (item.type === "request") return browser.requestEvidence(item.requestRef);
        if (item.type === "screenshot") return browser.screenshotEvidence(item.screenshotId);
        return item;
      }));
      requireFindingEvidence(input.confidence, evidence);
      const finding = await store.upsert({ ...input, evidence }, source.source, source.subagentId);
      return { content: [{ type: "text", text: `Finding recorded: ${finding.title}` }], details: { findingId: finding.id } };
    }
  });
}
