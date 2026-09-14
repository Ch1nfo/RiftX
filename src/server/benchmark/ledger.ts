/**
 * Authoritative benchmark task ledger, persisted per parent session. The
 * platform is the source of truth for completion state; the ledger adds
 * scheduling state (deferred, exhausted, signal tracking) the platform does
 * not track. Token is never written here.
 */

import { createHash, randomUUID } from "node:crypto";
import { access, mkdir, rm, writeFile } from "node:fs/promises";
import { join } from "node:path";
import { homedir } from "node:os";
import { readJsonStore, writeJsonStoreAtomic } from "@/server/json-store";
import { createSerializer } from "@/server/serializer";
import type { Challenge } from "./controller";
import { childHandoffSections, retainBlackboard } from "./blackboard";
import { assertFence, attemptContext, captureFence, type AttemptFence } from "./fencing";
import { emptyResources, FAILURE_PRIORITY, HarnessGateError, type AttemptIncident, type AttemptResources, type FailureSource, type TerminationSource } from "./attempt-observation";

export const BENCHMARK_MAX_CONTAINERS = 3;
export const BENCHMARK_MAX_SUBAGENTS = 2;
export const FIRST_ATTEMPT_WARNING_MS = 25 * 60 * 1000;
export const FIRST_ATTEMPT_LIMIT_MS = 30 * 60 * 1000;

export type ChallengeStatus = "pending" | "reserved" | "running" | "closing" | "deferred" | "solved" | "exhausted" | "orphaned";
/** Coverage is not a tactical round: it only prevents revisiting a challenge
 * until every challenge has received one real attempt. */
export type BenchmarkPhase = "coverage" | "revisit" | "completed";
export type ChallengeOwner = "main" | `subagent:${string}` | null;
export type ProgressSignalKind = "foothold" | "credential" | "privilege_change" | "exploit_primitive" | "stage_transition" | "decisive_rule_out" | "new_surface" | "note";

export type AttemptSummary = {
  attemptNumber: number;
  phase: BenchmarkPhase;
  worker: Exclude<ChallengeOwner, null>;
  approach: string;
  startedAt: number;
  endedAt: number;
  flagsBefore: number;
  flagsAfter: number;
  triedFamilies: string[];
  ruledOutFamilies: string[];
  stopReason: string;
  nextDistinctApproach: string;
  terminationReason?: string;
  terminationSource?: TerminationSource;
  activeGate?: string | null;
  lastProgressAt?: number;
  compactionCount?: number;
  toolErrorCount?: number;
  handoffStatus?: "not_requested" | "saved" | "failed";
  resources?: AttemptResources;
  lastProgressKind?: string | null;
  attemptId?: string;
  containerEpoch?: number;
};

export type ChallengeBudget = {
  firstAttempt: boolean;
  elapsedMs: number;
  warningDue: boolean;
  expired: boolean;
};

export type BlackboardEntry = {
  at: number;
  worker: Exclude<ChallengeOwner, null>;
  kind: ProgressSignalKind | "submission" | "attempt_end" | "handoff";
  summary: string;
  evidenceRef: string;
  approach: string;
  triedFamilies: string[];
  ruledOutFamilies: string[];
  nextProbe: string;
};

export type ChallengeState = {
  uniqueCode: string;
  description: string;
  difficulty: string;
  level: number;
  totalScore: number;
  flagCount: number;
  correctFlagCount: number;
  /** Per-challenge cumulative score from submit responses (hint deductions already included). */
  scoreObtained: number;
  /** Whether scoreObtained reflects the platform's score at the current correctFlagCount. */
  scoreKnown: boolean;
  isCompleted: boolean;
  status: ChallengeStatus;
  owner: ChallengeOwner;
  containerAddrs: string[];
  containerStatus: string;
  hintUsed: boolean;
  hintContent: string | null;
  lastSignalAt: number;
  lastSignalContent: string;
  lastMeaningfulSignalContent: string;
  attemptCount: number;
  currentAttemptStartedAt: number | null;
  currentAttemptPhase: BenchmarkPhase | null;
  currentAttemptWorker: Exclude<ChallengeOwner, null> | null;
  attemptId?: string;
  containerEpoch?: number;
  resources?: AttemptResources;
  progressRevision?: number;
  attemptIncident?: AttemptIncident;
  handoffStatus?: "not_requested" | "saved" | "failed";
  flagsAtAttemptStart: number;
  currentApproach: string;
  lastMeaningfulProgressAt: number;
  lastAcceptedFlagAt: number | null;
  hardDeadlineAt: number | null;
  firstAttemptWarningIssuedAt: number | null;
  blackboard: BlackboardEntry[];
  /** Cumulative online password enumeration time across all workers/attempts. */
  passwordEnumerationMs: number;
  approachHistory: AttemptSummary[];
  lastSignalKind: ProgressSignalKind | null;
  lastEvidenceRef: string;
  progressKeys: string[];
  triedFamilies: string[];
  ruledOutFamilies: string[];
  nextProbe: string;
  triedFlags: string[];
  matchedFlagIndexes: number[];
  deferredReason: string | null;
  pendingStatus: ChallengeStatus | undefined;
  reservationPreviousStatus: ChallengeStatus | undefined;
  reservationStartedNewAttempt: boolean;
  closeFailureRecorded: boolean;
  acquiredAt: number | null;
  solvedAt: number | null;
  /** Monotonic FIFO position assigned whenever the challenge is deferred for revisit. */
  revisitQueueOrder: number;
};

export type BenchmarkState = {
  phase: BenchmarkPhase;
  cumulativeScore: number;
  scoreExact: boolean;
  totalChallenges: number;
  solvedCount: number;
  exhaustedCount: number;
  activeContainers: number;
  lastSyncAt: number;
  vpnOk: boolean;
  vpnChecked: boolean;
  vpnClientIp: string;
  sharedIntel: SharedIntel[];
  challenges: Record<string, ChallengeState>;
};

export type ChallengeSyncGuard = Record<string, string>;

export type SharedIntel = {
  scope: "global" | "target";
  target: string;
  intel: string;
  publishedAt: number;
};

export type ChallengeMetric = {
  acquiredAt: number;
  solvedAt: number | null;
  durationMs: number | null;
  attempts: number;
  wrongFlags: number;
  hintsUsed: number;
  deferredCount: number;
};

export type BenchmarkMetrics = {
  startedAt: number;
  completedAt: number | null;
  totalWrongSubmissions: number;
  totalHintsUsed: number;
  totalDefers: number;
  duplicateAcquires: number;
  containerLeaks: number;
  compactionCount: number;
  challenges: Record<string, ChallengeMetric>;
  terminationCounts: Partial<Record<TerminationSource, number>>;
  incidentCounts: Partial<Record<FailureSource, number>>;
  resources: AttemptResources;
};

function benchmarkDir(parentSessionId: string) {
  return join(homedir(), ".riftx", "benchmark", parentSessionId);
}

export function isPartialChallenge(challenge: Pick<ChallengeState, "correctFlagCount" | "flagCount" | "isCompleted">): boolean {
  return challenge.correctFlagCount > 0 && challenge.correctFlagCount < challenge.flagCount && !challenge.isCompleted;
}

export function hasReusableBenchmarkContainer(challenge: Pick<ChallengeState, "status" | "containerStatus" | "containerAddrs"> | undefined): boolean {
  return Boolean(challenge
    && challenge.status === "orphaned"
    && challenge.containerStatus === "available"
    && challenge.containerAddrs.length > 0);
}

function isUnfinishedFirstAttempt(challenge: ChallengeState): boolean {
  return challenge.currentAttemptPhase === "coverage"
    && (challenge.status === "reserved" || challenge.status === "running" || challenge.status === "closing" || challenge.status === "orphaned");
}

function coverageIsComplete(state: BenchmarkState): boolean {
  return Object.values(state.challenges).every((challenge) =>
    challenge.isCompleted || (challenge.attemptCount > 0 && !isUnfinishedFirstAttempt(challenge))
  );
}

function cleanText(value: string, maxLength: number): string {
  const cleaned = value.replace(/[\u0000-\u001f\u007f]/g, " ").trim();
  const token = process.env.BENCHMARK_TOKEN ?? "";
  const redacted = token ? cleaned.split(token).join("[REDACTED_BENCHMARK_TOKEN]") : cleaned;
  return redacted.slice(0, maxLength);
}

function nullableFiniteNumber(value: unknown): number | null {
  if (value === null || value === undefined || value === "") return null;
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}

function progressKey(kind: ProgressSignalKind, evidenceRef: string): string {
  return `${kind}\u0000${cleanText(evidenceRef, 500).toLowerCase()}`;
}

function finishAttempt(challenge: ChallengeState, now: number, stopReason: string, metrics: BenchmarkMetrics, source?: TerminationSource): void {
  if (challenge.currentAttemptStartedAt === null || !challenge.currentAttemptWorker || !challenge.currentAttemptPhase) return;
  const timedOut = challenge.hardDeadlineAt !== null && now >= challenge.hardDeadlineAt;
  const fallback = timedOut ? "harness_timeout" : source ?? "solver_failure";
  const recorded = challenge.attemptIncident;
  const incident = recorded && FAILURE_PRIORITY[recorded.source] >= (FAILURE_PRIORITY[fallback as FailureSource] ?? 0) ? recorded : undefined;
  const terminationSource = source === "solved" ? source : incident?.source ?? fallback;
  const resources = { ...emptyResources(), ...challenge.resources, wallTime: Math.max(0, now - challenge.currentAttemptStartedAt) };
  challenge.approachHistory = [...challenge.approachHistory, {
    attemptNumber: challenge.attemptCount, phase: challenge.currentAttemptPhase, worker: challenge.currentAttemptWorker,
    approach: challenge.currentApproach || "(not recorded)", startedAt: challenge.currentAttemptStartedAt, endedAt: now,
    flagsBefore: challenge.flagsAtAttemptStart, flagsAfter: challenge.correctFlagCount,
    triedFamilies: challenge.triedFamilies.slice(-20), ruledOutFamilies: challenge.ruledOutFamilies.slice(-20),
    stopReason: cleanText(stopReason, 1_000), nextDistinctApproach: challenge.nextProbe,
    terminationReason: cleanText(terminationSource === "solved" ? stopReason : incident?.reason ?? stopReason, 1_000), terminationSource,
    activeGate: terminationSource === "solved" ? null : incident?.gate ?? (timedOut ? "timebox" : null),
    lastProgressKind: resources.lastProgressKind, lastProgressAt: resources.lastProgressAt,
    compactionCount: resources.compactionCount, toolErrorCount: resources.toolErrorCount,
    handoffStatus: challenge.handoffStatus ?? "not_requested", resources,
    attemptId: challenge.attemptId, containerEpoch: challenge.containerEpoch
  }].slice(-6);
  metrics.terminationCounts[terminationSource] = (metrics.terminationCounts[terminationSource] ?? 0) + 1;
  metrics.resources.wallTime += resources.wallTime;
  metrics.resources.progressEvents += resources.progressEvents;
  challenge.currentAttemptStartedAt = null;
  challenge.currentAttemptPhase = null;
  challenge.currentAttemptWorker = null;
  challenge.hardDeadlineAt = null;
}

