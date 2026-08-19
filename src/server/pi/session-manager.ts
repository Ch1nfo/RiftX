import { EventEmitter } from "node:events";
import { mkdir, unlink } from "node:fs/promises";
import { join } from "node:path";
import { Type } from "@sinclair/typebox";
import {
  AuthStorage,
  DefaultResourceLoader,
  ModelRegistry,
  SessionManager as PiSessionManager,
  SettingsManager,
  createAgentSession,
  defineTool,
  type AgentSession,
  type AgentSessionEvent,
  type ToolDefinition
} from "@mariozechner/pi-coding-agent";
import { completeSimple, type Model } from "@mariozechner/pi-ai";
import { readConfig, getAppPaths, updateConfig } from "@/server/config-store";
import type { ApprovalMode, ArchivedSession, ModelProfile, RiftxEvent, SessionSummary } from "@/lib/types";
import { ApprovalGate } from "./approval-gate";
import { createPermissionExtension } from "./permission-extension";
import { normalizeContextUsage } from "./usage";
import { buildChildPentestSystemPrompt, buildPentestSystemPrompt } from "./system-prompt";
import { evaluateApproval } from "./approval-evaluator";
import { createBrowserExtension, BrowserManager } from "@/browser";
import { randomUUID } from "node:crypto";
import { MutationLock } from "./mutation-lock";
import { SubagentManager, type SubagentRunnerContext } from "./subagent-manager";
import { textFromModelContent } from "./text-content";

type SessionRecord = {
  id: string;
  cwd: string;
  profile: ModelProfile;
  authStorage: AuthStorage;
  model: Model<any>;
  modelRegistry: ModelRegistry;
  session: AgentSession;
  gate: ApprovalGate;
  emitter: EventEmitter;
  unsubscribe: () => void;
  browser?: BrowserManager;
  mutationLock: MutationLock;
  subagents?: SubagentManager;
  dispose?: () => void;
  runtimeVersion?: number;
  abortPromise?: Promise<void>;
};

const RUNTIME_VERSION = 8;

declare global {
  // eslint-disable-next-line no-var
  var __riftxSessions: Map<string, SessionRecord> | undefined;
}

const sessions = globalThis.__riftxSessions ?? (globalThis.__riftxSessions = new Map<string, SessionRecord>());

type RuntimeDeps = {
  authStorage: AuthStorage;
  modelRegistry: ModelRegistry;
};

function eventPayload(event: AgentSessionEvent): RiftxEvent {
  const base = event as unknown as Record<string, unknown>;
  if (event.type === "message_update") {
    const assistant = base.assistantMessageEvent as Record<string, unknown> | undefined;
    return { type: assistant?.type === "text_delta" ? "text_delta" : assistant?.type === "thinking_delta" ? "thinking_delta" : "message", delta: assistant?.delta ?? "" };
  }
  if (event.type === "tool_execution_start") return { type: "tool_start", toolName: base.toolName, toolCallId: base.toolCallId, args: base.args };
  if (event.type === "tool_execution_update") {
    // Pi's AgentToolUpdateCallback payload is exposed as `partialResult`.
    // Reading the old `update` name turns every streamed tool update into
    // undefined, which the UI then renders literally after approval.
    return { type: "tool_update", toolName: base.toolName, toolCallId: base.toolCallId, update: base.partialResult ?? base.update };
  }
  if (event.type === "tool_execution_end") return { type: "tool_end", toolName: base.toolName, toolCallId: base.toolCallId, result: base.result, isError: base.isError };
  if (event.type === "agent_start") return { type: "session_state", state: "running" };
  if (event.type === "agent_end") return { type: "done" };
  if (event.type === "turn_end") return { type: "message", message: base.message, toolResults: base.toolResults };
  if (event.type === "auto_retry_start") return { type: "session_state", state: "retrying", attempt: base.attempt, error: base.errorMessage };
  if (event.type === "compaction_start") return { type: "session_state", state: "compacting", reason: base.reason };
  if (event.type === "compaction_end") return { type: "session_state", state: "running", reason: base.reason };
  return { type: event.type, ...base };
}

