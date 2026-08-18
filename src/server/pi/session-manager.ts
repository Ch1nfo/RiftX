import { EventEmitter } from "node:events";
import { mkdir, unlink } from "node:fs/promises";
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
  type ExtensionContext,
  type ToolDefinition
} from "@mariozechner/pi-coding-agent";
import { completeSimple, type Model } from "@mariozechner/pi-ai";
import { readConfig, getAppPaths, updateConfig } from "@/server/config-store";
import type { ApprovalMode, ArchivedSession, ModelProfile, RiftxEvent, SessionSummary } from "@/lib/types";
import { ApprovalGate } from "./approval-gate";
import { createPermissionExtension } from "./permission-extension";
import { normalizeContextUsage } from "./usage";
import { PENTEST_SYSTEM_PROMPT } from "./system-prompt";
import { evaluateApproval } from "./approval-evaluator";

type SessionRecord = {
  id: string;
  cwd: string;
  profile: ModelProfile;
  model: Model<any>;
  modelRegistry: ModelRegistry;
  session: AgentSession;
  gate: ApprovalGate;
  emitter: EventEmitter;
  unsubscribe: () => void;
};

declare global {
  // eslint-disable-next-line no-var
  var __riftxSessions: Map<string, SessionRecord> | undefined;
}

const sessions = globalThis.__riftxSessions ?? (globalThis.__riftxSessions = new Map<string, SessionRecord>());

function eventPayload(event: AgentSessionEvent): RiftxEvent {
  const base = event as unknown as Record<string, unknown>;
  if (event.type === "message_update") {
    const assistant = base.assistantMessageEvent as Record<string, unknown> | undefined;
    return { type: assistant?.type === "text_delta" ? "text_delta" : assistant?.type === "thinking_delta" ? "thinking_delta" : "message", delta: assistant?.delta ?? "" };
  }
  if (event.type === "tool_execution_start") return { type: "tool_start", toolName: base.toolName, toolCallId: base.toolCallId, args: base.args };
  if (event.type === "tool_execution_update") return { type: "tool_update", toolName: base.toolName, toolCallId: base.toolCallId, update: base.update };
  if (event.type === "tool_execution_end") return { type: "tool_end", toolName: base.toolName, toolCallId: base.toolCallId, result: base.result, isError: base.isError };
  if (event.type === "agent_start") return { type: "session_state", state: "running" };
  if (event.type === "agent_end") return { type: "done" };
  if (event.type === "turn_end") return { type: "message", message: base.message, toolResults: base.toolResults };
  if (event.type === "auto_retry_start") return { type: "session_state", state: "retrying", attempt: base.attempt, error: base.errorMessage };
  if (event.type === "compaction_start") return { type: "session_state", state: "compacting", reason: base.reason };
  if (event.type === "compaction_end") return { type: "session_state", state: "idle", reason: base.reason };
  return { type: event.type, ...base };
}

function createSubagentTool(parent: SessionRecord, childProfile: ModelProfile): ToolDefinition {
  return defineTool({
    name: "spawn_subagent",
    label: "Spawn subagent",
    description: "Delegate one focused read-only task to a child coding agent and return its findings.",
    promptSnippet: "spawn_subagent(task)",
    parameters: Type.Object({ task: Type.String({ description: "The focused task for the child agent." }) }),
    async execute(_toolCallId, params, signal, onUpdate) {
      const child = await createPiSession(childProfile, parent.cwd, new ApprovalGate(), undefined, true);
      try {
        child.session.subscribe((event) => {
          if (event.type === "message_update") {
            const message = event.assistantMessageEvent as { type?: string; delta?: string };
            if (message.type === "text_delta" && message.delta) onUpdate?.({ content: [{ type: "text", text: message.delta }], details: {} });
          }
        });
        await child.session.prompt(params.task);
        const messages = child.session.messages;
        const last = [...messages].reverse().find((message) => message.role === "assistant") as { content?: unknown } | undefined;
        return { content: [{ type: "text", text: typeof last?.content === "string" ? last.content : JSON.stringify(last?.content ?? "No result") }], details: { model: `${childProfile.provider}/${childProfile.model}` } };
      } finally {
        if (signal?.aborted) await child.session.abort();
        child.dispose();
      }
    }
  });
}

