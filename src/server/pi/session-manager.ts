import { EventEmitter } from "node:events";
import { mkdir, readFile, stat, unlink } from "node:fs/promises";
import { join, resolve } from "node:path";
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
import type { ApprovalMode, ArchivedSession, ContextUsage, FindingInput, FindingSource, ModelProfile, RiftxEvent, SessionSummary, SubagentTask } from "@/lib/types";
import { ApprovalGate } from "./approval-gate";
import { createPermissionExtension } from "./permission-extension";
import { resolvePromptMode } from "@/lib/prompt-mode";
import { emptyContextUsage, normalizeContextUsage } from "./usage";
import { buildChildPentestSystemPrompt, buildPentestSystemPrompt } from "./system-prompt";
import { evaluateApproval } from "./approval-evaluator";
import { createBrowserExtension, BrowserManager } from "@/browser";
import { randomUUID } from "node:crypto";
import { MutationLock } from "./mutation-lock";
import { SubagentManager, type SubagentRunnerContext } from "./subagent-manager";
import { textFromModelContent } from "./text-content";
import { EvidenceStore, getEvidenceStore, removeEvidence } from "./evidence-store";
import { estimateCompactedUsage, estimateMessagesContextUsage, installMidTurnCompaction } from "./mid-turn-compaction";

type SessionRecord = {
  id: string;
  cwd: string;
  profile: ModelProfile;
  authStorage: AuthStorage;
  model: Model<any>;
  modelRegistry: ModelRegistry;
  settingsManager: SettingsManager;
  sessionManager: PiSessionManager;
  session: AgentSession;
  gate: ApprovalGate;
  emitter: EventEmitter;
  unsubscribe: () => void;
  browser?: BrowserManager;
  mutationLock: MutationLock;
  subagents?: SubagentManager;
  evidenceStore: EvidenceStore;
  dispose?: () => void;
  runtimeVersion?: number;
  abortPromise?: Promise<void>;
  abortEpoch?: number;
};

const RUNTIME_VERSION = 10;

type SessionSnapshotCacheEntry = {
  modifiedMs: number;
  size: number;
  profileKey: string;
  snapshot: SessionSnapshot | null;
};

const sessionSnapshotCache = new Map<string, SessionSnapshotCacheEntry>();

type SessionSnapshot = {
  id: string;
  provider?: string;
  model?: string;
  profileId?: string;
  contextWindow: number;
  usage: ContextUsage;
};

declare global {
  // eslint-disable-next-line no-var
  var __riftxSessions: Map<string, SessionRecord> | undefined;
}

const sessions = globalThis.__riftxSessions ?? (globalThis.__riftxSessions = new Map<string, SessionRecord>());

type RuntimeDeps = {
  authStorage: AuthStorage;
  modelRegistry: ModelRegistry;
  evidenceStore: EvidenceStore;
  evidenceSessionId: string;
};

type FindingSourceInfo = { source: FindingSource; subagentId?: string };

type ToolEvidenceSnapshot = { toolCallId: string; toolName: string; content: string };

function resolveToolEvidence(session: AgentSession | undefined, requestedId: string, requestedName: string): ToolEvidenceSnapshot | undefined {
  if (!session) return undefined;
  const messages = session.messages as unknown as Array<{ role?: string; content?: unknown; toolCallId?: string }>;
  const textFromContent = (content: unknown) => Array.isArray(content)
    ? content.map((part: unknown) => typeof part === "string" ? part : part && typeof part === "object" && "text" in part ? String((part as { text?: unknown }).text ?? "") : "").join("")
    : String(content ?? "");
  const calls = messages.flatMap((message) => {
    const parts = Array.isArray(message.content) ? message.content : [];
    return parts.filter((part): part is { type?: string; id?: string; name?: string } => Boolean(part && typeof part === "object" && (part as { type?: string }).type === "toolCall"))
      .map((part) => ({ id: String(part.id ?? ""), name: String(part.name ?? "tool") }));
  });
  const selected = calls.find((call) => call.id === requestedId) ?? [...calls].reverse().find((call) => call.name === requestedName);
  if (!selected) return undefined;
  const result = messages.find((message) => message.role === "toolResult" && message.toolCallId === selected.id);
  const content = textFromContent(result?.content);
  return content ? { toolCallId: selected.id, toolName: selected.name, content } : undefined;
}

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

