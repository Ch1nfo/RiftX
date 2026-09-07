import type { ToolDefinition } from "@mariozechner/pi-coding-agent";
import { runWithDeadline } from "@/server/deadline";

/** Add a finite caller-visible deadline without changing a Pi tool's schema. */
export function withToolDeadline(tool: ToolDefinition, timeoutMs: number): ToolDefinition {
  const execute = tool.execute.bind(tool);
  tool.description += ` RiftX stops this operation after ${Math.round(timeoutMs / 1000)} seconds.`;
  tool.execute = (toolCallId, params, signal, onUpdate, context) => runWithDeadline(
    (deadlineSignal) => execute(toolCallId, params, deadlineSignal, onUpdate, context),
    { signal, timeoutMs, timeoutMessage: `${tool.name} timed out after ${timeoutMs}ms` }
  );
  return tool;
}
