import type { BenchmarkController } from "./controller";
import type { BenchmarkLedger, ChallengeOwner, ChallengeState } from "./ledger";

type Owner = Exclude<ChallengeOwner, null>;
type Attempt = Pick<ChallengeState, "uniqueCode" | "attemptCount" | "currentAttemptStartedAt">;

/** Abort only this worker's attempt. The main worker's children keep running. */
export function abortBenchmarkAttempt(record: {
  aborting?: boolean;
  abortEpoch?: number;
  benchmarkAttemptTimeoutEpoch?: number;
  abortPromise?: Promise<void>;
  shutdownPromise?: Promise<void>;
  compacting?: boolean;
  toolStatuses?: Map<string, unknown>;
  gate: { rejectAll(): void };
  session: { abortBash(): void; abortCompaction(): void; abort(): Promise<unknown> };
  browser?: { close(): Promise<unknown> };
}): Promise<void> {
  if (record.abortPromise) return record.abortPromise;
  if (record.shutdownPromise) return record.shutdownPromise;
  record.aborting = true;
  record.abortEpoch = (record.abortEpoch ?? 0) + 1;
  record.benchmarkAttemptTimeoutEpoch = (record.benchmarkAttemptTimeoutEpoch ?? 0) + 1;
  record.compacting = false;
  record.gate.rejectAll();
  record.session.abortBash();
  record.session.abortCompaction();
  const pending = Promise.allSettled([
    record.session.abort(),
    record.browser?.close() ?? Promise.resolve()
  ]).then(() => undefined);
  record.abortPromise = pending;
  return pending.finally(() => {
    record.toolStatuses?.clear();
    if (record.abortPromise === pending) record.abortPromise = undefined;
    if (!record.shutdownPromise) record.aborting = false;
  });
}

/** A wall-clock supervisor also expires workers that never make another tool call. */
export function startBenchmarkAttemptWatchdog(options: {
  ledger: BenchmarkLedger;
  controller: Pick<BenchmarkController, "closeChallenge">;
  owner: Owner;
  stopWorker: () => Promise<void>;
  isStopping: () => boolean;
  warn: (challenge: ChallengeState) => Promise<void>;
  event?: (event: "attempt_warning" | "attempt_timeout" | "attempt_closed" | "attempt_close_failed" | "attempt_watchdog_error", attempt?: Attempt) => void;
  pollMs?: number;
}) {
  const { ledger, controller, owner } = options;
  let disposed = false;
  let checking: Promise<void> | undefined;
  let warned: string | undefined;
  const retryCloseAt = new Map<string, number>();
  const recovering = new Set<string>();

  const close = async (attempt: Attempt) => {
    const current = ledger.getChallenge(attempt.uniqueCode);
    // Never let a delayed cleanup close a newly acquired environment.
    if (!current || current.attemptCount !== attempt.attemptCount || current.status !== "closing" || current.owner) {
      retryCloseAt.delete(attempt.uniqueCode);
      return;
    }
    try {
      await controller.closeChallenge(attempt.uniqueCode);
      if (ledger.getChallenge(attempt.uniqueCode)?.status === "closing") await ledger.confirmClosed(attempt.uniqueCode);
      retryCloseAt.delete(attempt.uniqueCode);
      options.event?.("attempt_closed", attempt);
    } catch {
      if (ledger.getChallenge(attempt.uniqueCode)?.status !== "closing") return;
      await ledger.markCloseFailed(attempt.uniqueCode);
      retryCloseAt.set(attempt.uniqueCode, Date.now() + 5_000);
      options.event?.("attempt_close_failed", attempt);
    }
  };

  const run = async () => {
    if (disposed || options.isStopping()) return;
    // The parent also recovers failed closes after a child watchdog is disposed
    // or after process restart. A slow platform close must not delay its own
    // worker's warning/expiry checks, so reconciliation runs separately.
    if (owner === "main") {
      for (const current of Object.values(ledger.getState().challenges)) {
        if (current.status !== "closing" || current.owner || recovering.has(current.uniqueCode)
          || Date.now() < (retryCloseAt.get(current.uniqueCode) ?? 0)) continue;
        const attempt = { uniqueCode: current.uniqueCode, attemptCount: current.attemptCount, currentAttemptStartedAt: current.currentAttemptStartedAt };
        recovering.add(attempt.uniqueCode);
        void ledger.runChallengeAction(attempt.uniqueCode, () => close(attempt))
          .catch(() => options.event?.("attempt_watchdog_error", attempt))
          .finally(() => recovering.delete(attempt.uniqueCode));
      }
    }
    const active = ledger.budgetForOwner(owner);
    if (!active || active.challenge.currentAttemptStartedAt === null) return;
    const { challenge, budget } = active;
    // Ledger getters expose mutable state. Capture the lease before any await.
    const attempt = { uniqueCode: challenge.uniqueCode, attemptCount: challenge.attemptCount, currentAttemptStartedAt: challenge.currentAttemptStartedAt! };
    const key = `${attempt.uniqueCode}:${attempt.currentAttemptStartedAt}`;
    if (budget.warningDue && !budget.expired && warned !== key) {
      await options.warn(challenge);
      warned = key;
      options.event?.("attempt_warning", attempt);
    }
    if (!budget.expired) return;
    // Existing submissions own this action lock. Let their authoritative reply
    // settle before changing ownership; their flag must not be lost at timeout.
    let stopping: Promise<void> | undefined;
    try {
      await ledger.runChallengeAction(attempt.uniqueCode, async () => {
        if (disposed || options.isStopping()) return;
        const current = ledger.getChallenge(attempt.uniqueCode);
        if (!current || current.owner !== owner || current.attemptCount !== attempt.attemptCount
          || current.currentAttemptStartedAt !== attempt.currentAttemptStartedAt
          || !ledger.budgetFor(attempt.uniqueCode)?.expired) return;
        // Freeze generation before expiry releases ownership, preventing this
        // worker's queued calls from acquiring another challenge during cleanup.
        stopping = options.stopWorker();
        // Observe rejection immediately while the platform close is in flight.
        void stopping.catch(() => undefined);
        const expired = await ledger.expireAttempt(attempt.uniqueCode, owner, attempt.currentAttemptStartedAt);
        if (!expired) return;
        options.event?.("attempt_timeout", attempt);
        await close(attempt);
      });
    } finally {
      // SDK abort waits for queued tools to settle. Those tools may themselves
      // be waiting for this challenge action lock, so drain only after release.
      await stopping;
    }
  };

  const check = (): Promise<void> => {
    if (checking) return checking;
    checking = run().catch(() => options.event?.("attempt_watchdog_error"))
      .finally(() => { checking = undefined; });
    return checking;
  };
  const timer = setInterval(() => { void check(); }, options.pollMs ?? 1_000);
  timer.unref();
  return {
    check,
    dispose() { disposed = true; clearInterval(timer); }
  };
}