function registerProfileModel(authStorage: AuthStorage, modelRegistry: ModelRegistry, profile: ModelProfile, replace = false) {
  if (profile.apiKey) authStorage.setRuntimeApiKey(profile.provider, profile.apiKey);
  if (replace || !modelRegistry.find(profile.provider, profile.model)) {
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
  return model;
}

function createFindingTool(store: EvidenceStore, source: FindingSourceInfo, browser: BrowserManager, getSession: () => AgentSession | undefined): ToolDefinition {
  return defineTool({
    name: "record_finding",
    label: "Record finding",
    description: "Record one evidence-backed finding in the parent session. Use only when there is a concrete, reviewable conclusion; use likely or suspected when validation is incomplete and never create findings to fill a quota.",
    promptSnippet: "record_finding(title, asset, confidence, impact, reproduction, evidence)",
    parameters: Type.Object({
      title: Type.String({ description: "Short finding title." }),
      asset: Type.String({ description: "Affected URL, host, route, or other asset." }),
      confidence: Type.Union([Type.Literal("confirmed"), Type.Literal("likely"), Type.Literal("suspected"), Type.Literal("not_reproducible")]),
      impact: Type.String({ description: "Short description of the actual or expected impact." }),
      reproduction: Type.String({ description: "Short reproducible validation steps or the reason reproduction is incomplete." }),
      evidence: Type.Array(Type.Union([
        Type.Object({ type: Type.Literal("quote"), quote: Type.String() }),
        Type.Object({ type: Type.Literal("tool"), toolCallId: Type.String(), toolName: Type.String() }),
        Type.Object({ type: Type.Literal("request"), requestRef: Type.String() }),
        Type.Object({ type: Type.Literal("screenshot"), screenshotId: Type.String() })
      ]), { minItems: 1 })
    }),
    async execute(_toolCallId, params) {
      const input = params as FindingInput;
      const evidence = await Promise.all(input.evidence.map(async (item) => {
        if (item.type === "tool") {
          const snapshot = resolveToolEvidence(getSession(), item.toolCallId, item.toolName);
          return snapshot ? { ...item, ...snapshot } : item;
        }
        if (item.type === "request") return browser.requestEvidence(item.requestRef);
        if (item.type === "screenshot") return browser.screenshotEvidence(item.screenshotId);
        return item;
      }));
      const finding = await store.upsert({ ...input, evidence }, source.source, source.subagentId);
      return { content: [{ type: "text", text: `Finding recorded: ${finding.title}` }], details: { findingId: finding.id } };
    }
  });
}

function createSubagentTool(manager: SubagentManager, getChildProfile: () => ModelProfile, cwd: string, mutationLock: MutationLock, runtimeDeps: RuntimeDeps): ToolDefinition {
  return defineTool({
    name: "spawn_subagent",
    label: "Spawn subagent",
    description: "Start one focused, independent Web penetration testing child Agent. The task runs in the background so the main Agent can continue independent work. Every spawned child is mandatory for the final assessment: RiftX waits for all spawned children to finish before requesting the final conclusion. Never poll child logs or task files with bash or sleep. Use only for meaningful independent work, never duplicate or state-dependent child work; the scheduler enforces the configured concurrency limit and queues excess tasks. The child cannot create another child.",
    promptSnippet: "spawn_subagent(task)",
    executionMode: "parallel",
    parameters: Type.Object({
      task: Type.String({ description: "A unique, self-contained task with a clear target surface, evidence goal, and no dependency on another child task." })
    }),
    async execute(_toolCallId, params, signal) {
      const childProfile = getChildProfile();
      const submitted = manager.submitTask(params.task, (context) => runChildSession(childProfile, cwd, mutationLock, context, runtimeDeps));
      void submitted.promise.catch(() => undefined);
      const taskLabel = submitted.task?.name || "subagent task";
      const state = submitted.task?.status || "queued";
      const text = submitted.duplicate
        ? `A matching subagent task is already ${state}. Its existing result will be delivered when complete.`
        : `Subagent task accepted in the background (${state}): ${taskLabel}. Continue independent work. RiftX will deliver all child results before the final conclusion.`;
      if (signal?.aborted && submitted.task?.id) manager.cancel(submitted.task.id);
      return { content: [{ type: "text", text }], details: { model: `${childProfile.provider}/${childProfile.model}`, taskId: submitted.task?.id, status: state, background: true } };
    }
  });
}

async function createPiSession(profile: ModelProfile, cwd: string, gate: ApprovalGate, child = false, sessionManagerOverride?: PiSessionManager, mutationLock = new MutationLock(), runtimeDeps?: RuntimeDeps, findingSource: FindingSourceInfo = { source: "main" }) {
  const paths = getAppPaths();
  await mkdir(paths.piAgent, { recursive: true, mode: 0o700 });
  const authStorage = runtimeDeps?.authStorage ?? AuthStorage.create(`${paths.piAgent}/auth.json`);
  const modelRegistry = runtimeDeps?.modelRegistry ?? ModelRegistry.create(authStorage, `${paths.piAgent}/models.json`);
  const model = registerProfileModel(authStorage, modelRegistry, profile, !runtimeDeps);

  const config = await readConfig();
  gate.setMode(config.approvalMode);
  const emitter = new EventEmitter();
  if (!child) {
    gate.onDecision((request, approved) => emitter.emit("event", { type: "approval_decided", approvalId: request.id, approval: request, approved }));
  }
  let record: SessionRecord | undefined;
  const permission = createPermissionExtension(
    gate,
    (event) => emitter.emit("event", event),
    (request) => evaluateApproval(record?.model ?? model, modelRegistry, request),
    mutationLock
  );
  const childProfile = config.childInherit ? profile : config.profiles.find((item) => item.id === config.childProfileId) ?? profile;
  const settingsManager = SettingsManager.create(cwd, paths.piAgent);
  settingsManager.setTransport(profile.transport);
  const sessionManager = sessionManagerOverride ?? PiSessionManager.create(cwd, child ? join(paths.subagents, "runtime") : paths.sessions);
  const evidenceSessionId = runtimeDeps?.evidenceSessionId ?? sessionManager.getSessionId();
  const evidenceStore = runtimeDeps?.evidenceStore ?? getEvidenceStore(evidenceSessionId, paths.evidence, (event) => emitter.emit("event", event));
  const subagentNameGenerator = !child ? async (task: string) => {
    const auth = await modelRegistry.getApiKeyAndHeaders(model);
    if (!auth.ok) return "";
    const response = await completeSimple(model, {
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
    return normalizeSessionTitle(textFromModelContent(response.content));
  } : undefined;
  const subagents = !child ? new SubagentManager(sessionManager.getSessionId(), paths.subagents, (event) => emitter.emit("event", event), config.maxConcurrentSubagents, config.approvalMode, subagentNameGenerator) : undefined;
  const getChildProfile = () => config.childInherit ? (record?.profile ?? profile) : childProfile;
  const browserSessionId = randomUUID();
  const browser = new BrowserManager({ cwd, sessionId: browserSessionId, evidenceRoot: paths.evidence, evidenceSessionId });
  let evidenceSession: AgentSession | undefined;
  const customTools = [createFindingTool(evidenceStore, findingSource, browser, () => evidenceSession), ...(subagents ? [createSubagentTool(subagents, getChildProfile, cwd, mutationLock, { authStorage, modelRegistry, evidenceStore, evidenceSessionId })] : [])];
  const browserExtension = createBrowserExtension({ cwd, sessionId: browserSessionId }, browser);
  const resourceLoader = new DefaultResourceLoader({
    cwd,
    agentDir: paths.piAgent,
    extensionFactories: [permission, browserExtension],
    noExtensions: true,
    systemPrompt: child ? buildChildPentestSystemPrompt() : buildPentestSystemPrompt(config.subagentAggressiveness, config.systemPromptEnabled ? config.systemPrompt : undefined)
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
    tools: ["read", "grep", "find", "ls", "bash", "write", "edit", "browser", "record_finding", ...(subagents ? ["spawn_subagent"] : [])],
    customTools,
    resourceLoader,
    sessionManager,
    settingsManager
  });
  evidenceSession = result.session;
  installMidTurnCompaction(result.session);
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
  record = {
    id: result.session.sessionId,
    cwd,
    profile,
    authStorage,
    model,
    modelRegistry,
    settingsManager,
    sessionManager,
    session: result.session,
    gate,
    emitter,
    browser,
    mutationLock,
    subagents,
    evidenceStore,
    runtimeVersion: RUNTIME_VERSION,
    abortEpoch: 0,
    unsubscribe: () => undefined
  };
  const unsubscribe = result.session.subscribe((event) => {
    const payload = event.type === "agent_end" && subagents?.hasActiveTasks()
      ? { type: "session_state", state: "waiting_for_subagents" }
      : eventPayload(event);
    emitter.emit("event", payload);
    const usage = event.type === "compaction_end" && event.result
      ? estimateCompactedUsage(result.session, record.profile.contextWindow)
      : usageFromRecord(record);
    if (usage) emitter.emit("event", { type: "usage", usage: normalizeContextUsage(usage, record.profile.contextWindow) });
  });
  record.unsubscribe = unsubscribe;
  if (subagents) {
    await subagents.initialize((context) => runChildSession(getChildProfile(), cwd, mutationLock, context, { authStorage, modelRegistry, evidenceStore, evidenceSessionId }));
  }
  record.dispose = () => {
    gate.rejectAll();
    void subagents?.abortAll();
    unsubscribe();
    void browser.close();
    result.session.dispose();
  };
  return record;
}

async function runChildSession(profile: ModelProfile, cwd: string, mutationLock: MutationLock, context: SubagentRunnerContext, runtimeDeps: RuntimeDeps) {
  const paths = getAppPaths();
  const threadDir = join(paths.subagents, context.task.parentSessionId, context.task.id);
  await mkdir(threadDir, { recursive: true, mode: 0o700 });
  const childSessionManager = PiSessionManager.create(cwd, threadDir);
  const child = await createPiSession(profile, cwd, context.gate, true, childSessionManager, mutationLock, runtimeDeps, { source: "subagent", subagentId: context.task.id });
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
    return { summary };
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

function summaryName(config: Awaited<ReturnType<typeof readConfig>>, id: string, firstMessage: string, archived = false) {
  if (config.sessionTitles[id]) return config.sessionTitles[id];
  if (firstMessage) return "Untitled task";
  return archived ? "Archived session" : "New session";
}

function usageFromRecord(record: SessionRecord): ContextUsage {
  const usage = record.session.getContextUsage();
  if (!usage) return emptyContextUsage(record.profile.contextWindow);
  if (usage.percent === null) return estimateCompactedUsage(record.session, record.profile.contextWindow);
  return normalizeContextUsage(usage, record.profile.contextWindow);
}

async function sessionSnapshotFromFile(path: string, profiles: ModelProfile[]): Promise<SessionSnapshot | null> {
  const profileKey = profiles.map((profile) => `${profile.id}:${profile.provider}:${profile.model}:${profile.contextWindow}`).join("|");
  try {
    const fileInfo = await stat(path);
    const cached = sessionSnapshotCache.get(path);
    if (cached && cached.modifiedMs === fileInfo.mtimeMs && cached.size === fileInfo.size && cached.profileKey === profileKey) {
      return cached.snapshot ? { ...cached.snapshot, usage: { ...cached.snapshot.usage } } : null;
    }
    const text = await readFile(path, "utf8");
    const lines = text.split(/\r?\n/).filter(Boolean);
    let sessionId = "";
    let provider = "";
    let model = "";
    let usage: ContextUsage | undefined;
    let postCompactionMessages: unknown[] | undefined;
    let hasPostCompactionUsage = false;
    for (const line of lines) {
      let entry: Record<string, unknown>;
      try {
        entry = JSON.parse(line) as Record<string, unknown>;
      } catch {
        continue;
      }
      if (!sessionId && entry.type === "session" && typeof entry.id === "string") sessionId = entry.id;
      if (entry.type === "model_change") {
        if (typeof entry.provider === "string") provider = entry.provider;
        if (typeof entry.modelId === "string") model = entry.modelId;
      }
      if (entry.type === "compaction") {
        postCompactionMessages = [{ role: "compactionSummary", summary: String(entry.summary ?? "") }];
        hasPostCompactionUsage = false;
      }
      if (entry.type === "branch_summary" && postCompactionMessages) {
        postCompactionMessages.push({ role: "branchSummary", summary: String(entry.summary ?? "") });
      }
      if (entry.type === "message") {
        const message = entry.message as Record<string, unknown> | undefined;
        if (postCompactionMessages && message) postCompactionMessages.push(message);
        if (message?.usage) {
          if (typeof message.provider === "string") provider = message.provider;
          if (typeof message.model === "string") model = message.model;
          const matchedProfile = profiles.find((profile) => profile.provider === provider && profile.model === model);
          const contextWindow = matchedProfile?.contextWindow ?? 0;
          usage = normalizeContextUsage(message.usage, contextWindow);
          if (postCompactionMessages && message.role === "assistant" && message.stopReason !== "aborted" && message.stopReason !== "error") {
            hasPostCompactionUsage = true;
          }
        }
      }
    }
    const matchedProfile = profiles.find((profile) => profile.provider === provider && profile.model === model);
    const contextWindow = matchedProfile?.contextWindow ?? usage?.contextWindow ?? 0;
    if (postCompactionMessages && !hasPostCompactionUsage) {
      usage = estimateMessagesContextUsage(postCompactionMessages, contextWindow);
    }
    const snapshot = {
      id: sessionId,
      provider: provider || matchedProfile?.provider,
      model: model || matchedProfile?.model,
      profileId: matchedProfile?.id,
      contextWindow,
      usage: usage ? {
        ...usage,
        contextWindow,
        remaining: usage.percent === null ? contextWindow : Math.max(0, contextWindow - usage.tokens),
        percent: usage.percent === null ? null : contextWindow > 0 ? Math.min(100, (usage.tokens / contextWindow) * 100) : null
      } : emptyContextUsage(contextWindow)
    };
    sessionSnapshotCache.set(path, { modifiedMs: fileInfo.mtimeMs, size: fileInfo.size, profileKey, snapshot });
    return { ...snapshot, usage: { ...snapshot.usage } };
  } catch {
    return null;
  }
}

async function buildSessionSnapshot(id: string, path?: string) {
  const live = sessions.get(id);
  if (live) {
    return {
      id,
      provider: live.profile.provider,
      model: live.profile.model,
      profileId: live.profile.id,
      contextWindow: live.profile.contextWindow,
      usage: usageFromRecord(live)
    } satisfies SessionSnapshot;
  }
  const config = await readConfig();
  const fromFile = path ? await sessionSnapshotFromFile(path, config.profiles) : null;
  if (fromFile) return fromFile;
  const fallbackProfile = config.profiles.find((profile) => profile.id === config.activeProfileId) ?? config.profiles[0];
  return {
    id,
    provider: fallbackProfile?.provider,
    model: fallbackProfile?.model,
    profileId: fallbackProfile?.id,
    contextWindow: fallbackProfile?.contextWindow ?? 0,
    usage: emptyContextUsage(fallbackProfile?.contextWindow ?? 0)
  } satisfies SessionSnapshot;
}

async function listWorkspaceSessionInfos(cwd: string) {
  const target = resolve(cwd);
  const infos = await PiSessionManager.list(cwd, getAppPaths().sessions);
  const launchDirectory = resolve(process.cwd());
  return infos.filter((info) => info.cwd ? resolve(info.cwd) === target : target === launchDirectory);
}

async function findSessionPath(id: string, cwd?: string) {
  const config = await readConfig();
  const rootCwd = cwd ?? config.cwd;
  const info = (await listWorkspaceSessionInfos(rootCwd)).find((item) => item.id === id);
  if (info?.path) return info.path;
  const archived = config.archivedSessions.find((item) => item.id === id);
  if (archived?.path) return archived.path;
  return "";
}

async function getOrCreateSession(id?: string) {
  const config = await readConfig();
  if (id && config.archivedSessionIds.includes(id)) throw new Error("Session is archived");
  if (id && sessions.has(id)) {
    const existing = sessions.get(id)!;
    // Rebuild stale process-global session objects after a dev-server reload
    // or runtime-version bump, while keeping persisted history on disk.
    if (existing.runtimeVersion === RUNTIME_VERSION && resolve(existing.cwd) === resolve(config.cwd)) return existing;
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
    const info = (await listWorkspaceSessionInfos(config.cwd)).find((item) => item.id === id);
    if (!info) throw new Error("Session does not belong to the current working directory");
    sessionManager = PiSessionManager.open(info.path, getAppPaths().sessions, config.cwd);
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

export async function setWorkingDirectory(input: string) {
  const cwd = resolve(input.trim());
  const directory = await stat(cwd).catch(() => null);
  if (!directory?.isDirectory()) throw new Error("Working directory does not exist or is not a directory");

  const config = await readConfig();
  if (config.cwd !== cwd) {
    for (const [id, record] of sessions) {
      record.gate.rejectAll();
      await record.subagents?.abortAll();
      record.session.abortBash();
      await record.session.abort().catch(() => undefined);
      record.unsubscribe();
      await record.browser?.close();
      record.session.dispose();
      sessions.delete(id);
    }
    await updateConfig({ cwd });
  }

  const sessionsList = (await listSessions()).filter((session) => !session.archived);
  return { cwd, sessions: sessionsList, activeSessionId: sessionsList[0]?.id ?? "" };
}

async function promptSession(id: string, text: string, mode: "prompt" | "steer" | "followUp" = "prompt") {
  const record = await getOrCreateSession(id);
  const resolvedMode = resolvePromptMode(mode, record.session.isStreaming);
  const knownTaskIds = new Set(record.subagents?.list().map((task) => task.id) ?? []);
  const activeBefore = new Set(record.subagents?.list().filter((task) => task.status === "queued" || task.status === "running").map((task) => task.id) ?? []);
  if (resolvedMode !== "steer") record.gate.beginTask();
  if (resolvedMode === "steer") await record.session.steer(text);
  else if (resolvedMode === "followUp") await record.session.followUp(text);
  else await record.session.prompt(text);
  if (resolvedMode !== "steer" && record.subagents) await waitForSubagentsBeforeConclusion(record, knownTaskIds, activeBefore, record.abortEpoch ?? 0);
  return record;
}

function terminalSubagent(task: SubagentTask) {
  return task.status === "completed" || task.status === "failed" || task.status === "cancelled" || task.status === "interrupted";
}

async function waitForSubagentsBeforeConclusion(record: SessionRecord, knownTaskIds: Set<string>, requiredTaskIds: Set<string>, abortEpoch: number) {
  const manager = record.subagents;
  if (!manager) return;
  for (const task of manager.list()) {
    if (!knownTaskIds.has(task.id)) requiredTaskIds.add(task.id);
  }
  const delivered = new Set<string>();
  while (requiredTaskIds.size > 0) {
    if (manager.hasActiveTasks()) await manager.waitForAll();
    if ((record.abortEpoch ?? 0) !== abortEpoch) return;
    const tasks = manager.list();
    for (const task of tasks) {
      if (!knownTaskIds.has(task.id)) requiredTaskIds.add(task.id);
    }
    const results = tasks.filter((task) => requiredTaskIds.has(task.id) && !delivered.has(task.id) && terminalSubagent(task));
    if (results.length) {
      const message = results.map((task) => {
        const status = task.status === "completed" ? "completed" : task.status;
        const summary = task.summary?.trim() || task.error?.trim() || "No result";
        return `[RiftX subagent result]\nSubagent: ${task.name}\nStatus: ${status}\nSummary:\n${summary}`;
      }).join("\n\n");
      for (const task of results) delivered.add(task.id);
      record.gate.beginTask();
      await record.session.prompt(`${message}\n\nAll delegated child tasks required for this assessment have now reached a terminal state. Synthesize the final conclusion using these results. Do not start more child tasks or poll task files.`);
    }
    const pending = tasks.some((task) => requiredTaskIds.has(task.id) && !delivered.has(task.id));
    if (!manager.hasActiveTasks() && !pending) return;
  }
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
    record.abortEpoch = (record.abortEpoch ?? 0) + 1;
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
  onEvent({ type: "usage", usage: usageFromRecord(record) });
  for (const task of record.subagents?.list() ?? []) onEvent({ type: "subagent_snapshot", task });
  for (const finding of await record.evidenceStore.list()) onEvent({ type: "finding", finding });
  for (const request of record.gate.pendingRequests()) onEvent({ type: "approval_required", approval: request });
  for (const request of record.subagents?.pendingApprovals() ?? []) onEvent({ type: "approval_required", approval: request });
  return () => {
    record.emitter.off("event", onEvent);
  };
}

export async function assertSessionRunnable(id: string) {
  const config = await readConfig();
  if (config.archivedSessionIds.includes(id)) throw new Error("Session is archived");
  await assertSessionInCurrentWorkspace(id);
}

export async function listFindings(id: string) {
  await assertSessionInCurrentWorkspace(id);
  const record = sessions.get(id);
  const store = record?.evidenceStore ?? getEvidenceStore(id, getAppPaths().evidence);
  return store.list();
}

export async function patchFinding(id: string, findingId: string, patch: { confidence?: "confirmed" | "likely" | "suspected" | "not_reproducible"; dismissed?: boolean }) {
  await assertSessionInCurrentWorkspace(id);
  const record = sessions.get(id);
  const store = record?.evidenceStore ?? getEvidenceStore(id, getAppPaths().evidence);
  return store.patch(findingId, { confidence: patch.confidence, status: patch.dismissed === undefined ? undefined : patch.dismissed ? "dismissed" : "open" });
}

export async function assertSessionInCurrentWorkspace(id: string) {
  const config = await readConfig();
  const live = sessions.get(id);
  if (live && resolve(live.cwd) === resolve(config.cwd)) return;
  const info = (await listWorkspaceSessionInfos(config.cwd)).find((item) => item.id === id);
  if (!info) throw new Error("Session does not belong to the current working directory");
}

export async function listSubagents(id: string) {
  const record = await getOrCreateSession(id);
  return { tasks: record.subagents?.list() ?? [], running: record.subagents?.runningCount ?? 0, maxConcurrent: record.subagents?.maxConcurrentSubagents ?? 0 };
}

export async function getSessionSnapshot(id: string) {
  const snapshot = await buildSessionSnapshot(id, await findSessionPath(id));
  return {
    id,
    provider: snapshot.provider ?? "",
    model: snapshot.model ?? "",
    profileId: snapshot.profileId ?? "",
    contextWindow: snapshot.contextWindow,
    usage: snapshot.usage
  };
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

  const entries = record.sessionManager.getBranch();
  entries.forEach((entry, messageIndex) => {
    if (entry.type !== "message") return;
    const message = entry.message;
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
        const content = String(item.text ?? "");
        if (content.startsWith("[RiftX subagent result]")) return;
        messages.push({ id, role: "user", content });
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
  const infos = await listWorkspaceSessionInfos(config.cwd);
  const persisted = await Promise.all(infos.map(async (info) => {
    const snapshot = await buildSessionSnapshot(info.id, info.path);
    return {
      id: info.id,
      path: info.path,
      name: summaryName(config, info.id, info.firstMessage, archived.has(info.id)),
      firstMessage: info.firstMessage,
      updatedAt: info.modified.toISOString(),
      archived: archived.has(info.id),
      profileId: snapshot.profileId,
      provider: snapshot.provider,
      model: snapshot.model,
      contextWindow: snapshot.contextWindow,
      usage: snapshot.usage
    } satisfies SessionSummary;
  }));
  const seen = new Set(persisted.map((item) => item.id));
  const live = [...sessions.values()]
    .filter((session) => resolve(session.cwd) === resolve(config.cwd) && !seen.has(session.id))
    .map((session) => ({
      id: session.id,
      path: session.session.sessionFile ?? "",
      name: summaryName(config, session.id, "", archived.has(session.id)),
      firstMessage: "",
      updatedAt: new Date().toISOString(),
      archived: archived.has(session.id),
      profileId: session.profile.id,
      provider: session.profile.provider,
      model: session.profile.model,
      contextWindow: session.profile.contextWindow,
      usage: usageFromRecord(session)
    } satisfies SessionSummary));
  const archivedMetadata = config.archivedSessions
    .filter((session) => !seen.has(session.id) && !live.some((item) => item.id === session.id))
    .map((session) => ({ ...session, name: summaryName(config, session.id, session.firstMessage, true), archived: true } satisfies SessionSummary));
  const archivedFallback = config.archivedSessionIds
    .filter((id) => !seen.has(id) && !archivedMetadata.some((session) => session.id === id))
    .map((id) => ({ id, path: "", name: summaryName(config, id, "", true), firstMessage: "", updatedAt: new Date().toISOString(), archived: true } satisfies SessionSummary));
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
    await record.subagents?.abortAll();
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
    await record.subagents?.abortAll();
    sessions.delete(id);
  }
  if (session?.path) {
    try {
      await unlink(session.path);
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error;
    }
  }
  await removeEvidence(id, getAppPaths().evidence);
  const { [id]: _removedTitle, ...sessionTitles } = config.sessionTitles;
  await updateConfig({ archivedSessionIds: config.archivedSessionIds.filter((item) => item !== id), archivedSessions: config.archivedSessions.filter((item) => item.id !== id), sessionTitles });
  return listSessions();
}

export async function setActiveProfile(profile: ModelProfile) {
  const prepared = [...sessions.values()].map((record) => ({
    record,
    model: registerProfileModel(record.authStorage, record.modelRegistry, profile, true),
    previousProfile: record.profile,
    previousModel: record.model
  }));
  for (const { record, model } of prepared) {
    if (!record.modelRegistry.hasConfiguredAuth(model)) throw new Error(`No API key for ${model.provider}/${model.id}`);
  }
  const switched: typeof prepared = [];
  try {
    for (const item of prepared) {
      await item.record.session.setModel(item.model);
      switched.push(item);
      item.record.session.setThinkingLevel(profile.thinkingLevel);
      item.record.settingsManager.setTransport(profile.transport);
      item.record.session.agent.transport = profile.transport;
      item.record.profile = profile;
      item.record.model = item.model;
    }
  } catch (error) {
    for (const item of switched.reverse()) {
      await item.record.session.setModel(item.previousModel).catch(() => undefined);
      item.record.session.setThinkingLevel(item.previousProfile.thinkingLevel);
      item.record.settingsManager.setTransport(item.previousProfile.transport);
      item.record.session.agent.transport = item.previousProfile.transport;
      item.record.profile = item.previousProfile;
      item.record.model = item.previousModel;
    }
    throw error;
  }
}
