import { EventEmitter } from "node:events";
import { mkdir, readFile, stat, unlink, rm } from "node:fs/promises";
import { isAbsolute, join, relative, resolve } from "node:path";
import { Type } from "@sinclair/typebox";
import {
  AuthStorage,
  DefaultResourceLoader,
  ModelRegistry,
  SessionManager as AgentSessionManager,
  SettingsManager,
  createAgentSession,
  defineTool,
  type AgentSession,
  type AgentSessionEvent,
  type ToolDefinition
} from "@mariozechner/pi-coding-agent";
import type { Model } from "@mariozechner/pi-ai";
import { readConfig, getAppPaths, getLaunchDirectory, updateConfig } from "@/server/config-store";
import { RiftxError } from "@/server/errors";
import type { ApprovalMode, ArchivedSession, ContextUsage, FindingInput, FindingSource, ModelProfile, RiftxEvent, SessionSummary } from "@/lib/types";
import { ApprovalGate } from "./approval-gate";
import { createPermissionExtension } from "./permission-extension";
import { resolvePromptMode } from "@/lib/prompt-mode";
import { emptyContextUsage, normalizeContextUsage } from "./usage";
import { buildChildPentestSystemPrompt, buildPentestSystemPrompt } from "./system-prompt";
import { evaluateApproval } from "./approval-evaluator";
import { createBrowserExtension, BrowserManager } from "@/browser";
import { MutationLock } from "./mutation-lock";
import { SubagentManager, type SubagentRunnerContext } from "./subagent-manager";
import { generateSessionTitle } from "./session-title";
import { EvidenceStore, getEvidenceStore, removeEvidence } from "./evidence-store";
import { estimateCompactedUsage, estimateMessagesContextUsage, installMidTurnCompaction } from "./mid-turn-compaction";
import { shouldDeliverSubagentCompletion, waitForSubagentsBeforeConclusion } from "./session-join";
import { setAgentTransport } from "./pi-internals";
import { prepareSkillPrompt, type SkillDescriptor } from "./skill-router";
import { createTimedBashTool } from "./bash-timeout";
import { BashConcurrency } from "./bash-concurrency";
import { abortSessionRecord, shutdownSessionRecord } from "./session-shutdown";
import { switchSessionProfile, withProfileSwitchLock } from "./apply-session-profile";
import { registerTrackedProfile, registerProfileModel, restoreProviderRegistration, type ProviderRegistrations } from "./model-registration";
import { extractLastAssistantResult } from "./subagent-result";
import { claimSubagentResult, enqueueSessionAction, finishSubagentResult, formatSubagentTerminalMessage } from "./session-join";

type SessionRecord = {
  id: string;
  cwd: string;
  profile: ModelProfile;
  authStorage: AuthStorage;
  model: Model<any>;
  modelRegistry: ModelRegistry;
  settingsManager: SettingsManager;
  sessionManager: AgentSessionManager;
  session: AgentSession;
  gate: ApprovalGate;
  emitter: EventEmitter;
  toolStatuses: Map<string, "queued" | "running">;
  unsubscribe: () => void;
  browser?: BrowserManager;
  mutationLock: MutationLock;
  bashConcurrency: BashConcurrency;
  subagents?: SubagentManager;
  evidenceStore: EvidenceStore;
  shutdownPromise?: Promise<void>;
  runtimeVersion?: number;
  abortPromise?: Promise<void>;
  aborting?: boolean;
  abortEpoch?: number;
  waitingForSubagents?: boolean;
  promptChain?: Promise<void>;
  subagentDeliveryInProgress?: boolean;
  deliveredSubagentResults: Set<string>;
  deliveringSubagentResults: Set<string>;
  skills: SkillDescriptor[];
  loadedSkills: Set<string>;
  providerRegistrations: ProviderRegistrations;
  profileSwitch?: Promise<unknown>;
};

const RUNTIME_VERSION = 27;

type SessionSnapshotCacheEntry = {
  modifiedMs: number;
  size: number;
  profileKey: string;
  snapshot: SessionSnapshot | null;
};