function syncGuardToken(challenge: ChallengeState): string {
  return JSON.stringify([
    challenge.status,
    challenge.owner,
    challenge.containerStatus,
    challenge.pendingStatus ?? null,
    challenge.attemptCount,
    challenge.currentAttemptStartedAt, challenge.attemptId, challenge.containerEpoch
  ]);
}

function enqueueForRevisit(state: BenchmarkState, challenge: ChallengeState, now: number): void {
  const latest = Object.values(state.challenges).reduce((max, candidate) => Math.max(max, candidate.revisitQueueOrder || 0), 0);
  challenge.revisitQueueOrder = Math.max(now, latest + 1);
}

function appendBlackboard(challenge: ChallengeState, entry: BlackboardEntry): void {
  const knownEvidence = challenge.blackboard.some((prior) => prior.evidenceRef === entry.evidenceRef && prior.kind === entry.kind);
  const previous = challenge.blackboard.at(-1);
  const duplicate = previous && previous.kind === entry.kind
    && previous.summary === entry.summary && previous.evidenceRef === entry.evidenceRef;
  if (!duplicate) challenge.blackboard = retainBlackboard([...challenge.blackboard, entry]);
  if (!duplicate && entry.evidenceRef && !["handoff", "attempt_end"].includes(entry.kind)
    && !entry.evidenceRef.startsWith("platform:pending:")
    && !knownEvidence) {
    challenge.progressRevision = (challenge.progressRevision ?? 0) + 1;
    if (challenge.resources) {
      challenge.resources.progressEvents++;
      challenge.resources.lastProgressAt = entry.at;
      challenge.resources.lastProgressKind = entry.kind;
    }
    if (challenge.attemptIncident?.source === "solver_failure") challenge.attemptIncident = undefined;
    if (challenge.resources) {
      challenge.resources.callsWithoutProgress = 0;
      challenge.resources.repeatCount = 0;
      challenge.resources.fingerprints = {};
      challenge.resources.progressRevision = challenge.progressRevision ?? 0;
    }
  }
}

function statePath(parentSessionId: string) {
  return join(benchmarkDir(parentSessionId), "state.json");
}

function metricsPath(parentSessionId: string) {
  return join(benchmarkDir(parentSessionId), "metrics.json");
}

function newChallengeState(challenge: Challenge): ChallengeState {
  const containerIsActive = challenge.container_status === "available" || challenge.container_status === "pending" || challenge.container_status === "stop_pending";
  const status: ChallengeStatus = challenge.is_completed
    ? (containerIsActive ? "closing" : "solved")
    : (containerIsActive ? (challenge.container_status === "stop_pending" ? "closing" : "orphaned") : "pending");
  return {
    uniqueCode: challenge.unique_code,
    description: challenge.description,
    difficulty: challenge.difficulty,
    level: challenge.level,
    totalScore: challenge.total_score,
    flagCount: challenge.flag_count,
    correctFlagCount: challenge.correct_flag_count,
    // The list endpoint exposes no score; per-challenge values arrive only
    // from submit responses, so a pre-solved challenge starts as unknown.
    scoreObtained: 0,
    scoreKnown: false,
    isCompleted: challenge.is_completed,
    status,
    owner: null,
    containerAddrs: challenge.container_addr,
    containerStatus: challenge.container_status,
    hintUsed: false,
    hintContent: null,
    lastSignalAt: 0,
    lastSignalContent: "",
    lastMeaningfulSignalContent: "",
    attemptCount: 0,
    currentAttemptStartedAt: null,
    currentAttemptPhase: null,
    currentAttemptWorker: null,
    attemptId: undefined,
    containerEpoch: containerIsActive ? 1 : 0,
    resources: emptyResources(),
    progressRevision: 0,
    handoffStatus: "not_requested",
    flagsAtAttemptStart: challenge.correct_flag_count,
    currentApproach: "",
    lastMeaningfulProgressAt: 0,
    lastAcceptedFlagAt: null,
    hardDeadlineAt: null,
    firstAttemptWarningIssuedAt: null,
    blackboard: [],
    passwordEnumerationMs: 0,
    approachHistory: [],
    lastSignalKind: null,
    lastEvidenceRef: "",
    progressKeys: [],
    triedFamilies: [],
    ruledOutFamilies: [],
    nextProbe: "",
    triedFlags: [],
    matchedFlagIndexes: [],
    deferredReason: null,
    pendingStatus: status === "closing" ? (challenge.is_completed ? "solved" : "deferred") : undefined,
    reservationPreviousStatus: undefined,
    reservationStartedNewAttempt: false,
    closeFailureRecorded: false,
    acquiredAt: null,
    solvedAt: null,
    revisitQueueOrder: 0
  };
}

function defaultMetrics(): BenchmarkMetrics {
  return {
    startedAt: Date.now(),
    completedAt: null,
    totalWrongSubmissions: 0,
    totalHintsUsed: 0,
    totalDefers: 0,
    duplicateAcquires: 0,
    containerLeaks: 0,
    compactionCount: 0,
    challenges: {}, terminationCounts: {}, incidentCounts: {}, resources: emptyResources()
  };
}

function defaultState(): BenchmarkState {
  return {
    phase: "coverage",
    cumulativeScore: 0,
    scoreExact: true,
    totalChallenges: 0,
    solvedCount: 0,
    exhaustedCount: 0,
    activeContainers: 0,
    lastSyncAt: 0,
    vpnOk: false,
    vpnChecked: false,
    vpnClientIp: "",
    sharedIntel: [],
    challenges: {}
  };
}

function hasActiveContainer(challenge: ChallengeState): boolean {
  // Orphaned is deliberately NOT in the status clause: an orphan whose
  // container the platform already stopped must release its slot, or three
  // such orphans permanently block every new acquire (defer/abandon are
  // owner-gated and cannot clear an unowned orphan). A live orphan still
  // counts via its containerStatus — confirmStarted/sync leave "available"
  // while the container runs.
  return challenge.status === "reserved" || challenge.status === "running"
    || challenge.status === "closing"
    || challenge.containerStatus === "available" || challenge.containerStatus === "pending" || challenge.containerStatus === "stop_pending";
}

function countActiveContainers(state: BenchmarkState, excludeUniqueCode?: string): number {
  return Object.values(state.challenges).filter((challenge) =>
    challenge.uniqueCode !== excludeUniqueCode && hasActiveContainer(challenge)
  ).length;
}

function flagHash(flag: string): string {
  return createHash("sha256").update(flag).digest("hex").slice(0, 16);
}

function requireOwner(challenge: ChallengeState, expectedOwner: Exclude<ChallengeOwner, null>, allowUnowned = false): void {
  const fence = attemptContext.getStore();
  if (fence) assertFence(challenge, fence, allowUnowned);
  if (challenge.owner === expectedOwner) return;
  if (allowUnowned && challenge.owner === null) return;
  throw new HarnessGateError("harness_concurrency", "owner", `Challenge ${challenge.uniqueCode} is owned by ${challenge.owner ?? "nobody"}, not by ${expectedOwner}`);
}

function recalculate(state: BenchmarkState, metrics?: BenchmarkMetrics, now: () => number = Date.now): BenchmarkState {
  const challenges = Object.values(state.challenges);
  state.solvedCount = challenges.filter((challenge) => challenge.status === "solved" || (challenge.status === "closing" && challenge.pendingStatus === "solved")).length;
  state.exhaustedCount = challenges.filter((challenge) => challenge.status === "exhausted").length;
  state.totalChallenges = challenges.length;
  state.activeContainers = countActiveContainers(state);
  // cumulative_score from the platform is PER-CHALLENGE (该题累计总得分).
  // The run total is the sum of the per-challenge values; it is exact only
  // when every challenge that has scored flags carries an authoritative value.
  state.cumulativeScore = challenges.reduce((total, challenge) => total + (Number.isFinite(challenge.scoreObtained) ? challenge.scoreObtained : 0), 0);
  state.scoreExact = challenges.filter((challenge) => challenge.correctFlagCount > 0).every((challenge) => challenge.scoreKnown === true);
  const allTerminal = challenges.length > 0 && challenges.every((challenge) => challenge.status === "solved" || challenge.status === "exhausted");
  state.phase = allTerminal ? "completed" : coverageIsComplete(state) ? "revisit" : "coverage";
  if (metrics) {
    if (allTerminal) metrics.completedAt ??= now();
    else metrics.completedAt = null;
  }
  return state;
}

export class BenchmarkLedger {
  private readonly serialize = createSerializer();
  private readonly actionSerializers = new Map<string, ReturnType<typeof createSerializer>>();
  private state: BenchmarkState = defaultState();
  private metrics: BenchmarkMetrics = defaultMetrics();
  private readonly parentSessionId: string;
  private readonly now: () => number;
  private readonly stateListeners = new Set<() => void>();
  onStateChange(listener: () => void) {
    this.stateListeners.add(listener);
    return () => { this.stateListeners.delete(listener); };
  }

  constructor(parentSessionId: string, now: () => number = Date.now) {
    this.parentSessionId = parentSessionId;
    this.now = now;
  }

