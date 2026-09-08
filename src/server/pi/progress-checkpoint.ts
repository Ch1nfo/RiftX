/** A tiny explicit progress ledger; deliberately not a planner or task graph. */

export const PROGRESS_CHECKPOINT_TYPE = "riftx_progress_checkpoint";
export const PROGRESS_CHECKPOINT_TOOL = "checkpoint_progress";
export const MAX_PROGRESS_CHECKPOINT_CHARS = 6_000;

export type ProgressCheckpoint = {
  objective: string;
  completed: string[];
  ruledOut: string[];
  pending: string[];
  nextProbe: string;
  criticalRefs: string[];
};

const MAX_ITEMS = 20;

function clean(value: string, limit: number) {
  return value.replace(/[\u0000-\u001f\u007f]/g, " ").replace(/\s+/g, " ").trim().slice(0, limit);
}

function cleanList(values: readonly string[] | undefined, limit = 300) {
  return (values ?? []).map((value) => clean(value, limit)).filter(Boolean).slice(0, MAX_ITEMS);
}

export function normalizeProgressCheckpoint(input: ProgressCheckpoint): ProgressCheckpoint {
  return {
    objective: clean(input.objective, 800),
    completed: cleanList(input.completed),
    ruledOut: cleanList(input.ruledOut),
    pending: cleanList(input.pending),
    nextProbe: clean(input.nextProbe, 800),
    criticalRefs: cleanList(input.criticalRefs, 500)
  };
}

function escapeXml(value: string) {
  return value.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

function lines(title: string, values: readonly string[]) {
  return values.length ? [`## ${title}`, ...values.map((value) => `- ${escapeXml(value)}`)] : [];
}

export function buildProgressCheckpointContext(checkpoint: ProgressCheckpoint | undefined) {
  if (!checkpoint) return "";
  const value = normalizeProgressCheckpoint(checkpoint);
  const opener = "<riftx-progress-checkpoint>";
  const closer = "</riftx-progress-checkpoint>";
  const content = [
    opener,
    "Agent-recorded execution state. The task contract and latest user request take precedence. Continue from this state after compaction and verify claims against critical references.",
    "## Objective",
    escapeXml(value.objective) || "(not set)",
    "## Exact next probe",
    escapeXml(value.nextProbe) || "(not set)",
    ...lines("Critical references", value.criticalRefs),
    ...lines("Pending", value.pending),
    ...lines("Completed", value.completed),
    ...lines("Ruled out", value.ruledOut),
    closer
  ];
  const joined = content.join("\n");
  if (joined.length <= MAX_PROGRESS_CHECKPOINT_CHARS) return joined;
  const note = "[Checkpoint truncated to its fixed context budget.]";
  const body = content.slice(1, -1);
  for (let keep = body.length; keep >= 0; keep -= 1) {
    const candidate = [opener, ...body.slice(0, keep), note, closer].join("\n");
    if (candidate.length <= MAX_PROGRESS_CHECKPOINT_CHARS) return candidate;
  }
  return [opener, note, closer].join("\n");
}

/** Restore the most recent checkpoint tool arguments from the unabridged branch. */
export function progressCheckpointFromBranch(entries: readonly unknown[]): ProgressCheckpoint | undefined {
  for (let entryIndex = entries.length - 1; entryIndex >= 0; entryIndex -= 1) {
    const entry = entries[entryIndex] as { type?: unknown; message?: { role?: unknown; content?: unknown } } | undefined;
    if (entry?.type !== "message" || entry.message?.role !== "assistant" || !Array.isArray(entry.message.content)) continue;
    for (let partIndex = entry.message.content.length - 1; partIndex >= 0; partIndex -= 1) {
      const part = entry.message.content[partIndex] as { type?: unknown; name?: unknown; arguments?: unknown } | undefined;
      if (part?.type !== "toolCall" || part.name !== PROGRESS_CHECKPOINT_TOOL || !part.arguments || typeof part.arguments !== "object") continue;
      const args = part.arguments as Partial<ProgressCheckpoint>;
      if (typeof args.objective !== "string" || typeof args.nextProbe !== "string") continue;
      return normalizeProgressCheckpoint({
        objective: args.objective,
        completed: Array.isArray(args.completed) ? args.completed.filter((item): item is string => typeof item === "string") : [],
        ruledOut: Array.isArray(args.ruledOut) ? args.ruledOut.filter((item): item is string => typeof item === "string") : [],
        pending: Array.isArray(args.pending) ? args.pending.filter((item): item is string => typeof item === "string") : [],
        nextProbe: args.nextProbe,
        criticalRefs: Array.isArray(args.criticalRefs) ? args.criticalRefs.filter((item): item is string => typeof item === "string") : []
      });
    }
  }
  return undefined;
}