const sessionSnapshotCache = new Map<string, SessionSnapshotCacheEntry>();
const SESSION_SNAPSHOT_CACHE_LIMIT = 256;

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
  // eslint-disable-next-line no-var
  var __riftxSessionCreation: Map<string, Promise<SessionRecord>> | undefined;
}

const sessions = globalThis.__riftxSessions ?? (globalThis.__riftxSessions = new Map<string, SessionRecord>());
const sessionCreation = globalThis.__riftxSessionCreation ?? (globalThis.__riftxSessionCreation = new Map<string, Promise<SessionRecord>>());

type RuntimeDeps = {
  evidenceStore: EvidenceStore;
  evidenceSessionId: string;
};

type FindingSourceInfo = { source: FindingSource; subagentId?: string };

type ToolEvidenceSnapshot = { toolCallId: string; toolName: string; content: string };

function textFromContent(content: unknown) {
  return Array.isArray(content)
    ? content.map((part: unknown) => typeof part === "string" ? part : part && typeof part === "object" && "text" in part ? String((part as { text?: unknown }).text ?? "") : "").join("")
    : String(content ?? "");
}

function resolveToolEvidence(session: AgentSession | undefined, requestedId: string): ToolEvidenceSnapshot | undefined {
  if (!session) return undefined;
  const messages = session.messages as unknown as Array<{ role?: string; content?: unknown; toolCallId?: string }>;
  const calls = messages.flatMap((message) => {
    const parts = Array.isArray(message.content) ? message.content : [];
    return parts.filter((part): part is { type?: string; id?: string; name?: string } => Boolean(part && typeof part === "object" && (part as { type?: string }).type === "toolCall"))
      .map((part) => ({ id: String(part.id ?? ""), name: String(part.name ?? "tool") }));
  });
  const selected = calls.find((call) => call.id === requestedId);
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
  if (event.type === "tool_execution_start") {
    const guarded = ["bash", "write", "edit", "browser"].includes(String(base.toolName));
    return { type: "tool_start", toolName: base.toolName, toolCallId: base.toolCallId, args: base.args, toolStatus: guarded ? "queued" : "running" } as RiftxEvent;
  }
  if (event.type === "tool_execution_update") {
    // The runtime's AgentToolUpdateCallback payload is exposed as `partialResult`.
    // Reading the old `update` name turns every streamed tool update into
    // undefined, which the UI then renders literally after approval.
    return { type: "tool_update", toolName: base.toolName, toolCallId: base.toolCallId, update: base.partialResult ?? base.update } as RiftxEvent;
  }
  if (event.type === "tool_execution_end") return { type: "tool_end", toolName: base.toolName, toolCallId: base.toolCallId, result: base.result, isError: base.isError } as RiftxEvent;
  if (event.type === "agent_start") return { type: "session_state", state: "running" };
  if (event.type === "agent_end") return { type: "done" };
  if (event.type === "turn_end") return { type: "message", message: base.message, toolResults: base.toolResults } as RiftxEvent;
  if (event.type === "auto_retry_start") return { type: "session_state", state: "retrying", attempt: base.attempt, error: base.errorMessage } as RiftxEvent;
  if (event.type === "compaction_start") return { type: "session_state", state: "compacting", reason: base.reason } as RiftxEvent;
  if (event.type === "compaction_end") return { type: "session_state", state: "running", reason: base.reason } as RiftxEvent;
  return { type: "message", message: base };
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
          const snapshot = resolveToolEvidence(getSession(), item.toolCallId);
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

function createSubagentTool(manager: SubagentManager, getChildProfile: () => ModelProfile, cwd: string, mutationLock: MutationLock, bashConcurrency: BashConcurrency, runtimeDeps: RuntimeDeps): ToolDefinition {
  return defineTool({
    name: "spawn_subagent",
    label: "Spawn subagent",
    description: "Start one focused, independent Web penetration testing child Agent. The task runs in the background so the main Agent can continue independent work, and each completed child result is returned as soon as it is available. Every spawned child is mandatory for the final assessment: if the main Agent reaches a conclusion while any child is still active, RiftX waits for all spawned children to finish before requesting that final conclusion. Never poll child logs or task files with bash or sleep. Use only for meaningful independent work, never duplicate or state-dependent child work; the scheduler enforces the configured concurrency limit and queues excess tasks. The child cannot create another child.",
    promptSnippet: "spawn_subagent(task)",
    executionMode: "parallel",
    parameters: Type.Object({
      task: Type.String({ description: "A unique, self-contained task with a clear target surface, evidence goal, and no dependency on another child task." })
    }),
    async execute(_toolCallId, params, signal) {
      const childProfile = getChildProfile();
      const submitted = manager.submitTask(params.task, (context) => runChildSession(childProfile, cwd, mutationLock, bashConcurrency, context, runtimeDeps));
      void submitted.promise.catch(() => undefined);
      const taskLabel = submitted.task?.name || "subagent task";
      const state = submitted.task?.status || "queued";
      const text = submitted.duplicate
        ? `A matching subagent task is already ${state}. Its existing result will be delivered when complete.`
        : `Subagent task accepted in the background (${state}): ${taskLabel}. Continue independent work. RiftX will return its result when it completes and will wait for it before a final conclusion if needed.`;
      if (signal?.aborted && submitted.task?.id) manager.cancel(submitted.task.id);
      return { content: [{ type: "text", text }], details: { model: `${childProfile.provider}/${childProfile.model}`, taskId: submitted.task?.id, status: state, background: true } };
    }
  });
}

async function createRuntimeSession(profile: ModelProfile, cwd: string, gate: ApprovalGate, child = false, sessionManagerOverride?: AgentSessionManager, mutationLock = new MutationLock(), bashConcurrencyOverride?: BashConcurrency, runtimeDeps?: RuntimeDeps, findingSource: FindingSourceInfo = { source: "main" }) {
  const paths = getAppPaths();
  await mkdir(paths.agent, { recursive: true, mode: 0o700 });
  const authStorage = AuthStorage.create(join(paths.agent, "auth.json"));
  const modelRegistry = ModelRegistry.create(authStorage, join(paths.agent, "models.json"));
  const providerRegistrations: ProviderRegistrations = new Map();
  const model = registerTrackedProfile(providerRegistrations, authStorage, modelRegistry, profile, true);

  const config = await readConfig();
  const bashConcurrency = bashConcurrencyOverride ?? new BashConcurrency(config.maxConcurrentSubagents + 1);
  gate.setMode(config.approvalMode);
  const emitter = new EventEmitter();
  const toolStatuses = new Map<string, "queued" | "running">();
  const trackToolStatus = (event: RiftxEvent) => {
    if (event.type === "done" || event.type === "error") {
      toolStatuses.clear();
      return;
    }
    const toolCallId = typeof event.toolCallId === "string" ? event.toolCallId : undefined;
    if (!toolCallId) return;
    if (event.type === "tool_start" || event.type === "tool_status") {
      if (event.toolStatus === "queued" || event.toolStatus === "running") toolStatuses.set(toolCallId, event.toolStatus);
    } else if (event.type === "tool_end") {
      toolStatuses.delete(toolCallId);
    }
  };
  const emitRuntimeEvent = (event: RiftxEvent) => {
    trackToolStatus(event);
    emitter.emit("event", event);
  };
  if (!child) {
    gate.onDecision((request, approved) => emitter.emit("event", { type: "approval_decided", approvalId: request.id, approval: request, approved }));
  }
  let record: SessionRecord | undefined;
  const childProfile = config.childInherit ? profile : config.profiles.find((item) => item.id === config.childProfileId) ?? profile;
  // Keep title work on the configured child profile when available so it does
  // not consume the main Agent's provider quota during a live turn.
  const settingsManager = SettingsManager.create(cwd, paths.agent);
  settingsManager.setTransport(profile.transport);
  const sessionManager = sessionManagerOverride ?? AgentSessionManager.create(cwd, child ? join(paths.subagents, "runtime") : paths.sessions);
  const evidenceSessionId = runtimeDeps?.evidenceSessionId ?? sessionManager.getSessionId();
  const browser = new BrowserManager({ evidenceRoot: paths.evidence, evidenceSessionId, scope: { rules: config.browserScope }, ignoreTlsErrors: config.browserIgnoreTlsErrors });
  const permission = createPermissionExtension(
    gate,
    (event) => emitRuntimeEvent(event as RiftxEvent),
    (request) => evaluateApproval(record?.model ?? model, modelRegistry, request),
    mutationLock,
    bashConcurrency,
    {
      check: (url) => browser.checkNavigationScope(url),
      authorizeOnce: (url, identity) => browser.authorizeOnce(url, identity),
      revokeOnce: (url, identity) => browser.revokeOnce(url, identity),
      grantScope: (url, exactPort) => browser.grantScope(url, exactPort),
      checkMappings: (mappings) => browser.checkHostMappingScope(mappings),
      authorizeMappingsOnce: (mappings) => browser.authorizeMappingTargetsOnce(mappings)
    }
  );
  const evidenceStore = runtimeDeps?.evidenceStore ?? getEvidenceStore(evidenceSessionId, paths.evidence, (event) => emitter.emit("event", event));
  const subagentNameGenerator = !child ? async (task: string) => {
    const titleAuthStorage = AuthStorage.inMemory();
    const titleModelRegistry = ModelRegistry.inMemory(titleAuthStorage);
    const titleModel = registerProfileModel(titleAuthStorage, titleModelRegistry, childProfile, true);
    return generateSessionTitle(titleModelRegistry, titleModel, task, "empty");
  } : undefined;
  const subagents = !child ? new SubagentManager(sessionManager.getSessionId(), paths.subagents, (event) => emitter.emit("event", event), config.maxConcurrentSubagents, config.approvalMode, subagentNameGenerator) : undefined;
  const getChildProfile = () => config.childInherit ? (record?.profile ?? profile) : childProfile;
  let evidenceSession: AgentSession | undefined;
  const customTools = [createTimedBashTool(cwd, { commandPrefix: settingsManager.getShellCommandPrefix(), shellPath: settingsManager.getShellPath() }) as unknown as ToolDefinition, createFindingTool(evidenceStore, findingSource, browser, () => evidenceSession), ...(subagents ? [createSubagentTool(subagents, getChildProfile, cwd, mutationLock, bashConcurrency, { evidenceStore, evidenceSessionId })] : [])];
  const browserExtension = createBrowserExtension({ evidenceRoot: paths.evidence, evidenceSessionId }, browser);
  const resourceLoader = new DefaultResourceLoader({
    cwd,
    agentDir: paths.agent,
    additionalSkillPaths: [paths.skills],
    extensionFactories: [permission, browserExtension],
    noExtensions: true,
    noSkills: true,
    systemPrompt: child ? buildChildPentestSystemPrompt() : buildPentestSystemPrompt(config.subagentAggressiveness, config.systemPromptEnabled ? config.systemPrompt : undefined)
  });
  // The SDK only reloads a resource loader it creates internally. RiftX supplies
  // its own loader, so load the custom system prompt and inline extensions before
  // createAgentSession builds the runtime.
  await resourceLoader.reload();
  const result = await createAgentSession({
    cwd,
    agentDir: paths.agent,
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
  // The runtime prepares parallel calls before executing them. Keep mutation
  // tools sequential so they cannot deadlock on the shared mutation lock.
  // Bash uses its own shared concurrency limiter, while spawn_subagent remains parallel.
  const runtimeAgent = (result.session as unknown as { agent?: { toolExecution?: "parallel" | "sequential"; state?: { tools?: Array<{ name: string; executionMode?: "parallel" | "sequential" }> } } }).agent;
  if (runtimeAgent) {
    runtimeAgent.toolExecution = "parallel";
    for (const tool of runtimeAgent.state?.tools ?? []) {
      if (["write", "edit", "browser"].includes(tool.name)) tool.executionMode = "sequential";
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
    toolStatuses,
    browser,
    mutationLock,
    bashConcurrency,
    subagents,
    evidenceStore,
    runtimeVersion: RUNTIME_VERSION,
    aborting: false,
    abortEpoch: 0,
    waitingForSubagents: false,
    deliveredSubagentResults: new Set(),
    deliveringSubagentResults: new Set(),
    skills: resourceLoader.getSkills().skills as SkillDescriptor[],
    providerRegistrations,
    loadedSkills: new Set(),
    unsubscribe: () => undefined
  };
  const unsubscribe = result.session.subscribe((event) => {
    if (event.type === "agent_end" && subagents?.hasActiveTasks() && !record.subagentDeliveryInProgress) record.waitingForSubagents = true;
    const payload = event.type === "agent_end" && subagents?.hasActiveTasks() && !record.subagentDeliveryInProgress
      ? { type: "session_state", state: "waiting_for_subagents" }
      : eventPayload(event);
    trackToolStatus(payload as RiftxEvent);
    emitter.emit("event", payload);
    const usage = event.type === "compaction_end" && event.result
      ? estimateCompactedUsage(result.session, record.profile.contextWindow)
      : usageFromRecord(record);
    if (usage) emitter.emit("event", { type: "usage", usage: normalizeContextUsage(usage, record.profile.contextWindow) });
  });
  record.unsubscribe = unsubscribe;
  if (subagents) {
    subagents.setCompletionHandler((task, childResult) => {
      void enqueueSessionAction(record, async () => {
        if (!shouldDeliverSubagentCompletion(record)) return;
        if (!claimSubagentResult(record, task.id)) return;
        const message = formatSubagentTerminalMessage(task, childResult.summary);
        record.subagentDeliveryInProgress = true;
        try {
          if (record.session.isStreaming) await record.session.steer(message);
          else {
            record.gate.beginTask();
            await record.session.prompt(message);
          }
          finishSubagentResult(record, task.id, true);
        } catch {
          finishSubagentResult(record, task.id, false);
        } finally {
          record.subagentDeliveryInProgress = false;
        }
      }).catch(() => undefined);
    });
    await subagents.initialize((context) => runChildSession(getChildProfile(), cwd, mutationLock, bashConcurrency, context, { evidenceStore, evidenceSessionId }));
  }
  return record;
}

async function runChildSession(profile: ModelProfile, cwd: string, mutationLock: MutationLock, bashConcurrency: BashConcurrency, context: SubagentRunnerContext, runtimeDeps: RuntimeDeps) {
  const paths = getAppPaths();
  const threadDir = join(paths.subagents, context.task.parentSessionId, context.task.id);
  await mkdir(threadDir, { recursive: true, mode: 0o700 });
  const childSessionManager = AgentSessionManager.create(cwd, threadDir);
  const child = await createRuntimeSession(profile, cwd, context.gate, true, childSessionManager, mutationLock, bashConcurrency, runtimeDeps, { source: "subagent", subagentId: context.task.id });
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
    const result = extractLastAssistantResult(child.session.sessionManager.getBranch());
    if (result.error) throw new Error(result.error);
    return { summary: result.summary ?? "" };
  } finally {
    unsubscribe();
    context.signal.removeEventListener("abort", abortChild);
    if (context.signal.aborted) await child.session.abort().catch(() => undefined);
    await shutdownSessionRecord(child);
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
    while (sessionSnapshotCache.size > SESSION_SNAPSHOT_CACHE_LIMIT) sessionSnapshotCache.delete(sessionSnapshotCache.keys().next().value!);
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
  const infos = await AgentSessionManager.list(cwd, getAppPaths().sessions);
  const launchDirectory = getLaunchDirectory();
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
  if (id && config.archivedSessionIds.includes(id)) throw new RiftxError("Session is archived", "SESSION_ARCHIVED", 404);
  if (id) {
    const pending = sessionCreation.get(id);
    if (pending) return pending;
  }
  const create = async () => {
    if (id && sessions.has(id)) {
      const existing = sessions.get(id)!;
      // Rebuild stale process-global session objects after a dev-server reload
      // or runtime-version bump, while keeping persisted history on disk.
      if (existing.runtimeVersion === RUNTIME_VERSION && resolve(existing.cwd) === resolve(config.cwd)) {
        // A record being torn down must not be handed out again: archive has
        // already claimed it and any prompt/abort would race its disposal.
        if (existing.shutdownPromise) throw new RiftxError("Session is shutting down", "SESSION_BUSY", 409);
        return existing;
      }
      await shutdownSessionRecord(existing);
      sessions.delete(id);
    }
    const currentConfig = await readConfig();
    const profile = await profileFor();
    let sessionManager: AgentSessionManager | undefined;
    if (id) {
      const info = (await listWorkspaceSessionInfos(currentConfig.cwd)).find((item) => item.id === id);
      if (!info) throw new RiftxError("Session does not belong to the current working directory", "SESSION_NOT_IN_WORKSPACE", 404);
      sessionManager = AgentSessionManager.open(info.path, getAppPaths().sessions, currentConfig.cwd);
    }
    const created = await createRuntimeSession(profile, currentConfig.cwd, new ApprovalGate(), false, sessionManager);
    sessions.set(created.id, created);
    return created;
  };
  if (id) {
    const pending = create();
    sessionCreation.set(id, pending);
    try {
      return await pending;
    } finally {
      if (sessionCreation.get(id) === pending) sessionCreation.delete(id);
    }
  }
  return create();
}

export async function createSession(): Promise<SessionSummary> {
  const config = await readConfig();
  const profile = await profileFor();
  const created = await createRuntimeSession(profile, config.cwd, new ApprovalGate());
  sessions.set(created.id, created);
  return {
    id: created.id,
    path: created.session.sessionFile ?? "",
    name: summaryName(config, created.id, ""),
    firstMessage: "",
    updatedAt: new Date().toISOString(),
    archived: false,
    profileId: created.profile.id,
    provider: created.profile.provider,
    model: created.profile.model,
    contextWindow: created.profile.contextWindow,
    usage: usageFromRecord(created)
  };
}

export async function setWorkingDirectory(input: string) {
  const cwd = resolve(input.trim());
  const directory = await stat(cwd).catch(() => null);
  if (!directory?.isDirectory()) throw new RiftxError("Working directory does not exist or is not a directory", "INVALID_WORKING_DIRECTORY", 400);

  const config = await readConfig();
  if (config.cwd !== cwd) {
    for (const [id, record] of sessions) {
      await shutdownSessionRecord(record);
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
  const prepared = resolvedMode === "steer" ? { prompt: text, skillContext: "", loaded: [] as string[] } : await prepareSkillPrompt(text, record.skills, record.loadedSkills);
  const promptAbortEpoch = record.abortEpoch ?? 0;
  const knownTaskIds = new Set(record.subagents?.list().map((task) => task.id) ?? []);
  const activeBefore = new Set(record.subagents?.list().filter((task) => task.status === "queued" || task.status === "running").map((task) => task.id) ?? []);
  let skillInjected = false;
  try {
    await enqueueSessionAction(record, async () => {
      if (resolvedMode === "steer") await record.session.steer(prepared.prompt);
      else if (resolvedMode === "followUp") {
        record.gate.beginTask();
        if (prepared.skillContext) {
          await record.session.sendCustomMessage({ customType: "riftx_skill_context", content: prepared.skillContext, display: false }, { deliverAs: "followUp" });
          skillInjected = true;
        }
        await record.session.followUp(text);
      }
      else {
        record.gate.beginTask();
        if (prepared.skillContext) {
          await record.session.sendCustomMessage({ customType: "riftx_skill_context", content: prepared.skillContext, display: false });
          skillInjected = true;
        }
        await record.session.prompt(text);
      }
    });
  } catch (error) {
    if (!skillInjected) prepared.loaded.forEach((name) => record.loadedSkills.delete(name));
    throw error;
  }
  if (resolvedMode !== "steer" && record.subagents) await waitForSubagentsBeforeConclusion(record, knownTaskIds, activeBefore, promptAbortEpoch);
  return record;
}

export async function summarizeSessionTitle(id: string, task: string) {
  const existingConfig = await readConfig();
  const existingTitle = existingConfig.sessionTitles[id]?.trim();
  if (existingTitle) return { title: existingTitle, sessions: (await listSessions()).filter((session) => !session.archived) };
  const record = await getOrCreateSession(id);
  const titleProfile = existingConfig.childInherit
    ? record.profile
    : existingConfig.profiles.find((item) => item.id === existingConfig.childProfileId) ?? record.profile;
  const titleAuthStorage = AuthStorage.inMemory();
  const titleModelRegistry = ModelRegistry.inMemory(titleAuthStorage);
  const titleModel = registerProfileModel(titleAuthStorage, titleModelRegistry, titleProfile, true);
  const title = await generateSessionTitle(titleModelRegistry, titleModel, task);
  const config = await readConfig();
  const latestTitle = config.sessionTitles[id]?.trim();
  if (latestTitle) return { title: latestTitle, sessions: (await listSessions()).filter((session) => !session.archived) };
  await updateConfig((current) => ({ sessionTitles: { ...current.sessionTitles, [id]: title } }));
  return { title, sessions: (await listSessions()).filter((session) => !session.archived) };
}

export async function startPromptSession(id: string, text: string, mode: "prompt" | "steer" | "followUp" = "prompt") {
  const record = await getOrCreateSession(id);
  // Keep the Agent single-run while a previous stop is still unwinding a tool.
  if (record.abortPromise) await record.abortPromise;
  void promptSession(id, text, mode).catch((error) => {
    record.emitter.emit("event", { type: "error", error: error instanceof Error ? error.message : "Agent request failed" });
  });
  return record;
}

export async function abortSession(id: string) {
  const record = await getOrCreateSession(id);
  await abortSessionRecord(record, (event) => record.emitter.emit("event", event));
}

export async function decideApproval(id: string, approvalId: string, approved: boolean, scope: "once" | "task" = "once") {
  const record = await getOrCreateSession(id);
  const request = record.gate.pendingRequests().find((item) => item.id === approvalId);
  if (approved && scope === "task" && request) record.gate.allowForTask(request);
  if (request) return record.gate.decide(approvalId, approved, scope === "task");
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
  for (const session of sessions.values()) {
    session.subagents?.setMaxConcurrent(maxConcurrentSubagents);
    session.bashConcurrency.setLimit(maxConcurrentSubagents + 1);
  }
  return maxConcurrentSubagents;
}

export async function subscribeSession(id: string, listener: (event: RiftxEvent) => void) {
  const record = await getOrCreateSession(id);
  const onEvent = (event: RiftxEvent) => listener({ ...event, sessionId: record.id });
  record.emitter.on("event", onEvent);
  // Replay state that may have happened before an SSE reconnect, especially an
  // approval request that is still holding the agent at a guarded tool call.
  if (record.waitingForSubagents) onEvent({ type: "session_state", state: "waiting_for_subagents" });
  else if (record.session.isStreaming) onEvent({ type: "session_state", state: "running" });
  else onEvent({ type: "session_state", state: "idle" });
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
  if (config.archivedSessionIds.includes(id)) throw new RiftxError("Session is archived", "SESSION_ARCHIVED", 404);
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
  if (!info) throw new RiftxError("Session does not belong to the current working directory", "SESSION_NOT_IN_WORKSPACE", 404);
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
  const messages: Array<{ id: string; role: "user" | "assistant" | "thinking" | "tool"; content: string; toolName?: string; toolCallId?: string; status?: "queued" | "running" | "done" | "error"; isError?: boolean }> = [];
  const toolIndexes = new Map<string, number>();
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
        if (content.startsWith("[RiftX subagent result]") || content.startsWith("[RiftX subagent status]")) return;
        messages.push({ id, role: "user", content });
      } else if (candidate.role === "assistant" && item.type === "thinking") {
        messages.push({ id, role: "thinking", content: String(item.thinking ?? ""), status: "done" });
      } else if (candidate.role === "assistant" && item.type === "text") {
        messages.push({ id, role: "assistant", content: String(item.text ?? "") });
      } else if (candidate.role === "assistant" && item.type === "toolCall") {
        const toolCallId = String(item.id ?? id);
        const toolIndex = messages.length;
        toolIndexes.set(toolCallId, toolIndex);
        messages.push({ id: toolCallId, role: "tool", toolCallId, toolName: String(item.name ?? "tool"), content: JSON.stringify(item.arguments ?? {}, null, 2), status: record.toolStatuses.get(toolCallId) ?? "running" });
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
  if (!summary) throw new RiftxError("session not found", "SESSION_NOT_FOUND", 404);
  if (!config.archivedSessionIds.includes(id)) {
    const metadata: ArchivedSession = {
      id: summary.id,
      path: summary.path,
      name: summary.name,
      firstMessage: summary.firstMessage,
      updatedAt: summary.updatedAt
    };
    await updateConfig((current) => current.archivedSessionIds.includes(id) ? {} : {
      archivedSessionIds: [...current.archivedSessionIds, id],
      archivedSessions: [...current.archivedSessions.filter((item) => item.id !== id), metadata]
    });
  }
  const record = sessions.get(id);
  if (record) {
    await shutdownSessionRecord(record);
    sessions.delete(id);
  }
  return listSessions();
}

export async function deleteArchivedSession(id: string) {
  const config = await readConfig();
  if (!config.archivedSessionIds.includes(id)) throw new RiftxError("session is not archived", "SESSION_NOT_ARCHIVED", 400);
  const session = (await listSessions()).find((item) => item.id === id);
  const record = sessions.get(id);
  if (record) {
    await shutdownSessionRecord(record);
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
  const subagentPath = resolve(getAppPaths().subagents, id);
  const subagentRoot = resolve(getAppPaths().subagents);
  // Containment via path.relative works on both separators; a string prefix
  // check silently fails on Windows backslash paths.
  const subagentRelative = relative(subagentRoot, subagentPath);
  if (subagentRelative && !subagentRelative.startsWith("..") && !isAbsolute(subagentRelative)) {
    await rm(subagentPath, { recursive: true, force: true });
  }
  await updateConfig((current) => {
    const { [id]: _removedTitle, ...sessionTitles } = current.sessionTitles;
    return {
      archivedSessionIds: current.archivedSessionIds.filter((item) => item !== id),
      archivedSessions: current.archivedSessions.filter((item) => item.id !== id),
      sessionTitles
    };
  });
  return listSessions();
}

/**
 * Switch the live model for one specific session (the global default is
 * persisted separately by the settings route). A running session other than
 * the target is never touched, and a streaming target keeps its current model
 * until it is idle — switching mid-run would change cost and behavior under
 * the caller's feet.
 */
/**
 * Switch one live session's model. The decision compares against the target
 * session's current profile (never the global default) and surfaces missing
 * or busy sessions as typed errors instead of silently succeeding.
 */
export async function setActiveProfile(profile: ModelProfile, sessionId?: string) {
  if (!sessionId) return false;
  const record = sessions.get(sessionId);
  if (!record) throw new RiftxError("Session not found", "SESSION_NOT_FOUND", 404);
  // The whole switch — capture, staging, commit, or rollback — runs inside a
  // per-session mutex: a second concurrent switch is rejected instead of
  // racing the first one's rollback against its commit.
  const switched = await withProfileSwitchLock(record, "reject", () => switchSessionProfile(record, profile, {
    prepareModel: (target, next) => {
      const sessionRecord = target as SessionRecord;
      // Capture the provider's real pre-switch registration; the tracked map
      // is only written on success, so it still holds this value on failure.
      const captured = sessionRecord.providerRegistrations.get(next.provider);
      // registerProfileModel writes the new key in TWO places: the record's
      // runtime API key override and the registry's provider request config.
      // restoreProviderRegistration undoes both — restoring the captured
      // registration, or removing a provider this failed switch introduced.
      const rollback = () => restoreProviderRegistration(
        { authStorage: sessionRecord.authStorage, modelRegistry: sessionRecord.modelRegistry, registrations: sessionRecord.providerRegistrations },
        next.provider,
        captured
      );
      let model: Model<any>;
      try {
        model = registerProfileModel(sessionRecord.authStorage, sessionRecord.modelRegistry, next, true);
      } catch (error) {
        // A failure mid-registration must not leave the new key behind.
        try { rollback(); } catch { /* restore is best-effort */ }
        throw error;
      }
      return { model, rollback };
    },
    hasConfiguredAuth: (model) => record.modelRegistry.hasConfiguredAuth(model as Model<any>),
    applyTransport: (session, transport) => setAgentTransport(session as AgentSession, transport)
  }));
  // Only a successful switch becomes the provider's tracked registration.
  if (switched) record.providerRegistrations.set(profile.provider, profile);
  return switched;
}
