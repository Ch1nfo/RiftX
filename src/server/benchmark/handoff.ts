import type { BenchmarkController } from "./controller";
import type { BenchmarkLedger } from "./ledger";

const timers = new WeakMap<BenchmarkLedger, Map<string, ReturnType<typeof setTimeout>>>();

async function closeExpired(controller: BenchmarkController, ledger: BenchmarkLedger, uniqueCode: string): Promise<void> {
  const challenge = ledger.getChallenge(uniqueCode);
  if (!challenge || challenge.status !== "handoff_waiting") return;
  if (!ledger.expiredHandoffs().some((candidate) => candidate.uniqueCode === uniqueCode)) {
    scheduleHandoffCleanup(controller, ledger, uniqueCode);
    return;
  }
  await ledger.expireHandoff(uniqueCode);
  try {
    await controller.closeChallenge(uniqueCode);
    await ledger.confirmClosed(uniqueCode);
  } catch {
    await ledger.markCloseFailed(uniqueCode);
  }
}

export function scheduleHandoffCleanup(controller: BenchmarkController, ledger: BenchmarkLedger, uniqueCode: string): void {
  const challenge = ledger.getChallenge(uniqueCode);
  if (!challenge || challenge.status !== "handoff_waiting" || !challenge.handoffExpiresAt) return;
  const byChallenge = timers.get(ledger) ?? new Map<string, ReturnType<typeof setTimeout>>();
  timers.set(ledger, byChallenge);
  const previous = byChallenge.get(uniqueCode);
  if (previous) clearTimeout(previous);
  const delay = Math.max(0, challenge.handoffExpiresAt - Date.now());
  const timer = setTimeout(() => {
    byChallenge.delete(uniqueCode);
    void ledger.runAction(() => closeExpired(controller, ledger, uniqueCode)).catch(() => undefined);
  }, delay);
  timer.unref?.();
  byChallenge.set(uniqueCode, timer);
}

export async function reconcileExpiredHandoffs(controller: BenchmarkController, ledger: BenchmarkLedger): Promise<void> {
  for (const challenge of ledger.expiredHandoffs()) {
    await closeExpired(controller, ledger, challenge.uniqueCode);
  }
  for (const challenge of Object.values(ledger.getState().challenges)) {
    if (challenge.status === "handoff_waiting") scheduleHandoffCleanup(controller, ledger, challenge.uniqueCode);
  }
}