async function createPiSession(profile: ModelProfile, cwd: string, gate: ApprovalGate, parent?: SessionRecord, child = false, sessionManagerOverride?: PiSessionManager) {
  const paths = getAppPaths();
  await mkdir(paths.piAgent, { recursive: true, mode: 0o700 });
  const authStorage = AuthStorage.create(`${paths.piAgent}/auth.json`);
  if (profile.apiKey) authStorage.setRuntimeApiKey(profile.provider, profile.apiKey);
  const modelRegistry = ModelRegistry.create(authStorage, `${paths.piAgent}/models.json`);
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
  const model = modelRegistry.find(profile.provider, profile.model) as Model<any> | undefined;
  if (!model) throw new Error(`Model ${profile.provider}/${profile.model} could not be loaded`);

  const config = await readConfig();
  gate.setMode(config.approvalMode);
  const emitter = new EventEmitter();
  const permission = createPermissionExtension(
    gate,
    (event) => emitter.emit("event", event),
    (request) => evaluateApproval(model, modelRegistry, request)
  );
  const childProfile = config.childInherit ? profile : config.profiles.find((item) => item.id === config.childProfileId) ?? profile;
  const customTools = parent && !child ? [createSubagentTool(parent, childProfile)] : [];
  const resourceLoader = new DefaultResourceLoader({
    cwd,
    agentDir: paths.piAgent,
    extensionFactories: [permission],
    noExtensions: true,
    systemPrompt: PENTEST_SYSTEM_PROMPT
  });
  // The SDK only reloads a resource loader it creates internally. RiftX supplies
  // its own loader, so load the custom system prompt and inline extensions before
  // createAgentSession builds the runtime.
  await resourceLoader.reload();
  const settingsManager = SettingsManager.create(cwd, paths.piAgent);
  const sessionManager = sessionManagerOverride ?? PiSessionManager.create(cwd, paths.sessions);
  const result = await createAgentSession({
    cwd,
    agentDir: paths.piAgent,
    authStorage,
    modelRegistry,
    model,
    thinkingLevel: profile.thinkingLevel,
    tools: child ? ["read", "grep", "find", "ls"] : ["read", "grep", "find", "ls", "bash", "write", "edit"],
    customTools,
    resourceLoader,
    sessionManager,
    settingsManager
  });
  const record: SessionRecord = {
    id: result.session.sessionId,
    cwd,
    profile,
    model,
    modelRegistry,
    session: result.session,
    gate,
    emitter,
    unsubscribe: () => undefined
  };
  const unsubscribe = result.session.subscribe((event) => {
    emitter.emit("event", eventPayload(event));
    const usage = result.session.getContextUsage();
    if (usage) emitter.emit("event", { type: "usage", usage: normalizeContextUsage(usage, profile.contextWindow) });
  });
  record.unsubscribe = unsubscribe;
  return {
    ...record,
    dispose() {
      gate.rejectAll();
      unsubscribe();
      result.session.dispose();
    }
  };
}

async function profileFor(id?: string) {
  const config = await readConfig();
  return config.profiles.find((profile) => profile.id === (id ?? config.activeProfileId)) ?? config.profiles[0];
}