function createSubagentTool(manager: SubagentManager, childProfile: ModelProfile, cwd: string, mutationLock: MutationLock, runtimeDeps: RuntimeDeps): ToolDefinition {
  return defineTool({
    name: "spawn_subagent",
    label: "Spawn subagent",
    description: "Start one focused, independent task in a background Web penetration testing child Agent. This tool returns immediately; continue independent main-Agent work while the child runs. The child result is delivered to the parent session when complete. Use only for meaningful independent work, never duplicate or state-dependent work; the scheduler enforces the configured concurrency limit and queues excess tasks. The child cannot create another child.",
    promptSnippet: "spawn_subagent(task)",
    executionMode: "parallel",
    parameters: Type.Object({ task: Type.String({ description: "A unique, self-contained task with a clear target surface, evidence goal, and no dependency on another child task." }) }),
    async execute(_toolCallId, params) {
      const submitted = manager.submitTask(params.task, (context) => runChildSession(childProfile, cwd, mutationLock, context, runtimeDeps));
      // The parent tool returns immediately; consume the background promise so
      // a child failure is represented by subagent_failed without an unhandled
      // rejection in the Node process.
      void submitted.promise.catch(() => undefined);
      const taskLabel = submitted.task?.name || "subagent task";
      const state = submitted.task?.status || "queued";
      const text = submitted.duplicate
        ? `A matching subagent task is already ${state}. Its existing result will be delivered when complete.`
        : `Subagent task accepted in the background (${state}): ${taskLabel}. Continue independent work; RiftX will deliver the child result when it completes.`;
      return { content: [{ type: "text", text }], details: { model: `${childProfile.provider}/${childProfile.model}`, taskId: submitted.task?.id, status: state, background: true } };
    }
  });
}

