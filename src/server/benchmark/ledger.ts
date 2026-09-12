/**
 * Authoritative benchmark task ledger, persisted per parent session. The
 * platform is the source of truth for completion state; the ledger adds
 * scheduling state (deferred, signal tracking) the platform does
 * not track. Token is never written here.
 */

import { createHash } from "node:crypto";
import { access, mkdir, rm, writeFile } from "node:fs/promises";
import { join } from "node:path";
import { homedir } from "node:os";
import { readJsonStore, writeJsonStoreAtomic } from "@/server/json-store";
import { createSerializer } from "@/server/serializer";
import type { Challenge } from "./controller";
import { childHandoffSections, retainBlackboard } from "./blackboard";

export const BENCHMARK_MAX_CONTAINERS = 3;
export const BENCHMARK_MAX_SUBAGENTS = 2;
export const FIRST_ATTEMPT_WARNING_MS = 25 * 60 * 1000;
export const FIRST_ATTEMPT_LIMIT_MS = 30 * 60 * 1000;

export type ChallengeStatus = "pending" | "reserved" | "running" | "closing" | "deferred" | "solved" | "orphaned";
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
  // A failed platform start is deferred without spending a solving attempt.
  // It joins the revisit FIFO instead of preventing other work from resuming.
  return Object.values(state.challenges).every((challenge) =>
    challenge.isCompleted || challenge.status === "deferred"
      || (challenge.status === "reserved" && challenge.reservationPreviousStatus === "deferred")
      || (challenge.attemptCount > 0 && !isUnfinishedFirstAttempt(challenge))
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

function finishAttempt(challenge: ChallengeState, now: number, stopReason: string): void {
  if (!challenge.currentAttemptStartedAt || !challenge.currentAttemptWorker || !challenge.currentAttemptPhase) return;
  challenge.approachHistory = [...challenge.approachHistory, {
    attemptNumber: challenge.attemptCount,
    phase: challenge.currentAttemptPhase,
    worker: challenge.currentAttemptWorker,
    approach: challenge.currentApproach || "(not recorded)",
    startedAt: challenge.currentAttemptStartedAt,
    endedAt: now,
    flagsBefore: challenge.flagsAtAttemptStart,
    flagsAfter: challenge.correctFlagCount,
    triedFamilies: challenge.triedFamilies.slice(-20),
    ruledOutFamilies: challenge.ruledOutFamilies.slice(-20),
    stopReason: cleanText(stopReason, 1_000),
    nextDistinctApproach: challenge.nextProbe
  }].slice(-6);
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
    challenge.currentAttemptStartedAt
  ]);
}

function enqueueForRevisit(state: BenchmarkState, challenge: ChallengeState, now: number): void {
  const latest = Object.values(state.challenges).reduce((max, candidate) => Math.max(max, candidate.revisitQueueOrder || 0), 0);
  challenge.revisitQueueOrder = Math.max(now, latest + 1);
}

function appendBlackboard(challenge: ChallengeState, entry: BlackboardEntry): void {
  const previous = challenge.blackboard.at(-1);
  const duplicate = previous && previous.kind === entry.kind
    && previous.summary === entry.summary && previous.evidenceRef === entry.evidenceRef;
  if (!duplicate) challenge.blackboard = retainBlackboard([...challenge.blackboard, entry]);
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
    challenges: {}
  };
}