  /** Loads or creates the ledger. Must be called before any other operation. */
  async initialize(): Promise<BenchmarkLedger> {
    await mkdir(benchmarkDir(this.parentSessionId), { recursive: true, mode: 0o700 });
    this.state = await readJsonStore<BenchmarkState>(statePath(this.parentSessionId)) ?? defaultState();
    this.metrics = await readJsonStore<BenchmarkMetrics>(metricsPath(this.parentSessionId)) ?? defaultMetrics();
    this.metrics.terminationCounts ??= {};
    this.metrics.incidentCounts ??= {};
    this.metrics.resources = { ...emptyResources(), ...this.metrics.resources };
    // Backward-compatible defaults for ledgers created before these fields existed.
    if (typeof this.state.scoreExact !== "boolean") this.state.scoreExact = false;
    if (typeof this.state.vpnChecked !== "boolean") this.state.vpnChecked = false;
    if (!Array.isArray(this.state.sharedIntel)) this.state.sharedIntel = [];
    this.state.phase = this.state.phase === "completed" ? "completed" : "coverage";
    for (const challenge of Object.values(this.state.challenges)) {
      const legacy = challenge as ChallengeState & {
        handoffExpiresAt?: unknown;
        reservationPreviousHandoffExpiresAt?: unknown;
      };
      challenge.reservationPreviousStatus ??= undefined;
      challenge.reservationStartedNewAttempt = challenge.reservationStartedNewAttempt === true;
      challenge.closeFailureRecorded ??= false;
      challenge.level = Number.isFinite(Number(challenge.level)) ? Number(challenge.level) : 0;
      // Ledgers from the pre-per-challenge era have no authoritative values;
      // scoreKnown=false makes recalculate() report the run total as a lower
      // bound until the next authoritative submit.
      challenge.scoreObtained = Number.isFinite(Number(challenge.scoreObtained)) ? Number(challenge.scoreObtained) : 0;
      challenge.scoreKnown = challenge.scoreKnown === true;
      challenge.attemptCount = Number.isFinite(Number(challenge.attemptCount)) ? Number(challenge.attemptCount) : 0;
      challenge.currentAttemptStartedAt = nullableFiniteNumber(challenge.currentAttemptStartedAt);
      challenge.currentAttemptPhase = challenge.currentAttemptStartedAt
        ? (challenge.attemptCount <= 1 ? "coverage" : "revisit")
        : null;
      challenge.currentAttemptWorker ??= null;
      challenge.attemptId = typeof challenge.attemptId === "string" ? challenge.attemptId : challenge.currentAttemptStartedAt !== null ? randomUUID() : undefined;
      challenge.resources = { ...emptyResources(challenge.currentAttemptStartedAt ?? 0), ...challenge.resources };
      challenge.progressRevision ??= 0;
      challenge.handoffStatus ??= "not_requested";
      challenge.containerEpoch = Number.isFinite(Number(challenge.containerEpoch)) ? Number(challenge.containerEpoch) : 0;
      challenge.flagsAtAttemptStart = Number.isFinite(Number(challenge.flagsAtAttemptStart)) ? Number(challenge.flagsAtAttemptStart) : challenge.correctFlagCount;
      challenge.currentApproach = typeof challenge.currentApproach === "string" ? challenge.currentApproach : "";
      challenge.lastMeaningfulProgressAt = Number.isFinite(Number(challenge.lastMeaningfulProgressAt))
        ? Number(challenge.lastMeaningfulProgressAt)
        : (Number.isFinite(Number(challenge.lastSignalAt)) ? Number(challenge.lastSignalAt) : 0);
      challenge.lastAcceptedFlagAt = nullableFiniteNumber(challenge.lastAcceptedFlagAt);
      challenge.lastMeaningfulSignalContent = typeof challenge.lastMeaningfulSignalContent === "string"
        ? challenge.lastMeaningfulSignalContent
        : (challenge.lastSignalKind ? challenge.lastSignalContent : "");
      challenge.hardDeadlineAt = challenge.currentAttemptStartedAt && challenge.attemptCount === 1
        ? challenge.currentAttemptStartedAt + FIRST_ATTEMPT_LIMIT_MS
        : null;
      challenge.firstAttemptWarningIssuedAt = nullableFiniteNumber(challenge.firstAttemptWarningIssuedAt);
      challenge.passwordEnumerationMs = Math.max(0, Number(challenge.passwordEnumerationMs) || 0);
      challenge.blackboard = Array.isArray(challenge.blackboard) ? retainBlackboard(challenge.blackboard) : [];
      challenge.blackboard = challenge.blackboard.map((entry) => ({
        ...entry,
        summary: cleanText(typeof entry.summary === "string" ? entry.summary : "", 2_000),
        evidenceRef: cleanText(typeof entry.evidenceRef === "string" ? entry.evidenceRef : "", 500),
        approach: cleanText(typeof entry.approach === "string" ? entry.approach : "", 300),
        triedFamilies: Array.isArray(entry.triedFamilies) ? entry.triedFamilies.map((item) => cleanText(String(item), 100)).slice(-10) : [],
        ruledOutFamilies: Array.isArray(entry.ruledOutFamilies) ? entry.ruledOutFamilies.map((item) => cleanText(String(item), 100)).slice(-10) : [],
        nextProbe: cleanText(typeof entry.nextProbe === "string" ? entry.nextProbe : "", 1_000)
      }));
      challenge.approachHistory = Array.isArray(challenge.approachHistory) ? challenge.approachHistory.slice(-6) : [];
      challenge.lastSignalKind ??= null;
      challenge.lastEvidenceRef = typeof challenge.lastEvidenceRef === "string" ? challenge.lastEvidenceRef : "";
      challenge.progressKeys = Array.isArray(challenge.progressKeys) ? challenge.progressKeys.slice(-30) : [];
      challenge.ruledOutFamilies = Array.isArray(challenge.ruledOutFamilies) ? challenge.ruledOutFamilies.slice(-20) : [];
      challenge.revisitQueueOrder = Number.isFinite(Number(challenge.revisitQueueOrder))
        ? Number(challenge.revisitQueueOrder)
        : (challenge.approachHistory.at(-1)?.endedAt ?? 0);
      if ((challenge.status as string) === "handoff_waiting") {
        challenge.status = challenge.containerStatus === "available" ? "orphaned" : "deferred";
        challenge.owner = null;
      }
      delete legacy.handoffExpiresAt;
      delete legacy.reservationPreviousHandoffExpiresAt;
    }
    // Restart recovery: running/claimed/reserved → orphaned (prioritize re-acquire);
    // closing → keep closing (the platform close attempt must be re-confirmed by sync).
    for (const challenge of Object.values(this.state.challenges)) {
      if (challenge.status === "running" || challenge.status === "reserved") {
        challenge.status = "orphaned";
        challenge.owner = null;
      }
      // closing stays closing — syncFromPlatform will check the platform's
      // container_status and either advance to the pending terminal status
      // (platform stopped) or keep it closing (still available on platform).
    }
    if (coverageIsComplete(this.state)) {
      this.state.phase = this.state.phase === "completed" ? "completed" : "revisit";
    }
    recalculate(this.state, this.metrics, this.now);
    await this.persist();
    return this;
  }

  private async persist() {
    for (const listener of this.stateListeners) listener();
    await writeJsonStoreAtomic(statePath(this.parentSessionId), this.state);
    await writeJsonStoreAtomic(metricsPath(this.parentSessionId), this.metrics);
  }

  getState(): Readonly<BenchmarkState> {
    return this.state;
  }

  getMetrics(): Readonly<BenchmarkMetrics> {
    return this.metrics;
  }

  runElapsedMs(): number {
    return Math.max(0, (this.metrics.completedAt ?? this.now()) - this.metrics.startedAt);
  }

  getChallenge(uniqueCode: string): ChallengeState | undefined {
    return this.state.challenges[uniqueCode];
  }

  captureSyncGuard(): ChallengeSyncGuard {
    return Object.fromEntries(Object.values(this.state.challenges).map((challenge) => [challenge.uniqueCode, syncGuardToken(challenge)]));
  }

  /** Serialize network mutations only for the same challenge. Different
   * challenges may progress concurrently; the ledger itself remains protected
   * by the short state serializer above. */
  runChallengeAction<T>(uniqueCode: string, action: () => Promise<T>): Promise<T> {
    let serializer = this.actionSerializers.get(uniqueCode);
    if (!serializer) {
      serializer = createSerializer();
      this.actionSerializers.set(uniqueCode, serializer);
    }
    const fence = attemptContext.getStore();
    return serializer(() => {
      if (fence && fence.uniqueCode === uniqueCode) assertFence(this.state.challenges[uniqueCode], fence);
      return action();
    });
  }

  async assertOwned(uniqueCode: string, expectedOwner: Exclude<ChallengeOwner, null>, allowedStatuses: ChallengeStatus[] = ["running"]): Promise<ChallengeState> {
    return this.serialize(async () => {
      const challenge = this.state.challenges[uniqueCode];
      if (!challenge) throw new Error(`Challenge ${uniqueCode} not found`);
      requireOwner(challenge, expectedOwner);
      if (!allowedStatuses.includes(challenge.status)) {
        throw new Error(`Challenge ${uniqueCode} is ${challenge.status}; expected ${allowedStatuses.join("/")}`);
      }
      return challenge;
    });
  }

  hasTriedFlag(uniqueCode: string, flag: string): boolean {
    return this.state.challenges[uniqueCode]?.triedFlags.includes(flagHash(flag)) ?? false;
  }

  /** Remember an ambiguous timed-out submission without counting it as wrong. */
  async recordSubmissionAttempt(uniqueCode: string, flag: string, expectedOwner: Exclude<ChallengeOwner, null>): Promise<void> {
    return this.serialize(async () => {
      const challenge = this.state.challenges[uniqueCode];
      if (!challenge) throw new Error(`Challenge ${uniqueCode} not found`);
      requireOwner(challenge, expectedOwner);
      const hash = flagHash(flag);
      appendBlackboard(challenge, { at: this.now(), worker: expectedOwner, kind: "note",
        summary: "Flag submission outcome is unknown; queued for bounded confirmation in this run.", evidenceRef: `platform:pending:${hash}`,
        approach: "", triedFamilies: [], ruledOutFamilies: [], nextProbe: "" });
      // The platform may have accepted, rejected, or penalized the request;
      // without its response this challenge's per-challenge score may have
      // moved, so its cached value is no longer authoritative.
      challenge.scoreKnown = false;
      recalculate(this.state, this.metrics, this.now);
      await this.persist();
    });
  }

  /** Publish bounded, secret-scrubbed cross-challenge intelligence. */
  async publishIntel(scope: "global" | "target", target: string, intel: string): Promise<SharedIntel> {
    return this.serialize(async () => {
      const configuredToken = process.env.BENCHMARK_TOKEN ?? "";
      const clean = intel.replace(/[\u0000-\u001f\u007f]/g, " ").trim().slice(0, 800);
      const redacted = configuredToken ? clean.split(configuredToken).join("[REDACTED_BENCHMARK_TOKEN]") : clean;
      if (!redacted) throw new Error("intel must not be empty");
      const normalizedTarget = scope === "global" ? "*" : target.trim().slice(0, 200);
      if (scope === "target" && !normalizedTarget) throw new Error("target is required for target-scoped intel");
      const entry = { scope, target: normalizedTarget, intel: redacted, publishedAt: this.now() } satisfies SharedIntel;
      const duplicate = this.state.sharedIntel.some((item) => item.scope === entry.scope && item.target === entry.target && item.intel === entry.intel);
      if (!duplicate) this.state.sharedIntel = [...this.state.sharedIntel, entry].slice(-30);
      await this.persist();
      return entry;
    });
  }