async function createPiSession(profile: ModelProfile, cwd: string, gate: ApprovalGate, child = false, sessionManagerOverride?: PiSessionManager, mutationLock = new MutationLock(), runtimeDeps?: RuntimeDeps) {
  const paths = getAppPaths();
  await mkdir(paths.piAgent, { recursive: true, mode: 0o700 });
  const authStorage = runtimeDeps?.authStorage ?? AuthStorage.create(`${paths.piAgent}/auth.json`);
  if (profile.apiKey) authStorage.setRuntimeApiKey(profile.provider, profile.apiKey);
  const modelRegistry = runtimeDeps?.modelRegistry ?? ModelRegistry.create(authStorage, `${paths.piAgent}/models.json`);
  if (!runtimeDeps || !modelRegistry.find(profile.provider, profile.model)) {
    modelRegistry.registerProvider(profile.provider, {
      baseUrl: profile.baseUrl,
      apiKey: profile.apiKey || "riftx-configured",
      api: profile.api,
      models: [{
        id: profile.model,
        name: profile.name,
        reasoning: profile.thinkingLevel !== "off",
        input: ["text"],
        cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
        contextWindow: profile.contextWindow,
        maxTokens: profile.maxTokens
      }]
    });
  }
  const model = modelRegistry.find(profile.provider, profile.model) as Model<any> | undefined;
  if (!model) throw new Error(`Model ${profile.provider}/${profile.model} could not be loaded`);

  const config = await readConfig();
  gate.setMode(config.approvalMode);
  const emitter = new EventEmitter();
  const permission = createPermissionExtension(
    gate,
    (event) => emitter.emit("event", event),
    (request) => evaluateApproval(model, modelRegistry, request),
    mutationLock
  );
  const childProfile = config.childInherit ? profile : config.profiles.find((item) => item.id === config.childProfileId) ?? profile;
  const settingsManager = SettingsManager.create(cwd, paths.piAgent);
  const sessionManager = sessionManagerOverride ?? PiSessionManager.create(cwd, child ? join(paths.subagents, "runtime") : paths.sessions);
  const subagents = !child ? new SubagentManager(sessionManager.getSessionId(), paths.subagents, (event) => emitter.emit("event", event), config.maxConcurrentSubagents, config.approvalMode) : undefined;
  const customTools = subagents ? [createSubagentTool(subagents, childProfile, cwd, mutationLock, { authStorage, modelRegistry })] : [];
  const browserSessionId = randomUUID();
  const browser = new BrowserManager({ cwd, sessionId: browserSessionId });
  const browserExtension = createBrowserExtension({ cwd, sessionId: browserSessionId }, browser);
  const resourceLoader = new DefaultResourceLoader({
    cwd,
    agentDir: paths.piAgent,
    extensionFactories: [permission, browserExtension],
    noExtensions: true,
    systemPrompt: child ? buildChildPentestSystemPrompt() : buildPentestSystemPrompt(config.subagentAggressiveness)
  });
  // The SDK only reloads a resource loader it creates internally. RiftX supplies
  // its own loader, so load the custom system prompt and inline extensions before
  // createAgentSession builds the runtime.
  await resourceLoader.reload();
  const result = await createAgentSession({
    cwd,
    agentDir: paths.piAgent,
    authStorage,
    modelRegistry,
    model,
    thinkingLevel: profile.thinkingLevel,
    tools: ["read", "grep", "find", "ls", "bash", "write", "edit", "browser", ...(subagents ? ["spawn_subagent"] : [])],
    customTools,
    resourceLoader,
    sessionManager,
    settingsManager
  });
  // Pi prepares parallel calls before executing them. Keep guarded mutation
  // tools sequential so they cannot deadlock on the shared mutation lock, but
  // allow independent spawn_subagent calls to run concurrently.
  const runtimeAgent = (result.session as unknown as { agent?: { toolExecution?: "parallel" | "sequential"; state?: { tools?: Array<{ name: string; executionMode?: "parallel" | "sequential" }> } } }).agent;
  if (runtimeAgent) {
    runtimeAgent.toolExecution = "parallel";
    for (const tool of runtimeAgent.state?.tools ?? []) {
      if (["bash", "write", "edit", "browser"].includes(tool.name)) tool.executionMode = "sequential";
      if (tool.name === "spawn_subagent") tool.executionMode = "parallel";
    }
  }
  const record: SessionRecord = {
    id: result.session.sessionId,
    cwd,
    profile,
    authStorage,
    model,
    modelRegistry,
    session: result.session,
    gate,
    emitter,
    browser,
    mutationLock,
    subagents,
    runtimeVersion: RUNTIME_VERSION,
    unsubscribe: () => undefined
  };
  const unsubscribe = result.session.subscribe((event) => {
    emitter.emit("event", eventPayload(event));
    const usage = result.session.getContextUsage();
    if (usage) emitter.emit("event", { type: "usage", usage: normalizeContextUsage(usage, profile.contextWindow) });
  });
  record.unsubscribe = unsubscribe;
  if (subagents) {
    subagents.setCompletionHandler((task, childResult) => {
      const summary = childResult.summary?.trim() || "No result";
      const message = `[RiftX subagent result]\nSubagent: ${task.name}\nTask: ${task.task}\nStatus: completed\nSummary:\n${summary}\n\nUse this result if it helps the current assessment. Do not repeat the same delegated task.`;
      const deliver = result.session.isStreaming
        ? result.session.steer(message)
        : result.session.followUp(message);
      void deliver.catch(() => undefined);
    });
    await subagents.initialize((context) => runChildSession(childProfile, cwd, mutationLock, context, { authStorage, modelRegistry }));
  }
  return {
    ...record,
    dispose() {
      gate.rejectAll();
      void subagents?.abortAll();
      unsubscribe();
      void browser.close();
      result.session.dispose();
    }
  };
}

