import { EventEmitter } from "node:events";
import { mkdir, stat, unlink, rm } from "node:fs/promises";
import { isAbsolute, join, relative, resolve } from "node:path";
import {
  AuthStorage,
  DefaultResourceLoader,
  ModelRegistry,
  SessionManager as AgentSessionManager,
  SettingsManager,
  createAgentSession,
  type AgentSession,
  type AgentSessionEvent,
  type ToolDefinition
} from "@mariozechner/pi-coding-agent";
import type { Model } from "@mariozechner/pi-ai";
import { readConfig, getAppPaths, updateConfig } from "@/server/config-store";
import { RiftxError } from "@/server/errors";
import { clampConcurrency, type ApprovalMode, type ArchivedSession, type ModelProfile, type RiftxEvent, type SessionSummary } from "@/lib/types";
import { ApprovalGate } from "./approval-gate";
import { createPermissionExtension, guardedTools } from "./permission-extension";
import { resolvePromptMode } from "@/lib/prompt-mode";
import { normalizeContextUsage } from "./usage";
import { buildChildPentestSystemPrompt, buildPentestSystemPrompt } from "./system-prompt";
import { evaluateApproval } from "./approval-evaluator";
import { createBrowserExtension, BrowserManager } from "@/browser";
import { MutationLock } from "./mutation-lock";
import { SubagentManager, type SubagentRunnerContext } from "./subagent-manager";
import { generateSessionTitle } from "./session-title";
import { generateSubagentSummary } from "./subagent-summary";
import { getEvidenceStore, removeEvidence } from "./evidence-store";
import { estimateCompactedUsage, installMidTurnCompaction } from "./mid-turn-compaction";
import { waitForSubagentsBeforeConclusion } from "./session-join";
import { setAgentTransport } from "./pi-internals";
import { prepareSkillPrompt, type SkillDescriptor } from "./skill-router";
import { createTimedBashTool } from "./bash-timeout";
import { createWebTools } from "@/server/web/tools";
import { createCrawlTool } from "@/browser/tools/crawl";
import { sessionToolNames } from "@/server/session-tools";
import { BashConcurrency } from "./bash-concurrency";
import { abortSessionRecord, shutdownSessionRecord } from "./session-shutdown";
import { switchSessionProfile, withProfileSwitchLock } from "./apply-session-profile";
import { registerTrackedProfile, registerProfileModel, restoreProviderRegistration, memoizedTitleRuntime, type ProviderRegistrations } from "./model-registration";
import { extractLastAssistantResult, buildSummaryTranscript } from "./subagent-result";
import { sessions, sessionCreation, RUNTIME_VERSION, type RuntimeDeps, type SessionRecord } from "./session-registry";
import { createFindingTool, type FindingSourceInfo } from "./tools/finding-tool";
import { createSubagentTool } from "./tools/subagent-tool";
import { listSessions, getSessionSnapshot, getSessionMessages as getMessages, summaryName, usageFromRecord, listWorkspaceSessionInfos } from "./session-snapshot";

// Facade re-exports: the API routes import everything from this module.
export { listSessions, getSessionSnapshot };
export async function getSessionMessages(id: string) {
  return getMessages(() => getOrCreateSession(id));
}
import { deliverSubagentCompletion, dispatchSessionAction, undeliveredTerminalTasks } from "./session-join";

