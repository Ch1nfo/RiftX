import type { BoardState } from "@/lib/collaboration";
import type { SubagentTask } from "@/lib/types";

/** Compatibility view only: SQLite remains the task authority. */
export function projectSubagents(state: BoardState): SubagentTask[] {
  return state.agents.filter((agent) => agent.role === "child").map((agent) => {
    const work = state.tasks.find((task) => task.id === agent.taskId) ?? state.tasks.filter((task) => task.owner === agent.id).at(-1);
    return { id: agent.id, parentSessionId: state.sessionId, threadId: agent.id, name: agent.name,
      task: work?.objective ?? "", status: agent.status === "running" ? "running" : work?.status === "done" ? "completed" : work?.status === "failed" ? "failed" : work?.status === "cancelled" ? "cancelled" : "interrupted",
      model: agent.model ?? "", createdAt: new Date(work?.createdAt ?? agent.lastActive).toISOString(), summary: work?.summary,
      error: work?.blockedReason, pendingApprovalCount: agent.pendingApprovalCount, logs: [] };
  });
}
