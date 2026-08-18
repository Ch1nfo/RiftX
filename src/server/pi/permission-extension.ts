import type { ExtensionFactory, ToolCallEvent } from "@mariozechner/pi-coding-agent";
import type { ApprovalRequest } from "@/lib/types";
import { ApprovalGate } from "./approval-gate";
import type { ApprovalEvaluation } from "./approval-evaluator";

const guardedTools = new Set(["bash", "write", "edit", "browser"]);
const readOnlyBrowserActions = new Set(["snapshot", "requests", "request_detail", "response_body", "cookies", "storage", "screenshot", "tabs"]);

export function createPermissionExtension(
  gate: ApprovalGate,
  onEvent: (event: Record<string, unknown>) => void,
  evaluate?: (request: ApprovalRequest) => Promise<ApprovalEvaluation>
): ExtensionFactory {
  return (pi) => {
    pi.on("tool_call", async (event: ToolCallEvent) => {
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
      if (gate.shouldBypass(request)) return;
      if (gate.approvalMode === "auto" && evaluate) {
        try {
          const evaluation = await evaluate(request);
          onEvent({ type: "approval_evaluated", approval: request, evaluation });
          if (evaluation.approved) return;
          return { block: true, reason: `Rejected by RiftX approval evaluator: ${evaluation.reason}` };
        } catch (error) {
          const reason = error instanceof Error ? error.message : "Approval evaluator failed";
          onEvent({ type: "approval_evaluation_error", approval: request, error: reason });
          return { block: true, reason: "Blocked because automatic approval could not be evaluated" };
        }
      }
      onEvent({ type: "approval_required", approval: request });
      const approved = await gate.waitForApproval(request);
      if (!approved) return { block: true, reason: "Blocked by RiftX safety gate" };
    });
  };
}
