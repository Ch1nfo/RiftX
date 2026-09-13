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
  type BashToolOptions,
  type ToolDefinition
} from "@mariozechner/pi-coding-agent";
import type { Api, Model } from "@mariozechner/pi-ai";
import { readConfig, getAppPaths, updateConfig } from "@/server/config-store";
import { RiftxError } from "@/server/errors";
import { clampConcurrency, type AppConfig, type ApprovalMode, type ArchivedSession, type ModelProfile, type RiftxEvent, type SessionSummary } from "@/lib/types";
import { ApprovalGate } from "./approval-gate";
import { createPermissionExtension, isGuardedTool } from "./permission-extension";
import { preparePromptDispatch } from "@/lib/prompt-mode";
import { normalizeContextUsage } from "./usage";
import { buildChildPentestSystemPrompt, buildPentestSystemPrompt } from "./system-prompt";
import { evaluateApproval } from "./approval-evaluator";
import { createBrowserExtension, BrowserManager } from "@/browser";
import { MutationLock } from "./mutation-lock";
import { SubagentManager, type SubagentRunnerContext } from "./subagent-manager";
import { generateSessionTitle } from "./session-title";
import { generateSubagentSummary } from "./subagent-summary";
import { getEvidenceStore, removeEvidence } from "./evidence-store";
import { installContextUsageTracking } from "./context-usage";
import { estimateCompactedUsage, installMidTurnCompaction } from "./mid-turn-compaction";
import { waitForSubagentsBeforeConclusion } from "./session-join";
import { setAgentTransport } from "./pi-internals";
import { activeSkillNamesFromBranch, loadSkillContext, prepareSkillPrompt, updateActiveSkills, type SkillDescriptor } from "./skill-router";
import { installReportSkillContextScope, PENTEST_REPORT_SKILL_NAME } from "./report-skill";
import { createTimedBashTool } from "./bash-timeout";
import { createTimedLocalTools } from "./local-tool-timeout";
import { createCrawlTool } from "@/browser/tools/crawl";
import { sessionToolNames } from "@/server/session-tools";
import { composeAttachmentText, type PromptAttachment, type PromptImage } from "@/lib/attachments";
import { withMcpReferences, type McpServerEntry } from "@/server/mcp/manager";
import { buildMcpTools } from "@/server/mcp/tools";
import { BashConcurrency } from "./bash-concurrency";
import { abortSessionRecord, shutdownSessionRecord } from "./session-shutdown";
import { switchSessionProfile, withProfileSwitchLock } from "./apply-session-profile";
import { registerTrackedProfile, registerProfileModel, sdkThinkingLevel, restoreProviderRegistration, memoizedTitleRuntime, type ProviderRegistrations } from "./model-registration";
import { extractLastAssistantResult, buildSummaryTranscript } from "./subagent-result";
import { buildInvestigationCapsule } from "./investigation-capsule";
import { refreshContinuityContext, type ContinuityContext } from "./continuity-context";
import { buildTaskContract, userRequestsFromBranch } from "./task-contract";
import { buildProgressCheckpointContext, progressCheckpointFromBranch, type ProgressCheckpoint } from "./progress-checkpoint";
import { createPentestCompactionExtension } from "./pentest-compaction";
import { archivedRestoreError, classifyArchivedRestore, restoredArchiveState } from "./session-archive";
import { sessions, sessionCreation, RUNTIME_VERSION, type RuntimeDeps, type SessionRecord } from "./session-registry";
import type { FindingSourceInfo } from "./tools/finding-tool";
import { listRunningSessionIds, listSessions, getSessionSnapshot, getSessionMessages as getMessages, summaryName, usageFromRecord, listWorkspaceSessionInfos, findTranscriptImage } from "./session-snapshot";
import { createToolOutputStore, listToolArtifacts, toolArtifactDir } from "@/server/tool-output";
import { beginPromptRequest, promptRequestStates as requestStatesFor, settlePromptRequest } from "./prompt-requests";
import { BenchmarkController, BenchmarkError } from "@/server/benchmark/controller";
import { BenchmarkWarningDelivery } from "@/server/benchmark/warning-delivery";
import { BenchmarkLedger, BENCHMARK_MAX_SUBAGENTS } from "@/server/benchmark/ledger";
import { createBenchmarkControlTool } from "@/server/benchmark/tools/control-tool";
import { persistBenchmarkEvidenceRef } from "@/server/benchmark/evidence";
import { createBenchmarkChildHandoff } from "@/server/benchmark/child-handoff";
import { createAssignBenchmarkChallengeTool } from "@/server/benchmark/tools/assign-tool";
import { buildBenchmarkContinuity } from "@/server/benchmark/continuity";
import { buildBenchmarkCompactionFallback } from "@/server/benchmark/compaction-fallback";
import { blockBenchmarkSampling, clearBenchmarkCompactionFailure } from "./compaction-budget";
import { installPasswordEnumerationBudget, installBenchmarkRepeatNotice } from "@/server/benchmark/effort";
import { checkBenchmarkToolExecutionGuard, installBenchmarkTimeboxGate } from "@/server/benchmark/timebox";
import { abortBenchmarkAttempt, startBenchmarkAttemptWatchdog } from "@/server/benchmark/attempt-watchdog";
import { benchmarkWorkspaceRoot, BenchmarkWorkspace, createWorkspaceLocalTools, benchmarkMutationLock, challengeDirectory } from "@/server/benchmark/workspace";
import { createChallengeSkillSelection } from "@/server/benchmark/challenge-skills";

type BenchmarkRuntime = { controller: BenchmarkController; ledger: BenchmarkLedger };

function benchmarkRuntimeCache() {
  const registry = globalThis as typeof globalThis & { __riftxBenchmark?: Map<string, BenchmarkRuntime> };
  return registry.__riftxBenchmark ?? (registry.__riftxBenchmark = new Map<string, BenchmarkRuntime>());
}

/** The headless runner observes the same authoritative ledger as the tools. */
export function getBenchmarkRuntime(id: string) {
  return benchmarkRuntimeCache().get(id);
}

export async function closeBenchmarkSession(id: string) {
  const record = sessions.get(id);
  if (record) {
    await shutdownSessionRecord(record);
    sessions.delete(id);
  }
  const runtime = benchmarkRuntimeCache().get(id);
  if (runtime) await archiveBenchmarkRuntime(runtime);
  benchmarkRuntimeCache().delete(id);
}

function benchmarkPlatformIsGone(error: unknown) {
  return error instanceof BenchmarkError
    && (error.kind === "not_found" || error.kind === "challenge_not_found" || error.kind === "invalid_state_task_ended");
}

function benchmarkContainerIsLive(status: string) {
  return status === "available" || status === "pending" || status === "stop_pending";
}

async function benchmarkRuntimeForCleanup(id: string): Promise<BenchmarkRuntime | undefined> {
  const cached = benchmarkRuntimeCache().get(id);
  if (cached) return cached;
  if (!await BenchmarkLedger.exists(id)) return undefined;
  if (!process.env.BENCHMARK_BASE_URL || !process.env.BENCHMARK_TOKEN) {
    throw new RiftxError("This session still has benchmark state. Set BENCHMARK_BASE_URL and BENCHMARK_TOKEN so RiftX can close its containers.", "BENCHMARK_CONFIG_REQUIRED", 409);
  }
  const runtime = { controller: new BenchmarkController(), ledger: await new BenchmarkLedger(id).initialize() };
  benchmarkRuntimeCache().set(id, runtime);
  return runtime;
}

