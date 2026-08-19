import { completeSimple, type Model } from "@mariozechner/pi-ai";
import type { ModelRegistry } from "@mariozechner/pi-coding-agent";
import type { ApprovalRequest } from "@/lib/types";
import { textFromModelContent } from "./text-content";

export const APPROVAL_EVALUATOR_PROMPT = `You are RiftX's internal approval evaluator for an authorized Web security assessment.

Decide based on one question: could this tool call materially harm either (1) the user's local computer, workspace, files, credentials, or services, or (2) the authorized system under test, its data, availability, users, or surrounding infrastructure?

Approve actions when they are read-only, diagnostic, analytical, or bounded security testing and do not create meaningful impact to either side. Do not reject an action merely because it uses a particular tool or technique. Judge the actual command, arguments, scope, destination, rate, reversibility, and likely side effects.

Reject actions that could delete or corrupt data, change system state without a clear safe rollback, establish persistence, expose or exfiltrate credentials or sensitive data, cause service disruption or resource exhaustion, send uncontrolled or out-of-scope traffic, affect real users, or expand beyond the authorized target. A clearly temporary, local, and reversible test artifact is acceptable when it cannot affect the target or the user's computer beyond the stated workspace.

Return JSON only, with exactly this shape:
{"decision":"approve"|"reject","reason":"short explanation"}

When the likely impact or scope cannot be determined from the tool call, reject and explain what is missing.`;

export type ApprovalEvaluation = {
  approved: boolean;
  reason: string;
};

function extractFirstJsonObject(raw: string) {
  const start = raw.indexOf("{");
  const end = raw.lastIndexOf("}");
  if (start < 0 || end < start) return raw;
  return raw.slice(start, end + 1);
}

export async function evaluateApproval(model: Model<any>, modelRegistry: ModelRegistry, request: ApprovalRequest): Promise<ApprovalEvaluation> {
  const auth = await modelRegistry.getApiKeyAndHeaders(model);
  if (!auth.ok) throw new Error(auth.error);
  const response = await completeSimple(model, {
    systemPrompt: APPROVAL_EVALUATOR_PROMPT,
    messages: [{
      role: "user",
      content: `Tool: ${request.toolName}\nInput:\n${JSON.stringify(request.input, null, 2)}`,
      timestamp: Date.now()
    }]
  }, {
    apiKey: auth.apiKey,
    headers: auth.headers,
    maxTokens: 256,
    temperature: 0,
    timeoutMs: 30_000,
    maxRetries: 0
  });
  const raw = textFromModelContent(response.content).trim();
  const parsed = JSON.parse(extractFirstJsonObject(raw)) as { decision?: string; reason?: string };
  return {
    approved: parsed.decision === "approve",
    reason: parsed.reason?.trim() || "No reason provided"
  };
}