  intelForChallenge(challenge: ChallengeState, containerAddrs: readonly string[] = challenge.containerAddrs): SharedIntel[] {
    const haystack = `${challenge.uniqueCode}\n${challenge.description}\n${containerAddrs.join("\n")}`.toLocaleLowerCase();
    return this.state.sharedIntel.filter((entry) => entry.scope === "global" || haystack.includes(entry.target.toLocaleLowerCase())).slice(-8);
  }

  /** Persist the latest VPN preflight result independently of platform sync.
   * A failed preflight happens before listChallenges(), so it needs its own
   * write path or a previous successful result would remain visible. */
  async recordVpnCheck(vpnOk: boolean, vpnClientIp: string, vpnChecked = true): Promise<BenchmarkState> {
    return this.serialize(async () => {
      this.state.vpnOk = vpnOk;
      this.state.vpnChecked = vpnChecked;
      this.state.vpnClientIp = vpnClientIp;
      await this.persist();
      return this.state;
    });
  }

  /** Full platform sync: platform values override local. The list endpoint provides no score —
   * per-challenge values arrive only from submit responses, so platform progress the ledger
   * cannot price marks the run total as a lower bound (scoreExact=false via recalculate). */
  async syncFromPlatform(challenges: Challenge[], vpnOk: boolean, vpnClientIp: string, vpnChecked = true, guard?: ChallengeSyncGuard): Promise<BenchmarkState> {
    return this.serialize(async () => {
      this.state.vpnOk = vpnOk;
      this.state.vpnChecked = vpnChecked;
      this.state.vpnClientIp = vpnClientIp;
      this.state.lastSyncAt = this.now();
      for (const platform of challenges) {
        const existing = this.state.challenges[platform.unique_code];
        if (!existing) {
          this.state.challenges[platform.unique_code] = newChallengeState(platform);
          continue;
        }
        const schedulingSnapshotIsCurrent = guard?.[platform.unique_code] === undefined
          || guard[platform.unique_code] === syncGuardToken(existing);
        // A list sync can observe progress that happened after a timed-out
        // submit, a process crash, or another worker. The list endpoint has no
        // score, so the cached score no longer corresponds to this progress.
        // On a backwards count change even the cached value is not a safe lower
        // bound; reset it to zero until a priced submit response is available.
        const previousCorrectFlagCount = existing.correctFlagCount;
        if (platform.correct_flag_count > previousCorrectFlagCount) {
          existing.scoreKnown = false;
        }
        // Platform is source of truth for these fields.
        // Counts are monotonic within a run. A concurrent list request can
        // return an older snapshot after submit completes; never let that
        // stale response erase newly accepted progress.
        existing.correctFlagCount = Math.max(existing.correctFlagCount, platform.correct_flag_count);
        if (platform.correct_flag_count > previousCorrectFlagCount) {
          const now = this.now();
          existing.lastSignalAt = now;
          existing.lastMeaningfulProgressAt = now;
          existing.lastAcceptedFlagAt = now;
          existing.lastSignalKind = "stage_transition";
          existing.lastEvidenceRef = `platform:sync-flag-count:${platform.correct_flag_count}`;
          existing.lastSignalContent = `Platform sync confirmed flag progress ${platform.correct_flag_count}/${platform.flag_count}`;
          existing.lastMeaningfulSignalContent = existing.lastSignalContent;
          appendBlackboard(existing, {
            at: now,
            worker: existing.currentAttemptWorker ?? "main",
            kind: "submission",
            summary: existing.lastSignalContent,
            evidenceRef: existing.lastEvidenceRef,
            approach: existing.currentApproach,
            triedFamilies: existing.triedFamilies.slice(-10),
            ruledOutFamilies: existing.ruledOutFamilies.slice(-10),
            nextProbe: existing.nextProbe
          });
        }
        existing.isCompleted = existing.isCompleted || platform.is_completed;
        if (schedulingSnapshotIsCurrent) {
          if (platform.container_status === "available" && platform.container_addr.length &&
            (existing.containerStatus === "stopped" || (existing.containerAddrs.length > 0 &&
              JSON.stringify([...existing.containerAddrs].sort()) !== JSON.stringify([...platform.container_addr].sort())))) {
            existing.containerEpoch = (existing.containerEpoch ?? 0) + 1;
          }
          existing.containerStatus = platform.container_status;
          // Platform available → take its addresses; platform stopped → clear stale local addresses.
          if (platform.container_status === "available" && platform.container_addr.length) {
            existing.containerAddrs = platform.container_addr;
          } else if (platform.container_status === "stopped" || platform.container_status === "stop_pending") {
            existing.containerAddrs = [];
          }
          // Closing → advance to pending terminal when the platform confirms stopped.
          if (existing.status === "closing" && platform.container_status === "stopped" && existing.pendingStatus) {
            existing.status = existing.pendingStatus;
            existing.pendingStatus = undefined;
            existing.owner = null;
            existing.closeFailureRecorded = false;
          }
        }
        existing.totalScore = platform.total_score;
        existing.flagCount = platform.flag_count;
        if (existing.isCompleted) {
          const platformContainerIsCurrent = schedulingSnapshotIsCurrent || platform.is_completed;
          if (platformContainerIsCurrent) {
            existing.containerStatus = platform.container_status;
            existing.containerAddrs = platform.container_status === "available" ? platform.container_addr : [];
          }
          if (existing.currentAttemptStartedAt) finishAttempt(existing, this.now(), "platform sync confirmed solved", this.metrics, "solved");
          const needsClose = platformContainerIsCurrent ? platform.container_status !== "stopped" : hasActiveContainer(existing);
          existing.status = needsClose ? "closing" : "solved";
          existing.pendingStatus = needsClose ? "solved" : undefined;
          existing.owner = null;
          existing.reservationPreviousStatus = undefined;
          existing.reservationStartedNewAttempt = false;
          if (!existing.solvedAt) existing.solvedAt = this.now();
        }
        if (schedulingSnapshotIsCurrent && !platform.is_completed && platform.container_status === "stopped"
          && (existing.status === "running" || existing.status === "orphaned")) {
          existing.status = "orphaned";
          existing.owner = null;
          existing.pendingStatus = undefined;
          existing.reservationPreviousStatus = undefined;
          existing.reservationStartedNewAttempt = false;
        } else if (schedulingSnapshotIsCurrent && !platform.is_completed && platform.container_status === "available"
          && existing.owner === null && (existing.status === "pending" || existing.status === "deferred")) {
          // The platform has a live container with no live local worker. Treat it
          // as recoverable instead of starting a second container.
          existing.status = "orphaned";
        }
        // Local scheduling state survives when the challenge is not complete.
      }
      recalculate(this.state, this.metrics, this.now);
      await this.persist();
      return this.state;
    });
  }

  /** Phase 1 of atomic acquire: reserve the challenge under the serializer (no platform calls).
   * The maxSubagents limit is checked INSIDE the same critical section, closing the
   * concurrent-assign race where 3 parallel assigns all passed the external check. */
  async reserve(uniqueCode: string, owner: ChallengeOwner, options?: { isSubagent?: boolean }): Promise<ChallengeState> {
    return this.serialize(async () => {
      if (!owner) throw new Error("An owner is required to reserve a benchmark challenge");
      const challenge = this.state.challenges[uniqueCode];
      if (!challenge) throw new Error(`Challenge ${uniqueCode} not found in ledger`);
      if (challenge.owner && challenge.owner !== owner) {
        this.metrics.duplicateAcquires += 1;
        throw new Error(`Challenge ${uniqueCode} is owned by ${challenge.owner}`);
      }
      const eligible = challenge.status === "pending" || challenge.status === "orphaned"
        || (challenge.status === "deferred" && challenge.attemptCount === 0)
        || (this.state.phase === "revisit" && challenge.status === "deferred");
      if (!eligible) throw new Error(`Challenge ${uniqueCode} cannot be reserved while ${challenge.status}`);

      const unseen = Object.values(this.state.challenges).filter((candidate) =>
        !candidate.isCompleted && candidate.attemptCount === 0
        && (candidate.status === "pending" || candidate.status === "orphaned")
      );
      const resumesLiveAttempt = challenge.status === "orphaned"
        && challenge.currentAttemptStartedAt !== null
        && hasReusableBenchmarkContainer(challenge);
      // Resuming a live orphan is recovery of an in-flight attempt, not a new
      // revisit: the attempt already owns its platform container slot, so it
      // cannot consume fresh capacity. Allowing it regardless of coverage
      // progress is also what frees a saturated container cap when stranded
      // revisit orphans hold all three slots while an unseen challenge waits.
      if (unseen.length > 0 && challenge.attemptCount > 0 && !resumesLiveAttempt) {
        throw new Error(`Coverage is not complete — ${unseen.length} unseen challenges remain, so ${uniqueCode} cannot be revisited yet`);
      }
      if (this.state.phase === "revisit" && !resumesLiveAttempt) {
        const next = this.candidates(1)[0];
        if (next && next.uniqueCode !== uniqueCode) {
          throw new Error(`Revisit queue is FIFO — choose ${next.uniqueCode} before ${uniqueCode}`);
        }
      }
      if (challenge.attemptCount === 0 && unseen.length > 0) {
        // `unseen` excludes challenges already reserved by another worker.
        // This enforces one full ascending queue: once the current cheapest
        // challenge is claimed, the next score becomes immediately eligible.
        // It deliberately does not restrict coverage to one score tier.
        const lowestScore = Math.min(...unseen.map((candidate) => candidate.totalScore));
        if (challenge.totalScore !== lowestScore) {
          throw new Error(`Coverage must proceed from low score to high score — choose a ${lowestScore}-point challenge before ${uniqueCode} (${challenge.totalScore} points)`);
        }
      }
      // One worker one challenge: this owner must not already hold another.
      const alreadyHolding = Object.values(this.state.challenges).find((candidate) =>
        candidate.owner === owner && candidate.uniqueCode !== uniqueCode &&
        (candidate.status === "reserved" || candidate.status === "running")
      );
      if (alreadyHolding) {
        throw new Error(`Owner ${owner} already holds ${alreadyHolding.uniqueCode} — one challenge per worker`);
      }
      // SubAgent slot check: inside the serializer so 3 concurrent assigns can't all pass.
      if (options?.isSubagent) {
        const subagentCount = this.activeSubagentCount();
        if (subagentCount >= BENCHMARK_MAX_SUBAGENTS) {
          throw new Error(`Benchmark SubAgent limit (${BENCHMARK_MAX_SUBAGENTS}) reached — wait for one to return or defer/abandon`);
        }
      }
      // Re-acquiring an orphan whose platform container is already live must
      // not count that same container twice against the three-slot cap.
      if (countActiveContainers(this.state, uniqueCode) >= BENCHMARK_MAX_CONTAINERS) {
        throw new Error(`Container limit (${BENCHMARK_MAX_CONTAINERS}) reached — defer or abandon one first`);
      }
      const wasOrphaned = challenge.status === "orphaned";
      const startsNewAttempt = !resumesLiveAttempt;
      challenge.reservationPreviousStatus = challenge.status;
      challenge.reservationStartedNewAttempt = startsNewAttempt;
      challenge.status = "reserved";
      challenge.owner = owner;
      challenge.pendingStatus = undefined;
      challenge.closeFailureRecorded = false;
      const now = this.now();
      if (!wasOrphaned) challenge.acquiredAt = now;
      if (startsNewAttempt) {
        // A restart leaves a stopped orphan's attempt open (a live orphan
        // resumes it above). Starting fresh must still record what the
        // interrupted attempt did, or the recovery brief loses that route.
        if (challenge.currentAttemptStartedAt !== null) {
          finishAttempt(challenge, now, "attempt interrupted by restart before a fresh attempt", this.metrics, "environment_failure");
        }
        // The attempt clock starts only after the platform start succeeds.
        // Slow or unstable control-plane calls must not steal solving time.
        challenge.currentAttemptWorker = owner;
      } else {
        challenge.currentAttemptWorker = owner;
      }
      if (!this.metrics.challenges[uniqueCode]) {
        this.metrics.challenges[uniqueCode] = { acquiredAt: now, solvedAt: null, durationMs: null, attempts: 0, wrongFlags: 0, hintsUsed: 0, deferredCount: 0 };
      }
      recalculate(this.state, this.metrics, this.now);
      await this.persist();
      return challenge;
    });
  }

