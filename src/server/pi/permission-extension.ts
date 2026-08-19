import type { ExtensionFactory, ToolCallEvent } from "@mariozechner/pi-coding-agent";
import type { ApprovalRequest } from "@/lib/types";
import { ApprovalGate } from "./approval-gate";
import type { ApprovalEvaluation } from "./approval-evaluator";
import type { MutationLock } from "./mutation-lock";

const guardedTools = new Set(["bash", "write", "edit", "browser"]);
const readOnlyBrowserActions = new Set(["snapshot", "requests", "request_detail", "response_body", "cookies", "storage", "screenshot", "tabs"]);

export function createPermissionExtension(
  gate: ApprovalGate,
  onEvent: (event: Record<string, unknown>) => void,
  evaluate?: (request: ApprovalRequest) => Promise<ApprovalEvaluation>,
  mutationLock?: MutationLock
): ExtensionFactory {
  return (pi) => {
    const releases = new Map<string, { release: () => void; cleanupAbort?: () => void }>();
    const releaseTool = (toolCallId: string) => {
      const entry = releases.get(toolCallId);
      if (!entry) return;
      releases.delete(toolCallId);
      entry.cleanupAbort?.();
      entry.release();
    };
    pi.on("tool_execution_end", (event) => {
      releaseTool(event.toolCallId);
    });
    pi.on("tool_call", async (event: ToolCallEvent, ctx) => {
      if (!guardedTools.has(event.toolName)) return;
      if (event.toolName === "browser") {
        const action = (event.input as { action?: unknown }).action;
        if (typeof action === "string" && readOnlyBrowserActions.has(action)) return;
      }
      const request: ApprovalRequest = {
        id: event.toolCallId,
        toolName: event.toolName as ApprovalRequest["toolName"],
        input: event.input,
        createdAt: new Date().toISOString()
      };
      let allowed = gate.shouldBypass(request);
      if (!allowed && gate.approvalMode === "auto" && evaluate) {
        try {
          const evaluation = await evaluate(request);
          onEvent({ type: "approval_evaluated", approval: request, evaluation });
          if (evaluation.approved) allowed = true;
          else return { block: true, reason: `Rejected by RiftX approval evaluator: ${evaluation.reason}` };
        } catch (error) {
          const reason = error instanceof Error ? error.message : "Approval evaluator failed";
          onEvent({ type: "approval_evaluation_error", approval: request, error: reason });
          return { block: true, reason: "Blocked because automatic approval could not be evaluated" };
        }
      }
      if (!allowed) {
        onEvent({ type: "approval_required", approval: request });
        const approved = await gate.waitForApproval(request, 120_000, ctx.signal);
        if (!approved) {
          return { block: true, reason: "Blocked by RiftX safety gate" };
        }
      }
      let release: (() => void) | undefined;
      try {
        release = mutationLock ? await mutationLock.acquire(ctx.signal) : undefined;
        if (release) {
          const onAbort = () => releaseTool(event.toolCallId);
          ctx.signal?.addEventListener("abort", onAbort, { once: true });
          releases.set(event.toolCallId, {
            release,
            cleanupAbort: () => ctx.signal?.removeEventListener("abort", onAbort)
          });
        }
      } catch (error) {
        return { block: true, reason: error instanceof Error ? error.message : "Mutation was blocked" };
      }
    });
  };
}
