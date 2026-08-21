import { SUBAGENT_LOG_LIMITS, type SubagentTask, type SubagentTaskPatch } from "./types";

function trimLogContent(content: string) {
  return content.length <= SUBAGENT_LOG_LIMITS.content ? content : content.slice(-SUBAGENT_LOG_LIMITS.content);
}

export function sameSubagentTask(left: SubagentTask, right: SubagentTask) {
  const leftLog = left.logs.at(-1);
  const rightLog = right.logs.at(-1);
  return left.name === right.name && left.status === right.status && left.threadId === right.threadId
    && left.model === right.model && left.summary === right.summary && left.error === right.error
    && left.pendingApprovalCount === right.pendingApprovalCount && left.logs.length === right.logs.length
    && leftLog?.content === rightLog?.content && leftLog?.status === rightLog?.status;
}

export function cloneSubagentTask(task: SubagentTask): SubagentTask {
  return { ...task, logs: task.logs.map((log) => ({ ...log })) };
}

export function mergeSubagentTasks(current: SubagentTask[], incoming: SubagentTask[]) {
  const next = current.slice();
  let changed = false;
  for (const task of incoming) {
    const index = next.findIndex((item) => item.id === task.id);
    if (index < 0) { next.push(task); changed = true; continue; }
    const previous = next[index];
    const stale = previous.logs.length > task.logs.length || (previous.status === "running" && task.status === "queued") || (previous.finishedAt && !task.finishedAt);
    if (!stale && !sameSubagentTask(previous, task)) { next[index] = task; changed = true; }
  }
  return changed ? next.sort((left, right) => left.createdAt.localeCompare(right.createdAt)) : current;
}

export function applySubagentTaskPatch(task: SubagentTask, patch: SubagentTaskPatch) {
  const next = cloneSubagentTask(task);
  if (patch.name !== undefined) next.name = patch.name;
  if (patch.threadId !== undefined) next.threadId = patch.threadId;
  if (patch.model !== undefined) next.model = patch.model;
  if (patch.pendingApprovalCount !== undefined) next.pendingApprovalCount = patch.pendingApprovalCount;
  if (patch.appendLog) {
    const index = next.logs.findIndex((log) => log.id === patch.appendLog?.id);
    const log = { ...patch.appendLog, content: trimLogContent(patch.appendLog.content) };
    if (index >= 0) next.logs[index] = { ...next.logs[index], ...log }; else next.logs.push(log);
  }
  if (patch.patchLog) {
    const index = next.logs.findIndex((log) => log.id === patch.patchLog?.id);
    if (index >= 0) {
      const log = { ...next.logs[index] };
      if (patch.patchLog.content !== undefined) log.content = trimLogContent(patch.patchLog.content);
      if (patch.patchLog.appendContent) log.content = trimLogContent(log.content + patch.patchLog.appendContent);
      if (patch.patchLog.status !== undefined) log.status = patch.patchLog.status;
      next.logs[index] = log;
    }
  }
  if (next.logs.length > SUBAGENT_LOG_LIMITS.entries) next.logs = next.logs.slice(-SUBAGENT_LOG_LIMITS.entries);
  return next;
}

export function mergeSubagentTaskPatch(current: SubagentTask[], patch: SubagentTaskPatch) {
  const index = current.findIndex((task) => task.id === patch.id);
  if (index < 0) return current;
  const updated = applySubagentTaskPatch(current[index], patch);
  if (sameSubagentTask(current[index], updated)) return current;
  const next = current.slice();
  next[index] = updated;
  return next;
}