  /** Phase 2: platform start succeeded — record container addresses and activate. */
  /** Identity-checked owner rebinding: only succeeds if the current owner matches expectedOwner. */
  async bindOwner(uniqueCode: string, expectedOwner: ChallengeOwner, newOwner: ChallengeOwner): Promise<ChallengeState> {
    return this.serialize(async () => {
      const challenge = this.state.challenges[uniqueCode];
      if (!challenge) throw new Error(`Challenge ${uniqueCode} not found`);
      if (challenge.owner !== expectedOwner) {
        throw new Error(`Owner mismatch for ${uniqueCode}: expected ${expectedOwner}, actual ${challenge.owner} — refusing to rebind`);
      }
      challenge.owner = newOwner;
      if (challenge.currentAttemptWorker === expectedOwner && newOwner) challenge.currentAttemptWorker = newOwner;
      await this.persist();
      return challenge;
    });
  }

  async confirmStarted(uniqueCode: string, containerAddrs: string[], expectedOwner: Exclude<ChallengeOwner, null>, containerRecreated = false): Promise<ChallengeState> {
    return this.serialize(async () => {
      const challenge = this.state.challenges[uniqueCode];
      if (!challenge) throw new Error(`Challenge ${uniqueCode} not found in ledger`);
      if (challenge.status !== "reserved") throw new Error(`Challenge ${uniqueCode} is ${challenge.status}, expected reserved`);
      requireOwner(challenge, expectedOwner);
      if (containerAddrs.length === 0) throw new Error(`Challenge ${uniqueCode} started without a container address`);
      const startsNewAttempt = challenge.reservationStartedNewAttempt;
      const now = this.now();
      challenge.status = "running";
      challenge.reservationPreviousStatus = undefined;
      challenge.reservationStartedNewAttempt = false;
      const sameLiveContainer = challenge.containerStatus === "available" && challenge.containerAddrs.length > 0
        && JSON.stringify([...challenge.containerAddrs].sort()) === JSON.stringify([...containerAddrs].sort());
      if (containerRecreated || !sameLiveContainer) challenge.containerEpoch = (challenge.containerEpoch ?? 0) + 1;
      challenge.containerAddrs = containerAddrs;
      challenge.containerStatus = "available";
      if (startsNewAttempt || challenge.currentAttemptStartedAt === null) {
        challenge.attemptCount += 1;
        challenge.attemptId = randomUUID();
        challenge.resources = emptyResources(now);
        challenge.attemptIncident = undefined;
        challenge.handoffStatus = "not_requested";
        challenge.currentAttemptStartedAt = now;
        challenge.currentAttemptPhase = challenge.attemptCount === 1 ? "coverage" : "revisit";
        challenge.currentAttemptWorker = expectedOwner;
        challenge.flagsAtAttemptStart = challenge.correctFlagCount;
        challenge.currentApproach = "";
        challenge.lastSignalAt = now;
        challenge.lastMeaningfulProgressAt = now;
        challenge.hardDeadlineAt = challenge.attemptCount === 1 ? now + FIRST_ATTEMPT_LIMIT_MS : null;
        challenge.firstAttemptWarningIssuedAt = null;
        const metric = this.metrics.challenges[uniqueCode];
        if (metric) metric.attempts += 1;
      }
      recalculate(this.state, this.metrics, this.now);
      await this.persist();
      return challenge;
    });
  }

  /** Rollback a failed start after a successful reserve. */
  async releaseReservation(uniqueCode: string, expectedOwner: Exclude<ChallengeOwner, null>, options?: { resourceUnavailable?: boolean; reason?: string }): Promise<ChallengeState> {
    return this.serialize(async () => {
      const challenge = this.state.challenges[uniqueCode];
      if (!challenge) throw new Error(`Challenge ${uniqueCode} not found`);
      if (challenge.status !== "reserved") throw new Error(`Challenge ${uniqueCode} is ${challenge.status}, expected reserved`);
      requireOwner(challenge, expectedOwner);
      const previousStatus = challenge.reservationPreviousStatus ?? "pending";
      const startedNewAttempt = challenge.reservationStartedNewAttempt;
      challenge.status = options?.resourceUnavailable && challenge.attemptCount === 0 ? "deferred" : previousStatus;
      challenge.reservationPreviousStatus = undefined;
      challenge.reservationStartedNewAttempt = false;
      challenge.owner = null;
      if (startedNewAttempt) {
        challenge.currentAttemptWorker = null;
      }
      if (options?.resourceUnavailable && challenge.attemptCount === 0) {
        const now = this.now();
        challenge.deferredReason = cleanText(options.reason ?? "platform resource unavailable", 1_000);
        enqueueForRevisit(this.state, challenge, now);
        appendBlackboard(challenge, {
          at: now,
          worker: expectedOwner,
          kind: "attempt_end",
          summary: challenge.deferredReason,
          evidenceRef: "platform:resource-unavailable",
          approach: "platform start",
          triedFamilies: [],
          ruledOutFamilies: [],
          nextProbe: "Retry after every other challenge has received its first attempt"
        });
      }
      recalculate(this.state, this.metrics, this.now);
      await this.persist();
      return challenge;
    });
  }

  /** Atomically acquire a challenge (convenience: reserve + confirmStarted in one). */
  async acquire(uniqueCode: string, owner: ChallengeOwner, containerAddrs: string[]): Promise<ChallengeState> {
    if (!owner) throw new Error("An owner is required to acquire a benchmark challenge");
    await this.reserve(uniqueCode, owner);
    return this.confirmStarted(uniqueCode, containerAddrs, owner);
  }

  /** Record a checkpoint. Only evidence-backed progress (or a new Endgame
   * approach epoch) resets time; rewriting the same observation never does. */
  async checkpoint(
    uniqueCode: string,
    signal: string,
    triedFamilies: string[] | undefined,
    nextProbe: string | undefined,
    expectedOwner: Exclude<ChallengeOwner, null>,
    options?: { signalKind?: ProgressSignalKind; evidenceRef?: string; currentApproach?: string; ruledOutFamilies?: string[]; supersedesEvidenceRef?: string }
  ): Promise<{ updated: boolean; extended: boolean; challenge: ChallengeState }> {
    return this.serialize(async () => {
      const challenge = this.state.challenges[uniqueCode];
      if (!challenge) throw new Error(`Challenge ${uniqueCode} not found`);
      requireOwner(challenge, expectedOwner);
      const normalizedSignal = cleanText(signal, 2_000);
      if (!normalizedSignal) throw new Error("signal must not be empty");
      const isNew = normalizedSignal !== challenge.lastSignalContent;
      if (isNew) challenge.lastSignalContent = normalizedSignal;
      if (triedFamilies?.length) {
        const normalized = triedFamilies.map((item) => cleanText(item, 100)).filter(Boolean);
        challenge.triedFamilies = [...new Set([...challenge.triedFamilies, ...normalized])].slice(-20);
      }
      if (options?.supersedesEvidenceRef) {
        const superseded = cleanText(options.supersedesEvidenceRef, 500);
        const removedExclusions = challenge.blackboard.filter((entry) => entry.evidenceRef === superseded && entry.kind === "decisive_rule_out")
          .flatMap((entry) => entry.ruledOutFamilies);
        const remainingExclusions = challenge.blackboard.filter((entry) => entry.evidenceRef !== superseded && entry.kind === "decisive_rule_out")
          .flatMap((entry) => entry.ruledOutFamilies);
        challenge.ruledOutFamilies = challenge.ruledOutFamilies.filter((family) => !removedExclusions.includes(family) || remainingExclusions.includes(family));
        challenge.progressKeys = challenge.progressKeys.filter((key) => !key.endsWith(`\u0000${superseded.toLowerCase()}`));
        challenge.blackboard = challenge.blackboard.filter((entry) => entry.evidenceRef !== superseded);
        if (challenge.lastEvidenceRef === superseded) {
          challenge.lastEvidenceRef = "";
          challenge.lastMeaningfulSignalContent = "";
        }
      }
      if (options?.ruledOutFamilies?.length) {
        const normalized = options.ruledOutFamilies.map((item) => cleanText(item, 100)).filter(Boolean);
        challenge.ruledOutFamilies = [...new Set([...challenge.ruledOutFamilies, ...normalized])].slice(-20);
      }
      if (nextProbe) challenge.nextProbe = cleanText(nextProbe, 1_000);
      const approach = options?.currentApproach ? cleanText(options.currentApproach, 300) : "";
      if (approach) challenge.currentApproach = approach;

      const kind = options?.signalKind ?? "note";
      const evidenceRef = cleanText(options?.evidenceRef ?? "", 500);
      const strongKind = kind === "foothold" || kind === "credential" || kind === "privilege_change"
        || kind === "exploit_primitive" || kind === "stage_transition";
      const decisiveRuleOut = kind === "decisive_rule_out" && Boolean(evidenceRef)
        && Boolean(options?.ruledOutFamilies?.some((family) => family.trim()));
      const key = progressKey(kind, evidenceRef);
      const newEvidence = !challenge.progressKeys.includes(key);
      const evidenceBacked = (strongKind && Boolean(evidenceRef)) || decisiveRuleOut;
      if (newEvidence && evidenceBacked) {
        const now = this.now();
        challenge.lastSignalAt = now;
        challenge.lastMeaningfulProgressAt = now;
        challenge.lastMeaningfulSignalContent = normalizedSignal;
        challenge.lastSignalKind = kind;
        challenge.lastEvidenceRef = evidenceRef;
        challenge.progressKeys = [...challenge.progressKeys, key].slice(-30);
      }
      appendBlackboard(challenge, {
        at: this.now(),
        worker: expectedOwner,
        kind,
        summary: normalizedSignal,
        evidenceRef,
        approach: challenge.currentApproach,
        triedFamilies: challenge.triedFamilies.slice(-10),
        ruledOutFamilies: (options?.ruledOutFamilies ?? []).map((family) => cleanText(family, 100)).filter(Boolean).slice(-10),
        nextProbe: challenge.nextProbe
      });
      await this.persist();
      return { updated: isNew, extended: false, challenge };
    });
  }

