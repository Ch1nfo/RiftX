import { mkdir, readFile, writeFile } from "node:fs/promises";
import { join } from "node:path";
import { randomUUID } from "node:crypto";
import { SUBAGENT_LOG_LIMITS, type ApprovalMode, type ApprovalRequest, type RiftxEvent, type SubagentLogEntry, type SubagentTask, type SubagentTaskPatch } from "@/lib/types";
import { ApprovalGate } from "./approval-gate";
import { summarizeToolResult } from "@/lib/tool-result";

export type SubagentResult = { summary: string };
type TaskMetaUpdate = { threadId?: string; model?: string };

export type SubagentRunnerContext = {
  task: SubagentTask;
  gate: ApprovalGate;
  signal: AbortSignal;
  emit: (event: RiftxEvent) => void;
  updateTaskMeta: (update: TaskMetaUpdate) => void;
};

export type SubagentRunner = (context: SubagentRunnerContext) => Promise<SubagentResult>;
export type SubagentCompletionHandler = (task: SubagentTask, result: SubagentResult) => void;

type QueueItem = {
  task: SubagentTask;
  runner: SubagentRunner;
  resolve?: (result: SubagentResult) => void;
  reject?: (error: unknown) => void;
};

type Runtime = {
  controller: AbortController;
  gate: ApprovalGate;
};

function taskName(task: string) {
  const firstLine = task.trim().split(/\r?\n/).map((line) => line.trim()).find(Boolean) ?? "Subagent task";
  return Array.from(firstLine).slice(0, 48).join("");
}

function now() {
  return new Date().toISOString();
}

function normalizeTask(task: string) {
  return task.trim().replace(/\s+/g, " ").toLocaleLowerCase();
}

function trimLogContent(content: string) {
  if (content.length <= SUBAGENT_LOG_LIMITS.content) return content;
  return content.slice(-SUBAGENT_LOG_LIMITS.content);
}

function cloneTask(task: SubagentTask): SubagentTask {
  return {
    ...task,
    logs: task.logs.map((log) => ({ ...log }))
  };
}

export class SubagentManager {
  private readonly tasks = new Map<string, SubagentTask>();
  private readonly runtimes = new Map<string, Runtime>();
  private readonly queue: QueueItem[] = [];
  private readonly taskPromises = new Map<string, Promise<SubagentResult>>();
  private readonly awaitedTasks = new Set<string>();
  private active = 0;
  private maxConcurrent: number;
  private approvalMode: ApprovalMode = "request";
  private runner?: SubagentRunner;
  private completionHandler?: SubagentCompletionHandler;
  private initialized = false;
  private persistChain = Promise.resolve();
  private persistTimer: ReturnType<typeof setTimeout> | undefined;

  constructor(
    private readonly parentSessionId: string,
    private readonly storageRoot: string,
    private readonly emitParent: (event: RiftxEvent) => void,
    maxConcurrent: number,
    approvalMode: ApprovalMode
  ) {
    this.maxConcurrent = Math.min(8, Math.max(1, Math.round(maxConcurrent)));
    this.approvalMode = approvalMode;
  }

  private get storagePath() {
    return join(this.storageRoot, this.parentSessionId, "tasks.json");
  }

  async initialize(runner: SubagentRunner) {
    if (this.initialized) return;
    this.runner = runner;
    await mkdir(join(this.storageRoot, this.parentSessionId), { recursive: true, mode: 0o700 });
    try {
      const parsed = JSON.parse(await readFile(this.storagePath, "utf8")) as { tasks?: SubagentTask[] };
      for (const task of Array.isArray(parsed.tasks) ? parsed.tasks : []) {
        if (!task?.id || task.parentSessionId !== this.parentSessionId) continue;
        delete (task as SubagentTask & { usage?: unknown }).usage;
        if (task.status === "running") {
          task.status = "interrupted";
          task.error = "RiftX was restarted while this task was running.";
          task.finishedAt = now();
        }
        task.logs = Array.isArray(task.logs) ? task.logs : [];
        task.pendingApprovalCount = 0;
        this.tasks.set(task.id, task);
      }
    } catch {
      // A missing or malformed task file should not prevent the parent session from opening.
    }
    this.initialized = true;
    for (const task of this.tasks.values()) {
      if (task.status === "queued") {
        const promise = new Promise<SubagentResult>((resolve, reject) => {
          this.queue.push({ task, runner, resolve, reject });
        });
        void promise.catch(() => undefined);
        this.taskPromises.set(task.id, promise);
      }
    }
    await this.persist();
    this.pump();
  }