async function archiveBenchmarkRuntime(runtime: BenchmarkRuntime) {
  let liveCodes = new Set<string>();
  try {
    const platform = await runtime.controller.listChallenges();
    const current = runtime.ledger.getState();
    await runtime.ledger.syncFromPlatform(platform, current.vpnOk, current.vpnClientIp, current.vpnChecked);
    liveCodes = new Set(platform.filter((challenge) => benchmarkContainerIsLive(challenge.container_status)).map((challenge) => challenge.unique_code));
  } catch (error) {
    if (!benchmarkPlatformIsGone(error)) throw error;
  }

  const cleanupCodes = Object.values(runtime.ledger.getState().challenges)
    .filter((challenge) => liveCodes.has(challenge.uniqueCode)
      || challenge.owner !== null
      || challenge.currentAttemptStartedAt !== null
      || challenge.status === "reserved"
      || challenge.status === "running"
      || challenge.status === "closing"
      || challenge.status === "orphaned")
    .map((challenge) => challenge.uniqueCode);
  const results = await Promise.allSettled(cleanupCodes.map((uniqueCode) => runtime.ledger.runChallengeAction(uniqueCode, async () => {
    const closeRequired = liveCodes.has(uniqueCode);
    await runtime.ledger.releaseForSessionCleanup(uniqueCode, "session archived", closeRequired);
    if (closeRequired) {
      try {
        await runtime.controller.closeChallenge(uniqueCode);
      } catch (error) {
        if (!benchmarkPlatformIsGone(error)) {
          await runtime.ledger.markCloseFailed(uniqueCode).catch(() => undefined);
          throw error;
        }
      }
      const challenge = runtime.ledger.getChallenge(uniqueCode);
      if (challenge?.status === "closing" && challenge.pendingStatus) await runtime.ledger.confirmClosed(uniqueCode);
    }
  })));
  const failed = results.filter((result) => result.status === "rejected");
  if (failed.length) {
    throw new RiftxError(`Could not close ${failed.length} benchmark container(s); retry archive after connectivity recovers`, "BENCHMARK_CONTAINER_CLOSE_FAILED", 409);
  }
}

async function deleteBenchmarkRuntime(runtime: BenchmarkRuntime) {
  let platform;
  try {
    platform = await runtime.controller.listChallenges();
    const current = runtime.ledger.getState();
    await runtime.ledger.syncFromPlatform(platform, current.vpnOk, current.vpnClientIp, current.vpnChecked);
  } catch (error) {
    if (benchmarkPlatformIsGone(error)) return;
    throw error;
  }
  const liveCodes = platform.filter((challenge) => benchmarkContainerIsLive(challenge.container_status)).map((challenge) => challenge.unique_code);
  const results = await Promise.allSettled(liveCodes.map((uniqueCode) => runtime.ledger.runChallengeAction(uniqueCode, async () => {
    try {
      await runtime.controller.closeChallenge(uniqueCode);
    } catch (error) {
      if (!benchmarkPlatformIsGone(error)) throw error;
    }
  })));
  const failed = results.filter((result) => result.status === "rejected");
  if (failed.length) {
    throw new RiftxError(`Could not close ${failed.length} benchmark container(s); retry deletion after connectivity recovers`, "BENCHMARK_CONTAINER_CLOSE_FAILED", 409);
  }
}

// Facade re-exports: the API routes import everything from this module.
export { listRunningSessionIds, listSessions, getSessionSnapshot };
export async function getSessionMessages(id: string) {
  return getMessages(() => getOrCreateSession(id));
}

/** Explicit request states for reconnect reconciliation. Absence is unknown,
 * never an implicit success signal. */
export function promptRequestStates(id: string) {
  const record = sessions.get(id);
  return record ? requestStatesFor(record) : {};
}

/** Compatibility projection for clients from the first attachment revision. */
export function failedPromptRequestIds(id: string) {
  return Object.entries(promptRequestStates(id)).filter(([, state]) => state === "failed").map(([key]) => key);
}

/** Resolves one transcript image on demand; materializes the record like /messages does. */
export async function getTranscriptImage(id: string, ref: string) {
  const record = await getOrCreateSession(id);
  return findTranscriptImage(record, ref);
}
import { deliverSubagentCompletion, dispatchSessionAction, enqueueSessionAction, undeliveredTerminalTasks } from "./session-join";

/** SSE diets: drop image parts (the UI renders screenshots via their id-based URL) so a
 * single capture no longer ships its full base64 payload through the stream. */
function withoutImageParts<T>(result: T): T {
  const record = result as unknown as { content?: unknown };
  if (!result || typeof result !== "object" || !Array.isArray(record.content)) return result;
  const content = record.content
    .filter((part) => !(part && typeof part === "object" && (part as { type?: string }).type === "image"));
  return { ...(result as object), content } as T;
}

function eventPayload(event: AgentSessionEvent): RiftxEvent {
  const base = event as unknown as Record<string, unknown>;
  if (event.type === "message_update") {
    const assistant = base.assistantMessageEvent as Record<string, unknown> | undefined;
    return { type: assistant?.type === "text_delta" ? "text_delta" : assistant?.type === "thinking_delta" ? "thinking_delta" : "message", delta: assistant?.delta ?? "" };
  }
  if (event.type === "tool_execution_start") {
    const guarded = isGuardedTool(String(base.toolName));
    return { type: "tool_start", toolName: base.toolName, toolCallId: base.toolCallId, args: base.args, toolStatus: guarded ? "queued" : "running" } as RiftxEvent;
  }
  if (event.type === "tool_execution_update") {
    // The runtime's AgentToolUpdateCallback payload is exposed as `partialResult`.
    // Reading the old `update` name turns every streamed tool update into
    // undefined, which the UI then renders literally after approval.
    return { type: "tool_update", toolName: base.toolName, toolCallId: base.toolCallId, update: base.partialResult ?? base.update } as RiftxEvent;
  }
  if (event.type === "tool_execution_end") return { type: "tool_end", toolName: base.toolName, toolCallId: base.toolCallId, result: withoutImageParts(base.result), isError: base.isError } as RiftxEvent;
  if (event.type === "agent_start") return { type: "session_state", state: "running" };
  if (event.type === "agent_end") return { type: "done" };
  if (event.type === "turn_end") return { type: "message", message: base.message, toolResults: Array.isArray(base.toolResults) ? base.toolResults.map((item) => withoutImageParts(item)) : base.toolResults, turnEnd: true } as RiftxEvent;
  if (event.type === "auto_retry_start") return { type: "session_state", state: "retrying", attempt: base.attempt, error: base.errorMessage } as RiftxEvent;
  if (event.type === "compaction_start") return { type: "session_state", state: "compacting", reason: base.reason } as RiftxEvent;
  if (event.type === "compaction_end") return { type: "session_state", state: "running", reason: base.reason } as RiftxEvent;
  return { type: "message", message: base };
}


type CreateRuntimeSessionOptions = {
  profile: ModelProfile;
  cwd: string;
  gate: ApprovalGate;
  /** Subagent children reuse the parent's locks, concurrency limiter, and evidence store. */
  child?: boolean;
  sessionManagerOverride?: AgentSessionManager;
  mutationLock?: MutationLock;
  bashConcurrencyOverride?: BashConcurrency;
  runtimeDeps?: RuntimeDeps;
  findingSource?: FindingSourceInfo;
};

async function createRuntimeSession(options: CreateRuntimeSessionOptions) {
  const config = await readConfig();
  // The MCP references must be released if construction throws partway: no
  // session record exists yet, so shutdown would never release them.
  return withMcpReferences(config.mcpServers, (mcpEntries) => buildRuntimeSession(options, config, mcpEntries));
}