export async function getOrCreateSession(id?: string) {
  const config = await readConfig();
  if (id && sessions.has(id)) {
    const existing = sessions.get(id)!;
    // A dev-server reload can leave a session object created with the old
    // coding-agent prompt in the process-global cache. Rebuild that runtime
    // on first access so an existing session immediately uses RiftX's
    // penetration-testing identity without losing its persisted history.
    if (
      existing.session.systemPrompt.includes("advanced Web penetration testing") &&
      !existing.session.systemPrompt.includes("general coding assistant")
    ) return existing;
    existing.gate.rejectAll();
    existing.unsubscribe();
    existing.session.dispose();
    sessions.delete(id);
  }
  const profile = await profileFor();
  let sessionManager: PiSessionManager | undefined;
  if (id) {
    const info = (await PiSessionManager.list(config.cwd, getAppPaths().sessions)).find((item) => item.id === id);
    if (info) sessionManager = PiSessionManager.open(info.path, getAppPaths().sessions, config.cwd);
  }
  const created = await createPiSession(profile, config.cwd, new ApprovalGate(), undefined, false, sessionManager);
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

export async function createSessionForProfile(profileId: string) {
  const config = await readConfig();
  const profile = config.profiles.find((item) => item.id === profileId);
  if (!profile) throw new Error("profile not found");
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

function textFromTitleResponse(content: unknown) {
  if (!Array.isArray(content)) return "";
  return content
    .filter((part): part is { type?: string; text?: string } => typeof part === "object" && part !== null)
    .filter((part) => part.type === "text")
    .map((part) => part.text ?? "")
    .join("\n");
}

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
  const title = normalizeSessionTitle(textFromTitleResponse(response.content));
  const config = await readConfig();
  const latestTitle = config.sessionTitles[id]?.trim();
  if (latestTitle) return { title: latestTitle, sessions: (await listSessions()).filter((session) => !session.archived) };
  await updateConfig({ sessionTitles: { ...config.sessionTitles, [id]: title } });
  return { title, sessions: (await listSessions()).filter((session) => !session.archived) };
}

export async function startPromptSession(id: string, text: string, mode: "prompt" | "steer" | "followUp" = "prompt") {
  const record = await getOrCreateSession(id);
  void promptSession(id, text, mode).catch((error) => {
    record.emitter.emit("event", { type: "error", error: error instanceof Error ? error.message : "Agent request failed" });
  });
  return record;
}

export async function abortSession(id: string) {
  const record = await getOrCreateSession(id);
  await record.session.abort();
}

export async function decideApproval(id: string, approvalId: string, approved: boolean, scope: "once" | "task" = "once") {
  const record = await getOrCreateSession(id);
  const request = record.gate.pendingRequests().find((item) => item.id === approvalId);
  if (approved && scope === "task" && request) record.gate.allowForTask(request);
  return record.gate.decide(approvalId, approved);
}

export async function setApprovalMode(mode: ApprovalMode) {
  const config = await updateConfig({ approvalMode: mode });
  for (const session of sessions.values()) session.gate.setMode(mode);
  return config;
}

export async function subscribeSession(id: string, listener: (event: RiftxEvent) => void) {
  const record = await getOrCreateSession(id);
  const onEvent = (event: RiftxEvent) => listener({ ...event, sessionId: record.id });
  record.emitter.on("event", onEvent);
  // Replay state that may have happened before an SSE reconnect, especially an
  // approval request that is still holding the agent at a guarded tool call.
  if (record.session.isStreaming) onEvent({ type: "session_state", state: "running" });
  for (const request of record.gate.pendingRequests()) onEvent({ type: "approval_required", approval: request });
  return () => {
    record.emitter.off("event", onEvent);
    // A disconnected client cannot approve a high-risk operation. Reject any
    // outstanding approval so the agent exits instead of waiting indefinitely.
    record.gate.rejectAll();
  };
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
        messages.push({ id, role: "tool", toolCallId, toolName: String(item.name ?? "tool"), content: JSON.stringify(item.arguments ?? {}, null, 2), status: "done" });
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
    name: config.sessionTitles[info.id] ?? (info.firstMessage ? "未命名任务" : "New session"),
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
    .map((session) => ({ ...session, name: config.sessionTitles[session.id] ?? (session.firstMessage ? "未命名任务" : "New session"), archived: true }));
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
    record.gate.rejectAll();
    record.unsubscribe();
    record.session.dispose();
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
    record.gate.rejectAll();
    record.unsubscribe();
    record.session.dispose();
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
    session.gate.rejectAll();
    session.unsubscribe();
    session.session.dispose();
    sessions.delete(id);
  }
}