  setMaxConcurrent(value: number) {
    this.maxConcurrent = Math.min(8, Math.max(1, Math.round(value)));
    this.pump();
  }

  setApprovalMode(mode: ApprovalMode) {
    this.approvalMode = mode;
    for (const runtime of this.runtimes.values()) runtime.gate.setMode(mode);
  }

  get maxConcurrentSubagents() {
    return this.maxConcurrent;
  }

  get runningCount() {
    return this.active;
  }

  setCompletionHandler(handler: SubagentCompletionHandler | undefined) {
    this.completionHandler = handler;
  }

  list() {
    return [...this.tasks.values()].sort((left, right) => left.createdAt.localeCompare(right.createdAt));
  }

  pendingApprovals() {
    return [...this.runtimes.entries()].flatMap(([taskId, runtime]) => {
      const task = this.tasks.get(taskId);
      return runtime.gate.pendingRequests().map((request) => task ? {
        ...request,
        subagentId: task.id,
        threadId: task.threadId,
        agentName: task.name,
        taskSummary: task.task
      } : request);
    });
  }

  private schedulePersist(delayMs = 150) {
    if (this.persistTimer) return;
    this.persistTimer = setTimeout(() => {
      this.persistTimer = undefined;
      void this.persist();
    }, delayMs);
  }

  private enqueue(taskText: string, runner = this.runner, waitForResult = false) {
    if (!runner) throw new Error("Subagent manager is not initialized");
    const task: SubagentTask = {
      id: randomUUID(),
      parentSessionId: this.parentSessionId,
      threadId: "",
      name: taskName(taskText),
      task: taskText,
      status: "queued",
      model: "",
      createdAt: now(),
      pendingApprovalCount: 0,
      logs: []
    };
    this.tasks.set(task.id, task);
    if (waitForResult) this.awaitedTasks.add(task.id);
    this.emitTask("subagent_queued", task);
    this.schedulePersist();
    const promise = new Promise<SubagentResult>((resolve, reject) => {
      this.queue.push({ task, runner, resolve, reject });
      this.pump();
    });
    this.taskPromises.set(task.id, promise);
    return { task, promise };
  }

  submitTask(taskText: string, runner = this.runner, waitForResult = false) {
    const key = normalizeTask(taskText);
    const duplicate = [...this.tasks.values()].find((task) => (task.status === "queued" || task.status === "running") && normalizeTask(task.task) === key);
    if (duplicate) {
      const promise = this.taskPromises.get(duplicate.id);
      if (waitForResult && promise) this.awaitedTasks.add(duplicate.id);
      return {
        task: duplicate,
        promise: promise ?? Promise.reject(new Error("The matching subagent task has no result promise.")),
        duplicate: true
      };
    }
    try {
      const queued = this.enqueue(taskText, runner, waitForResult);
      return { task: queued.task, promise: queued.promise, duplicate: false };
    } catch (error) {
      return { task: undefined, promise: Promise.reject(error), duplicate: false };
    }
  }

  submit(taskText: string, runner = this.runner): Promise<SubagentResult> {
    return this.submitTask(taskText, runner).promise;
  }

  async retry(taskId: string) {
    const previous = this.tasks.get(taskId);
    if (!previous) throw new Error("Subagent task not found");
    if (previous.status === "queued" || previous.status === "running") throw new Error("Subagent task is still active");
    return this.enqueue(previous.task).task;
  }

  cancel(taskId: string) {
    const task = this.tasks.get(taskId);
    if (!task) return false;
    if (task.status === "queued") {
      const queueIndex = this.queue.findIndex((item) => item.task.id === taskId);
      const queueItem = queueIndex >= 0 ? this.queue.splice(queueIndex, 1)[0] : undefined;
      task.status = "cancelled";
      task.finishedAt = now();
      task.error = "Cancelled before the child Agent started.";
      this.emitTask("subagent_cancelled", task);
      queueItem?.reject?.(new Error(task.error));
      this.taskPromises.delete(task.id);
      this.awaitedTasks.delete(task.id);
      this.schedulePersist();
      return true;
    }
    const runtime = this.runtimes.get(taskId);
    if (!runtime || task.status !== "running") return false;
    task.status = "cancelled";
    task.finishedAt = now();
    task.error = "Cancelled by the user.";
    runtime.gate.rejectAll();
    runtime.controller.abort();
    this.emitTask("subagent_cancelled", task);
    this.schedulePersist();
    return true;
  }