  /** Evidence references recorded for a challenge are the only references a submit may use. */
  hasEvidenceReference(uniqueCode: string, evidenceRef: string): boolean {
    const challenge = this.state.challenges[uniqueCode];
    if (!challenge || !evidenceRef.trim()) return false;
    return challenge.blackboard.some((entry) => entry.evidenceRef === evidenceRef)
      || challenge.lastEvidenceRef === evidenceRef;
  }

  assertAttemptFence(uniqueCode: string, owner: Exclude<ChallengeOwner, null>, attemptId?: string, containerEpoch?: number): void {
    const challenge = this.state.challenges[uniqueCode];
    if (!challenge) throw new Error(`Challenge ${uniqueCode} not found`);
    requireOwner(challenge, owner);
    assertFence(challenge, { uniqueCode, owner, attemptId: attemptId ?? challenge.attemptId ?? "", containerEpoch: containerEpoch ?? challenge.containerEpoch ?? 0 });
  }

  /** Save final reports even after explicit defer; do not alter ownership or progress. */
  async recordChildHandoff(uniqueCode: string, worker: `subagent:${string}`, summary: string): Promise<void> {
    const sections = childHandoffSections(summary);
    // An empty report is solver behavior (e.g. the model spent its output budget on
    // thinking), not a harness fault — attribute it to the attempt and move on.
    if (!summary.trim()) {
      const fence = captureFence(this.state.challenges[uniqueCode], worker);
      if (fence) await this.recordAttemptIncident(fence, "solver_failure", "Child returned an empty report", "handoff");
      return;
    }
    await this.serialize(async () => {
      const challenge = this.state.challenges[uniqueCode];
      if (!challenge || (challenge.owner !== worker && challenge.currentAttemptWorker !== worker
        && challenge.approachHistory.at(-1)?.worker !== worker)) return;
      if (challenge.currentAttemptStartedAt !== null && challenge.currentAttemptWorker !== worker) return;
      const fence = attemptContext.getStore();
      if (fence) assertFence(challenge, fence, challenge.owner === null);
      const directory = join(benchmarkDir(this.parentSessionId), "handoffs");
      await mkdir(directory, { recursive: true, mode: 0o700 });
      const reportPath = join(directory, `${createHash("sha256").update(worker + summary).digest("hex")}.txt`);
      const token = process.env.BENCHMARK_TOKEN;
      await writeFile(reportPath, token ? summary.split(token).join("[REDACTED_BENCHMARK_TOKEN]") : summary, { mode: 0o600 });
      challenge.handoffStatus = "saved";
      const ended = challenge.approachHistory.at(-1);
      if (ended && ended.attemptId === challenge.attemptId) ended.handoffStatus = "saved";
      if (!sections.length) sections.push("UNCERTAINTIES: Automatic extraction of this child report failed. Inspect the original report as unverified data; do not inherit its plan.");
      for (const section of sections) appendBlackboard(challenge, {
        at: this.now(), worker, kind: "handoff", summary: cleanText(section, 2_000),
        evidenceRef: reportPath, approach: "", triedFamilies: [], ruledOutFamilies: [], nextProbe: ""
      });
      await this.persist();
    });
  }

  async recordPendingSubmissionStatus(uniqueCode: string, worker: Exclude<ChallengeOwner, null>, summary: string, flag: string): Promise<void> {
    await this.serialize(async () => {
      const challenge = this.state.challenges[uniqueCode];
      if (!challenge) return;
      const fence = attemptContext.getStore();
      if (fence) assertFence(challenge, fence);
      appendBlackboard(challenge, { at: this.now(), worker, kind: "note", summary: cleanText(summary, 1_000),
        evidenceRef: `platform:pending:${flagHash(flag)}`, approach: "", triedFamilies: [], ruledOutFamilies: [], nextProbe: "" });
      await this.persist();
    });
  }

  async recordPasswordEnumerationTime(uniqueCode: string, elapsedMs: number): Promise<void> {
    await this.serialize(async () => {
      const challenge = this.state.challenges[uniqueCode];
      if (!challenge || !Number.isFinite(elapsedMs)) return;
      challenge.passwordEnumerationMs += Math.max(0, elapsedMs);
      await this.persist();
    });
  }

  /** Record a flag submission result. `challengeScore` is the platform's PER-CHALLENGE
   * cumulative score (hint deductions already included). Flags are stored as SHA-256
   * hashes to avoid persisting plaintext answers. */
  async recordSubmission(uniqueCode: string, flag: string, correct: boolean, challengeScore: number | undefined, correctFlagCount: number, matchedFlagIndex: number | null, expectedOwner: Exclude<ChallengeOwner, null>, allowUnowned = false): Promise<ChallengeState> {
    return this.serialize(async () => {
      const challenge = this.state.challenges[uniqueCode];
      if (!challenge) throw new Error(`Challenge ${uniqueCode} not found`);
      requireOwner(challenge, expectedOwner, allowUnowned);
      const previousCorrectFlagCount = challenge.correctFlagCount;
      if (challengeScore !== undefined) {
        challenge.scoreObtained = challengeScore;
        challenge.scoreKnown = true;
      } else if (correct) {
        // Progress advanced without a priced response — this challenge's
        // cached value no longer matches its correctFlagCount.
        challenge.scoreKnown = false;
      }
      challenge.correctFlagCount = correctFlagCount;
      const hash = flagHash(flag);
      challenge.blackboard = challenge.blackboard.filter((entry) => entry.evidenceRef !== `platform:pending:${hash}`);
      if (!challenge.triedFlags.includes(hash)) challenge.triedFlags.push(hash);
      if (correct && matchedFlagIndex !== null && !challenge.matchedFlagIndexes.includes(matchedFlagIndex)) {
        challenge.matchedFlagIndexes.push(matchedFlagIndex);
      }
      if (correct && correctFlagCount > previousCorrectFlagCount) {
        const now = this.now();
        challenge.lastSignalAt = now;
        challenge.lastMeaningfulProgressAt = now;
        challenge.lastAcceptedFlagAt = now;
        challenge.lastSignalKind = "stage_transition";
        challenge.lastEvidenceRef = `platform:flag-count:${correctFlagCount}`;
        challenge.lastSignalContent = `Platform accepted a new flag; progress ${correctFlagCount}/${challenge.flagCount}`;
        challenge.lastMeaningfulSignalContent = challenge.lastSignalContent;
        appendBlackboard(challenge, {
          at: now,
          worker: expectedOwner,
          kind: "submission",
          summary: challenge.lastSignalContent,
          evidenceRef: challenge.lastEvidenceRef,
          approach: challenge.currentApproach,
          triedFamilies: challenge.triedFamilies.slice(-10),
          ruledOutFamilies: challenge.ruledOutFamilies.slice(-10),
          nextProbe: challenge.nextProbe
        });
      }
      const metric = this.metrics.challenges[uniqueCode];
      if (metric) {
        if (!correct) metric.wrongFlags += 1;
        this.metrics.totalWrongSubmissions += correct ? 0 : 1;
      }
      recalculate(this.state, this.metrics, this.now);
      await this.persist();
      return challenge;
    });
  }

  /** Mark logical completion. A live container remains in closing until confirmed stopped.
   * `challengeScore` is the platform's per-challenge cumulative; omit it when no priced
   * response was received (recalculate keeps the run total a lower bound). */
  async markSolved(uniqueCode: string, challengeScore: number | undefined, expectedOwner: Exclude<ChallengeOwner, null>, allowUnowned = false): Promise<ChallengeState> {
    return this.serialize(async () => {
      const challenge = this.state.challenges[uniqueCode];
      if (!challenge) throw new Error(`Challenge ${uniqueCode} not found`);
      requireOwner(challenge, expectedOwner, allowUnowned);
      const now = this.now();
      finishAttempt(challenge, now, "solved", this.metrics, "solved");
      const needsClose = hasActiveContainer(challenge);
      challenge.status = needsClose ? "closing" : "solved";
      challenge.pendingStatus = needsClose ? "solved" : undefined;
      challenge.owner = null;
      challenge.isCompleted = true;
      challenge.solvedAt = now;
      if (!needsClose) {
        challenge.containerAddrs = [];
        challenge.containerStatus = "stopped";
      }
      if (challengeScore !== undefined) {
        challenge.scoreObtained = challengeScore;
        challenge.scoreKnown = true;
      }
      const metric = this.metrics.challenges[uniqueCode];
      if (metric && challenge.acquiredAt) {
        metric.solvedAt = now;
        metric.durationMs = now - challenge.acquiredAt;
      }
      recalculate(this.state, this.metrics, this.now);
      await this.persist();
      return challenge;
    });
  }

  /** Defer: save recovery state, set closing (close must be confirmed before terminal). */
  async defer(uniqueCode: string, reason: string, nextProbe: string | undefined, expectedOwner: Exclude<ChallengeOwner, null>): Promise<ChallengeState> {
    return this.serialize(async () => {
      const challenge = this.state.challenges[uniqueCode];
      if (!challenge) throw new Error(`Challenge ${uniqueCode} not found`);
      requireOwner(challenge, expectedOwner);
      challenge.deferredReason = cleanText(reason, 1_000);
      if (nextProbe) challenge.nextProbe = cleanText(nextProbe, 1_000);
      const now = this.now();
      appendBlackboard(challenge, {
        at: now,
        worker: expectedOwner,
        kind: "attempt_end",
        summary: challenge.deferredReason || "Deferred for a later attempt",
        evidenceRef: challenge.lastEvidenceRef,
        approach: challenge.currentApproach,
        triedFamilies: challenge.triedFamilies.slice(-10),
        ruledOutFamilies: challenge.ruledOutFamilies.slice(-10),
        nextProbe: challenge.nextProbe
      });
      finishAttempt(challenge, now, challenge.deferredReason || "deferred", this.metrics, "deferred");
      enqueueForRevisit(this.state, challenge, now);
      const metric = this.metrics.challenges[uniqueCode];
      if (metric) metric.deferredCount += 1;
      this.metrics.totalDefers += 1;
      challenge.status = "closing";
      challenge.owner = null;
      challenge.pendingStatus = "deferred";
      recalculate(this.state, this.metrics, this.now);
      await this.persist();
      return challenge;
    });
  }