async function runChildSession(profile: ModelProfile, cwd: string, mutationLock: MutationLock, context: SubagentRunnerContext, runtimeDeps: RuntimeDeps) {
  const paths = getAppPaths();
  const threadDir = join(paths.subagents, context.task.parentSessionId, context.task.id);
  await mkdir(threadDir, { recursive: true, mode: 0o700 });
  const childSessionManager = PiSessionManager.create(cwd, threadDir);
  const child = await createPiSession(profile, cwd, context.gate, true, childSessionManager, mutationLock, runtimeDeps);
  context.task.model = `${profile.provider}/${profile.model}`;
  context.updateTaskMeta({ model: context.task.model, threadId: child.id });
  const abortChild = () => {
    child.gate.rejectAll();
    child.session.abortBash();
    void child.browser?.close();
    void child.session.abort().catch(() => undefined);
  };
  if (context.signal.aborted) {
    abortChild();
    throw new Error("Subagent task was cancelled before the child session started.");
  }
  else context.signal.addEventListener("abort", abortChild, { once: true });
  const unsubscribe = (() => {
    const listener = (event: RiftxEvent) => context.emit(event);
    child.emitter.on("event", listener);
    return () => child.emitter.off("event", listener);
  })();
  try {
    await child.session.prompt(context.task.task);
    const last = [...child.session.messages].reverse().find((message) => message.role === "assistant") as { content?: unknown } | undefined;
    const summary = textFromModelContent(last?.content).trim() || "No result";
    const usage = child.session.getContextUsage();
    return { summary, usage: usage ? normalizeContextUsage(usage, profile.contextWindow) : undefined };
  } finally {
    unsubscribe();
    context.signal.removeEventListener("abort", abortChild);
    if (context.signal.aborted) await child.session.abort().catch(() => undefined);
    child.dispose?.();
  }
}

async function profileFor(id?: string) {
  const config = await readConfig();
  return config.profiles.find((profile) => profile.id === (id ?? config.activeProfileId)) ?? config.profiles[0];
}

export async function getOrCreateSession(id?: string) {
  const config = await readConfig();
  if (id && sessions.has(id)) {
    const existing = sessions.get(id)!;
    // Rebuild stale process-global session objects after a dev-server reload
    // or runtime-version bump, while keeping persisted history on disk.
    if (existing.runtimeVersion === RUNTIME_VERSION) return existing;
    existing.gate.rejectAll();
    await existing.subagents?.abortAll();
    existing.unsubscribe();
    await existing.browser?.close();
    existing.session.dispose();
    sessions.delete(id);
  }
  const profile = await profileFor();
  let sessionManager: PiSessionManager | undefined;
  if (id) {
    const info = (await PiSessionManager.list(config.cwd, getAppPaths().sessions)).find((item) => item.id === id);
    if (info) sessionManager = PiSessionManager.open(info.path, getAppPaths().sessions, config.cwd);
  }
  const created = await createPiSession(profile, config.cwd, new ApprovalGate(), false, sessionManager);
  sessions.set(created.id, created);
  return created;
}

export async function createSession() {
  const config = await readConfig();
  const profile = await profileFor();
  const created = await createPiSession(profile, config.cwd, new ApprovalGate());
  sessions.set(created.id, created);
  return created;
}

export async function promptSession(id: string, text: string, mode: "prompt" | "steer" | "followUp" = "prompt") {
  const record = await getOrCreateSession(id);
  if (mode !== "steer") record.gate.beginTask();
  if (mode === "steer") await record.session.steer(text);
  else if (mode === "followUp") await record.session.followUp(text);
  else await record.session.prompt(text);
  return record;
}

const TASK_TITLE_PROMPT = `You create concise session titles for RiftX, an authorized Web security testing assistant.

Given the user's latest task, return exactly one short title in the same language as the task. Describe the main goal, not the full instructions. Keep it between 6 and 32 characters when possible. Do not use Markdown, quotes, prefixes, numbering, or a trailing period. Return title text only.`;

