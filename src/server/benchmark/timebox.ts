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
      return {
        content: [{ type: "text" as const, text: `FIRST_ATTEMPT_COMPLETE for ${active.challenge.uniqueCode}: the fixed 30-minute first attempt has ended. This solving tool was not executed. Immediately write one concise benchmark_control checkpoint with findings, attempted routes, ruled-out assumptions, artifacts, and the exact next probe; then defer and move to the next eligible challenge.` }],
        details: { timeboxExpired: true, uniqueCode: active.challenge.uniqueCode, reason: "fixed first-attempt limit" }
      };
    }
    return original(toolCallId, params, signal, ...rest);
  };
}
