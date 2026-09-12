import { createHash } from "node:crypto";
import { BenchmarkError, type BenchmarkController } from "./controller";
import type { BenchmarkLedger, ChallengeOwner } from "./ledger";

type PendingSubmission = {
  uniqueCode: string; flag: string; owner: Exclude<ChallengeOwner, null>;
  attempts: number; retryAt: number; evidenceRef: string;
};
// Candidates live only for this run; the persisted ledger contains hashes/notes.
const queues = new WeakMap<BenchmarkLedger, Map<string, PendingSubmission>>();
const draining = new WeakMap<BenchmarkLedger, Promise<void>>();
const retryDelays = [30_000, 60_000, 120_000];
const key = (code: string, flag: string) => createHash("sha256").update(JSON.stringify([code, flag])).digest("hex");

export function pendingSubmission(ledger: BenchmarkLedger, code: string, flag: string) {
  const entry = queues.get(ledger)?.get(key(code, flag));
  return entry ? { exhausted: entry.attempts >= retryDelays.length, attempts: entry.attempts } : undefined;
}

export function clearPendingSubmission(ledger: BenchmarkLedger, code: string, flag: string): void {
  queues.get(ledger)?.delete(key(code, flag));
}

export function enqueuePendingSubmission(ledger: BenchmarkLedger, uniqueCode: string, flag: string, owner: Exclude<ChallengeOwner, null>, now = Date.now()) {
  let queue = queues.get(ledger);
  if (!queue) queues.set(ledger, queue = new Map());
  const id = key(uniqueCode, flag);
  if (!queue.has(id)) queue.set(id, { uniqueCode, flag, owner, attempts: 0, retryAt: now + retryDelays[0], evidenceRef: ledger.getChallenge(uniqueCode)?.lastEvidenceRef ?? "" });
}

export function hasPendingSubmissions(ledger: BenchmarkLedger): boolean {
  return [...(queues.get(ledger)?.values() ?? [])].some((entry) => entry.attempts < retryDelays.length);
}

export function retryPendingSubmissions(controller: BenchmarkController, ledger: BenchmarkLedger, now = Date.now()): Promise<void> {
  const active = draining.get(ledger);
  if (active) return active;
  const run = async () => {
    const queue = queues.get(ledger);
    if (!queue) return;
    const selected = new Set<string>();
    const due = [...queue.entries()].filter(([, entry]) => {
      if (entry.retryAt > now || entry.attempts >= retryDelays.length || selected.has(entry.uniqueCode)) return false;
      selected.add(entry.uniqueCode);
      return true;
    });
    await Promise.all(due.map(([id, entry]) => ledger.runChallengeAction(entry.uniqueCode, async () => {
      entry.attempts++;
      entry.retryAt = now + (retryDelays[entry.attempts] ?? Infinity);
      try {
        const guard = ledger.captureSyncGuard();
        const board = await controller.listChallenges();
        const state = ledger.getState();
        await ledger.syncFromPlatform(board, state.vpnOk, state.vpnClientIp, state.vpnChecked, guard);
        const challenge = ledger.getChallenge(entry.uniqueCode);
        if (!challenge) { entry.attempts = retryDelays.length; return; }
        if (challenge.isCompleted) { queue.delete(id); return; }
        // Counts alone cannot identify which pending flag was accepted. The
        // platform's exact-candidate duplicate response resolves that ambiguity.
        let result: Awaited<ReturnType<BenchmarkController["submitFlag"]>>;
        let duplicate = false;
        try { result = await controller.submitFlag(entry.uniqueCode, entry.flag); }
        catch (error) {
          if (!(error instanceof BenchmarkError) || error.kind !== "duplicate_submit") throw error;
          duplicate = true;
          const match = (await controller.listChallenges()).find((item) => item.unique_code === entry.uniqueCode);
          if (!match) throw new BenchmarkError("connection_error", "Duplicate confirmation has no challenge snapshot");
          result = { unique_code: entry.uniqueCode, correct: true, awarded: 0, cumulative_score: 0,
            correct_flag_count: match.correct_flag_count, total_flag_count: match.flag_count, matched_flag_index: null };
        }
        const owner = ledger.getChallenge(entry.uniqueCode)?.owner ?? entry.owner;
        await ledger.recordSubmission(entry.uniqueCode, entry.flag, result.correct, duplicate ? undefined : result.cumulative_score,
          result.correct_flag_count, result.matched_flag_index, owner, true);
        if (result.correct && result.correct_flag_count >= result.total_flag_count) {
          const solved = await ledger.markSolved(entry.uniqueCode, duplicate ? undefined : result.cumulative_score, owner, true);
          if (solved.status === "closing") {
            try { await controller.closeChallenge(entry.uniqueCode); await ledger.confirmClosed(entry.uniqueCode); }
            catch { await ledger.markCloseFailed(entry.uniqueCode); }
          }
        }
        queue.delete(id);
      } catch (error) {
        if (!(error instanceof BenchmarkError) || !["timeout", "connection_error", "internal_error", "resource_unavailable"].includes(error.kind)) {
          entry.attempts = retryDelays.length;
        }
        // Keep the unresolved candidate distinct from an accepted/rejected one.
        await ledger.recordPendingSubmissionStatus(entry.uniqueCode, entry.owner,
          (entry.attempts >= retryDelays.length ? "Pending flag confirmation exhausted its bounded recovery attempts; outcome remains unknown." : "Pending flag confirmation will retry after backoff; outcome remains unknown.") + (entry.evidenceRef ? ` Source evidence: ${entry.evidenceRef}` : ""), entry.flag);
      }
    })));
  };
  const promise = run().finally(() => draining.delete(ledger));
  draining.set(ledger, promise);
  return promise;
}