function normalizeSessionTitle(raw: string) {
  const firstLine = raw.trim().split(/\r?\n/).map((line) => line.trim()).find(Boolean) ?? "";
  const title = firstLine.replace(/^[-*#\d.)\s]+/, "").replace(/^([`'\"]+)|([`'\"]+)$/g, "").trim();
  if (!title) throw new Error("模型没有返回有效任务标题");
  return Array.from(title).slice(0, 32).join("");
}

export async function summarizeSessionTitle(id: string, task: string) {
  const existingConfig = await readConfig();
  const existingTitle = existingConfig.sessionTitles[id]?.trim();
  if (existingTitle) return { title: existingTitle, sessions: (await listSessions()).filter((session) => !session.archived) };
  const record = await getOrCreateSession(id);
  const auth = await record.modelRegistry.getApiKeyAndHeaders(record.model);
  if (!auth.ok) throw new Error(auth.error);
  const response = await completeSimple(record.model, {
    systemPrompt: TASK_TITLE_PROMPT,
    messages: [{ role: "user", content: task, timestamp: Date.now() }]
  }, {
    apiKey: auth.apiKey,
    headers: auth.headers,
    maxTokens: 64,
    temperature: 0,
    timeoutMs: 20_000,
    maxRetries: 0
  });
  const title = normalizeSessionTitle(textFromModelContent(response.content));
  const config = await readConfig();
  const latestTitle = config.sessionTitles[id]?.trim();
  if (latestTitle) return { title: latestTitle, sessions: (await listSessions()).filter((session) => !session.archived) };
  await updateConfig({ sessionTitles: { ...config.sessionTitles, [id]: title } });
  return { title, sessions: (await listSessions()).filter((session) => !session.archived) };
}

export async function startPromptSession(id: string, text: string, mode: "prompt" | "steer" | "followUp" = "prompt") {
  const record = await getOrCreateSession(id);
  // Keep Pi single-run while a previous stop is still unwinding a tool.
  if (record.abortPromise) await record.abortPromise;
  void promptSession(id, text, mode).catch((error) => {
    record.emitter.emit("event", { type: "error", error: error instanceof Error ? error.message : "Agent request failed" });
  });
  return record;
}

export async function abortSession(id: string) {
  const record = await getOrCreateSession(id);
  if (record.abortPromise) return record.abortPromise;
  record.abortPromise = (async () => {
    // Release approval/mutation waits first, then explicitly stop Pi's bash
    // controller. AgentSession.abort() only aborts the model loop; bash has a
    // separate controller in the Pi SDK.
    record.gate.rejectAll();
    const subagentAbort = record.subagents?.abortAll();
    record.session.abortBash();
    await Promise.allSettled([
      record.session.abort(),
      subagentAbort ?? Promise.resolve()
    ]);
    record.emitter.emit("event", { type: "session_state", state: "idle" });
    record.emitter.emit("event", { type: "done", aborted: true });
  })();
  try {
    await record.abortPromise;
  } finally {
    record.abortPromise = undefined;
  }
}

export async function decideApproval(id: string, approvalId: string, approved: boolean, scope: "once" | "task" = "once") {
  const record = await getOrCreateSession(id);
  const request = record.gate.pendingRequests().find((item) => item.id === approvalId);
  if (approved && scope === "task" && request) record.gate.allowForTask(request);
  if (request) return record.gate.decide(approvalId, approved);
  return record.subagents?.decideApproval(approvalId, approved, scope) ?? false;
}

export async function setApprovalMode(mode: ApprovalMode) {
  const config = await updateConfig({ approvalMode: mode });
  for (const session of sessions.values()) {
    session.gate.setMode(mode);
    session.subagents?.setApprovalMode(mode);
  }
  return config;
}

export async function setMaxConcurrentSubagents(value: number) {
  const maxConcurrentSubagents = Math.min(8, Math.max(1, Math.round(Number(value) || 3)));
  for (const session of sessions.values()) session.subagents?.setMaxConcurrent(maxConcurrentSubagents);
  return maxConcurrentSubagents;
}

export async function subscribeSession(id: string, listener: (event: RiftxEvent) => void) {
  const record = await getOrCreateSession(id);
  const onEvent = (event: RiftxEvent) => listener({ ...event, sessionId: record.id });
  record.emitter.on("event", onEvent);
  // Replay state that may have happened before an SSE reconnect, especially an
  // approval request that is still holding the agent at a guarded tool call.
  if (record.session.isStreaming) onEvent({ type: "session_state", state: "running" });
  for (const request of record.gate.pendingRequests()) onEvent({ type: "approval_required", approval: request });
  for (const request of record.subagents?.pendingApprovals() ?? []) onEvent({ type: "approval_required", approval: request });
  return () => {
    record.emitter.off("event", onEvent);
  };
}

export async function listSubagents(id: string) {
  const record = await getOrCreateSession(id);
  return { tasks: record.subagents?.list() ?? [], running: record.subagents?.runningCount ?? 0, maxConcurrent: record.subagents?.maxConcurrentSubagents ?? 0 };
}

export async function cancelSubagent(id: string, taskId: string) {
  const record = await getOrCreateSession(id);
  return record.subagents?.cancel(taskId) ?? false;
}

export async function retrySubagent(id: string, taskId: string) {
  const record = await getOrCreateSession(id);
  return await record.subagents?.retry(taskId) ?? null;
}

export async function getSessionMessages(id: string) {
  const record = await getOrCreateSession(id);
  const messages: Array<{ id: string; role: "user" | "assistant" | "thinking" | "tool"; content: string; toolName?: string; toolCallId?: string; status?: "done" | "error"; isError?: boolean }> = [];
  const toolIndexes = new Map<string, number>();
  const textFromContent = (content: unknown) => Array.isArray(content)
    ? content.map((part: unknown) => typeof part === "string" ? part : part && typeof part === "object" && "text" in part ? String((part as { text?: unknown }).text ?? "") : "").join("")
    : String(content ?? "");

  record.session.messages.forEach((message, messageIndex) => {
    const candidate = message as unknown as { role?: string; content?: unknown; toolCallId?: string; toolName?: string; isError?: boolean };
    if (candidate.role === "toolResult") {
      const toolCallId = candidate.toolCallId ?? `${record.id}-${messageIndex}`;
      const result = textFromContent(candidate.content);
      const toolIndex = toolIndexes.get(toolCallId);
      if (toolIndex === undefined) {
        messages.push({ id: `${record.id}-${messageIndex}`, role: "tool", toolCallId, toolName: candidate.toolName ?? "tool", content: result, status: candidate.isError ? "error" : "done", isError: Boolean(candidate.isError) });
      } else {
        messages[toolIndex] = { ...messages[toolIndex], content: result, status: candidate.isError ? "error" : "done", isError: Boolean(candidate.isError) };
      }
      return;
    }

    const parts = Array.isArray(candidate.content) ? candidate.content : [{ type: "text", text: String(candidate.content ?? "") }];
    parts.forEach((part: unknown, partIndex: number) => {
      if (!part || typeof part !== "object") return;
      const item = part as { type?: string; text?: unknown; thinking?: unknown; id?: string; name?: string; arguments?: unknown };
      const id = `${record.id}-${messageIndex}-${partIndex}`;
      if (candidate.role === "user" && item.type === "text") {
        messages.push({ id, role: "user", content: String(item.text ?? "") });
      } else if (candidate.role === "assistant" && item.type === "thinking") {
        messages.push({ id, role: "thinking", content: String(item.thinking ?? ""), status: "done" });
      } else if (candidate.role === "assistant" && item.type === "text") {
        messages.push({ id, role: "assistant", content: String(item.text ?? "") });
      } else if (candidate.role === "assistant" && item.type === "toolCall") {
        const toolCallId = String(item.id ?? id);
        const toolIndex = messages.length;
        toolIndexes.set(toolCallId, toolIndex);
        messages.push({ id: toolCallId, role: "tool", toolCallId, toolName: String(item.name ?? "tool"), content: JSON.stringify(item.arguments ?? {}, null, 2), status: "done" });
      }
    });
  });
  return messages;
}

export async function listSessions(): Promise<SessionSummary[]> {
  const config = await readConfig();
  const archived = new Set(config.archivedSessionIds);
  const infos = await PiSessionManager.list(config.cwd, getAppPaths().sessions);
  const persisted = infos.map((info) => ({
    id: info.id,
    path: info.path,
    name: config.sessionTitles[info.id] ?? (info.firstMessage ? "Untitled task" : "New session"),
    firstMessage: info.firstMessage,
    updatedAt: info.modified.toISOString(),
    archived: archived.has(info.id)
  }));
  const seen = new Set(persisted.map((item) => item.id));
  const live = [...sessions.values()]
    .filter((session) => !seen.has(session.id))
    .map((session) => ({ id: session.id, path: session.session.sessionFile ?? "", name: config.sessionTitles[session.id] ?? "New session", firstMessage: "", updatedAt: new Date().toISOString(), archived: archived.has(session.id) }));
  const archivedMetadata = config.archivedSessions
    .filter((session) => !seen.has(session.id) && !live.some((item) => item.id === session.id))
    .map((session) => ({ ...session, name: config.sessionTitles[session.id] ?? (session.firstMessage ? "Untitled task" : "New session"), archived: true }));
  const archivedFallback = config.archivedSessionIds
    .filter((id) => !seen.has(id) && !archivedMetadata.some((session) => session.id === id))
    .map((id) => ({ id, path: "", name: config.sessionTitles[id] ?? "Archived session", firstMessage: "", updatedAt: new Date().toISOString(), archived: true }));
  return [...live, ...persisted, ...archivedMetadata, ...archivedFallback];
}

export async function archiveSession(id: string) {
  const config = await readConfig();
  const sessionsList = await listSessions();
  const summary = sessionsList.find((session) => session.id === id);
  if (!summary) throw new Error("session not found");
  if (!config.archivedSessionIds.includes(id)) {
    const metadata: ArchivedSession = {
      id: summary.id,
      path: summary.path,
      name: summary.name,
      firstMessage: summary.firstMessage,
      updatedAt: summary.updatedAt
    };
    await updateConfig({ archivedSessionIds: [...config.archivedSessionIds, id], archivedSessions: [...config.archivedSessions.filter((item) => item.id !== id), metadata] });
  }
  const record = sessions.get(id);
  if (record) {
    record.dispose?.();
    sessions.delete(id);
  }
  return listSessions();
}

export async function deleteArchivedSession(id: string) {
  const config = await readConfig();
  if (!config.archivedSessionIds.includes(id)) throw new Error("session is not archived");
  const session = (await listSessions()).find((item) => item.id === id);
  const record = sessions.get(id);
  if (record) {
    record.dispose?.();
    sessions.delete(id);
  }
  if (session?.path) {
    try {
      await unlink(session.path);
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error;
    }
  }
  const { [id]: _removedTitle, ...sessionTitles } = config.sessionTitles;
  await updateConfig({ archivedSessionIds: config.archivedSessionIds.filter((item) => item !== id), archivedSessions: config.archivedSessions.filter((item) => item.id !== id), sessionTitles });
  return listSessions();
}

export async function setActiveProfile(profileId: string) {
  await updateConfig({ activeProfileId: profileId });
  for (const [id, session] of sessions) {
    session.dispose?.();
    sessions.delete(id);
  }
}