function eventPayload(event: AgentSessionEvent): RiftxEvent {
  const base = event as unknown as Record<string, unknown>;
  if (event.type === "message_update") {
    const assistant = base.assistantMessageEvent as Record<string, unknown> | undefined;
    return { type: assistant?.type === "text_delta" ? "text_delta" : assistant?.type === "thinking_delta" ? "thinking_delta" : "message", delta: assistant?.delta ?? "" };
  }
  if (event.type === "tool_execution_start") {
    const guarded = guardedTools.has(String(base.toolName));
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


async function createRuntimeSession(profile: ModelProfile, cwd: string, gate: ApprovalGate, child = false, sessionManagerOverride?: AgentSessionManager, mutationLock = new MutationLock(), bashConcurrencyOverride?: BashConcurrency, runtimeDeps?: RuntimeDeps, findingSource: FindingSourceInfo = { source: "main" }) {
  const paths = getAppPaths();
  await mkdir(paths.agent, { recursive: true, mode: 0o700 });
  const authStorage = AuthStorage.create(join(paths.agent, "auth.json"));
  const modelRegistry = ModelRegistry.create(authStorage, join(paths.agent, "models.json"));
  const providerRegistrations: ProviderRegistrations = new Map();
  const model = registerTrackedProfile(providerRegistrations, authStorage, modelRegistry, profile, true);

  const config = await readConfig();
  const bashConcurrency = bashConcurrencyOverride ?? new BashConcurrency(config.maxConcurrentSubagents + 1);
  // Browser state changes have their own lock. Bash still shares the file
  // mutation lock with write/edit, but a long read-heavy Bash scan must not
  // block navigation or interaction in the Browser runtime.
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
    (request) => evaluateApproval(record?.model ?? model, modelRegistry, request, config.browserScope),
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
    const { titleModelRegistry, titleModel } = memoizedTitleRuntime(childProfile, () => {
      const titleAuthStorage = AuthStorage.inMemory();
      const titleModelRegistry = ModelRegistry.inMemory(titleAuthStorage);
      return { titleModelRegistry, titleModel: registerProfileModel(titleAuthStorage, titleModelRegistry, childProfile, true) };
    });
    return generateSessionTitle(titleModelRegistry, titleModel, task, "empty");
  } : undefined;
  const subagents = !child ? new SubagentManager(sessionManager.getSessionId(), paths.subagents, (event) => emitter.emit("event", event), config.maxConcurrentSubagents, config.approvalMode, subagentNameGenerator) : undefined;
  const getChildProfile = () => config.childInherit ? (record?.profile ?? profile) : childProfile;
  let evidenceSession: AgentSession | undefined;
  const customTools = [createTimedBashTool(cwd, { commandPrefix: settingsManager.getShellCommandPrefix(), shellPath: settingsManager.getShellPath() }) as unknown as ToolDefinition, createFindingTool(evidenceStore, findingSource, browser, () => evidenceSession), createCrawlTool(browser), ...createWebTools({
        // Read per call: saving a key in settings applies to already-running
        // sessions on their next search, with no re-open needed.
        getTavilyApiKey: async () => (await readConfig()).webSearch?.tavilyApiKey
      }), ...(subagents ? [createSubagentTool(subagents, getChildProfile, cwd, mutationLock, bashConcurrency, { evidenceStore, evidenceSessionId }, runChildSession)] : [])];
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
    // Hard whitelist (see src/server/session-tools.ts): the SDK silently
    // drops any tool — built-in or custom — whose name is absent here.
    tools: sessionToolNames(Boolean(subagents)),
    customTools,
    resourceLoader,
    sessionManager,
    settingsManager
  });
  evidenceSession = result.session;
  installMidTurnCompaction(result.session);
  // The runtime prepares parallel calls before executing them. The SDK runs
  // an ENTIRE batch sequentially if any single tool is marked sequential
  // (agent-loop.js:235: hasSequentialToolCall → executeToolCallsSequential),
  // so a batch like [bash(400s), write, web_search] would queue everything
  // behind the 400s bash. No RiftX tool uses executionMode: "sequential" —
  // write/edit coordinate through the exclusive MutationLock in
  // beforeToolCall (they already wait for each other there), and Bash uses
  // its own shared concurrency limiter. crawl serializes through
  // BrowserManager.run(). Everything stays in the parallel lane.
  const runtimeAgent = (result.session as unknown as { agent?: { toolExecution?: "parallel" | "sequential"; state?: { tools?: Array<{ name: string; executionMode?: "parallel" | "sequential"; execute?: (toolCallId: string, params: unknown, signal?: AbortSignal, ...rest: unknown[]) => Promise<unknown> }> } } }).agent;
  if (runtimeAgent) {
    runtimeAgent.toolExecution = "parallel";
    for (const tool of runtimeAgent.state?.tools ?? []) {
      tool.executionMode = "parallel";
      // Locks are acquired at EXECUTION time (not beforeToolCall) to avoid
      // the SDK parallel-executor deadlock: beforeToolCall handlers all run
      // before any execution starts, so a shared holder (bash) would never
      // release while an exclusive waiter (write) is stuck in pre-processing.
      // The execute wrapper acquires and releases around the real execute.
      if (tool.name === "bash" && typeof tool.execute === "function") {
        const original = tool.execute.bind(tool);
        tool.execute = async (toolCallId: string, params: unknown, signal?: AbortSignal, ...rest: unknown[]) => {
          const bashRelease = await bashConcurrency.acquire(signal);
          let mutationRelease: (() => void) | undefined;
          try {
            mutationRelease = mutationLock ? await mutationLock.acquireShared(signal) : undefined;
          } catch (error) {
            bashRelease();
            throw error;
          }
          try {
            return await original(toolCallId, params, signal, ...rest);
          } finally {
            mutationRelease?.();
            bashRelease();
          }
        };
      }
      if ((tool.name === "write" || tool.name === "edit") && typeof tool.execute === "function") {
        const original = tool.execute.bind(tool);
        tool.execute = async (toolCallId: string, params: unknown, signal?: AbortSignal, ...rest: unknown[]) => {
          const release = mutationLock ? await mutationLock.acquire(signal) : undefined;
          try {
            return await original(toolCallId, params, signal, ...rest);
          } finally {
            release?.();
          }
        };
      }
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
    if (event.type === "agent_end" && subagents && !record.subagentDeliveryInProgress) {
      // Only enter the waiting state when subagents are actually still
      // running. Setting it unconditionally would make SSE reconnects replay
      // a stale waiting_for_subagents state and suppress deliveries in the
      // next turn's streaming phase.
      const hasActive = subagents.hasActiveTasks();
      // The model just finished its turn. Deliver any stranded subagent
      // results now while the model is idle — but only when no other
      // subagent is still running (partial results defer to the batch).
      // Checks the persisted delivery mark so post-restart legacy tasks
      // aren't re-injected.
      const stranded = undeliveredTerminalTasks(record, subagents.list());
      if (hasActive) {
        // Still waiting for the active batch. Stranded results from earlier
        // failed deliveries stay pending — the completion handler delivers
        // them alongside the final active result when the batch completes.
        record.waitingForSubagents = true;
      } else if (stranded.length > 0) {
        record.waitingForSubagents = false;
        for (const task of stranded) {
          void deliverSubagentCompletion(record, task, task.summary).catch(() => undefined);
        }
      } else {
        record.waitingForSubagents = false;
      }
    }
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
      void deliverSubagentCompletion(record, task, childResult.summary)
        .then(() => {
          // When this was the last active subagent, deliver any previously
          // stranded results alongside this one — otherwise a result that
          // failed delivery earlier stays stranded until the next user
          // prompt.
          if (subagents && !subagents.hasActiveTasks()) {
            // All subagents are done: clear the waiting state so SSE
            // reconnects replay idle instead of a stale waiting_for_subagents.
            record.waitingForSubagents = false;
            const stranded = undeliveredTerminalTasks(record, subagents.list());
            for (const entry of stranded) {
              void deliverSubagentCompletion(record, entry, entry.summary).catch(() => undefined);
            }
          }
        })
        .catch(() => undefined);
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
    void child.browser?.shutdown();
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
    // Match and inline the relevant skill for the delegated task, mirroring the
    // main session's prompt preparation, so children inherit domain guidance.
    const prepared = await prepareSkillPrompt(context.task.task, child.skills, child.loadedSkills);
    await child.session.prompt(prepared.prompt);
    let result = extractLastAssistantResult(child.session.sessionManager.getBranch());
    if (result.error) throw new Error(result.error);

    // The model can spend its entire output budget on thinking (hitting the
    // max-tokens limit before producing any text). The session context still
    // holds all the work — a short follow-up asking for a concise summary
    // lets the model deliver its result within a few hundred tokens.
    if (!result.summary?.trim() && !context.signal.aborted) {
      // Fallback via the lightweight summary model (thinking off, short
      // output) instead of another full run on the same session — the first
      // run already burned the output budget on thinking; a second identical
      // call would likely repeat that. The transcript includes both assistant
      // text AND tool results — the actual evidence lives in toolResult
      // messages, not in the model's plans. Sensitive values (cookies,
      // tokens) are truncated per-entry to avoid sending full credentials to
      // the summary model.
      try {
        const branchText = buildSummaryTranscript(child.session.sessionManager.getBranch());
        if (branchText.trim()) {
          const existingConfig = await readConfig();
          const titleProfile = existingConfig.childInherit ? profile : existingConfig.profiles.find((item) => item.id === existingConfig.childProfileId) ?? profile;
          const { titleModelRegistry, titleModel } = memoizedTitleRuntime(titleProfile, () => {
            const titleAuthStorage = AuthStorage.inMemory();
            const titleModelRegistry = ModelRegistry.inMemory(titleAuthStorage);
            return { titleModelRegistry, titleModel: registerProfileModel(titleAuthStorage, titleModelRegistry, titleProfile, true) };
          });
          const summary = await generateSubagentSummary(titleModelRegistry, titleModel, branchText);
          if (summary.trim()) return { summary: summary.trim() };
        }
      } catch {
        // Title-model fallback is best-effort; if it fails the result stays
        // empty and markEmpty produces the correct status.
      }
    }

    return { summary: result.summary ?? "" };
  } finally {
    unsubscribe();
    context.signal.removeEventListener("abort", abortChild);
    if (context.signal.aborted) await child.session.abort().catch(() => undefined);
    await shutdownSessionRecord(child);
  }
}