  async abortAll() {
    const pending = [...this.taskPromises.values()];
    for (const task of this.tasks.values()) {
      if (task.status === "queued" || task.status === "running") this.cancel(task.id);
    }
    await Promise.allSettled(pending);
  }

  rejectAllApprovals() {
    for (const runtime of this.runtimes.values()) runtime.gate.rejectAll();
  }

  decideApproval(approvalId: string, approved: boolean, scope: "once" | "task" = "once") {
    for (const runtime of this.runtimes.values()) {
      const request = runtime.gate.pendingRequests().find((item) => item.id === approvalId);
      if (!request) continue;
      if (approved && scope === "task") runtime.gate.allowForTask(request);
      return runtime.gate.decide(approvalId, approved);
    }
    return false;
  }

  private pump() {
    while (this.active < this.maxConcurrent && this.queue.length) {
      const item = this.queue.shift()!;
      if (item.task.status !== "queued") continue;
      this.active += 1;
      void this.run(item).finally(() => {
        this.active -= 1;
        this.pump();
      });
    }
  }

  private async run(item: QueueItem) {
    const { task, runner } = item;
    const controller = new AbortController();
    const gate = new ApprovalGate(this.approvalMode);
    gate.onDecision((request, approved) => {
      task.pendingApprovalCount = Math.max(0, task.pendingApprovalCount - 1);
      this.emitTask("approval_decided", task, { type: "approval_decided", approvalId: request.id, approved });
    });
    this.runtimes.set(task.id, { controller, gate });
    task.status = "running";
    task.startedAt = now();
    task.pendingApprovalCount = 0;
    this.emitTask("subagent_start", task);
    await this.persist();
      const context: SubagentRunnerContext = {
        task,
        gate,
        signal: controller.signal,
        emit: (event) => this.emitTask(event.type, task, event),
        updateTaskMeta: (update) => {
          if (update.threadId !== undefined) task.threadId = update.threadId;
          if (update.model !== undefined) task.model = update.model;
          this.emitTask("subagent_update", task);
          this.schedulePersist();
        }
      };
    try {
      if (controller.signal.aborted) throw new Error("Subagent task was cancelled before it started.");
      const result = await runner(context);
      if ((task.status as string) === "cancelled") {
        item.reject?.(new Error(task.error ?? "Subagent task was cancelled."));
      } else {
        task.status = "completed";
        task.finishedAt = now();
        task.summary = result.summary;
        this.emitTask("subagent_done", task);
        if (!this.awaitedTasks.has(task.id)) this.completionHandler?.(task, result);
        item.resolve?.(result);
      }
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      if ((task.status as string) !== "cancelled") {
        task.status = controller.signal.aborted ? "cancelled" : "failed";
        task.finishedAt = now();
        task.error = message;
        task.logs.push({ id: randomUUID(), type: "error", content: message, status: "error", createdAt: now() });
        this.emitTask(task.status === "cancelled" ? "subagent_cancelled" : "subagent_failed", task);
      }
      item.reject?.(error);
    } finally {
      gate.rejectAll();
      this.runtimes.delete(task.id);
      this.taskPromises.delete(task.id);
      this.awaitedTasks.delete(task.id);
      task.pendingApprovalCount = 0;
      await this.persist();
    }
  }

