/**
 * Authoritative benchmark task ledger, persisted per parent session. The
 * platform is the source of truth for completion state; the ledger adds
 * scheduling state (deferred, exhausted, signal tracking) the platform does
 * not track. Token is never written here.
 */

import { createHash } from "node:crypto";
import { access, mkdir, rm } from "node:fs/promises";
import { join } from "node:path";
import { homedir } from "node:os";
import { readJsonStore, writeJsonStoreAtomic } from "@/server/json-store";
import { createSerializer } from "@/server/serializer";
import type { Challenge } from "./controller";
import type { BrowserHandoffState } from "@/browser";

export const BENCHMARK_MAX_CONTAINERS = 3;
export const BENCHMARK_MAX_SUBAGENTS = 2;
export const HANDOFF_GRACE_MS = 2 * 60 * 1000;

export type ChallengeStatus = "pending" | "reserved" | "running" | "closing" | "deferred" | "handoff_waiting" | "solved" | "exhausted" | "orphaned";
export type BenchmarkPhase = "first_pass" | "second_pass" | "endgame" | "completed";
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

export type BudgetPolicy = {
  label: "first_pass" | "second_pass" | "late_recovery" | "endgame";
  noProgressMs: number;
  initialLeaseMs: number;
  signalExtensionMs: number;
  maxSignalExtensions: number;
  flagMomentumMs: number;
  workerMaxMs: number | null;
};

