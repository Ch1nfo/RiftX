import type { SubagentTask } from "@/lib/types";

type JoinManager = {
  list(): SubagentTask[];
  hasActiveTasks(): boolean;
  waitForAll(): Promise<void>;
};

export type SubagentJoinRecord = {
  subagents?: JoinManager;
  abortPromise?: Promise<void>;
  aborting?: boolean;
  abortEpoch?: number;
  waitingForSubagents?: boolean;
  deliveredSubagentResults: Set<string>;
  gate: { beginTask(): void };
  session: { prompt(message: string): Promise<void> };
};

function terminalSubagent(task: SubagentTask) {
  return task.status === "completed" || task.status === "failed" || task.status === "cancelled" || task.status === "interrupted";
}

export function shouldDeliverSubagentCompletion(record: Pick<SubagentJoinRecord, "waitingForSubagents" | "abortPromise" | "aborting">) {
  return !record.waitingForSubagents && !record.abortPromise && !record.aborting;
}

export async function waitForSubagentsBeforeConclusion(record: SubagentJoinRecord, knownTaskIds: Set<string>, requiredTaskIds: Set<string>, abortEpoch: number) {
  const manager = record.subagents;
  if (!manager) return;
  if ((record.abortEpoch ?? 0) !== abortEpoch) return;
  for (const task of manager.list()) {
    if (!knownTaskIds.has(task.id)) requiredTaskIds.add(task.id);
  }
  const hasUndeliveredTerminal = manager.list().some((task) => requiredTaskIds.has(task.id)
    && !record.deliveredSubagentResults.has(task.id)
    && terminalSubagent(task));
  if (!manager.hasActiveTasks() && !hasUndeliveredTerminal) return;
  while (requiredTaskIds.size > 0) {
    if (manager.hasActiveTasks()) await manager.waitForAll();
    if ((record.abortEpoch ?? 0) !== abortEpoch) return;
    const tasks = manager.list();
    for (const task of tasks) {
      if (!knownTaskIds.has(task.id)) requiredTaskIds.add(task.id);
    }
    const results = tasks.filter((task) => requiredTaskIds.has(task.id) && !record.deliveredSubagentResults.has(task.id) && terminalSubagent(task));
    if (results.length) {
      const message = results.map((task) => {
        const status = task.status === "completed" ? "completed" : task.status;
        const summary = task.summary?.trim() || task.error?.trim() || "No result";
        return `[RiftX subagent result]\nSubagent: ${task.name}\nStatus: ${status}\nSummary:\n${summary}`;
      }).join("\n\n");
      for (const task of results) record.deliveredSubagentResults.add(task.id);
      record.waitingForSubagents = false;
      record.gate.beginTask();
      await record.session.prompt(`${message}\n\nAll delegated child tasks required for this assessment have now reached a terminal state. Synthesize the final conclusion using these results. Do not start more child tasks or poll task files.`);
    }
    const pending = tasks.some((task) => requiredTaskIds.has(task.id) && !record.deliveredSubagentResults.has(task.id));
    if (!manager.hasActiveTasks() && !pending) return;
  }
}