  private emitTask(type: string, task: SubagentTask, event?: RiftxEvent) {
    let taskPatch: SubagentTaskPatch | undefined;
    if (event?.type === "approval_required") {
      const approval = event.approval as ApprovalRequest;
      const enriched: ApprovalRequest = { ...approval, subagentId: task.id, threadId: task.threadId, agentName: task.name, taskSummary: task.task };
      task.pendingApprovalCount += 1;
      taskPatch = { id: task.id, pendingApprovalCount: task.pendingApprovalCount };
      this.emitParent({ ...event, type, approval: enriched, subagentId: task.id, taskPatch });
      this.schedulePersist();
      return;
    }
    if (event?.type === "approval_evaluated" || event?.type === "approval_evaluation_error") {
      this.emitParent({ ...event, subagentId: task.id });
      return;
    }
    if (event?.type === "approval_decided") {
      taskPatch = { id: task.id, pendingApprovalCount: task.pendingApprovalCount };
    }
    if (event?.type === "tool_start") {
      const log: SubagentLogEntry = { id: String(event.toolCallId ?? randomUUID()), type: "tool", toolName: String(event.toolName ?? "tool"), content: JSON.stringify(event.args ?? {}, null, 2), status: "running", createdAt: now() };
      task.logs.push(log);
      taskPatch = { id: task.id, appendLog: { ...log } };
    } else if (event?.type === "tool_update") {
      const log = task.logs.find((entry) => entry.id === String(event.toolCallId));
      if (log) {
        const update = event.update;
        const nextContent = typeof update === "string"
          ? update
          : update && typeof update === "object" && "content" in update && typeof (update as { content?: unknown }).content === "string"
            ? (update as { content: string }).content
            : JSON.stringify(update ?? "", null, 2);
        log.content = trimLogContent(nextContent);
        taskPatch = { id: task.id, patchLog: { id: log.id, content: log.content } };
      }
    } else if (event?.type === "tool_end") {
      const log = task.logs.find((entry) => entry.id === String(event.toolCallId));
      if (log) {
        log.content = trimLogContent(summarizeToolResult(event.result));
        log.status = event.isError ? "error" : "done";
        taskPatch = { id: task.id, patchLog: { id: log.id, content: log.content, status: log.status } };
      }
    } else if (event?.type === "thinking_delta" || event?.type === "text_delta") {
      const typeName = event.type === "thinking_delta" ? "thinking" : "text";
      const last = task.logs.at(-1);
      const delta = String(event.delta ?? "");
      if (!last || last.type !== typeName) {
        const log = { id: randomUUID(), type: typeName, content: trimLogContent(delta), createdAt: now() } satisfies SubagentLogEntry;
        task.logs.push(log);
        taskPatch = { id: task.id, appendLog: { ...log } };
      } else {
        last.content = trimLogContent(last.content + delta);
        taskPatch = { id: task.id, patchLog: { id: last.id, appendContent: delta } };
      }
    }
    if (!event && type === "subagent_update") {
      taskPatch = {
        id: task.id,
        pendingApprovalCount: task.pendingApprovalCount,
        threadId: task.threadId || undefined,
        model: task.model || undefined
      };
    }
    if (task.logs.length > SUBAGENT_LOG_LIMITS.entries) task.logs = task.logs.slice(-SUBAGENT_LOG_LIMITS.entries);
    const publicType = type === "thinking_delta" || type === "text_delta"
      ? "subagent_update"
      : type.startsWith("subagent_") || type === "approval_required" || type === "approval_evaluated" || type === "approval_evaluation_error" || type === "approval_decided"
        ? type
        : "subagent_update";
    const shouldSendFullTask = publicType === "subagent_queued"
      || publicType === "subagent_start"
      || publicType === "subagent_done"
      || publicType === "subagent_failed"
      || publicType === "subagent_cancelled"
      || publicType === "subagent_interrupted";
    this.emitParent({
      ...event,
      type: publicType,
      subagentId: task.id,
      ...(shouldSendFullTask ? { task: cloneTask(task) } : taskPatch ? { taskPatch } : {})
    });
    if (event?.type === "thinking_delta" || event?.type === "text_delta" || event?.type === "tool_update") this.schedulePersist(350);
    else this.schedulePersist();
  }

  private persist() {
    if (this.persistTimer) {
      clearTimeout(this.persistTimer);
      this.persistTimer = undefined;
    }
    this.persistChain = this.persistChain.then(async () => {
      await mkdir(join(this.storageRoot, this.parentSessionId), { recursive: true, mode: 0o700 });
      await writeFile(this.storagePath, `${JSON.stringify({ tasks: this.list() }, null, 2)}\n`, { mode: 0o600 });
    }).catch(() => undefined);
    return this.persistChain;
  }
}
