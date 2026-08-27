import type { Finding, SubagentTask } from "@/lib/types";
import type { MergeableMessage } from "@/lib/message-merge";

/** Pure helpers for resolving evidence links (tool/request clicks) to targets. */

export type EvidenceTarget = { kind: "tool"; toolCallId: string } | { kind: "subagent"; taskId: string; logId: string };

export function containsToken(text: string, token: string) {
  const escaped = token.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  return new RegExp(`(?:^|[^\\w])${escaped}(?:[^\\w]|$)`).test(text);
}

export function findRequestTarget(requestRef: string, finding: Finding, messages: MergeableMessage[], subagents: SubagentTask[]): EvidenceTarget | null {
  const searchSubagent = (taskId?: string) => {
    const tasks = taskId ? subagents.filter((task) => task.id === taskId) : subagents;
    for (const task of tasks) {
      const log = task.logs.find((entry) => containsToken(entry.content, requestRef));
      if (log) return { kind: "subagent", taskId: task.id, logId: log.id } as const;
    }
    return null;
  };
  // Search only what the conversation can render: spawn_subagent calls carry
  // the subagent prompt, so a request ref echoed into it would otherwise
  // resolve to a message that is never displayed.
  const visible = messages.filter((message) => message.toolName !== "spawn_subagent");
  const searchMain = () => {
    const tool = visible.find((message) => message.role === "tool" && containsToken(message.content, requestRef));
    return tool?.toolCallId ? { kind: "tool", toolCallId: tool.toolCallId } as const : null;
  };
  if (finding.source === "subagent") return finding.subagentId ? searchSubagent(finding.subagentId) : null;
  if (finding.subagentId) return searchSubagent(finding.subagentId) ?? searchMain();
  return searchMain();
}