function defaultState(): BenchmarkState {
  return {
    phase: "coverage",
    cumulativeScore: 0,
    scoreExact: true,
    totalChallenges: 0,
    solvedCount: 0,
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
  // such orphans permanently block every new acquire (defer is
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
  if (challenge.owner === expectedOwner) return;
  if (allowUnowned && challenge.owner === null) return;
  throw new Error(`Challenge ${challenge.uniqueCode} is owned by ${challenge.owner ?? "nobody"}, not by ${expectedOwner}`);
}

function recalculate(state: BenchmarkState, metrics?: BenchmarkMetrics, now: () => number = Date.now): BenchmarkState {
  const challenges = Object.values(state.challenges);
  state.solvedCount = challenges.filter((challenge) => challenge.status === "solved" || (challenge.status === "closing" && challenge.pendingStatus === "solved")).length;
  state.totalChallenges = challenges.length;
  state.activeContainers = countActiveContainers(state);
  // cumulative_score from the platform is PER-CHALLENGE (该题累计总得分).
  // The run total is the sum of the per-challenge values; it is exact only
  // when every challenge that has scored flags carries an authoritative value.
  state.cumulativeScore = challenges.reduce((total, challenge) => total + (Number.isFinite(challenge.scoreObtained) ? challenge.scoreObtained : 0), 0);
  state.scoreExact = challenges.filter((challenge) => challenge.correctFlagCount > 0).every((challenge) => challenge.scoreKnown === true);
  const allSolved = challenges.length > 0 && challenges.every((challenge) => challenge.isCompleted && challenge.status === "solved");
  state.phase = allSolved ? "completed" : coverageIsComplete(state) ? "revisit" : "coverage";
  if (metrics) {
    if (allSolved) metrics.completedAt ??= now();
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

  constructor(parentSessionId: string, now: () => number = Date.now) {
    this.parentSessionId = parentSessionId;
    this.now = now;
  }

  /** Loads or creates the ledger. Must be called before any other operation. */
  async initialize(): Promise<BenchmarkLedger> {
    await mkdir(benchmarkDir(this.parentSessionId), { recursive: true, mode: 0o700 });
    this.state = await readJsonStore<BenchmarkState>(statePath(this.parentSessionId)) ?? defaultState();
    this.metrics = await readJsonStore<BenchmarkMetrics>(metricsPath(this.parentSessionId)) ?? defaultMetrics();
    delete (this.state as BenchmarkState & { exhaustedCount?: number }).exhaustedCount;
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
      // Older runs allowed permanent abandonment. Restore unfinished work,
      // including a close that was still pending when the process stopped.
      if ((challenge.pendingStatus as string) === "exhausted") challenge.pendingStatus = "deferred";
      if ((challenge.reservationPreviousStatus as string) === "exhausted") challenge.reservationPreviousStatus = "deferred";
      if ((challenge.status as string) === "exhausted") {
        const stopped = challenge.containerStatus === "stopped";
        challenge.status = challenge.isCompleted ? (stopped ? "solved" : "closing")
          : stopped ? "deferred" : challenge.containerStatus === "stop_pending" ? "closing" : "orphaned";
        challenge.pendingStatus = challenge.status === "closing" ? (challenge.isCompleted ? "solved" : "deferred") : undefined;
        challenge.owner = null;
        challenge.reservationPreviousStatus = undefined;
        challenge.reservationStartedNewAttempt = false;
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
    return serializer(action);
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
          if (existing.currentAttemptStartedAt) finishAttempt(existing, this.now(), "platform sync confirmed solved");
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
          throw new Error(`Benchmark SubAgent limit (${BENCHMARK_MAX_SUBAGENTS}) reached — continue your challenge until a worker returns or a justified handoff frees a slot`);
        }
      }
      // Re-acquiring an orphan whose platform container is already live must
      // not count that same container twice against the three-slot cap.
      if (countActiveContainers(this.state, uniqueCode) >= BENCHMARK_MAX_CONTAINERS) {
        throw new Error(`Container limit (${BENCHMARK_MAX_CONTAINERS}) reached — continue an active challenge or defer one for a justified handoff first`);
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
          finishAttempt(challenge, now, "attempt interrupted by restart before a fresh attempt");
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

  async confirmStarted(uniqueCode: string, containerAddrs: string[], expectedOwner: Exclude<ChallengeOwner, null>): Promise<ChallengeState> {
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
      challenge.containerAddrs = containerAddrs;
      challenge.containerStatus = "available";
      if (startsNewAttempt || challenge.currentAttemptStartedAt === null) {
        challenge.attemptCount += 1;
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

  /** Save final reports even after explicit defer; do not alter ownership or progress. */
  async recordChildHandoff(uniqueCode: string, worker: `subagent:${string}`, summary: string): Promise<void> {
    const sections = childHandoffSections(summary);
    if (!summary.trim()) return;
    await this.serialize(async () => {
      const challenge = this.state.challenges[uniqueCode];
      if (!challenge || (challenge.owner !== worker && challenge.currentAttemptWorker !== worker
        && !challenge.approachHistory.some((attempt) => attempt.worker === worker))) return;
      const directory = join(benchmarkDir(this.parentSessionId), "handoffs");
      await mkdir(directory, { recursive: true, mode: 0o700 });
      const reportPath = join(directory, `${createHash("sha256").update(worker + summary).digest("hex")}.txt`);
      const token = process.env.BENCHMARK_TOKEN;
      await writeFile(reportPath, token ? summary.split(token).join("[REDACTED_BENCHMARK_TOKEN]") : summary, { mode: 0o600 });
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
      if (challengeScore !== undefined && correctFlagCount >= previousCorrectFlagCount) {
        challenge.scoreObtained = challengeScore;
        challenge.scoreKnown = true;
      } else if (correct && correctFlagCount > previousCorrectFlagCount) {
        // Progress advanced without a priced response — this challenge's
        // cached value no longer matches its correctFlagCount.
        challenge.scoreKnown = false;
      }
      challenge.correctFlagCount = Math.max(previousCorrectFlagCount, correctFlagCount);
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
      finishAttempt(challenge, now, "solved");
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

  /** Preserve platform-available environments only after coverage, with at most
   * three unsolved challenges left. This is not an application health probe. */
  isEndgame(): boolean {
    return this.state.phase === "revisit"
      && Object.values(this.state.challenges).filter((challenge) => !challenge.isCompleted).length <= BENCHMARK_MAX_CONTAINERS;
  }

  private preserveEnvironment(challenge: ChallengeState): boolean {
    return this.isEndgame() && challenge.attemptCount > 1
      && challenge.containerStatus === "available" && challenge.containerAddrs.length > 0;
  }

  /** End an attempt. Final-three revisits preserve available environments unless reset is explicit. */
  async defer(uniqueCode: string, reason: string, nextProbe: string | undefined, expectedOwner: Exclude<ChallengeOwner, null>, resetEnvironment = false): Promise<ChallengeState> {
    return this.serialize(async () => {
      const challenge = this.state.challenges[uniqueCode];
      if (!challenge) throw new Error(`Challenge ${uniqueCode} not found`);
      requireOwner(challenge, expectedOwner);
      const preserve = !resetEnvironment && this.preserveEnvironment(challenge);
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
      finishAttempt(challenge, now, challenge.deferredReason || "deferred");
      enqueueForRevisit(this.state, challenge, now);
      const metric = this.metrics.challenges[uniqueCode];
      if (metric) metric.deferredCount += 1;
      this.metrics.totalDefers += 1;
      challenge.status = preserve ? "orphaned" : "closing";
      challenge.owner = null;
      challenge.pendingStatus = preserve ? undefined : "deferred";
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
      if (challenge.status === "solved" || challenge.status === "closing") {
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
   * Callers close only when status is closing; orphaned means the environment is preserved. */
  async releaseOnSubagentExit(uniqueCode: string, reason: string, expectedOwner: `subagent:${string}`): Promise<ChallengeState> {
    return this.serialize(async () => {
      const challenge = this.state.challenges[uniqueCode];
      if (!challenge) throw new Error(`Challenge ${uniqueCode} not found`);
      if (challenge.status === "solved") return challenge;
      requireOwner(challenge, expectedOwner);
      const preserve = this.preserveEnvironment(challenge);
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
      finishAttempt(challenge, now, reason);
      challenge.status = preserve ? "orphaned" : "closing";
      challenge.owner = null;
      challenge.deferredReason = cleanText(reason, 1_000);
      challenge.pendingStatus = preserve ? undefined : "deferred";
      enqueueForRevisit(this.state, challenge, now);
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
      if (challenge.currentAttemptStartedAt !== null) finishAttempt(challenge, now, reason);
      challenge.owner = null;
      challenge.reservationPreviousStatus = undefined;
      challenge.reservationStartedNewAttempt = false;
      challenge.closeFailureRecorded = false;

      let terminal: "solved" | "deferred";
      if (challenge.isCompleted || challenge.status === "solved" || challenge.pendingStatus === "solved") {
        terminal = "solved";
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

  /** Sample a due notice without consuming it before the model receives it. */
  firstAttemptWarningFor(owner: Exclude<ChallengeOwner, null>): ChallengeState | undefined {
    const active = Object.values(this.state.challenges).find((challenge) =>
      challenge.owner === owner && challenge.status === "running"
    );
    return active && this.budgetFor(active.uniqueCode)?.warningDue ? structuredClone(active) : undefined;
  }

  async acknowledgeFirstAttemptWarning(owner: Exclude<ChallengeOwner, null>, uniqueCode: string, currentAttemptStartedAt: number | null): Promise<boolean> {
    return this.serialize(async () => {
      const active = this.state.challenges[uniqueCode];
      if (!active || active.owner !== owner || active.status !== "running"
        || active.currentAttemptStartedAt !== currentAttemptStartedAt
        || !this.budgetFor(uniqueCode)?.warningDue) return false;
      active.firstAttemptWarningIssuedAt = this.now();
      await this.persist();
      return true;
    });
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
        : liveOrphans.length > 0 && !this.isEndgame()
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
        const orphanDelta = this.isEndgame() ? 0 : Number(b.status === "orphaned") - Number(a.status === "orphaned");
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

  /** Increment compaction counter for metrics. */
  async recordCompaction(): Promise<void> {
    return this.serialize(async () => {
      this.metrics.compactionCount += 1;
      await this.persist();
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