  /** Abandon: terminal exhausted (close must be confirmed). */
  async abandon(uniqueCode: string, reason: string, expectedOwner: Exclude<ChallengeOwner, null>): Promise<ChallengeState> {
    return this.serialize(async () => {
      const challenge = this.state.challenges[uniqueCode];
      if (!challenge) throw new Error(`Challenge ${uniqueCode} not found`);
      requireOwner(challenge, expectedOwner);
      challenge.deferredReason = cleanText(reason, 1_000);
      finishAttempt(challenge, this.now(), challenge.deferredReason || "exhausted", this.metrics);
      challenge.status = "closing";
      challenge.owner = null;
      challenge.pendingStatus = "exhausted";
      recalculate(this.state, this.metrics, this.now);
      await this.persist();
      return challenge;
    });
  }

  /** Confirm the platform closed the container; transition to the pending terminal status. */
  async confirmClosed(uniqueCode: string): Promise<ChallengeState> {
    return this.serialize(async () => {
      const challenge = this.state.challenges[uniqueCode];
      if (!challenge) throw new Error(`Challenge ${uniqueCode} not found`);
      const fence = attemptContext.getStore();
      if (fence) assertFence(challenge, fence, true);
      if (challenge.status !== "closing" || !challenge.pendingStatus) {
        throw new Error(`Challenge ${uniqueCode} is ${challenge.status}, not awaiting close confirmation`);
      }
      challenge.status = challenge.pendingStatus ?? "deferred";
      challenge.pendingStatus = undefined;
      challenge.owner = null;
      challenge.containerAddrs = [];
      challenge.containerStatus = "stopped";
      challenge.closeFailureRecorded = false;
      recalculate(this.state, this.metrics, this.now);
      await this.persist();
      return challenge;
    });
  }

  /** Close failed: the container is still alive on the platform — track the leak
   * and keep it visible in the active count even for solved challenges. */
  async markCloseFailed(uniqueCode: string): Promise<ChallengeState> {
    return this.serialize(async () => {
      const challenge = this.state.challenges[uniqueCode];
      if (!challenge) throw new Error(`Challenge ${uniqueCode} not found`);
      const fence = attemptContext.getStore();
      if (fence) assertFence(challenge, fence, true);
      if (!challenge.closeFailureRecorded) this.metrics.containerLeaks += 1;
      challenge.closeFailureRecorded = true;
      // Keep the container visible in the active count — for solved challenges,
      // revert to closing so countActiveContainers() sees it (solved with a
      // still-running container is not truly freed). Preserve "stop_pending":
      // it already counts as active and is the more accurate platform state.
      if (challenge.containerStatus !== "stop_pending") challenge.containerStatus = "available";
      if (challenge.status === "solved") {
        challenge.status = "closing";
        challenge.pendingStatus = "solved";
      }
      recalculate(this.state, this.metrics, this.now);
      await this.persist();
      return challenge;
    });
  }

  /** Record hint usage and content. */
  async recordHint(uniqueCode: string, hint: string | null, expectedOwner: Exclude<ChallengeOwner, null>): Promise<ChallengeState> {
    return this.serialize(async () => {
      const challenge = this.state.challenges[uniqueCode];
      if (!challenge) throw new Error(`Challenge ${uniqueCode} not found`);
      if (challenge.attemptCount < 2) throw new Error("Hints are available only from the second attempt onward");
      if (challenge.status === "solved" || challenge.status === "exhausted" || challenge.status === "closing") {
        throw new Error(`Challenge ${uniqueCode} is ${challenge.status}; a hint would be wasted`);
      }
      // Main may buy a hint for an unowned deferred challenge before assigning
      // it to a fresh second-pass worker, but never for another live worker.
      requireOwner(challenge, expectedOwner, expectedOwner === "main");
      challenge.hintUsed = true;
      challenge.hintContent = hint;
      // The raw API contract applies the deduction to subsequent flag awards;
      // points already obtained remain authoritative. The next submit response
      // will update scoreObtained with the post-hint per-challenge total.
      const metric = this.metrics.challenges[uniqueCode];
      if (metric) metric.hintsUsed += 1;
      this.metrics.totalHintsUsed += 1;
      await this.persist();
      return challenge;
    });
  }

  /** Release a challenge when a subagent exits abnormally without completing it.
   * Sets closing state — the caller must confirmClosed() after the platform confirms. */
  async releaseOnSubagentExit(uniqueCode: string, reason: string, expectedOwner: `subagent:${string}`, pendingStatus: "deferred" | "exhausted" = "deferred"): Promise<ChallengeState> {
    return this.serialize(async () => {
      const challenge = this.state.challenges[uniqueCode];
      if (!challenge) throw new Error(`Challenge ${uniqueCode} not found`);
      if (challenge.status === "solved" || challenge.status === "exhausted") return challenge;
      requireOwner(challenge, expectedOwner);
      const now = this.now();
      appendBlackboard(challenge, {
        at: now,
        worker: expectedOwner,
        kind: "attempt_end",
        summary: cleanText(reason, 1_000),
        evidenceRef: challenge.lastEvidenceRef,
        approach: challenge.currentApproach,
        triedFamilies: challenge.triedFamilies.slice(-10),
        ruledOutFamilies: challenge.ruledOutFamilies.slice(-10),
        nextProbe: challenge.nextProbe
      });
      finishAttempt(challenge, now, reason, this.metrics);
      challenge.status = "closing";
      challenge.owner = null;
      challenge.deferredReason = cleanText(reason, 1_000);
      challenge.pendingStatus = pendingStatus;
      if (pendingStatus === "deferred") enqueueForRevisit(this.state, challenge, now);
      recalculate(this.state, this.metrics, this.now);
      await this.persist();
      return challenge;
    });
  }

  /** Release any locally active challenge while archiving its parent session.
   * Unlike worker-facing defer, cleanup may adopt an orphan left by a restart. */
  async releaseForSessionCleanup(uniqueCode: string, reason: string, closeRequired: boolean): Promise<ChallengeState> {
    return this.serialize(async () => {
      const challenge = this.state.challenges[uniqueCode];
      if (!challenge) throw new Error(`Challenge ${uniqueCode} not found`);
      const now = this.now();
      const cleanupWorker = challenge.currentAttemptWorker ?? challenge.owner ?? "main";
      if (challenge.currentAttemptStartedAt !== null) finishAttempt(challenge, now, reason, this.metrics, "cancelled");
      challenge.owner = null;
      challenge.reservationPreviousStatus = undefined;
      challenge.reservationStartedNewAttempt = false;
      challenge.closeFailureRecorded = false;

      let terminal: "solved" | "exhausted" | "deferred";
      if (challenge.isCompleted || challenge.status === "solved" || challenge.pendingStatus === "solved") {
        terminal = "solved";
      } else if (challenge.status === "exhausted" || challenge.pendingStatus === "exhausted") {
        terminal = "exhausted";
      } else {
        terminal = "deferred";
        challenge.deferredReason = cleanText(reason, 1_000);
        enqueueForRevisit(this.state, challenge, now);
        appendBlackboard(challenge, {
          at: now,
          worker: cleanupWorker,
          kind: "attempt_end",
          summary: challenge.deferredReason,
          evidenceRef: challenge.lastEvidenceRef,
          approach: challenge.currentApproach,
          triedFamilies: challenge.triedFamilies.slice(-10),
          ruledOutFamilies: challenge.ruledOutFamilies.slice(-10),
          nextProbe: challenge.nextProbe
        });
      }

      challenge.status = closeRequired ? "closing" : terminal;
      challenge.pendingStatus = closeRequired ? terminal : undefined;
      if (!closeRequired) {
        challenge.containerAddrs = [];
        challenge.containerStatus = "stopped";
      }
      recalculate(this.state, this.metrics, this.now);
      await this.persist();
      return challenge;
    });
  }

  /** Count of active benchmark subagent challenges. */
  activeSubagentCount(): number {
    return Object.values(this.state.challenges).filter((challenge) =>
      challenge.owner !== null && challenge.owner !== "main"
      && (challenge.status === "reserved" || challenge.status === "running")
    ).length;
  }

  budgetFor(uniqueCode: string): ChallengeBudget | undefined {
    const challenge = this.state.challenges[uniqueCode];
    if (!challenge) return undefined;
    const now = this.now();
    const startedAt = challenge.currentAttemptStartedAt ?? now;
    const elapsedMs = Math.max(0, now - startedAt);
    const firstAttempt = challenge.attemptCount === 1 && challenge.currentAttemptStartedAt !== null;
    return {
      firstAttempt,
      elapsedMs,
      warningDue: firstAttempt && elapsedMs >= FIRST_ATTEMPT_WARNING_MS && challenge.firstAttemptWarningIssuedAt === null,
      expired: firstAttempt && now >= (challenge.hardDeadlineAt ?? startedAt + FIRST_ATTEMPT_LIMIT_MS)
    };
  }

  budgetForOwner(owner: Exclude<ChallengeOwner, null>): { challenge: ChallengeState; budget: ChallengeBudget } | undefined {
    const challenge = Object.values(this.state.challenges).find((candidate) =>
      candidate.owner === owner && (candidate.status === "running" || candidate.status === "reserved")
    );
    if (!challenge) return undefined;
    const budget = this.budgetFor(challenge.uniqueCode);
    return budget ? { challenge, budget } : undefined;
  }

  /** Whether a challenge's attempt budget is exhausted. */
  isBudgetExhausted(uniqueCode: string): boolean {
    return this.budgetFor(uniqueCode)?.expired ?? false;
  }

  /** Consume the only proactive timer notice. Routine context remains silent. */
  async consumeFirstAttemptWarning(owner: Exclude<ChallengeOwner, null>): Promise<ChallengeState | undefined> {
    return this.serialize(async () => {
      const active = Object.values(this.state.challenges).find((candidate) =>
        candidate.owner === owner && candidate.status === "running"
      );
      if (!active || !this.budgetFor(active.uniqueCode)?.warningDue) return undefined;
      active.firstAttemptWarningIssuedAt = this.now();
      await this.persist();
      return active;
    });
  }

