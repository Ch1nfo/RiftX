import type { BenchmarkLedger, ChallengeOwner } from "./ledger";

type ExecutableTool = {
  name: string;
  execute?: (toolCallId: string, params: unknown, signal?: AbortSignal, ...rest: unknown[]) => Promise<unknown>;
};

export function installBenchmarkTimeboxGate(
  tool: ExecutableTool,
  ledger: BenchmarkLedger,
  owner: Exclude<ChallengeOwner, null>,
  assignedChallenge?: string
): void {
  if (tool.name === "benchmark_control" || tool.name === "assign_benchmark_challenge" || typeof tool.execute !== "function") return;
  const original = tool.execute.bind(tool);
  tool.execute = async (toolCallId: string, params: unknown, signal?: AbortSignal, ...rest: unknown[]) => {
    if (assignedChallenge) {
      const assigned = ledger.getChallenge(assignedChallenge);
      if (!assigned || assigned.owner !== owner || (assigned.status !== "running" && assigned.status !== "reserved")) {
        return {
          content: [{ type: "text" as const, text: `CHALLENGE_RELEASED for ${assignedChallenge}. This SubAgent no longer owns an active challenge, so the solving tool was not executed. Return your structured result now; only benchmark_control may record final scoped state.` }],
          details: { challengeReleased: true, uniqueCode: assignedChallenge }
        };
      }
    }
    const active = ledger.budgetForOwner(owner);
    if (active?.budget.expired) {
      const reason = active.budget.workerRotationDue
        ? "30 minutes without an accepted flag; hand off to a fresh worker"
        : active.budget.hardExpired
          ? "attempt lease expired"
          : "no meaningful progress within the current phase budget";
      return {
        content: [{ type: "text" as const, text: `TIMEBOX_EXPIRED for ${active.challenge.uniqueCode}: ${reason}. This solving tool was not executed. Use benchmark_control to submit, record evidence-backed progress, defer for a materially different approach, or abandon only if no viable hypothesis remains.` }],
        details: { timeboxExpired: true, uniqueCode: active.challenge.uniqueCode, reason }
      };
    }
    return original(toolCallId, params, signal, ...rest);
  };
}
