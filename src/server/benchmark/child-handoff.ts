import type { SubagentTask } from "@/lib/types";
import type { BenchmarkController } from "./controller";
import type { BenchmarkLedger } from "./ledger";

type HandoffLedger = Pick<BenchmarkLedger, "getChallenge" | "runChallengeAction" | "recordChildHandoff" | "releaseOnSubagentExit" | "confirmClosed" | "markCloseFailed">;

/** Persist the report before freeing a worker's platform slot or delivering its result. */
export function createBenchmarkChildHandoff(options: {
  ledger: HandoffLedger;
  controller: Pick<BenchmarkController, "closeChallenge">;
}) {
  const { ledger, controller } = options;
  const savedReports = new Set<string>();
  return async (task: SubagentTask, summary?: string): Promise<void> => {
    const uniqueCode = task.benchmarkChallenge;
    if (!uniqueCode) return;
    const worker = `subagent:${task.id}` as const;
    const key = JSON.stringify([uniqueCode, worker]);
    try {
      await ledger.runChallengeAction(uniqueCode, async () => {
        const current = ledger.getChallenge(uniqueCode);
        if (!current) throw new Error("The child handoff has no matching benchmark challenge.");
        if (!savedReports.has(key)) {
          if (current.owner !== worker && current.currentAttemptWorker !== worker
            && !current.approachHistory.some((attempt) => attempt.worker === worker)) {
            throw new Error("The child handoff cannot be associated with a recorded attempt.");
          }
          const report = summary?.trim() ? summary : task.summary?.trim() ? task.summary
            : JSON.stringify({ worker, status: task.status, error: task.error ?? "" });
          await ledger.recordChildHandoff(uniqueCode, worker, report);
          // Only persistence success permits this cache entry; cleanup can still need retries.
          savedReports.add(key);
        }

        const owned = ledger.getChallenge(uniqueCode);
        if (owned?.owner === worker && ["running", "reserved", "orphaned"].includes(owned.status)) {
          await ledger.releaseOnSubagentExit(uniqueCode, `Subagent ${task.status}`, worker);
        }
        const closing = ledger.getChallenge(uniqueCode);
        const lastWorker = closing?.currentAttemptWorker ?? closing?.approachHistory.at(-1)?.worker;
        // A retry of an older completion must never close a replacement worker's environment.
        if (closing?.status !== "closing" || closing.owner || lastWorker !== worker) return;
        try {
          await controller.closeChallenge(uniqueCode);
        } catch (error) {
          await ledger.markCloseFailed(uniqueCode).catch((markError) => {
            console.warn("[Benchmark] Could not persist child cleanup failure", { taskId: task.id, uniqueCode }, markError);
          });
          throw error;
        }
        await ledger.confirmClosed(uniqueCode);
      });
    } catch (error) {
      console.warn("[Benchmark] Child handoff or cleanup is pending; result remains undelivered", { taskId: task.id, uniqueCode }, error);
      throw error;
    }
  };
}
