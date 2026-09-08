import { Type } from "@sinclair/typebox";
import { defineTool, type ToolDefinition } from "@mariozechner/pi-coding-agent";
import { normalizeProgressCheckpoint, PROGRESS_CHECKPOINT_TOOL, type ProgressCheckpoint } from "../progress-checkpoint";

export function createProgressCheckpointTool(update: (checkpoint: ProgressCheckpoint) => void): ToolDefinition {
  return defineTool({
    name: PROGRESS_CHECKPOINT_TOOL,
    label: "Checkpoint progress",
    description: "Replace the compact continuity checkpoint for a long task. Use at meaningful phase boundaries, after a SubAgent batch, or before moving to a new attack direction; do not call after every probe.",
    promptSnippet: "checkpoint_progress(objective, completed, ruledOut, pending, nextProbe, criticalRefs)",
    parameters: Type.Object({
      objective: Type.String({ maxLength: 800 }),
      completed: Type.Array(Type.String({ maxLength: 300 }), { maxItems: 20 }),
      ruledOut: Type.Array(Type.String({ maxLength: 300 }), { maxItems: 20 }),
      pending: Type.Array(Type.String({ maxLength: 300 }), { maxItems: 20 }),
      nextProbe: Type.String({ maxLength: 800 }),
      criticalRefs: Type.Array(Type.String({ maxLength: 500 }), { maxItems: 20 })
    }),
    async execute(_toolCallId, params) {
      const checkpoint = normalizeProgressCheckpoint(params);
      update(checkpoint);
      return {
        content: [{ type: "text" as const, text: `Progress checkpoint updated: ${checkpoint.completed.length} completed, ${checkpoint.ruledOut.length} ruled out, ${checkpoint.pending.length} pending.` }],
        details: { pending: checkpoint.pending.length, nextProbe: checkpoint.nextProbe }
      };
    }
  });
}

