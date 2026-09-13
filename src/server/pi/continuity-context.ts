import type { AgentSession } from "@mariozechner/pi-coding-agent";
import { INVESTIGATION_CAPSULE_TYPE } from "./investigation-capsule";
import { PROGRESS_CHECKPOINT_TYPE } from "./progress-checkpoint";
import { TASK_CONTRACT_TYPE } from "./task-contract";

export const SKILL_CONTEXT_TYPE = "riftx_skill_context";
export const TOOL_INVENTORY_CONTEXT_TYPE = "riftx_tool_inventory";

export type ContinuityContext = {
  taskContract?: string;
  skillContext?: string;
  investigationCapsule?: string;
  progressCheckpoint?: string;
  toolInventory?: string;
};

const TYPES = new Set([TASK_CONTRACT_TYPE, SKILL_CONTEXT_TYPE, INVESTIGATION_CAPSULE_TYPE, PROGRESS_CHECKPOINT_TYPE, TOOL_INVENTORY_CONTEXT_TYPE]);

export function isContinuityMessage(message: unknown) {
  if (!message || typeof message !== "object") return false;
  const candidate = message as { role?: unknown; customType?: unknown };
  return candidate.role === "custom" && typeof candidate.customType === "string" && TYPES.has(candidate.customType);
}

/** Replace every continuity block as one ordered, duplicate-free tail packet. */
export function upsertContinuityContext(messages: unknown[], context: ContinuityContext) {
  const retained = messages.filter((message) => !isContinuityMessage(message));
  const timestamp = Date.now();
  const blocks = [
    [TASK_CONTRACT_TYPE, context.taskContract],
    [SKILL_CONTEXT_TYPE, context.skillContext],
    [INVESTIGATION_CAPSULE_TYPE, context.investigationCapsule],
    [PROGRESS_CHECKPOINT_TYPE, context.progressCheckpoint],
    [TOOL_INVENTORY_CONTEXT_TYPE, context.toolInventory]
  ] as const;
  const continuityMessages = blocks.flatMap(([customType, content], index) => content?.trim()
    ? [{ role: "custom" as const, customType, content, display: false, timestamp: timestamp + index }]
    : []);
  messages.splice(0, messages.length, ...retained, ...continuityMessages);
}

export function refreshContinuityContext(session: AgentSession, context: ContinuityContext) {
  upsertContinuityContext(session.agent.state.messages as unknown[], context);
}

/** Challenge acquire/release can refresh discovery without rebuilding other continuity. */
export function upsertToolInventory(messages: unknown[], content?: string) {
  const retained = messages.filter((message) => {
    const candidate = message as { role?: unknown; customType?: unknown } | null;
    return !candidate || candidate.role !== "custom" || candidate.customType !== TOOL_INVENTORY_CONTEXT_TYPE;
  });
  if (content?.trim()) retained.push({ role: "custom", customType: TOOL_INVENTORY_CONTEXT_TYPE, content, display: false, timestamp: Date.now() });
  messages.splice(0, messages.length, ...retained);
}
