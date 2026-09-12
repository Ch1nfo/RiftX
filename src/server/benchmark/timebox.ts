import { FIRST_ATTEMPT_LIMIT_MS, type BenchmarkLedger, type ChallengeOwner } from "./ledger";
import { resolveBashTimeout } from "@/server/pi/bash-timeout-policy";
import { BrowserExecutionBlockedError, withBrowserExecutionGuard } from "@/browser/runtime/execution-guard";

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
  const blockedResult = () => {
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
        content: [{ type: "text" as const, text: `FIRST_ATTEMPT_COMPLETE for ${active.challenge.uniqueCode}: the fixed 30-minute first attempt has ended. This solving tool was not executed. Immediately write one concise benchmark_control checkpoint with observed findings, evidence, tested routes, supported exclusions, artifacts and unresolved questions. Let the next worker reassess independently and choose a different hypothesis; then defer and move to the next eligible challenge.` }],
        details: { timeboxExpired: true, uniqueCode: active.challenge.uniqueCode, reason: "fixed first-attempt limit" }
      };
    }
    return undefined;
  };
  tool.execute = async (toolCallId: string, params: unknown, signal?: AbortSignal, ...rest: unknown[]) => {
    const blocked = blockedResult();
    if (blocked) return blocked;
    if (tool.name === "browser" || tool.name === "crawl") {
      let denied: ReturnType<typeof blockedResult>;
      return withBrowserExecutionGuard(() => {
        denied = blockedResult();
        if (denied) throw new BrowserExecutionBlockedError(denied.content[0].text);
      }, async () => {
        try {
          const result = await original(toolCallId, params, signal, ...rest);
          if (!denied) return result;
          // Crawl keeps pages it finished before the next queued page was
          // denied. Preserve that partial report alongside the budget notice.
          const partial = result as { content: unknown[]; details?: Record<string, unknown> };
          return { ...partial, content: [...partial.content, ...denied.content], details: { ...partial.details, ...denied.details } };
        } catch (error) {
          if (denied && error instanceof BrowserExecutionBlockedError) return denied;
          throw error;
        }
      });
    }
    const active = ledger.budgetForOwner(owner);
    if (tool.name !== "bash" || !active?.budget.firstAttempt) return original(toolCallId, params, signal, ...rest);
    const remaining = Math.max(1, Math.floor(FIRST_ATTEMPT_LIMIT_MS - active.budget.elapsedMs));
    const deadline = new AbortController();
    const timer = setTimeout(() => deadline.abort(new Error("Benchmark first attempt ended")), remaining);
    const input = params as { timeout?: number };
    try {
      return await original(toolCallId, { ...input, timeout: Math.min(resolveBashTimeout(input.timeout), remaining / 1000) },
        signal ? AbortSignal.any([signal, deadline.signal]) : deadline.signal, ...rest);
    } catch (error) {
      if (!deadline.signal.aborted || signal?.aborted) throw error;
      return { content: [{ type: "text", text: `FIRST_ATTEMPT_COMPLETE: the command was interrupted at the existing first-attempt deadline. Save observed findings and defer.\n${error instanceof Error ? error.message : String(error)}` }], details: { timeboxExpired: true }, isError: true };
    } finally { clearTimeout(timer); }
  };
}
