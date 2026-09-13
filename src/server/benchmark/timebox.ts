import type { BenchmarkLedger, ChallengeOwner } from "./ledger";
import { BrowserExecutionBlockedError, checkBrowserExecutionGuard, withBrowserExecutionGuard } from "@/browser/runtime/execution-guard";

export const checkBenchmarkToolExecutionGuard = checkBrowserExecutionGuard;

type ExecutableTool = {
  name: string;
  execute?: (toolCallId: string, params: unknown, signal?: AbortSignal, ...rest: unknown[]) => Promise<unknown>;
};
type AttemptIdentity = { uniqueCode: string; currentAttemptStartedAt: number | null };

export function installBenchmarkTimeboxGate(
  tool: ExecutableTool,
  ledger: BenchmarkLedger,
  owner: Exclude<ChallengeOwner, null>,
  assignedChallenge?: string
): void {
  if (["benchmark_control", "assign_benchmark_challenge", "checkpoint_progress"].includes(tool.name)
    || typeof tool.execute !== "function") return;
  const original = tool.execute.bind(tool);
  const blockedResult = (attempt?: AttemptIdentity) => {
    const active = ledger.budgetForOwner(owner);
    const expectedCode = attempt?.uniqueCode ?? assignedChallenge;
    if (expectedCode && (!active || active.challenge.uniqueCode !== expectedCode
      || (attempt && active.challenge.currentAttemptStartedAt !== attempt.currentAttemptStartedAt))) {
      return {
        content: [{ type: "text" as const, text: `ATTEMPT_RELEASED: ${expectedCode} no longer belongs to this execution. Preserve evidence and stop solving.` }],
        details: { challengeReleased: true, uniqueCode: expectedCode }
      };
    }
    if (active?.budget.expired) {
      return {
        content: [{ type: "text" as const, text: `ATTEMPT_TIMEBOX_COMPLETE: ${active.challenge.uniqueCode}. Solving tools are blocked; checkpoint, submission and cleanup remain available.` }],
        details: { timeboxExpired: true, uniqueCode: active.challenge.uniqueCode, reason: "attempt_deadline" }
      };
    }
    return undefined;
  };
  tool.execute = async (toolCallId: string, params: unknown, signal?: AbortSignal, ...rest: unknown[]) => {
    signal?.throwIfAborted();
    const active = ledger.budgetForOwner(owner);
    const attempt: AttemptIdentity | undefined = active ? {
      uniqueCode: active.challenge.uniqueCode, currentAttemptStartedAt: active.challenge.currentAttemptStartedAt
    } : undefined;
    let denied = blockedResult(attempt);
    if (denied) return denied;
    const deadline = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    const check = () => {
      denied = blockedResult(attempt);
      if (denied) throw new BrowserExecutionBlockedError(denied.content[0].text);
    };
    const scheduleDeadline = () => {
      denied = blockedResult(attempt);
      if (denied) {
        deadline.abort(new BrowserExecutionBlockedError(denied.content[0].text));
        return;
      }
      if (!attempt) return;
      const current = ledger.budgetForOwner(owner);
      if (current?.budget.deadlineAt != null) {
        // Read the current ledger again when the timer fires: verified progress
        // may have extended this same attempt after the operation began.
        timer = setTimeout(scheduleDeadline, Math.max(1, Math.ceil(current.budget.remainingMs)));
      }
    };
    scheduleDeadline();
    const executionSignal = signal ? AbortSignal.any([signal, deadline.signal]) : deadline.signal;
    try {
      return await withBrowserExecutionGuard(check, async () => {
        const result = await original(toolCallId, params, executionSignal, ...rest);
        if (!denied) return result;
        // A crawl may return useful completed pages before its next queued
        // operation is denied. Keep that evidence together with the notice.
        const partial = result as { content?: unknown[]; details?: Record<string, unknown> } | undefined;
        return Array.isArray(partial?.content)
          ? { ...partial, content: [...partial.content, ...denied.content], details: { ...partial.details, ...denied.details } }
          : denied;
      });
    } catch (error) {
      if (signal?.aborted) throw error;
      const failure = denied as ReturnType<typeof blockedResult>;
      if (failure && (deadline.signal.aborted || error instanceof BrowserExecutionBlockedError)) {
        return { ...failure, ...(deadline.signal.aborted ? { isError: true } : {}) };
      }
      throw error;
    } finally {
      clearTimeout(timer);
    }
  };
}