export type ChallengeBudget = {
  policy: BudgetPolicy;
  elapsedMs: number;
  sinceProgressMs: number;
  hardRemainingMs: number | null;
  noProgressExpired: boolean;
  hardExpired: boolean;
  workerRotationDue: boolean;
  expired: boolean;
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
  progressExtensions: number;
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
  reservationPreviousHandoffExpiresAt: number | null;
  reservationStartedNewAttempt: boolean;
  closeFailureRecorded: boolean;
  acquiredAt: number | null;
  solvedAt: number | null;
  handoffExpiresAt: number | null;
  /** Local-only authenticated browser state; never included in continuity or tool output. */
  browserHandoffState: BrowserHandoffState | null;
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

const MINUTE = 60_000;

export function isPartialChallenge(challenge: Pick<ChallengeState, "correctFlagCount" | "flagCount" | "isCompleted">): boolean {
  return challenge.correctFlagCount > 0 && challenge.correctFlagCount < challenge.flagCount && !challenge.isCompleted;
}

export function hasReusableBenchmarkContainer(challenge: Pick<ChallengeState, "status" | "containerStatus" | "containerAddrs"> | undefined): boolean {
  return Boolean(challenge
    && (challenge.status === "handoff_waiting" || challenge.status === "orphaned")
    && challenge.containerStatus === "available"
    && challenge.containerAddrs.length > 0);
}

function unfinishedCount(state: BenchmarkState): number {
  return Object.values(state.challenges).filter((challenge) => !challenge.isCompleted
    && challenge.status !== "solved" && challenge.status !== "exhausted").length;
}

export function budgetPolicyFor(state: BenchmarkState): BudgetPolicy {
  if (state.phase === "endgame") {
    return { label: "endgame", noProgressMs: 12 * MINUTE, initialLeaseMs: 15 * MINUTE, signalExtensionMs: 5 * MINUTE, maxSignalExtensions: Number.POSITIVE_INFINITY, flagMomentumMs: 10 * MINUTE, workerMaxMs: null };
  }
  if (state.phase === "second_pass" && unfinishedCount(state) <= 10) {
    return { label: "late_recovery", noProgressMs: 12 * MINUTE, initialLeaseMs: 25 * MINUTE, signalExtensionMs: 5 * MINUTE, maxSignalExtensions: 2, flagMomentumMs: 10 * MINUTE, workerMaxMs: 45 * MINUTE };
  }
  if (state.phase === "second_pass") {
    return { label: "second_pass", noProgressMs: 10 * MINUTE, initialLeaseMs: 16 * MINUTE, signalExtensionMs: 4 * MINUTE, maxSignalExtensions: 2, flagMomentumMs: 8 * MINUTE, workerMaxMs: 40 * MINUTE };
  }
  return { label: "first_pass", noProgressMs: 8 * MINUTE, initialLeaseMs: 12 * MINUTE, signalExtensionMs: 3 * MINUTE, maxSignalExtensions: 2, flagMomentumMs: 6 * MINUTE, workerMaxMs: 30 * MINUTE };
}

function cleanText(value: string, maxLength: number): string {
  return value.replace(/[\u0000-\u001f\u007f]/g, " ").trim().slice(0, maxLength);
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
  challenge.progressExtensions = 0;
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
    progressExtensions: 0,
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
    reservationPreviousHandoffExpiresAt: null,
    reservationStartedNewAttempt: false,
    closeFailureRecorded: false,
    acquiredAt: null,
    solvedAt: null,
    handoffExpiresAt: null,
    browserHandoffState: null
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
    phase: "first_pass",
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
  if (challenge.owner === expectedOwner) return;
  if (allowUnowned && challenge.owner === null) return;
  throw new Error(`Challenge ${challenge.uniqueCode} is owned by ${challenge.owner ?? "nobody"}, not by ${expectedOwner}`);
}

function recalculate(state: BenchmarkState): BenchmarkState {
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
  if (allTerminal) state.phase = "completed";
  return state;
}

export class BenchmarkLedger {
  private readonly serialize = createSerializer();
  private readonly serializeAction = createSerializer();
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
    // Backward-compatible defaults for ledgers created before these fields existed.
    if (typeof this.state.scoreExact !== "boolean") this.state.scoreExact = false;
    if (typeof this.state.vpnChecked !== "boolean") this.state.vpnChecked = false;
    if (!Array.isArray(this.state.sharedIntel)) this.state.sharedIntel = [];
    for (const challenge of Object.values(this.state.challenges)) {
      challenge.reservationPreviousStatus ??= undefined;
      challenge.reservationPreviousHandoffExpiresAt = nullableFiniteNumber(challenge.reservationPreviousHandoffExpiresAt);
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
      challenge.currentAttemptPhase ??= null;
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
      challenge.hardDeadlineAt = nullableFiniteNumber(challenge.hardDeadlineAt);
      challenge.progressExtensions = Number.isFinite(Number(challenge.progressExtensions)) ? Number(challenge.progressExtensions) : 0;
      challenge.approachHistory = Array.isArray(challenge.approachHistory) ? challenge.approachHistory.slice(-6) : [];
      challenge.lastSignalKind ??= null;
      challenge.lastEvidenceRef = typeof challenge.lastEvidenceRef === "string" ? challenge.lastEvidenceRef : "";
      challenge.progressKeys = Array.isArray(challenge.progressKeys) ? challenge.progressKeys.slice(-30) : [];
      challenge.ruledOutFamilies = Array.isArray(challenge.ruledOutFamilies) ? challenge.ruledOutFamilies.slice(-20) : [];
      challenge.handoffExpiresAt = nullableFiniteNumber(challenge.handoffExpiresAt);
      challenge.browserHandoffState = challenge.browserHandoffState?.version === 1 ? challenge.browserHandoffState : null;
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
    recalculate(this.state);
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
    return Math.max(0, this.now() - this.metrics.startedAt);
  }

  getChallenge(uniqueCode: string): ChallengeState | undefined {
    return this.state.challenges[uniqueCode];
  }

  /** Serialize complete controller actions across the parent and all children. */
  runAction<T>(action: () => Promise<T>): Promise<T> {
    return this.serializeAction(action);
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
      if (!challenge.triedFlags.includes(hash)) challenge.triedFlags.push(hash);
      // The platform may have accepted, rejected, or penalized the request;
      // without its response this challenge's per-challenge score may have
      // moved, so its cached value is no longer authoritative.
      challenge.scoreKnown = false;
      recalculate(this.state);
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
  async syncFromPlatform(challenges: Challenge[], vpnOk: boolean, vpnClientIp: string, vpnChecked = true): Promise<BenchmarkState> {
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
        // A list sync can observe progress that happened after a timed-out
        // submit, a process crash, or another worker. The list endpoint has no
        // score, so the cached score no longer corresponds to this progress.
        // On a backwards count change even the cached value is not a safe lower
        // bound; reset it to zero until a priced submit response is available.
        const previousCorrectFlagCount = existing.correctFlagCount;
        if (platform.correct_flag_count !== previousCorrectFlagCount) {
          if (platform.correct_flag_count < existing.correctFlagCount) existing.scoreObtained = 0;
          existing.scoreKnown = false;
        }
        // Platform is source of truth for these fields.
        existing.correctFlagCount = platform.correct_flag_count;
        if (platform.correct_flag_count > previousCorrectFlagCount) {
          const now = this.now();
          existing.lastSignalAt = now;
          existing.lastMeaningfulProgressAt = now;
          existing.lastAcceptedFlagAt = now;
          existing.lastSignalKind = "stage_transition";
          existing.lastEvidenceRef = `platform:sync-flag-count:${platform.correct_flag_count}`;
          existing.lastSignalContent = `Platform sync confirmed flag progress ${platform.correct_flag_count}/${platform.flag_count}`;
          existing.lastMeaningfulSignalContent = existing.lastSignalContent;
          if (existing.currentAttemptStartedAt && existing.owner) {
            const policy = budgetPolicyFor(this.state);
            const workerCap = policy.workerMaxMs === null ? Number.POSITIVE_INFINITY : existing.currentAttemptStartedAt + policy.workerMaxMs;
            existing.hardDeadlineAt = Math.min(workerCap, Math.max(existing.hardDeadlineAt ?? now, now + policy.flagMomentumMs));
          }
        }
        existing.isCompleted = platform.is_completed;
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
        if (existing.status === "handoff_waiting" && platform.container_status === "stopped") {
          existing.status = "deferred";
          existing.owner = null;
          existing.handoffExpiresAt = null;
          existing.browserHandoffState = null;
        }
        existing.totalScore = platform.total_score;
        existing.flagCount = platform.flag_count;
        if (platform.is_completed) {
          if (existing.currentAttemptStartedAt) finishAttempt(existing, this.now(), "platform sync confirmed solved");
          const needsClose = platform.container_status !== "stopped";
          existing.status = needsClose ? "closing" : "solved";
          existing.pendingStatus = needsClose ? "solved" : undefined;
          existing.owner = null;
          existing.reservationPreviousStatus = undefined;
          existing.reservationStartedNewAttempt = false;
          if (!existing.solvedAt) existing.solvedAt = this.now();
        } else if (existing.status === "solved") {
          // Platform says not completed — local stale state; revert to deferred.
          existing.status = "deferred";
          existing.solvedAt = null;
        }
        if (!platform.is_completed && platform.container_status === "stopped"
          && (existing.status === "running" || existing.status === "orphaned")) {
          existing.status = "orphaned";
          existing.owner = null;
          existing.pendingStatus = undefined;
          existing.reservationPreviousStatus = undefined;
          existing.reservationStartedNewAttempt = false;
        } else if (!platform.is_completed && platform.container_status === "available"
          && existing.owner === null && (existing.status === "pending" || existing.status === "deferred")) {
          // The platform has a live container with no live local worker. Treat it
          // as recoverable instead of starting a second container.
          existing.status = "orphaned";
        }
        // Local scheduling state survives when the challenge is not complete.
      }
      recalculate(this.state);
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
      const eligible = challenge.status === "pending" || challenge.status === "orphaned" || challenge.status === "handoff_waiting"
        || ((this.state.phase === "second_pass" || this.state.phase === "endgame") && challenge.status === "deferred");
      if (!eligible) throw new Error(`Challenge ${uniqueCode} cannot be reserved while ${challenge.status}`);
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
      // A live orphan is the same in-flight attempt after a Runtime restart.
      // A stopped orphan needs a new platform container and a fresh attempt.
      const resumesLiveAttempt = wasOrphaned && hasReusableBenchmarkContainer(challenge)
        && challenge.currentAttemptStartedAt !== null;
      const startsNewAttempt = !resumesLiveAttempt;
      challenge.reservationPreviousStatus = challenge.status;
      challenge.reservationPreviousHandoffExpiresAt = challenge.handoffExpiresAt;
      challenge.reservationStartedNewAttempt = startsNewAttempt;
      challenge.status = "reserved";
      challenge.owner = owner;
      challenge.pendingStatus = undefined;
      challenge.closeFailureRecorded = false;
      const now = this.now();
      if (!wasOrphaned) challenge.acquiredAt = now;
      if (startsNewAttempt) {
        challenge.lastSignalAt = now;
        challenge.lastMeaningfulProgressAt = now;
      }
      challenge.handoffExpiresAt = null;
      if (startsNewAttempt) {
        // A restart leaves a stopped orphan's attempt open (a live orphan
        // resumes it above). Starting fresh must still record what the
        // interrupted attempt did, or the recovery brief loses that route.
        if (challenge.currentAttemptStartedAt !== null) {
          finishAttempt(challenge, now, "attempt interrupted by restart before a fresh attempt");
        }
        const policy = budgetPolicyFor(this.state);
        challenge.attemptCount += 1;
        challenge.currentAttemptStartedAt = now;
        challenge.currentAttemptPhase = this.state.phase;
        challenge.currentAttemptWorker = owner;
        challenge.flagsAtAttemptStart = challenge.correctFlagCount;
        challenge.currentApproach = "";
        challenge.hardDeadlineAt = now + policy.initialLeaseMs;
        challenge.progressExtensions = 0;
        // These are challenge-global recovery facts. Retaining them prevents a
        // fresh worker from extending its clock with evidence already used by
        // the previous attempt.
        if (challenge.reservationPreviousStatus !== "handoff_waiting") challenge.browserHandoffState = null;
        const metric = this.metrics.challenges[uniqueCode];
        if (metric) metric.attempts += 1;
      } else {
        challenge.currentAttemptWorker = owner;
        if (!challenge.hardDeadlineAt) {
          const policy = budgetPolicyFor(this.state);
          challenge.hardDeadlineAt = now + policy.initialLeaseMs;
        }
      }
      if (!this.metrics.challenges[uniqueCode]) {
        this.metrics.challenges[uniqueCode] = { acquiredAt: now, solvedAt: null, durationMs: null, attempts: startsNewAttempt ? 1 : 0, wrongFlags: 0, hintsUsed: 0, deferredCount: 0 };
      }
      recalculate(this.state);
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
      challenge.status = "running";
      challenge.reservationPreviousStatus = undefined;
      challenge.reservationPreviousHandoffExpiresAt = null;
      challenge.reservationStartedNewAttempt = false;
      challenge.containerAddrs = containerAddrs;
      challenge.containerStatus = "available";
      recalculate(this.state);
      await this.persist();
      return challenge;
    });
  }

  /** Rollback a failed start after a successful reserve. */
  async releaseReservation(uniqueCode: string, expectedOwner: Exclude<ChallengeOwner, null>): Promise<ChallengeState> {
    return this.serialize(async () => {
      const challenge = this.state.challenges[uniqueCode];
      if (!challenge) throw new Error(`Challenge ${uniqueCode} not found`);
      if (challenge.status !== "reserved") throw new Error(`Challenge ${uniqueCode} is ${challenge.status}, expected reserved`);
      requireOwner(challenge, expectedOwner);
      const previousStatus = challenge.reservationPreviousStatus ?? "pending";
      const previousHandoffExpiresAt = challenge.reservationPreviousHandoffExpiresAt;
      const startedNewAttempt = challenge.reservationStartedNewAttempt;
      challenge.status = previousStatus;
      challenge.reservationPreviousStatus = undefined;
      challenge.reservationPreviousHandoffExpiresAt = null;
      challenge.reservationStartedNewAttempt = false;
      challenge.owner = null;
      challenge.handoffExpiresAt = previousStatus === "handoff_waiting" ? previousHandoffExpiresAt : null;
      if (startedNewAttempt) {
        challenge.attemptCount = Math.max(0, challenge.attemptCount - 1);
        const metric = this.metrics.challenges[uniqueCode];
        if (metric) metric.attempts = Math.max(0, metric.attempts - 1);
        challenge.currentAttemptStartedAt = null;
        challenge.currentAttemptPhase = null;
        challenge.currentAttemptWorker = null;
        challenge.hardDeadlineAt = null;
        challenge.progressExtensions = 0;
      }
      recalculate(this.state);
      await this.persist();
      return challenge;
    });
  }

  async saveBrowserHandoffState(uniqueCode: string, state: BrowserHandoffState | undefined, expectedOwner: Exclude<ChallengeOwner, null>): Promise<void> {
    return this.serialize(async () => {
      const challenge = this.state.challenges[uniqueCode];
      if (!challenge) throw new Error(`Challenge ${uniqueCode} not found`);
      requireOwner(challenge, expectedOwner);
      const token = process.env.BENCHMARK_TOKEN ?? "";
      challenge.browserHandoffState = state
        ? JSON.parse(
            JSON.stringify(state)
              // The handoff preserves authentication, not answers: a flag-shaped
              // value riding in a cookie or storage entry is redacted like the
              // token. The class excludes the JSON string terminator so the
              // match stays inside a single string value.
              .replace(/flag\{[^}"\\]{1,256}\}/gi, "flag{REDACTED}")
              .split(token).join("[REDACTED_BENCHMARK_TOKEN]")
          ) as BrowserHandoffState
        : null;
      await this.persist();
    });
  }

  /** Restore an existing warm handoff after dispatch failed after the live
   * container had already been confirmed. This is a reservation rollback,
   * not a completed attempt, so it must not add a fake approach-history row. */
  async restoreWarmHandoffAfterAssignmentFailure(uniqueCode: string, expectedOwner: Exclude<ChallengeOwner, null>): Promise<ChallengeState> {
    return this.serialize(async () => {
      const challenge = this.state.challenges[uniqueCode];
      if (!challenge) throw new Error(`Challenge ${uniqueCode} not found`);
      requireOwner(challenge, expectedOwner);
      if (challenge.status !== "reserved" && challenge.status !== "running") {
        throw new Error(`Challenge ${uniqueCode} is ${challenge.status}; cannot restore a failed warm-handoff assignment`);
      }
      challenge.status = "handoff_waiting";
      challenge.owner = null;
      challenge.pendingStatus = undefined;
      challenge.attemptCount = Math.max(0, challenge.attemptCount - 1);
      const metric = this.metrics.challenges[uniqueCode];
      if (metric) metric.attempts = Math.max(0, metric.attempts - 1);
      challenge.currentAttemptStartedAt = null;
      challenge.currentAttemptPhase = null;
      challenge.currentAttemptWorker = null;
      challenge.hardDeadlineAt = null;
      challenge.progressExtensions = 0;
      challenge.reservationPreviousStatus = undefined;
      challenge.reservationPreviousHandoffExpiresAt = null;
      challenge.reservationStartedNewAttempt = false;
      challenge.handoffExpiresAt = this.now() + HANDOFF_GRACE_MS;
      recalculate(this.state);
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
    options?: { signalKind?: ProgressSignalKind; evidenceRef?: string; currentApproach?: string; ruledOutFamilies?: string[] }
  ): Promise<{ updated: boolean; extended: boolean; challenge: ChallengeState }> {
    return this.serialize(async () => {
      const challenge = this.state.challenges[uniqueCode];
      if (!challenge) throw new Error(`Challenge ${uniqueCode} not found`);
      requireOwner(challenge, expectedOwner);
      const normalizedSignal = cleanText(signal, 2_000);
      if (!normalizedSignal) throw new Error("signal must not be empty");
      const isNew = normalizedSignal !== challenge.lastSignalContent;
      if (isNew) challenge.lastSignalContent = normalizedSignal;
      const previousNextProbe = challenge.nextProbe;
      if (triedFamilies?.length) {
        const normalized = triedFamilies.map((item) => cleanText(item, 100)).filter(Boolean);
        challenge.triedFamilies = [...new Set([...challenge.triedFamilies, ...normalized])].slice(-20);
      }
      if (options?.ruledOutFamilies?.length) {
        const normalized = options.ruledOutFamilies.map((item) => cleanText(item, 100)).filter(Boolean);
        challenge.ruledOutFamilies = [...new Set([...challenge.ruledOutFamilies, ...normalized])].slice(-20);
      }
      if (nextProbe) challenge.nextProbe = cleanText(nextProbe, 1_000);
      const approach = options?.currentApproach ? cleanText(options.currentApproach, 300) : "";
      const approachChanged = Boolean(approach && approach !== challenge.currentApproach);
      if (approach) challenge.currentApproach = approach;

      const kind = options?.signalKind ?? "note";
      const evidenceRef = cleanText(options?.evidenceRef ?? "", 500);
      const strongKind = kind === "foothold" || kind === "credential" || kind === "privilege_change"
        || kind === "exploit_primitive" || kind === "stage_transition";
      const decisiveRuleOut = kind === "decisive_rule_out" && Boolean(evidenceRef)
        && Boolean(challenge.nextProbe) && challenge.nextProbe !== previousNextProbe;
      const endgameApproachEpoch = this.state.phase === "endgame" && approachChanged;
      const key = progressKey(kind, evidenceRef || (endgameApproachEpoch ? `approach:${approach}` : ""));
      const newEvidence = !challenge.progressKeys.includes(key);
      const evidenceBacked = (strongKind && Boolean(evidenceRef)) || decisiveRuleOut;
      const qualifiesForExtension = newEvidence && (evidenceBacked || endgameApproachEpoch);
      let extended = false;
      if (qualifiesForExtension) {
        const now = this.now();
        const policy = budgetPolicyFor(this.state);
        const previousDeadline = challenge.hardDeadlineAt;
        challenge.lastSignalAt = now;
        challenge.lastMeaningfulProgressAt = now;
        challenge.lastMeaningfulSignalContent = normalizedSignal;
        challenge.lastSignalKind = kind;
        challenge.lastEvidenceRef = evidenceRef;
        challenge.progressKeys = [...challenge.progressKeys, key].slice(-30);
        if (endgameApproachEpoch) {
          challenge.hardDeadlineAt = now + policy.initialLeaseMs;
        } else if (challenge.progressExtensions < policy.maxSignalExtensions && challenge.currentAttemptStartedAt) {
          challenge.progressExtensions += 1;
          const workerCap = policy.workerMaxMs === null ? Number.POSITIVE_INFINITY : challenge.currentAttemptStartedAt + policy.workerMaxMs;
          challenge.hardDeadlineAt = Math.min(workerCap, (challenge.hardDeadlineAt ?? now) + policy.signalExtensionMs);
        }
        extended = challenge.hardDeadlineAt !== previousDeadline;
      }
      await this.persist();
      return { updated: isNew, extended, challenge };
    });
  }

  /** Record a flag submission result. `challengeScore` is the platform's PER-CHALLENGE
   * cumulative score (hint deductions already included). Flags are stored as SHA-256
   * hashes to avoid persisting plaintext answers. */
  async recordSubmission(uniqueCode: string, flag: string, correct: boolean, challengeScore: number | undefined, correctFlagCount: number, matchedFlagIndex: number | null, expectedOwner: Exclude<ChallengeOwner, null>): Promise<ChallengeState> {
    return this.serialize(async () => {
      const challenge = this.state.challenges[uniqueCode];
      if (!challenge) throw new Error(`Challenge ${uniqueCode} not found`);
      requireOwner(challenge, expectedOwner);
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
      if (!challenge.triedFlags.includes(hash)) challenge.triedFlags.push(hash);
      if (correct && matchedFlagIndex !== null && !challenge.matchedFlagIndexes.includes(matchedFlagIndex)) {
        challenge.matchedFlagIndexes.push(matchedFlagIndex);
      }
      if (correct && correctFlagCount > previousCorrectFlagCount) {
        const now = this.now();
        const policy = budgetPolicyFor(this.state);
        challenge.lastSignalAt = now;
        challenge.lastMeaningfulProgressAt = now;
        challenge.lastAcceptedFlagAt = now;
        challenge.lastSignalKind = "stage_transition";
        challenge.lastEvidenceRef = `platform:flag-count:${correctFlagCount}`;
        challenge.lastSignalContent = `Platform accepted a new flag; progress ${correctFlagCount}/${challenge.flagCount}`;
        challenge.lastMeaningfulSignalContent = challenge.lastSignalContent;
        const attemptStart = challenge.currentAttemptStartedAt ?? now;
        const workerCap = policy.workerMaxMs === null ? Number.POSITIVE_INFINITY : attemptStart + policy.workerMaxMs;
        challenge.hardDeadlineAt = Math.min(workerCap, Math.max(challenge.hardDeadlineAt ?? now, now + policy.flagMomentumMs));
      }
      const metric = this.metrics.challenges[uniqueCode];
      if (metric) {
        if (!correct) metric.wrongFlags += 1;
        this.metrics.totalWrongSubmissions += correct ? 0 : 1;
      }
      recalculate(this.state);
      await this.persist();
      return challenge;
    });
  }

  /** Mark logical completion. A live container remains in closing until confirmed stopped.
   * `challengeScore` is the platform's per-challenge cumulative; omit it when no priced
   * response was received (recalculate keeps the run total a lower bound). */
  async markSolved(uniqueCode: string, challengeScore: number | undefined, expectedOwner: Exclude<ChallengeOwner, null>): Promise<ChallengeState> {
    return this.serialize(async () => {
      const challenge = this.state.challenges[uniqueCode];
      if (!challenge) throw new Error(`Challenge ${uniqueCode} not found`);
      requireOwner(challenge, expectedOwner);
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
      recalculate(this.state);
      await this.persist();
      return challenge;
    });
  }

  /** Defer: save recovery state, set closing (close must be confirmed before terminal). */
  async defer(uniqueCode: string, reason: string, nextProbe: string | undefined, expectedOwner: Exclude<ChallengeOwner, null>, options?: { preserveContainer?: boolean }): Promise<ChallengeState> {
    return this.serialize(async () => {
      const challenge = this.state.challenges[uniqueCode];
      if (!challenge) throw new Error(`Challenge ${uniqueCode} not found`);
      requireOwner(challenge, expectedOwner);
      challenge.deferredReason = reason.replace(/[\u0000-\u001f\u007f]/g, " ").trim().slice(0, 1_000);
      if (nextProbe) challenge.nextProbe = nextProbe.replace(/[\u0000-\u001f\u007f]/g, " ").trim().slice(0, 1_000);
      const now = this.now();
      finishAttempt(challenge, now, challenge.deferredReason || "deferred");
      const metric = this.metrics.challenges[uniqueCode];
      if (metric) metric.deferredCount += 1;
      this.metrics.totalDefers += 1;
      // Recovery/Endgame can transfer a valuable live target to a fresh
      // worker without destroying login/shell state. It still occupies one
      // of the platform's three container slots during the short grace.
      challenge.status = options?.preserveContainer ? "handoff_waiting" : "closing";
      challenge.owner = null;
      challenge.pendingStatus = options?.preserveContainer ? undefined : "deferred";
      challenge.handoffExpiresAt = options?.preserveContainer ? now + HANDOFF_GRACE_MS : null;
      recalculate(this.state);
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
      challenge.deferredReason = reason.replace(/[\u0000-\u001f\u007f]/g, " ").trim().slice(0, 1_000);
      finishAttempt(challenge, this.now(), challenge.deferredReason || "exhausted");
      challenge.status = "closing";
      challenge.owner = null;
      challenge.pendingStatus = "exhausted";
      challenge.handoffExpiresAt = null;
      recalculate(this.state);
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
      challenge.browserHandoffState = null;
      challenge.closeFailureRecorded = false;
      challenge.handoffExpiresAt = null;
      recalculate(this.state);
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
      recalculate(this.state);
      await this.persist();
      return challenge;
    });
  }

  /** Move an expired warm handoff into the ordinary two-phase close path. */
  async expireHandoff(uniqueCode: string): Promise<ChallengeState> {
    return this.serialize(async () => {
      const challenge = this.state.challenges[uniqueCode];
      if (!challenge) throw new Error(`Challenge ${uniqueCode} not found`);
      if (challenge.status !== "handoff_waiting") return challenge;
      challenge.status = "closing";
      challenge.pendingStatus = "deferred";
      challenge.handoffExpiresAt = null;
      recalculate(this.state);
      await this.persist();
      return challenge;
    });
  }

  expiredHandoffs(): ChallengeState[] {
    const now = this.now();
    return Object.values(this.state.challenges).filter((challenge) =>
      challenge.status === "handoff_waiting" && challenge.handoffExpiresAt !== null && challenge.handoffExpiresAt <= now
    );
  }

  /** Record hint usage and content. */
  async recordHint(uniqueCode: string, hint: string | null, expectedOwner: Exclude<ChallengeOwner, null>): Promise<ChallengeState> {
    return this.serialize(async () => {
      const challenge = this.state.challenges[uniqueCode];
      if (!challenge) throw new Error(`Challenge ${uniqueCode} not found`);
      if (this.state.phase !== "second_pass" && this.state.phase !== "endgame") throw new Error("Hints are available only in recovery/endgame");
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
  async releaseOnSubagentExit(uniqueCode: string, reason: string, expectedOwner: `subagent:${string}`, pendingStatus: "deferred" | "exhausted" = "deferred", options?: { preserveContainer?: boolean }): Promise<ChallengeState> {
    return this.serialize(async () => {
      const challenge = this.state.challenges[uniqueCode];
      if (!challenge) throw new Error(`Challenge ${uniqueCode} not found`);
      if (challenge.status === "solved" || challenge.status === "exhausted") return challenge;
      requireOwner(challenge, expectedOwner);
      const now = this.now();
      finishAttempt(challenge, now, reason);
      challenge.status = options?.preserveContainer ? "handoff_waiting" : "closing";
      challenge.owner = null;
      challenge.deferredReason = reason.replace(/[\u0000-\u001f\u007f]/g, " ").trim().slice(0, 1_000);
      challenge.pendingStatus = options?.preserveContainer ? undefined : pendingStatus;
      challenge.handoffExpiresAt = options?.preserveContainer ? now + HANDOFF_GRACE_MS : null;
      recalculate(this.state);
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

  /** Time since last signal on a challenge, in ms. */
  signalElapsedMs(uniqueCode: string): number {
    const challenge = this.state.challenges[uniqueCode];
    if (!challenge || !challenge.lastSignalAt) return 0;
    return Math.max(0, this.now() - (challenge.lastMeaningfulProgressAt || challenge.lastSignalAt));
  }

  budgetFor(uniqueCode: string): ChallengeBudget | undefined {
    const challenge = this.state.challenges[uniqueCode];
    if (!challenge) return undefined;
    const now = this.now();
    const policy = budgetPolicyFor(this.state);
    const startedAt = challenge.currentAttemptStartedAt ?? now;
    const sinceProgressMs = Math.max(0, now - (challenge.lastMeaningfulProgressAt || startedAt));
    const elapsedMs = Math.max(0, now - startedAt);
    const noProgressExpired = sinceProgressMs >= policy.noProgressMs;
    const hardRemainingMs = challenge.hardDeadlineAt === null ? null : challenge.hardDeadlineAt - now;
    const hardExpired = hardRemainingMs !== null && hardRemainingMs <= 0;
    const endgameProgressAnchor = Math.max(startedAt, challenge.lastAcceptedFlagAt ?? 0);
    const workerRotationDue = this.state.phase === "endgame" && now - endgameProgressAnchor >= 30 * MINUTE;
    return { policy, elapsedMs, sinceProgressMs, hardRemainingMs, noProgressExpired, hardExpired, workerRotationDue, expired: noProgressExpired || hardExpired || workerRotationDue };
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

  /** Candidate challenges for acquisition, ordered by priority. */
  candidates(count = 10, offset = 0): ChallengeState[] {
    const difficultyOrder: Record<string, number> = { easy: 0, medium: 1, hard: 2 };
    return Object.values(this.state.challenges)
      .filter((challenge) => challenge.status === "pending" || challenge.status === "orphaned" || challenge.status === "handoff_waiting"
        || ((this.state.phase === "second_pass" || this.state.phase === "endgame") && challenge.status === "deferred"))
      .sort((left, right) => {
        const handoffDelta = Number(right.status === "handoff_waiting") - Number(left.status === "handoff_waiting");
        if (handoffDelta !== 0) return handoffDelta;
        if (this.state.phase === "endgame") {
          const oneFlagDelta = Number(right.flagCount - right.correctFlagCount === 1) - Number(left.flagCount - left.correctFlagCount === 1);
          if (oneFlagDelta !== 0) return oneFlagDelta;
          const partialDelta = Number(isPartialChallenge(right)) - Number(isPartialChallenge(left));
          if (partialDelta !== 0) return partialDelta;
          const durable = (challenge: ChallengeState) => challenge.lastSignalKind === "foothold" || challenge.lastSignalKind === "credential"
            || challenge.lastSignalKind === "privilege_change" || challenge.lastSignalKind === "stage_transition";
          const durableDelta = Number(durable(right)) - Number(durable(left));
          if (durableDelta !== 0) return durableDelta;
          const scoreDelta = (right.totalScore - right.scoreObtained) - (left.totalScore - left.scoreObtained);
          if (scoreDelta !== 0) return scoreDelta;
          const remainingDelta = (left.flagCount - left.correctFlagCount) - (right.flagCount - right.correctFlagCount);
          if (remainingDelta !== 0) return remainingDelta;
        }
        if (this.state.phase !== "first_pass") {
          const partialDelta = Number(isPartialChallenge(right)) - Number(isPartialChallenge(left));
          if (partialDelta !== 0) return partialDelta;
          const remainingDelta = (left.flagCount - left.correctFlagCount) - (right.flagCount - right.correctFlagCount);
          if (remainingDelta !== 0) return remainingDelta;
          const scoreDelta = (right.totalScore - right.scoreObtained) - (left.totalScore - left.scoreObtained);
          if (scoreDelta !== 0) return scoreDelta;
          const progressDelta = right.lastMeaningfulProgressAt - left.lastMeaningfulProgressAt;
          if (progressDelta !== 0) return progressDelta;
          const attemptDelta = left.attemptCount - right.attemptCount;
          if (attemptDelta !== 0) return attemptDelta;
        }
        // Orphaned first (recover existing state), then easy→hard, then high score.
        const orphanDelta = Number(right.status === "orphaned") - Number(left.status === "orphaned");
        if (orphanDelta !== 0) return orphanDelta;
        const difficultyDelta = (difficultyOrder[left.difficulty] ?? 1) - (difficultyOrder[right.difficulty] ?? 1);
        if (difficultyDelta !== 0) return difficultyDelta;
        const scoreDelta = right.totalScore - left.totalScore;
        if (scoreDelta !== 0) return scoreDelta;
        const flagDelta = right.flagCount - left.flagCount;
        return flagDelta !== 0 ? flagDelta : left.uniqueCode.localeCompare(right.uniqueCode);
      })
      .slice(offset, offset + count);
  }

  /** Transition to second_pass when no pending challenges remain. */
  async maybeAdvancePhase(): Promise<BenchmarkPhase> {
    return this.serialize(async () => {
      if (this.state.phase === "first_pass") {
        // Do not enter recovery merely because every pending challenge has
        // been handed to a worker. The first pass ends only after every live,
        // reserved, orphaned, and closing first-pass attempt has settled.
        const hasFirstPassWork = Object.values(this.state.challenges).some((challenge) =>
          challenge.status === "pending" || challenge.status === "orphaned"
          || challenge.status === "reserved"
          || challenge.status === "running" || challenge.status === "closing"
        );
        if (!hasFirstPassWork) this.state.phase = "second_pass";
      }
      if (this.state.phase === "second_pass") {
        const remaining = unfinishedCount(this.state);
        if (remaining > 0 && remaining <= 3) this.state.phase = "endgame";
      }
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
