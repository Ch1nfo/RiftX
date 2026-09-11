import type { SessionRecord } from "@/server/pi/session-registry";
import { enqueueSessionAction } from "@/server/pi/session-join";
import { BENCHMARK_MAX_CONTAINERS, hasReusableBenchmarkContainer, type BenchmarkLedger } from "./ledger";

export function benchmarkMainBusy(record: Pick<SessionRecord, "session" | "compacting" | "gate" | "aborting" | "abortPromise" | "shutdownPromise" | "subagentDeliveryInProgress" | "deliveringSubagentResults" | "toolStatuses" | "pendingSessionActions">): boolean {
  return record.session.isStreaming || Boolean(record.compacting || record.aborting || record.abortPromise || record.shutdownPromise
    || record.subagentDeliveryInProgress || record.deliveringSubagentResults.size || record.toolStatuses.size || record.pendingSessionActions)
    || record.gate.pendingRequests().length > 0;
}

/** Claim a main turn synchronously; recheck delivery/ownership at dispatch time. */
export function queueBenchmarkContinuation(record: SessionRecord, ledger: BenchmarkLedger, message: string): boolean {
  if (benchmarkMainBusy(record) || !benchmarkMainHasWork(ledger)) return false;
  void enqueueSessionAction(record, async () => {
    if (benchmarkMainBusy({ ...record, pendingSessionActions: 0 }) || !benchmarkMainHasWork(ledger)) return;
    record.waitingForSubagents = false;
    record.gate.beginTask();
    await record.session.prompt(message);
  }).catch((error) => record.emitter.emit("event", { type: "error", error: error instanceof Error ? error.message : String(error) }));
  return true;
}

export function benchmarkMainHasWork(ledger: BenchmarkLedger): boolean {
  const state = ledger.getState();
  if (state.phase === "completed") return false;
  if (ledger.budgetForOwner("main")) return true;
  if (ledger.candidates().some((candidate) => state.activeContainers < BENCHMARK_MAX_CONTAINERS || hasReusableBenchmarkContainer(candidate))) return true;
  // Reconciliation is useful when a slot is stuck closing or no worker remains.
  return Object.values(state.challenges).some((challenge) => challenge.status === "closing")
    || !Object.values(state.challenges).some((challenge) => challenge.owner && ["running", "reserved"].includes(challenge.status));
}