async function buildRuntimeSession(options: CreateRuntimeSessionOptions, config: AppConfig, mcpEntries: McpServerEntry[]) {
  const { profile, cwd, gate, child = false, sessionManagerOverride, mutationLock = new MutationLock(), bashConcurrencyOverride, runtimeDeps, findingSource = { source: "main" } } = options;
  const paths = getAppPaths();
  await mkdir(paths.agent, { recursive: true, mode: 0o700 });
  const authStorage = AuthStorage.create(join(paths.agent, "auth.json"));
  const modelRegistry = ModelRegistry.create(authStorage, join(paths.agent, "models.json"));
  const providerRegistrations: ProviderRegistrations = new Map();
  const model = registerTrackedProfile(providerRegistrations, authStorage, modelRegistry, profile, true);

  const bashConcurrency = bashConcurrencyOverride ?? new BashConcurrency((process.env.BENCHMARK_BASE_URL && process.env.BENCHMARK_TOKEN ? BENCHMARK_MAX_SUBAGENTS : config.maxConcurrentSubagents) + 1);
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
  // Deliberately let + separate assignment: closures below forward-reference
  // `record` before it is built, and read it only after assignment.
  // eslint-disable-next-line prefer-const
  let record: SessionRecord | undefined;
  const childProfile = config.childInherit ? profile : config.profiles.find((item) => item.id === config.childProfileId) ?? profile;
  // Keep title work on the configured child profile when available so it does
  // not consume the main Agent's provider quota during a live turn.
  const settingsManager = SettingsManager.create(cwd, paths.agent);
  settingsManager.setTransport(profile.transport);
  const sessionManager = sessionManagerOverride ?? AgentSessionManager.create(cwd, child ? join(paths.subagents, "runtime") : paths.sessions);
  const initialBranch = sessionManager.getBranch();
  const activeSkillNames = new Set(activeSkillNamesFromBranch(initialBranch));
  const progressCheckpoint: ProgressCheckpoint | undefined = progressCheckpointFromBranch(initialBranch);
  const evidenceSessionId = runtimeDeps?.evidenceSessionId ?? sessionManager.getSessionId();
  const outputStore = createToolOutputStore(paths.artifacts, evidenceSessionId, child ? findingSource.subagentId : undefined);
  const mcpTools = mcpEntries.flatMap((entry) => buildMcpTools(entry, { audience: child ? "child" : "main", outputStore }));
  // Benchmark tools: only for sessions that have BENCHMARK_BASE_URL configured.
  // Controller and Ledger are cached on globalThis so a WebUI restart reuses the
  // SAME instances (the ledger file is per-parent-session; a second in-memory
  // instance would overwrite it). Children receive the shared runtime via
  // RuntimeDeps; restart recovery also finds it through this global cache.
  const benchmarkEnv = process.env.BENCHMARK_BASE_URL && process.env.BENCHMARK_TOKEN;
  const workspaceRoot = benchmarkWorkspaceRoot(cwd, process.env.BENCHMARK_BASE_URL ?? "");
  const cachedBenchmark = benchmarkEnv ? benchmarkRuntimeCache().get(evidenceSessionId) : undefined;
  const benchmarkController = benchmarkEnv
    ? runtimeDeps?.benchmark?.controller ?? cachedBenchmark?.controller ?? new BenchmarkController()
    : undefined;
  const benchmarkLedger = benchmarkController
    ? runtimeDeps?.benchmark?.ledger ?? cachedBenchmark?.ledger ?? await new BenchmarkLedger(evidenceSessionId).initialize()
    : undefined;
  if (benchmarkController && benchmarkLedger && evidenceSessionId) {
    benchmarkRuntimeCache().set(evidenceSessionId, { controller: benchmarkController, ledger: benchmarkLedger });
  }
  const browser = new BrowserManager({ evidenceRoot: paths.evidence, evidenceSessionId, scope: { rules: config.browserScope }, ignoreTlsErrors: config.browserIgnoreTlsErrors });
  // Child sessions: grant browser scope for the assigned challenge's container
  // addresses on the child's OWN BrowserManager (the parent's manager is a
  // different instance and its grants don't transfer).
  if (child && runtimeDeps?.benchmark?.containerAddrs?.length) {
    for (const addr of runtimeDeps.benchmark.containerAddrs) {
      const normalized = addr.includes("://") ? addr : `http://${addr}/`;
      browser.grantScope(normalized, true);
    }
  } else if (!child && benchmarkLedger) {
    // Archive/reopen rebuilds BrowserManager while retaining the live ledger.
    // Restore exact scopes for the main worker's active container(s).
    for (const challenge of Object.values(benchmarkLedger.getState().challenges)) {
      if (challenge.owner !== "main" || (challenge.status !== "running" && challenge.status !== "reserved")) continue;
      for (const addr of challenge.containerAddrs) {
        const normalized = addr.includes("://") ? addr : `http://${addr}/`;
        browser.grantScope(normalized, true);
      }
    }
  }
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
  // Benchmark branch: hard-code the subagent scheduler to the benchmark's
  // fixed concurrency, regardless of the user's general setting.
  const subagentConcurrency = benchmarkController ? BENCHMARK_MAX_SUBAGENTS : config.maxConcurrentSubagents;
  const subagents = !child ? new SubagentManager(sessionManager.getSessionId(), paths.subagents, (event) => emitter.emit("event", event), subagentConcurrency, config.approvalMode, subagentNameGenerator) : undefined;
  const getChildProfile = () => config.childInherit ? (record?.profile ?? profile) : childProfile;
  // Same forward-closure pattern as `record`: the finding tool reads this
  // lazily, after the session below has been created.
  // eslint-disable-next-line prefer-const
  let evidenceSession: AgentSession | undefined;
  let skills: SkillDescriptor[] = [];
  const benchmarkOwner: "main" | `subagent:${string}` = child ? `subagent:${findingSource.subagentId ?? "child"}` : "main";
  const initialChallenge = benchmarkLedger?.budgetForOwner(benchmarkOwner)?.challenge;
  const workspace = benchmarkLedger ? new BenchmarkWorkspace(workspaceRoot, initialChallenge?.uniqueCode, () => browser.run(() => browser.close())) : undefined;
  let selectChallengeSkills: ((description?: string) => Promise<void>) | undefined;
  const benchmarkTools: ToolDefinition[] = benchmarkController && benchmarkLedger
    ? [createBenchmarkControlTool(
        benchmarkController,
        benchmarkLedger,
        browser,
        () => benchmarkOwner,
        child ? runtimeDeps?.benchmark?.assignedChallenge : undefined,
        async (challenge) => {
          await workspace!.activate(challenge.uniqueCode);
          await selectChallengeSkills!(challenge.description);
        },
        async () => {
          await selectChallengeSkills!();
          await workspace!.activate();
        },
        async (reference, uniqueCode) => {
          const runDirectory = join(paths.root, "benchmark", evidenceSessionId);
          const browserEvidenceDirectory = join(paths.evidence, evidenceSessionId);
          return persistBenchmarkEvidenceRef(reference, {
            directory: challengeDirectory(join(runDirectory, "evidence"), uniqueCode),
            cwd: workspace!.cwd,
            allowedRoots: [workspace!.cwd, toolArtifactDir(paths.artifacts, evidenceSessionId), browserEvidenceDirectory,
              join(runDirectory, "evidence"), join(runDirectory, "handoffs")],
            evidenceDirectory: browserEvidenceDirectory,
            browser,
            resolveToolEvidence: (toolCallId) => {
              const entry = evidenceSession?.sessionManager.getBranch().slice().reverse().find((entry) =>
                entry.type === "message" && entry.message.role === "toolResult" && entry.message.toolCallId === toolCallId);
              if (entry?.type !== "message" || entry.message.role !== "toolResult") return undefined;
              const details = entry.message.details as { artifactPath?: unknown } | undefined;
              return { toolName: entry.message.toolName, content: JSON.stringify(entry.message.content),
                ...(typeof details?.artifactPath === "string" ? { artifactPath: details.artifactPath } : {}) };
            }
          });
        }
      )]
    : [];
  const bashOptions: BashToolOptions = {
    commandPrefix: settingsManager.getShellCommandPrefix(),
    shellPath: settingsManager.getShellPath(),
    ...(benchmarkController ? {
      spawnHook: (context) => {
        const env = { ...context.env };
        delete env.BENCHMARK_TOKEN;
        delete env.BENCHMARK_BASE_URL;
        delete env.BENCHMARK_VPN_URL;
        delete env.RIFTX_LLM_API_KEY;
        delete env.RIFTX_CHILD_LLM_API_KEY;
        return { ...context, env };
      }
    } : {})
  };
  const localTools = workspace ? createWorkspaceLocalTools(() => workspace.cwd, bashOptions)
    : [...createTimedLocalTools(cwd), createTimedBashTool(cwd, bashOptions) as ToolDefinition];
  const customTools = [...localTools, ...benchmarkTools, createCrawlTool(browser, outputStore), ...(subagents && benchmarkController && benchmarkLedger ? [createAssignBenchmarkChallengeTool(benchmarkController, benchmarkLedger, async (task, uniqueCode, containerAddrs, reservationOwner) => {
        // Bridge to the existing subagent spawn mechanism. The benchmark
        // metadata (uniqueCode, containerAddrs) is stored on the SubagentTask
        // itself so retry/restart can recover the binding regardless of the
        // taskId→owner mapping.
        const benchmarkRuntime = {
          controller: benchmarkController,
          ledger: benchmarkLedger,
          assignedChallenge: uniqueCode,
          containerAddrs
        };
        let releaseBinding!: () => void;
        let rejectBinding!: (error: unknown) => void;
        const bindingReady = new Promise<void>((resolve, reject) => {
          releaseBinding = resolve;
          rejectBinding = reject;
        });
        const submitted = subagents.submitTask(task, async (context) => {
          // The queue can start immediately. Do not create the child runtime
          // until the ledger owner is the real task id, otherwise a fast first
          // checkpoint/submit races the temporary reservation owner.
          await bindingReady;
          return runChildSession(getChildProfile(), cwd, mutationLock, bashConcurrency, context, { evidenceStore, evidenceSessionId, benchmark: benchmarkRuntime });
        });
        void submitted.promise.catch(() => undefined);
        if (submitted.duplicate) {
          releaseBinding();
          return { taskId: submitted.task?.id, duplicate: true };
        }
        if (!submitted.task) {
          const error = new Error(`Could not create a SubAgent task for ${uniqueCode}`);
          rejectBinding(error);
          throw error;
        }
        try {
          await subagents.setBenchmarkBinding(submitted.task.id, uniqueCode, containerAddrs);
          await benchmarkLedger.bindOwner(uniqueCode, reservationOwner, `subagent:${submitted.task.id}`);
          releaseBinding();
          // A cancel that landed during binding saw the owner as the temporary
          // reservation, so its completion cleanup found nothing to release.
          // If the task is already terminal, undo the binding we just wrote —
          // otherwise the challenge stays owned by a task that will never run.
          const taskStatus = submitted.task.status;
          if (taskStatus !== "queued" && taskStatus !== "running") {
            // Same two-phase release as the completion handler. The caller
            // already holds this challenge's network-action serializer.
            let released = false;
            try {
              await benchmarkLedger.releaseOnSubagentExit(uniqueCode, `subagent task ${taskStatus} during binding`, `subagent:${submitted.task.id}`);
              released = true;
              if (benchmarkLedger.getChallenge(uniqueCode)?.status === "closing") {
                await benchmarkController.closeChallenge(uniqueCode);
                await benchmarkLedger.confirmClosed(uniqueCode);
              }
            } catch {
              // A failed platform close is a tracked leak; an owner mismatch
              // means the completion handler's cleanup already released it.
              if (released) await benchmarkLedger.markCloseFailed(uniqueCode).catch(() => undefined);
            }
            return { taskId: submitted.task.id, duplicate: false, cancelled: true };
          }
        } catch (error) {
          rejectBinding(error);
          throw error;
        }
        return { taskId: submitted.task?.id, duplicate: submitted.duplicate };
      })] : []),
      // spawn_subagent is NOT created on the benchmark branch — only
      // assign_benchmark_challenge can dispatch, enforcing reservation,
      // container limits, and one-worker-one-challenge.
      ...mcpTools];
  const browserExtension = createBrowserExtension({ evidenceRoot: paths.evidence, evidenceSessionId }, browser);
  const compactionExtension = createPentestCompactionExtension({
    getSession: () => evidenceSession,
    modelRegistry,
    getActiveSkills: () => [...activeSkillNames],
    benchmarkFallback: benchmarkLedger ? {
      buildSummary: (maxChars) => buildBenchmarkCompactionFallback({
        ledger: benchmarkLedger, worker: benchmarkOwner, workingDirectory: workspace?.cwd ?? cwd,
        assignedChallenge: child ? runtimeDeps?.benchmark?.assignedChallenge : undefined,
        ledgerFile: join(paths.root, "benchmark", evidenceSessionId, "state.json"),
        sessionFile: sessionManager.getSessionFile()
      }, maxChars),
      getContinuityContext: () => getContinuityContext(true),
      onError: (error) => {
        if (evidenceSession) blockBenchmarkSampling(evidenceSession, error);
        emitter.emit("event", { type: "error", error: error.message });
      }
    } : undefined
  });
  const resourceLoader = new DefaultResourceLoader({
    cwd,
    agentDir: paths.agent,
    additionalSkillPaths: [paths.skills],
    extensionFactories: [permission, browserExtension, compactionExtension],
    noExtensions: true,
    // Disable default SDK search paths; additionalSkillPaths above remains enabled.
    noSkills: true,
    // `pentest-report` is a reserved opt-in skill. Force the policy even for
    // an older user-installed copy whose frontmatter predates the flag.
    skillsOverride: ({ skills, diagnostics }) => ({
      skills: skills.map((skill) => skill.name === PENTEST_REPORT_SKILL_NAME
        ? { ...skill, disableModelInvocation: true }
        : skill),
      diagnostics
    }),
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
    thinkingLevel: sdkThinkingLevel(profile.thinkingLevel),
    // Hard whitelist (see src/server/session-tools.ts): the SDK silently
    // drops any tool — built-in or custom — whose name is absent here.
    tools: [...sessionToolNames(Boolean(subagents)), ...mcpTools.map((tool) => tool.name)],
    customTools,
    resourceLoader,
    sessionManager,
    settingsManager
  });
  evidenceSession = result.session;
  const warningDelivery = benchmarkLedger ? new BenchmarkWarningDelivery(benchmarkLedger, benchmarkOwner) : undefined;
  if (benchmarkController) {
    const stream = result.session.agent.streamFn;
    // The SDK otherwise caps its default request budget at 32k, even for larger profiles.
    result.session.agent.streamFn = (model, context, options) => {
      warningDelivery?.sample(context.messages);
      return stream(model, context, { ...options, maxTokens: model.maxTokens });
    };
  }
  skills = resourceLoader.getSkills().skills as SkillDescriptor[];
  if (benchmarkLedger) {
    selectChallengeSkills = createChallengeSkillSelection(skills, activeSkillNames, (content) => {
      sessionManager.appendCustomMessageEntry("riftx_skill_context", content, false);
    });
    await selectChallengeSkills(initialChallenge?.description);
  }
  const skillContextCache = new Map<string, string>();
  const activeSkillContext = async () => {
    const parts = await Promise.all([...activeSkillNames].map(async (name) => {
      const cached = skillContextCache.get(name);
      if (cached !== undefined) return cached;
      const skill = skills.find((candidate) => candidate.name === name);
      if (!skill) return "";
      try {
        const context = await loadSkillContext(skill);
        skillContextCache.set(name, context);
        return context;
      } catch {
        return "";
      }
    }));
    return parts.filter(Boolean).join("\n\n");
  };
  const getContinuityContext = async (preview = false): Promise<ContinuityContext> => {
    if (!preview) warningDelivery?.prepare();
    // Budget inspection must not change workspaces or consume one-shot warnings.
    if (!preview && benchmarkLedger && workspace) {
      const active = benchmarkLedger.budgetForOwner(benchmarkOwner)?.challenge;
      if (await workspace.reconcile(active?.uniqueCode)) await selectChallengeSkills!(active?.description);
    }
    const skillContext = await activeSkillContext();
    // Benchmark continuity uses the ledger and cached active skills, without
    // scanning findings/artifacts on every provider request.
    if (benchmarkLedger) {
      const worker = child ? `subagent:${findingSource.subagentId ?? "child"}` as const : "main" as const;
      const warning = benchmarkLedger.attemptWarningFor(worker);
      const investigationCapsule = buildBenchmarkContinuity(benchmarkLedger, worker, warning, workspace?.cwd);
      if (!preview) warningDelivery?.prepare(warning, investigationCapsule);
      return {
        taskContract: "",
        skillContext,
        investigationCapsule,
        progressCheckpoint: ""
      };
    }
    const findings = await evidenceStore.list();
    const relevantFindings = child
      ? findings.filter((finding) => finding.source === "main" || finding.subagentId === findingSource.subagentId)
      : findings;
    const artifacts = await listToolArtifacts(paths.artifacts, evidenceSessionId);
    return {
      taskContract: buildTaskContract(userRequestsFromBranch(sessionManager.getBranch()), { cwd, browserScope: config.browserScope }),
      skillContext,
      investigationCapsule: buildInvestigationCapsule(relevantFindings, subagents?.list() ?? [], artifacts, browser.continuitySnapshot()),
      progressCheckpoint: buildProgressCheckpointContext(progressCheckpoint)
    };
  };
  const refreshContinuity = async (includeTaskContract = true) => {
    const continuity = await getContinuityContext();
    if (!includeTaskContract) continuity.taskContract = "";
    refreshContinuityContext(result.session, continuity);
  };
  // installMidTurnCompaction now refreshes continuity on every sampling call;
  // recordCompaction must only fire when a REAL compaction occurred. Split the
  // two concerns: the mid-turn hook returns whether it compacted, and we
  // increment only in that case via the compaction_end event handler (below).
  // Sampling-time continuity refresh is a benchmark need (dynamic budgets,
  // live ownership). Ordinary pentest sessions keep the cheaper contract:
  // continuity is rebuilt only after a real compaction.
  installMidTurnCompaction(result.session, getContinuityContext, { samplingRefresh: Boolean(benchmarkLedger) });
  // Install after compaction so the final context sent to the provider drops
  // stale report-skill messages unless the current user request asks for one.
  installReportSkillContextScope(result.session);
  installContextUsageTracking(result.session);
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
      // Benchmark timeboxes are enforcement, not prompt decoration. Let an
      // already-running call finish, then reject subsequent solving calls
      // until the worker records real progress or yields the challenge.
      // Locks are acquired at EXECUTION time (not beforeToolCall) to avoid
      // the SDK parallel-executor deadlock: beforeToolCall handlers all run
      // before any execution starts, so a shared holder (bash) would never
      // release while an exclusive waiter (write) is stuck in pre-processing.
      // The execute wrapper acquires and releases around the real execute.
      const fileLock = () => benchmarkLedger && workspace ? benchmarkMutationLock(benchmarkLedger, workspace.cwd) : mutationLock;
      if (benchmarkLedger) installPasswordEnumerationBudget(tool, benchmarkLedger, benchmarkOwner);
      if (tool.name === "bash" && typeof tool.execute === "function") {
        const original = tool.execute.bind(tool);
        tool.execute = async (toolCallId: string, params: unknown, signal?: AbortSignal, ...rest: unknown[]) => {
          const bashRelease = await bashConcurrency.acquire(signal);
          let mutationRelease: (() => void) | undefined;
          try {
            mutationRelease = await fileLock().acquireShared(signal);
          } catch (error) {
            bashRelease();
            throw error;
          }
          try {
            checkBenchmarkToolExecutionGuard();
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
          const release = await fileLock().acquire(signal);
          try {
            checkBenchmarkToolExecutionGuard();
            return await original(toolCallId, params, signal, ...rest);
          } finally {
            release?.();
          }
        };
      }
      // Check the timebox before file/concurrency locks, but after any pending
      // workspace transition so it uses the current challenge's budget.
      if (benchmarkLedger) installBenchmarkTimeboxGate(tool, benchmarkLedger, benchmarkOwner, child ? runtimeDeps?.benchmark?.assignedChallenge : undefined);
      // A challenge transition waits for this worker's running tools; queued
      // calls from the previous challenge cannot execute in the new directory.
      if (benchmarkLedger) installBenchmarkRepeatNotice(tool, benchmarkLedger, benchmarkOwner);
      workspace?.install(tool);
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
    mcpEntries,
    evidenceStore,
    runtimeVersion: RUNTIME_VERSION,
    aborting: false,
    abortEpoch: 0,
    waitingForSubagents: false,
    compacting: false,
    deliveredSubagentResults: new Set(),
    deliveringSubagentResults: new Set(),
    skills,
    activeSkillNames,
    progressCheckpoint,
    providerRegistrations,
    loadedSkills: new Set([...activeSkillNames].filter((name) => name !== PENTEST_REPORT_SKILL_NAME)),
    unsubscribe: () => undefined
  };
  const unsubscribe = result.session.subscribe((event) => {
    if (event.type === "message_end") {
      void warningDelivery?.complete(event.message).catch(() => undefined);
    }
    if (event.type === "compaction_start") record.compacting = true;
    else if (event.type === "compaction_end") {
      record.compacting = false;
      // Increment the REAL compaction counter only when a compaction occurred.
      if (benchmarkLedger && event.result) {
        const details = event.result.details as { riftx?: { fallback?: unknown } } | undefined;
        void benchmarkLedger.recordCompaction(Boolean(details?.riftx?.fallback)).catch(() => undefined);
      }
      // Covers ordinary end-of-turn/manual compaction. Mid-turn compaction
      // also refreshes its detached sampling array inside the transform hook.
      // Serialized on the prompt chain so the continuity splice can never
      // interleave with a running SDK turn.
      void enqueueSessionAction(record, refreshContinuity).catch((error) => {
        console.warn("RiftX could not refresh continuity context after compaction:", error);
      });
    }
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
      if (hasActive && !benchmarkLedger) {
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
      : event.type === "compaction_end"
        ? { type: "session_state", state: result.session.isStreaming ? "running" : "idle", reason: event.reason }
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
    if (benchmarkController && benchmarkLedger) {
      record.prepareSubagentCompletion = createBenchmarkChildHandoff({ ledger: benchmarkLedger, controller: benchmarkController });
    }
    subagents.setCompletionHandler(async (task, childResult) => {
      // Saving does not start a model turn, so it must also run during Stop.
      // A failed save remains pending for the recovery timer or next resume.
      try {
        await record.prepareSubagentCompletion?.(task, childResult.summary);
      } catch (error) {
        console.warn("[benchmark] Completion handoff remains pending", { taskId: task.id }, error);
        return;
      }
      // Every delivery route persists the handoff and reconciles its slot first.
      return deliverSubagentCompletion(record, task, childResult.summary)
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
    await subagents.initialize(async (context) => {
      // Recovery/retry runner: the benchmark metadata is persisted on the
      // SubagentTask itself (benchmarkChallenge / benchmarkContainerAddrs), so
      // retry creates a new taskId but still recovers the binding. If the
      // metadata is absent AND we're in benchmark mode, reject the task —
      // a benchmark-mode child must NEVER run unrestricted.
      const meta = context.task;
      const hasBenchmarkBinding = Boolean(meta.benchmarkChallenge || meta.benchmarkContainerAddrs?.length);
      if (benchmarkController && benchmarkLedger && !hasBenchmarkBinding) {
        const message = `Benchmark subagent task ${meta.id} lost its challenge binding (no benchmarkChallenge metadata) — cannot run unrestricted. Re-assign the challenge.`;
        console.warn(`RiftX: ${message}`);
        return Promise.reject(new Error(message));
      }
      // Partial binding check: challenge code without container addrs (or vice
      // versa) means the metadata is incomplete — treat as lost binding.
      const hasCode = Boolean(meta.benchmarkChallenge);
      const hasAddrs = Boolean(meta.benchmarkContainerAddrs?.length);
      if (benchmarkController && benchmarkLedger && hasCode !== hasAddrs) {
        const message = `Benchmark subagent task ${meta.id} has partial binding (code=${hasCode}, addrs=${hasAddrs}) — rejecting to avoid unrestricted child.`;
        console.warn(`RiftX: ${message}`);
        return Promise.reject(new Error(message));
      }
      const recoverBenchmark = benchmarkController && benchmarkLedger && meta.benchmarkChallenge
        ? {
            controller: benchmarkController,
            ledger: benchmarkLedger,
            assignedChallenge: meta.benchmarkChallenge,
            containerAddrs: meta.benchmarkContainerAddrs ?? []
          }
        : undefined;
      if (recoverBenchmark) {
        const recoveredUniqueCode = recoverBenchmark.assignedChallenge;
        const owner: `subagent:${string}` = `subagent:${context.task.id}`;
        const recoveryLedger = recoverBenchmark.ledger;
        const recoveryController = recoverBenchmark.controller;
        await recoveryLedger.runChallengeAction(recoveredUniqueCode, async () => {
          // Full-process restart invalidates the local worker lease. Reconcile
          // with the platform before reviving a child so a stopped or already
          // solved container is never resumed from stale task metadata.
          const platform = (await recoveryController.listChallenges()).find((item) => item.unique_code === meta.benchmarkChallenge);
          if (!platform) throw new Error(`Benchmark challenge ${meta.benchmarkChallenge} no longer exists on the platform`);
          const currentState = recoveryLedger.getState();
          await recoveryLedger.syncFromPlatform([platform], currentState.vpnOk, currentState.vpnClientIp, currentState.vpnChecked);
          if (platform.is_completed) throw new Error(`Benchmark challenge ${meta.benchmarkChallenge} is already complete; the recovered child will not rerun it`);
          if (platform.container_status !== "available" || platform.container_addr.length === 0) {
            throw new Error(`Benchmark challenge ${meta.benchmarkChallenge} has no live recoverable container (status=${platform.container_status})`);
          }
          recoverBenchmark.containerAddrs = platform.container_addr;
          await subagents.setBenchmarkBinding(context.task.id, meta.benchmarkChallenge!, platform.container_addr);
          const challenge = recoveryLedger.getChallenge(meta.benchmarkChallenge!);
          if (!challenge) throw new Error(`Benchmark challenge ${meta.benchmarkChallenge} is missing from the recovered ledger`);
          if (challenge.owner === owner && challenge.status === "running") return;
          if (challenge.owner !== null || challenge.status !== "orphaned") {
            throw new Error(`Benchmark challenge ${challenge.uniqueCode} cannot be recovered by ${owner}: status=${challenge.status}, owner=${challenge.owner ?? "none"}`);
          }
          await recoveryLedger.reserve(challenge.uniqueCode, owner, { isSubagent: true });
          await recoveryLedger.confirmStarted(challenge.uniqueCode, recoverBenchmark.containerAddrs, owner);
        });
      }
      return runChildSession(getChildProfile(), cwd, mutationLock, bashConcurrency, context, {
        evidenceStore, evidenceSessionId,
        ...(recoverBenchmark ? { benchmark: recoverBenchmark } : {})
      });
    });
  }
  // Continuity messages are rebuilt from canonical JSONL/findings/task state.
  // The task contract and active skill are needed immediately only when this
  // runtime resumes an already-compacted branch; otherwise avoid duplicating
  // the still-verbatim initial user request and skill message.
  await refreshContinuity(initialBranch.some((entry) => entry.type === "compaction")).catch((error) => {
    console.warn("RiftX could not restore continuity context:", error);
  });
  if (benchmarkLedger && benchmarkController) {
    const watchdog = startBenchmarkAttemptWatchdog({
      ledger: benchmarkLedger,
      controller: benchmarkController,
      owner: benchmarkOwner,
      stopWorker: () => abortBenchmarkAttempt(record),
      isStopping: () => Boolean(record.shutdownPromise || record.aborting),
      warn: async (challenge) => {
        const deadline = benchmarkLedger.budgetFor(challenge.uniqueCode)?.deadlineAt;
        await result.session.sendCustomMessage({
          customType: "riftx_benchmark_attempt_warning",
          content: `Attempt ${challenge.attemptCount} for ${challenge.uniqueCode} has reached 25 minutes. Save a checkpoint now with confirmed progress, durable evidence references, tried approaches, evidence-backed exclusions, and a different candidate nextProbe requiring revalidation. The framework will stop this attempt at ${deadline ? new Date(deadline).toISOString() : "its deadline"} and release its environment.`,
          display: false
        }, { deliverAs: "steer", triggerTurn: false });
      },
      event: (event, attempt) => console.log(JSON.stringify({
        time: new Date().toISOString(), event, worker: benchmarkOwner,
        ...(attempt ? { uniqueCode: attempt.uniqueCode, attempt: attempt.attemptCount, startedAt: attempt.currentAttemptStartedAt } : {})
      }))
    });
    // Terminal results remain undelivered after a transient storage/platform
    // error. Retry even when no model turn arrives to trigger the normal join.
    const retryingHandoffs = new Set<string>();
    const handoffRetryTimer = subagents ? setInterval(() => {
      if (record.shutdownPromise || record.aborting) return;
      for (const task of undeliveredTerminalTasks(record, subagents.list())) {
        if (retryingHandoffs.has(task.id)) continue;
        retryingHandoffs.add(task.id);
        const retry = record.benchmarkHandoffPaused
          ? record.prepareSubagentCompletion?.(task, task.summary) ?? Promise.resolve()
          : deliverSubagentCompletion(record, task, task.summary, { retries: 0 });
        void retry.catch((error) => {
          console.warn("[benchmark] Handoff recovery failed; result remains pending", error);
        }).finally(() => retryingHandoffs.delete(task.id));
      }
    }, 5_000) : undefined;
    handoffRetryTimer?.unref();
    const unsubscribeWithWatchdog = record.unsubscribe;
    record.unsubscribe = () => { clearInterval(handoffRetryTimer); watchdog.dispose(); unsubscribeWithWatchdog(); };
  }
  return record;
}

async function runChildSession(profile: ModelProfile, cwd: string, mutationLock: MutationLock, bashConcurrency: BashConcurrency, context: SubagentRunnerContext, runtimeDeps: RuntimeDeps) {
  const paths = getAppPaths();
  const threadDir = join(paths.subagents, context.task.parentSessionId, context.task.id);
  await mkdir(threadDir, { recursive: true, mode: 0o700 });
  const childSessionManager = AgentSessionManager.create(cwd, threadDir);
  const child = await createRuntimeSession({ profile, cwd, gate: context.gate, child: true, sessionManagerOverride: childSessionManager, mutationLock, bashConcurrencyOverride: bashConcurrency, runtimeDeps, findingSource: { source: "subagent", subagentId: context.task.id } });
  const abortChild = () => {
    child.gate.rejectAll();
    child.session.abortBash();
    void child.browser?.shutdown().catch(() => undefined);
    void child.session.abort().catch(() => undefined);
  };
  let unsubscribe: () => void = () => undefined;
  try {
    context.task.model = `${profile.provider}/${profile.model}`;
    context.updateTaskMeta({ model: context.task.model, threadId: child.id });
    if (context.signal.aborted) {
      throw new Error("Subagent task was cancelled before the child session started.");
    }
    else context.signal.addEventListener("abort", abortChild, { once: true });
    unsubscribe = (() => {
      const listener = (event: RiftxEvent) => context.emit(event);
      child.emitter.on("event", listener);
      return () => child.emitter.off("event", listener);
    })();
    // Match the delegated task and retain its active skill through compaction.
    if (!runtimeDeps.benchmark) {
      const prepared = await prepareSkillPrompt(context.task.task, child.skills, child.loadedSkills);
      updateActiveSkills(child.activeSkillNames, prepared);
      if (prepared.skillContext || (prepared.resetActiveSkills && !prepared.matched.length)) {
        await child.session.sendCustomMessage({ customType: "riftx_skill_context", content: prepared.skillContext, display: false });
      }
    }
    try {
      await child.session.prompt(context.task.task);
    } catch (error) {
      if (!child.benchmarkAttemptTimeoutEpoch) throw error;
    }
    if (child.benchmarkAttemptTimeoutEpoch) {
      return { summary: "Attempt time limit reached. Checkpoint and attempt history were saved for a later retry." };
    }
    const result = extractLastAssistantResult(child.session.sessionManager.getBranch());
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
    const created = await createRuntimeSession({ profile, cwd: currentConfig.cwd, gate: new ApprovalGate(), sessionManagerOverride: sessionManager });
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
  const created = await createRuntimeSession({ profile, cwd: config.cwd, gate: new ApprovalGate() });
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
    usage: usageFromRecord(created),
    running: false
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
      // These sessions are being discarded for a different workspace and will
      // not reopen in this process — drop their benchmark runtimes too. A later
      // reopen gets a fresh ledger whose restart recovery orphans stale
      // challenges instead of resuming workers that no longer exist.
      benchmarkRuntimeCache().delete(id);
    }
    await updateConfig({ cwd });
  }

  const sessionsList = (await listSessions()).filter((session) => !session.archived);
  return { cwd, sessions: sessionsList, activeSessionId: sessionsList[0]?.id ?? "" };
}

type PromptExtras = { images?: PromptImage[]; attachments?: PromptAttachment[]; requestId?: string };
type PromptDispatchHooks = { onAccepted?: (composedText: string) => void; onFailed?: (error: unknown) => void };

async function promptSession(id: string, text: string, mode: "prompt" | "steer" | "followUp" = "prompt", extras: PromptExtras = {}, hooks?: PromptDispatchHooks) {
  const record = await getOrCreateSession(id);
  const dispatch = await preparePromptDispatch(
    mode,
    () => record.session.isStreaming,
    () => getBenchmarkRuntime(id)
      ? Promise.resolve({ prompt: text, skillContext: "", loaded: [] as string[], matched: [] as string[], resetActiveSkills: false })
      : prepareSkillPrompt(text, record.skills, record.loadedSkills),
    () => ({ prompt: text, skillContext: "", loaded: [] as string[], matched: [] as string[], resetActiveSkills: false })
  );
  const resolvedMode = dispatch.mode;
  const ready = dispatch.prepared;
  // Skill delivery is mode-split so the skill body is read exactly once:
  // - prompt/followUp: the hidden custom message carries ready.skillContext
  //   and the persisted user message stays the RAW text + attachments.
  //   (ready.prompt ALSO embeds the skill body — sending it here would make
  //   the model read the full SKILL.md twice and leak it into the bubble.)
  // - steer: no separate persistence channel exists, so the composed prompt
  //   (skill inline + text) goes through the steer queue as before.
  const attachmentBlock = composeAttachmentText(extras.attachments ?? []);
  const finalText = `${resolvedMode === "steer" ? ready.prompt : text}${attachmentBlock}`;
  const images = extras.images?.length ? extras.images.map((image) => ({ type: "image" as const, data: image.data, mimeType: image.mimeType })) : undefined;
  const promptAbortEpoch = record.abortEpoch ?? 0;
  const knownTaskIds = new Set(record.subagents?.list().map((task) => task.id) ?? []);
  const activeBefore = new Set(record.subagents?.list().filter((task) => task.status === "queued" || task.status === "running").map((task) => task.id) ?? []);
  let skillInjected = false;
  let dispatchAccepted = false;
  const acceptDispatch = () => {
    if (dispatchAccepted) return;
    dispatchAccepted = true;
    record.benchmarkHandoffPaused = false;
    // Explicit abstention clears stale guidance; bare continuations preserve it.
    updateActiveSkills(record.activeSkillNames, ready);
    settlePromptRequest(record, extras.requestId, "accepted");
    // Reports the composed text THIS dispatch actually accepted — the single
    // mode resolution above is the only authority on what that text is.
    hooks?.onAccepted?.(finalText);
  };
  try {
    await dispatchSessionAction(record, resolvedMode, async () => {
      if (resolvedMode === "steer") {
        await record.session.steer(finalText, images);
        acceptDispatch();
      }
      else if (resolvedMode === "followUp") {
        record.gate.beginTask();
        if (ready.skillContext || (ready.resetActiveSkills && !ready.matched.length)) {
          await record.session.sendCustomMessage({ customType: "riftx_skill_context", content: ready.skillContext, display: false }, { deliverAs: "followUp" });
          skillInjected = true;
        }
        await record.session.followUp(finalText, images);
        acceptDispatch();
      }
      else {
        record.gate.beginTask();
        if (ready.skillContext || (ready.resetActiveSkills && !ready.matched.length)) {
          await record.session.sendCustomMessage({ customType: "riftx_skill_context", content: ready.skillContext, display: false });
          skillInjected = true;
        }
        let preflightReported = false;
        await record.session.prompt(finalText, {
          images,
          // Pi reports this only after model/auth/compaction/extensions pass,
          // immediately before the agent run starts. This is the acceptance
          // boundary; marking before the call made failed unreachable.
          preflightResult: (success) => {
            preflightReported = true;
            if (success) acceptDispatch();
          }
        });
        // Defensive compatibility with a future SDK that omits the internal
        // hook after resolving successfully.
        if (!preflightReported) acceptDispatch();
      }
    });
  } catch (error) {
    settlePromptRequest(record, extras.requestId, "failed", error instanceof Error ? error.message : String(error));
    if (!dispatchAccepted) hooks?.onFailed?.(error);
    if (!skillInjected) ready.loaded.forEach((name) => record.loadedSkills.delete(name));
    throw error;
  }
  // Reset the waiting flag after ANY response (steer included). The flag was
  // set when the previous turn ended; the model just responded again, so the
  // "waiting" state is stale. waitForSubagentsBeforeConclusion is only safe
  // when the model is NOT still streaming (it calls session.prompt, which the
  // SDK rejects with "already processing" during an active run) — defer to
  // the agent_end handler for streaming sessions.
  if (record.subagents && !getBenchmarkRuntime(id)) {
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

export async function startPromptSession(id: string, text: string, mode: "prompt" | "steer" | "followUp" = "prompt", extras: PromptExtras = {}) {
  const record = await getOrCreateSession(id);
  // Reject images on the synchronous path so the route answers 400 instead of
  // letting the provider layer silently degrade them to placeholders — the
  // user believes the model saw the image.
  if (extras.images?.length && record.profile.supportsImages !== true) {
    throw new RiftxError("当前模型不支持图像输入，请在模型配置中开启“支持图像输入”或移除图片", "MODEL_DOES_NOT_SUPPORT_IMAGES", 400);
  }
  // Keep the Agent single-run while a previous stop is still unwinding a tool.
  if (record.abortPromise) await record.abortPromise;
  // Idempotency key FIRST: a replayed requestId must be rejected before any
  // skill preparation mutates record.loadedSkills, or the 409 would leave the
  // skill marked loaded without ever being injected.
  if (!beginPromptRequest(record, extras.requestId)) {
    throw new RiftxError("Duplicate request id — this send was already accepted", "DUPLICATE_REQUEST_ID", 409);
  }
  // The response below does NOT pre-resolve the mode or pre-compose text:
  // promptSession resolves it once, at dispatch time, against the live
  // isStreaming — anything computed here could race a turn boundary and
  // disagree with what is actually persisted. The REAL composed text of this
  // dispatch travels back through the acceptance hook.
  let accepted = false;
  let acceptRequest!: (composedText: string) => void;
  let rejectRequest!: (error: unknown) => void;
  const acceptance = new Promise<string>((resolve, reject) => {
    acceptRequest = (composedText: string) => { accepted = true; resolve(composedText); };
    rejectRequest = reject;
  });
  const running = promptSession(id, text, mode, extras, { onAccepted: acceptRequest, onFailed: rejectRequest });
  void running.catch((error) => {
    if (!accepted) {
      settlePromptRequest(record, extras.requestId, "failed", error instanceof Error ? error.message : String(error));
      rejectRequest(error);
      return;
    }
    // A failure after acceptance must not make the user retry an already
    // dispatched or queued message. Surface it as a session error without a
    // requestId so attachment recovery is not triggered.
    record.emitter.emit("event", { type: "error", error: error instanceof Error ? error.message : "Agent request failed" });
  });
  const composedText = await acceptance;
  return { record, composedText, requestState: "accepted" as const };
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
    if (record.compacting) onEvent({ type: "session_state", state: "compacting" });
    else if (record.waitingForSubagents) onEvent({ type: "session_state", state: "waiting_for_subagents" });
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
  const benchmarkRuntime = await benchmarkRuntimeForCleanup(id);
  const record = sessions.get(id);
  if (record) {
    await shutdownSessionRecord(record);
    sessions.delete(id);
  }
  if (benchmarkRuntime) await archiveBenchmarkRuntime(benchmarkRuntime);
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
  return listSessions();
}

async function sessionPathExists(path: string) {
  if (!path) return false;
  try {
    await stat(path);
    return true;
  } catch {
    return false;
  }
}

export async function restoreArchivedSession(id: string) {
  const config = await readConfig();
  const inCurrentWorkspace = Boolean((await listWorkspaceSessionInfos(config.cwd)).find((item) => item.id === id));
  const archivedPath = config.archivedSessions.find((item) => item.id === id)?.path ?? "";
  let sessionFileExists = inCurrentWorkspace || await sessionPathExists(archivedPath);
  if (!sessionFileExists) {
    sessionFileExists = (await AgentSessionManager.list(config.cwd, getAppPaths().sessions)).some((item) => item.id === id);
  }
  const decision = classifyArchivedRestore({
    archived: config.archivedSessionIds.includes(id),
    inCurrentWorkspace,
    sessionFileExists
  });
  if (!decision.ok) {
    const { message, status } = archivedRestoreError(decision.code);
    throw new RiftxError(message, decision.code, status);
  }
  await updateConfig((current) => restoredArchiveState(current, id));
  return listSessions();
}

export async function deleteArchivedSession(id: string) {
  const config = await readConfig();
  if (!config.archivedSessionIds.includes(id)) throw new RiftxError("session is not archived", "SESSION_NOT_ARCHIVED", 400);
  const benchmarkRuntime = await benchmarkRuntimeForCleanup(id);
  const session = (await listSessions()).find((item) => item.id === id);
  const record = sessions.get(id);
  if (record) {
    await shutdownSessionRecord(record);
    sessions.delete(id);
  }
  if (benchmarkRuntime) await deleteBenchmarkRuntime(benchmarkRuntime);
  if (session?.path) {
    try {
      await unlink(session.path);
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error;
    }
  }
  await removeEvidence(id, getAppPaths().evidence);
  const artifactPath = resolve(toolArtifactDir(getAppPaths().artifacts, id));
  const artifactRoot = resolve(getAppPaths().artifacts);
  const artifactRelative = relative(artifactRoot, artifactPath);
  if (artifactRelative && !artifactRelative.startsWith("..") && !isAbsolute(artifactRelative)) {
    await rm(artifactPath, { recursive: true, force: true });
  }
  const subagentPath = resolve(getAppPaths().subagents, id);
  const subagentRoot = resolve(getAppPaths().subagents);
  // Containment via path.relative works on both separators; a string prefix
  // check silently fails on Windows backslash paths.
  const subagentRelative = relative(subagentRoot, subagentPath);
  if (subagentRelative && !subagentRelative.startsWith("..") && !isAbsolute(subagentRelative)) {
    await rm(subagentPath, { recursive: true, force: true });
  }
  benchmarkRuntimeCache().delete(id);
  await BenchmarkLedger.destroy(id);
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
      let model: Model<Api>;
      try {
        model = registerProfileModel(sessionRecord.authStorage, sessionRecord.modelRegistry, next, true);
      } catch (error) {
        // A failure mid-registration must not leave the new key behind.
        try { rollback(); } catch { /* restore is best-effort */ }
        throw error;
      }
      return { model, rollback };
    },
    hasConfiguredAuth: (model) => record.modelRegistry.hasConfiguredAuth(model as Model<Api>),
    applyTransport: (session, transport) => setAgentTransport(session as AgentSession, transport)
  }));
  // Only a successful switch becomes the provider's tracked registration.
  if (switched) {
    record.providerRegistrations.set(profile.provider, profile);
    // A corrected key, endpoint, or output limit may keep the same model ID.
    // Let the next request validate its budget and attempt compaction again.
    clearBenchmarkCompactionFailure(record.session);
  }
  return switched;
}