async function profileFor() {
  const config = await readConfig();
  return config.profiles.find((profile) => profile.id === config.activeProfileId) ?? config.profiles[0];
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
    await dispatchSessionAction(record, resolvedMode, async () => {
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
  // Reset the waiting flag after ANY response (steer included). The flag was
  // set when the previous turn ended; the model just responded again, so the
  // "waiting" state is stale. waitForSubagentsBeforeConclusion is only safe
  // when the model is NOT still streaming (it calls session.prompt, which the
  // SDK rejects with "already processing" during an active run) — defer to
  // the agent_end handler for streaming sessions.
  if (record.subagents) {
    record.waitingForSubagents = false;
    if (!record.session.isStreaming) {
      await waitForSubagentsBeforeConclusion(record, knownTaskIds, activeBefore, promptAbortEpoch);
    }
  }
  return record;
}

export async function summarizeSessionTitle(id: string, task: string) {
  const existingConfig = await readConfig();
  const existingTitle = existingConfig.sessionTitles[id]?.trim();
  if (existingTitle) return { title: existingTitle, sessions: (await listSessions()).filter((session) => !session.archived) };
  // Resolve the title model WITHOUT materializing a session runtime. Callers
  // fire title backfills concurrently with the session's first prompt: both
  // used to land on the same shared creation promise, and the title call —
  // believing it owned the record — tore it down mid-prompt. A live record's
  // own profile is used when present; otherwise the same active-profile
  // resolution a fresh creation would use.
  const live = sessions.get(id);
  const sessionProfile = live?.profile ?? await profileFor();
  const titleProfile = existingConfig.childInherit
    ? sessionProfile
    : existingConfig.profiles.find((item) => item.id === existingConfig.childProfileId) ?? sessionProfile;
  const { titleModelRegistry, titleModel } = memoizedTitleRuntime(titleProfile, () => {
    const titleAuthStorage = AuthStorage.inMemory();
    const titleModelRegistry = ModelRegistry.inMemory(titleAuthStorage);
    return { titleModelRegistry, titleModel: registerProfileModel(titleAuthStorage, titleModelRegistry, titleProfile, true) };
  });
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
  const maxConcurrentSubagents = clampConcurrency(Number(value) || 3);
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
  try {
    // Replay state that may have happened before an SSE reconnect, especially
    // an approval request that is still holding the agent at a guarded tool
    // call. If any replay step throws, the listener must come off again —
    // the route's cleanup would otherwise hold a default no-op and every
    // reconnect would leak another listener onto the emitter.
    if (record.waitingForSubagents) onEvent({ type: "session_state", state: "waiting_for_subagents" });
    else if (record.session.isStreaming) onEvent({ type: "session_state", state: "running" });
    else onEvent({ type: "session_state", state: "idle" });
    onEvent({ type: "usage", usage: usageFromRecord(record) });
    for (const task of record.subagents?.list() ?? []) onEvent({ type: "subagent_snapshot", task });
    for (const finding of await record.evidenceStore.list()) onEvent({ type: "finding", finding });
    for (const request of record.gate.pendingRequests()) onEvent({ type: "approval_required", approval: request });
    for (const request of record.subagents?.pendingApprovals() ?? []) onEvent({ type: "approval_required", approval: request });
  } catch (error) {
    record.emitter.off("event", onEvent);
    throw error;
  }
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

export async function cancelSubagent(id: string, taskId: string) {
  const record = await getOrCreateSession(id);
  return record.subagents?.cancel(taskId) ?? false;
}

export async function retrySubagent(id: string, taskId: string) {
  const record = await getOrCreateSession(id);
  return await record.subagents?.retry(taskId) ?? null;
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
  const switched = await withProfileSwitchLock(record, () => switchSessionProfile(record, profile, {
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