  /** Candidate challenges for acquisition, ordered by priority. */
  candidates(count = 10, offset = 0): ChallengeState[] {
    const challenges = Object.values(this.state.challenges);
    const liveOrphans = challenges.filter((challenge) => challenge.status === "orphaned" && hasReusableBenchmarkContainer(challenge));
    const liveCoverageOrphans = liveOrphans.filter((challenge) => challenge.currentAttemptPhase === "coverage");
    const liveRevisitOrphans = liveOrphans.filter((challenge) => challenge.currentAttemptPhase !== "coverage");
    const stoppedCoverageOrphans = challenges.filter((challenge) => challenge.status === "orphaned"
      && challenge.currentAttemptPhase === "coverage" && !hasReusableBenchmarkContainer(challenge));
    const unseen = challenges.filter((challenge) => !challenge.isCompleted && challenge.attemptCount === 0
      && (challenge.status === "pending" || challenge.status === "orphaned"));
    const unavailableCoverage = challenges.filter((challenge) => !challenge.isCompleted
      && challenge.attemptCount === 0 && challenge.status === "deferred");
    // Tiered so coverage keeps priority. A stranded revisit orphan surfaces
    // last during coverage: resuming it is permitted (it already owns its
    // platform slot) and may be the only way to free a saturated container cap.
    const pool: Array<{ challenge: ChallengeState; tier: number }> =
      unseen.length > 0 || liveCoverageOrphans.length > 0
        ? [
            ...liveCoverageOrphans.map((challenge) => ({ challenge, tier: 0 })),
            ...unseen.filter((challenge) => !liveCoverageOrphans.includes(challenge)).map((challenge) => ({ challenge, tier: 1 })),
            ...liveRevisitOrphans.map((challenge) => ({ challenge, tier: 2 }))
          ]
        : stoppedCoverageOrphans.length > 0
          ? [
              ...stoppedCoverageOrphans.map((challenge) => ({ challenge, tier: 0 })),
              ...liveRevisitOrphans.map((challenge) => ({ challenge, tier: 1 }))
            ]
        : unavailableCoverage.length > 0
          ? unavailableCoverage.map((challenge) => ({ challenge, tier: 0 }))
        : liveOrphans.length > 0
          ? liveOrphans.map((challenge) => ({ challenge, tier: 0 }))
          : coverageIsComplete(this.state)
            ? challenges.filter((challenge) => !challenge.isCompleted
              && (challenge.status === "deferred" || challenge.status === "orphaned" || challenge.status === "pending"))
              .map((challenge) => ({ challenge, tier: 0 }))
            : [];
    const unseenCount = unseen.length;
    return pool
      .sort((left, right) => {
        if (left.tier !== right.tier) return left.tier - right.tier;
        const a = left.challenge;
        const b = right.challenge;
        const orphanDelta = Number(b.status === "orphaned") - Number(a.status === "orphaned");
        if (orphanDelta !== 0) return orphanDelta;
        if (unseenCount === 0) {
          const queueDelta = a.revisitQueueOrder - b.revisitQueueOrder;
          if (queueDelta !== 0) return queueDelta;
        }
        const scoreDelta = a.totalScore - b.totalScore;
        if (scoreDelta !== 0) return scoreDelta;
        return a.uniqueCode.localeCompare(b.uniqueCode);
      })
      .slice(offset, offset + count)
      .map((entry) => entry.challenge);
  }

  /** Derive coverage/revisit state; there is no tactical round scheduler. */
  async maybeAdvancePhase(): Promise<BenchmarkPhase> {
    return this.serialize(async () => {
      if (this.state.phase !== "completed") this.state.phase = coverageIsComplete(this.state) ? "revisit" : "coverage";
      await this.persist();
      return this.state.phase;
    });
  }

  /** Mark the run as completed. */
  async complete(): Promise<BenchmarkState> {
    return this.serialize(async () => {
      this.state.phase = "completed";
      this.metrics.completedAt = this.now();
      await this.persist();
      return this.state;
    });
  }

  /** Increment compaction counter for metrics. */
  async recordCompaction(fence?: AttemptFence): Promise<void> {
    return this.serialize(async () => {
      this.metrics.compactionCount += 1;
      this.metrics.resources.compactionCount++;
      const challenge = fence ? this.state.challenges[fence.uniqueCode] : undefined;
      if (challenge && challenge.attemptId === fence?.attemptId && challenge.containerEpoch === fence?.containerEpoch) {
        (challenge.resources ??= emptyResources(this.now())).compactionCount++;
      }
      await this.persist();
    });
  }

  /** Observation never adopts a replacement worker's identity. */
  async recordAttemptIncident(fence: AttemptFence | undefined, source: FailureSource, reason: string, gate: string | null = null, metricsOnly = false): Promise<void> {
    if (!fence) return;
    await this.serialize(async () => {
      this.metrics.incidentCounts[source] = (this.metrics.incidentCounts[source] ?? 0) + 1;
      const challenge = this.state.challenges[fence.uniqueCode];
      const same = !metricsOnly && challenge?.attemptId === fence.attemptId && challenge.containerEpoch === fence.containerEpoch;
      const incident: AttemptIncident = { source, reason: cleanText(reason, 1_000), gate, at: this.now() };
      if (same && challenge.currentAttemptWorker === fence.owner && challenge.currentAttemptStartedAt !== null) {
        if (!challenge.attemptIncident || (FAILURE_PRIORITY[source] > FAILURE_PRIORITY[challenge.attemptIncident.source] || source === challenge.attemptIncident.source)) challenge.attemptIncident = incident;
        if (source === "harness_bad_handoff") challenge.handoffStatus = "failed";
      } else if (same) {
        // A close or handoff can fail just after logical release. Amend only
        // that attempt, never the replacement attempt or its blackboard.
        const ended = challenge.approachHistory.at(-1);
        if (ended?.attemptId === fence.attemptId && ended.worker === fence.owner && ended.terminationSource !== "solved"
          && FAILURE_PRIORITY[source] >= (FAILURE_PRIORITY[ended.terminationSource as FailureSource] ?? 0)) {
          const previous = ended.terminationSource ?? "unknown";
          this.metrics.terminationCounts[previous] = Math.max(0, (this.metrics.terminationCounts[previous] ?? 0) - 1);
          this.metrics.terminationCounts[source] = (this.metrics.terminationCounts[source] ?? 0) + 1;
          ended.terminationSource = source;
          ended.terminationReason = incident.reason;
          ended.activeGate = gate;
          if (source === "harness_bad_handoff") ended.handoffStatus = "failed";
        }
      }
      // Rejected old-generation calls only affect metrics, not ledger state.
      if (same) await this.persist();
      else await writeJsonStoreAtomic(metricsPath(this.parentSessionId), this.metrics);
    });
  }

  /** A recovered compaction is still counted in metrics, but must not become the attempt's final cause. */
  async clearAttemptIncident(fence: AttemptFence | undefined, source: FailureSource): Promise<void> {
    if (!fence) return;
    await this.serialize(async () => {
      const challenge = this.state.challenges[fence.uniqueCode];
      if (!challenge || challenge.attemptId !== fence.attemptId || challenge.containerEpoch !== fence.containerEpoch
        || challenge.currentAttemptWorker !== fence.owner || challenge.attemptIncident?.source !== source) return;
      challenge.attemptIncident = undefined;
      await this.persist();
    });
  }

  async observeTool(fence: AttemptFence, observation: { fingerprint: string; wallTime: number; estimatedTokens: number; error: boolean }): Promise<Record<string, unknown> | undefined> {
    return this.serialize(async () => {
      const challenge = this.state.challenges[fence.uniqueCode];
      if (!challenge || challenge.attemptId !== fence.attemptId || challenge.containerEpoch !== fence.containerEpoch) return;
      const ended = challenge.currentAttemptStartedAt === null ? challenge.approachHistory.at(-1) : undefined;
      if (ended && ended.attemptId !== fence.attemptId) return;
      const resources = ended?.resources ?? (challenge.resources ??= emptyResources(this.now()));
      const progress = challenge.progressRevision ?? 0;
      if (resources.progressRevision !== progress) {
        resources.callsWithoutProgress = 0; resources.repeatCount = 0; resources.fingerprints = {}; resources.progressRevision = progress;
      }
      resources.toolCalls++;
      resources.callsWithoutProgress++;
      resources.toolWallTime += observation.wallTime;
      resources.estimatedTokens += observation.estimatedTokens;
      resources.toolErrorCount += Number(observation.error);
      resources.wallTime = ended ? ended.endedAt - ended.startedAt : Math.max(0, this.now() - (challenge.currentAttemptStartedAt ?? this.now()));
      const count = (resources.fingerprints[observation.fingerprint] ?? 0) + 1;
      resources.fingerprints[observation.fingerprint] = count;
      if (Object.keys(resources.fingerprints).length > 32) delete resources.fingerprints[Object.keys(resources.fingerprints)[0]];
      resources.repeatCount += Number(count > 1);
      if (ended) ended.toolErrorCount = resources.toolErrorCount;
      for (const [key, amount] of Object.entries({ toolCalls: 1, toolWallTime: observation.wallTime, estimatedTokens: observation.estimatedTokens, toolErrorCount: Number(observation.error), repeatCount: Number(count > 1) })) {
        this.metrics.resources[key as "toolCalls"] += amount;
      }
      const stalled = resources.callsWithoutProgress >= 20 || (resources.callsWithoutProgress >= 5 && this.now() - resources.lastProgressAt >= 5 * 60_000);
      const warn = !ended && (count % 3 === 0 || (stalled && resources.toolCalls - resources.lastWarningCall >= 10));
      if (warn) resources.lastWarningCall = resources.toolCalls;
      await this.persist();
      return warn ? { kind: count % 3 === 0 ? "REPEATED_WITHOUT_NEW_INFORMATION" : "NO_DURABLE_PROGRESS", challenge: fence.uniqueCode,
        attemptId: fence.attemptId, containerEpoch: fence.containerEpoch, attempt: challenge.attemptCount,
        lastProgressAt: resources.lastProgressAt, lastProgressKind: resources.lastProgressKind,
        callsWithoutProgress: resources.callsWithoutProgress, repeatCount: resources.repeatCount,
        suggestion: "Recheck assumptions, call benchmark_skill_hint, or try a different direction. This warning does not block execution." } : undefined;
    });
  }

  /** Delete the entire ledger directory (used by cleanup). */
  static async exists(parentSessionId: string): Promise<boolean> {
    try {
      await access(statePath(parentSessionId));
      return true;
    } catch {
      return false;
    }
  }

  static async destroy(parentSessionId: string): Promise<void> {
    await rm(benchmarkDir(parentSessionId), { recursive: true, force: true }).catch(() => undefined);
  }
}
