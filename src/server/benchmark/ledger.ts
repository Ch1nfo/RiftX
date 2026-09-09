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

export const BENCHMARK_MAX_CONTAINERS = 3;
export const BENCHMARK_MAX_SUBAGENTS = 2;
export const SIGNAL_BUDGET_MS = 8 * 60 * 1000;

export type ChallengeStatus = "pending" | "reserved" | "running" | "closing" | "deferred" | "solved" | "exhausted" | "orphaned";
export type BenchmarkPhase = "first_pass" | "second_pass" | "completed";
export type ChallengeOwner = "main" | `subagent:${string}` | null;

export type ChallengeState = {
  uniqueCode: string;
  description: string;
  difficulty: string;
  level: string;
  totalScore: number;
  flagCount: number;
  correctFlagCount: number;
  isCompleted: boolean;
  status: ChallengeStatus;
  owner: ChallengeOwner;
  containerAddrs: string[];
  containerStatus: string;
  hintUsed: boolean;
  hintContent: string | null;
  lastSignalAt: number;
  lastSignalContent: string;
  triedFamilies: string[];
  nextProbe: string;
  triedFlags: string[];
  matchedFlagIndexes: number[];
  deferredReason: string | null;
  pendingStatus: ChallengeStatus | undefined;
  reservationPreviousStatus: ChallengeStatus | undefined;
  closeFailureRecorded: boolean;
  acquiredAt: number | null;
  solvedAt: number | null;
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
    isCompleted: challenge.is_completed,
    status,
    owner: null,
    containerAddrs: challenge.container_addr,
    containerStatus: challenge.container_status,
    hintUsed: false,
    hintContent: null,
    lastSignalAt: 0,
    lastSignalContent: "",
    triedFamilies: [],
    nextProbe: "",
    triedFlags: [],
    matchedFlagIndexes: [],
    deferredReason: null,
    pendingStatus: status === "closing" ? (challenge.is_completed ? "solved" : "deferred") : undefined,
    reservationPreviousStatus: undefined,
    closeFailureRecorded: false,
    acquiredAt: null,
    solvedAt: null
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

  constructor(parentSessionId: string) {
    this.parentSessionId = parentSessionId;
  }

  /** Loads or creates the ledger. Must be called before any other operation. */
  async initialize(): Promise<BenchmarkLedger> {
    await mkdir(benchmarkDir(this.parentSessionId), { recursive: true, mode: 0o700 });
    this.state = await readJsonStore<BenchmarkState>(statePath(this.parentSessionId)) ?? defaultState();
    this.metrics = await readJsonStore<BenchmarkMetrics>(metricsPath(this.parentSessionId)) ?? defaultMetrics();
    // Backward-compatible defaults for ledgers created before these fields existed.
    if (typeof this.state.scoreExact !== "boolean") this.state.scoreExact = false;
    if (!Array.isArray(this.state.sharedIntel)) this.state.sharedIntel = [];
    for (const challenge of Object.values(this.state.challenges)) {
      challenge.reservationPreviousStatus ??= undefined;
      challenge.closeFailureRecorded ??= false;
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
      // without its response the cached cumulative score is no longer exact.
      this.state.scoreExact = false;
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
      const entry = { scope, target: normalizedTarget, intel: redacted, publishedAt: Date.now() } satisfies SharedIntel;
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

  /** Full platform sync: platform values override local. Omit cumulativeScore when the list endpoint does not provide it. */
  async syncFromPlatform(challenges: Challenge[], cumulativeScore: number | undefined, vpnOk: boolean, vpnClientIp: string): Promise<BenchmarkState> {
    return this.serialize(async () => {
      this.state.vpnOk = vpnOk;
      this.state.vpnClientIp = vpnClientIp;
      this.state.lastSyncAt = Date.now();
      // Only overwrite the score when the caller actually has a real value.
      if (cumulativeScore !== undefined) {
        this.state.cumulativeScore = cumulativeScore;
        this.state.scoreExact = true;
      }
      for (const platform of challenges) {
        const existing = this.state.challenges[platform.unique_code];
        if (!existing) {
          this.state.challenges[platform.unique_code] = newChallengeState(platform);
          if (cumulativeScore === undefined && platform.correct_flag_count > 0) this.state.scoreExact = false;
          continue;
        }
        // Platform is source of truth for these fields.
        if (cumulativeScore === undefined && platform.correct_flag_count > existing.correctFlagCount) {
          this.state.scoreExact = false;
        }
        existing.correctFlagCount = platform.correct_flag_count;
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
        existing.totalScore = platform.total_score;
        existing.flagCount = platform.flag_count;
        if (platform.is_completed) {
          const needsClose = platform.container_status !== "stopped";
          existing.status = needsClose ? "closing" : "solved";
          existing.pendingStatus = needsClose ? "solved" : undefined;
          existing.owner = null;
          existing.reservationPreviousStatus = undefined;
          if (!existing.solvedAt) existing.solvedAt = Date.now();
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
      const challenge = this.state.challenges[uniqueCode];
      if (!challenge) throw new Error(`Challenge ${uniqueCode} not found in ledger`);
      if (challenge.owner && challenge.owner !== owner) {
        this.metrics.duplicateAcquires += 1;
        throw new Error(`Challenge ${uniqueCode} is owned by ${challenge.owner}`);
      }
      const eligible = challenge.status === "pending" || challenge.status === "orphaned"
        || (this.state.phase === "second_pass" && challenge.status === "deferred");
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
      challenge.reservationPreviousStatus = challenge.status;
      challenge.status = "reserved";
      challenge.owner = owner;
      challenge.pendingStatus = undefined;
      challenge.closeFailureRecorded = false;
      if (!wasOrphaned) challenge.acquiredAt = Date.now();
      challenge.lastSignalAt = Date.now();
      if (!this.metrics.challenges[uniqueCode]) {
        this.metrics.challenges[uniqueCode] = { acquiredAt: Date.now(), solvedAt: null, durationMs: null, attempts: 0, wrongFlags: 0, hintsUsed: 0, deferredCount: 0 };
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
      challenge.status = challenge.reservationPreviousStatus ?? "pending";
      challenge.reservationPreviousStatus = undefined;
      challenge.owner = null;
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

  /** Record a signal. Only genuinely different content resets the budget. */
  async checkpoint(uniqueCode: string, signal: string, triedFamilies: string[] | undefined, nextProbe: string | undefined, expectedOwner: Exclude<ChallengeOwner, null>): Promise<{ updated: boolean; challenge: ChallengeState }> {
    return this.serialize(async () => {
      const challenge = this.state.challenges[uniqueCode];
      if (!challenge) throw new Error(`Challenge ${uniqueCode} not found`);
      requireOwner(challenge, expectedOwner);
      const normalizedSignal = signal.replace(/[\u0000-\u001f\u007f]/g, " ").trim().slice(0, 2_000);
      if (!normalizedSignal) throw new Error("signal must not be empty");
      const isNew = normalizedSignal !== challenge.lastSignalContent;
      if (isNew) {
        challenge.lastSignalAt = Date.now();
        challenge.lastSignalContent = normalizedSignal;
      }
      if (triedFamilies?.length) {
        const normalized = triedFamilies.map((item) => item.replace(/[\u0000-\u001f\u007f]/g, " ").trim().slice(0, 100)).filter(Boolean);
        challenge.triedFamilies = [...new Set([...challenge.triedFamilies, ...normalized])].slice(-20);
      }
      if (nextProbe) challenge.nextProbe = nextProbe.replace(/[\u0000-\u001f\u007f]/g, " ").trim().slice(0, 1_000);
      const metric = this.metrics.challenges[uniqueCode];
      if (metric) metric.attempts += 1;
      await this.persist();
      return { updated: isNew, challenge };
    });
  }

  /** Record a flag submission result. Flags are stored as SHA-256 hashes to avoid persisting plaintext answers. */
  async recordSubmission(uniqueCode: string, flag: string, correct: boolean, cumulativeScore: number | undefined, correctFlagCount: number, matchedFlagIndex: number | null, expectedOwner: Exclude<ChallengeOwner, null>): Promise<ChallengeState> {
    return this.serialize(async () => {
      const challenge = this.state.challenges[uniqueCode];
      if (!challenge) throw new Error(`Challenge ${uniqueCode} not found`);
      requireOwner(challenge, expectedOwner);
      if (cumulativeScore !== undefined) {
        this.state.cumulativeScore = cumulativeScore;
        this.state.scoreExact = true;
      } else if (correct) {
        this.state.scoreExact = false;
      }
      challenge.correctFlagCount = correctFlagCount;
      const hash = flagHash(flag);
      if (!challenge.triedFlags.includes(hash)) challenge.triedFlags.push(hash);
      if (correct && matchedFlagIndex !== null && !challenge.matchedFlagIndexes.includes(matchedFlagIndex)) {
        challenge.matchedFlagIndexes.push(matchedFlagIndex);
      }
      const metric = this.metrics.challenges[uniqueCode];
      if (metric) {
        if (!correct) metric.wrongFlags += 1;
        this.metrics.totalWrongSubmissions += correct ? 0 : 1;
      }
      await this.persist();
      return challenge;
    });
  }

  /** Mark logical completion. A live container remains in closing until confirmed stopped. */
  async markSolved(uniqueCode: string, cumulativeScore: number | undefined, expectedOwner: Exclude<ChallengeOwner, null>): Promise<ChallengeState> {
    return this.serialize(async () => {
      const challenge = this.state.challenges[uniqueCode];
      if (!challenge) throw new Error(`Challenge ${uniqueCode} not found`);
      requireOwner(challenge, expectedOwner);
      const needsClose = hasActiveContainer(challenge);
      challenge.status = needsClose ? "closing" : "solved";
      challenge.pendingStatus = needsClose ? "solved" : undefined;
      challenge.owner = null;
      challenge.isCompleted = true;
      challenge.solvedAt = Date.now();
      if (!needsClose) {
        challenge.containerAddrs = [];
        challenge.containerStatus = "stopped";
      }
      if (cumulativeScore !== undefined) {
        this.state.cumulativeScore = cumulativeScore;
        this.state.scoreExact = true;
      } else {
        this.state.scoreExact = false;
      }
      const metric = this.metrics.challenges[uniqueCode];
      if (metric && challenge.acquiredAt) {
        metric.solvedAt = Date.now();
        metric.durationMs = Date.now() - challenge.acquiredAt;
      }
      recalculate(this.state);
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
      challenge.deferredReason = reason.replace(/[\u0000-\u001f\u007f]/g, " ").trim().slice(0, 1_000);
      if (nextProbe) challenge.nextProbe = nextProbe.replace(/[\u0000-\u001f\u007f]/g, " ").trim().slice(0, 1_000);
      const metric = this.metrics.challenges[uniqueCode];
      if (metric) metric.deferredCount += 1;
      this.metrics.totalDefers += 1;
      // Keep the container count until close is confirmed; the tool calls
      // confirmClosed() after the platform confirms.
      challenge.status = "closing";
      challenge.owner = null;
      challenge.pendingStatus = "deferred";
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
      challenge.status = "closing";
      challenge.owner = null;
      challenge.pendingStatus = "exhausted";
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
      challenge.closeFailureRecorded = false;
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

  /** Record hint usage and content. */
  async recordHint(uniqueCode: string, hint: string | null, expectedOwner: Exclude<ChallengeOwner, null>): Promise<ChallengeState> {
    return this.serialize(async () => {
      const challenge = this.state.challenges[uniqueCode];
      if (!challenge) throw new Error(`Challenge ${uniqueCode} not found`);
      if (this.state.phase !== "second_pass") throw new Error("Hints are available only in the second pass");
      if (challenge.status === "solved" || challenge.status === "exhausted" || challenge.status === "closing") {
        throw new Error(`Challenge ${uniqueCode} is ${challenge.status}; a hint would be wasted`);
      }
      // Main may buy a hint for an unowned deferred challenge before assigning
      // it to a fresh second-pass worker, but never for another live worker.
      requireOwner(challenge, expectedOwner, expectedOwner === "main");
      challenge.hintUsed = true;
      challenge.hintContent = hint;
      // Hint cost changes the platform score, but this endpoint does not
      // return the new cumulative total.
      this.state.scoreExact = false;
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
      challenge.status = "closing";
      challenge.owner = null;
      challenge.deferredReason = reason.replace(/[\u0000-\u001f\u007f]/g, " ").trim().slice(0, 1_000);
      challenge.pendingStatus = pendingStatus;
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
    return Date.now() - challenge.lastSignalAt;
  }

  /** Whether a challenge's signal budget is exhausted. */
  isBudgetExhausted(uniqueCode: string): boolean {
    return this.signalElapsedMs(uniqueCode) > SIGNAL_BUDGET_MS;
  }

  /** Candidate challenges for acquisition, ordered by priority. */
  candidates(count = 10, offset = 0): ChallengeState[] {
    const difficultyOrder: Record<string, number> = { easy: 0, medium: 1, hard: 2 };
    return Object.values(this.state.challenges)
      .filter((challenge) => challenge.status === "pending" || challenge.status === "orphaned" || (this.state.phase === "second_pass" && challenge.status === "deferred"))
      .sort((left, right) => {
        // Orphaned first (recover existing state), then easy→hard, then high score.
        const orphanDelta = Number(right.status === "orphaned") - Number(left.status === "orphaned");
        if (orphanDelta !== 0) return orphanDelta;
        const difficultyDelta = (difficultyOrder[left.difficulty] ?? 1) - (difficultyOrder[right.difficulty] ?? 1);
        if (difficultyDelta !== 0) return difficultyDelta;
        return right.totalScore - left.totalScore;
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
      await this.persist();
      return this.state.phase;
    });
  }

  /** Mark the run as completed. */
  async complete(): Promise<BenchmarkState> {
    return this.serialize(async () => {
      this.state.phase = "completed";
      this.metrics.completedAt = Date.now();
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
