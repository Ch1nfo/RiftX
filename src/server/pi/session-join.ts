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
  deliveringSubagentResults?: Set<string>;
  promptChain?: Promise<void>;
  subagentDeliveryInProgress?: boolean;
  gate: { beginTask(): void };
  session: { prompt(message: string): Promise<void> };
};

/** Serialize SDK prompt-like calls; AgentSession rejects overlapping runs. */
export function enqueueSessionAction(record: Pick<SubagentJoinRecord, "promptChain">, action: () => Promise<void>) {
  const previous = record.promptChain ?? Promise.resolve();
  const next = previous.catch(() => undefined).then(action);
  record.promptChain = next.catch(() => undefined);
  return next;
}

function terminalSubagent(task: SubagentTask) {
  return task.status === "completed" || task.status === "empty" || task.status === "failed" || task.status === "cancelled" || task.status === "interrupted";
}

export function formatSubagentTerminalMessage(task: SubagentTask, summary?: string) {
  const cleanSummary = summary?.trim();
  if (task.status === "completed" && cleanSummary) {
    return `[RiftX subagent result]\nSubagent: ${task.name}\nStatus: completed\nSummary:\n${cleanSummary}\n\nUse this result in the current assessment. Do not repeat the same delegated task.`;
  }
  const detail = task.status === "empty"
    ? "The child Agent completed without a final text response. Do not treat this task as evidence."
    : task.error?.trim() || `The child Agent ended with status: ${task.status}. Do not treat this task as evidence.`;
  return `[RiftX subagent status]\nSubagent: ${task.name}\nStatus: ${task.status}\nDetails:\n${detail}\n\nDo not treat this task as evidence or repeat the same delegated task unless the parent explicitly requests a retry.`;
}

export function shouldDeliverSubagentCompletion(record: Pick<SubagentJoinRecord, "waitingForSubagents" | "abortPromise" | "aborting">) {
  return !record.waitingForSubagents && !record.abortPromise && !record.aborting;
}

export function claimSubagentResult(record: Pick<SubagentJoinRecord, "deliveredSubagentResults" | "deliveringSubagentResults">, taskId: string) {
  if (record.deliveredSubagentResults.has(taskId)) return false;
  const delivering = record.deliveringSubagentResults ?? (record.deliveringSubagentResults = new Set());
  if (delivering.has(taskId)) return false;
  delivering.add(taskId);
  return true;
}

export function finishSubagentResult(record: Pick<SubagentJoinRecord, "deliveredSubagentResults" | "deliveringSubagentResults">, taskId: string, delivered: boolean) {
  record.deliveringSubagentResults?.delete(taskId);
  if (delivered) record.deliveredSubagentResults.add(taskId);
}

export async function waitForSubagentsBeforeConclusion(record: SubagentJoinRecord, knownTaskIds: Set<string>, requiredTaskIds: Set<string>, abortEpoch: number) {
  const manager = record.subagents;
  if (!manager) return;
  if ((record.abortEpoch ?? 0) !== abortEpoch) return;
  for (const task of manager.list()) {
    // Only wait for tasks that were already active before this turn or were
    // created during it. Historical terminal tasks are already represented in
    // the transcript and must not be re-injected after a restart.
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
    const results = tasks.filter((task) => requiredTaskIds.has(task.id)
      && !record.deliveredSubagentResults.has(task.id)
      && !record.deliveringSubagentResults?.has(task.id)
      && terminalSubagent(task));
    if (results.length) {
      const message = results.map((task) => formatSubagentTerminalMessage(task, task.summary)).join("\n\n");
      for (const task of results) claimSubagentResult(record, task.id);
      record.waitingForSubagents = false;
      try {
        record.subagentDeliveryInProgress = true;
        await enqueueSessionAction(record, async () => {
          record.gate.beginTask();
          await record.session.prompt(`${message}\n\nAll delegated child tasks required for this assessment have now reached a terminal state. Synthesize the final conclusion using these results. Do not start more child tasks or poll task files.`);
        });
        for (const task of results) finishSubagentResult(record, task.id, true);
      } catch (error) {
        for (const task of results) finishSubagentResult(record, task.id, false);
        throw error;
      } finally {
        record.subagentDeliveryInProgress = false;
      }
    }
    // If no task is active and no recognized terminal result was produced,
    // stop rather than spinning forever on an unknown persisted status.
    if (!manager.hasActiveTasks()) return;
  }
}
