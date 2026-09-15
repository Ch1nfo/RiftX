import { createHash } from "node:crypto";
import { performance } from "node:perf_hooks";
import { MutationLock } from "@/server/pi/mutation-lock";
import { resolveBashTimeout } from "@/server/pi/bash-timeout-policy";
import type { BenchmarkLedger, ChallengeOwner } from "./ledger";

export const PASSWORD_ENUMERATION_BUDGET_MS = 120_000;
export const PASSWORD_ENUMERATION_CALL_MS = 30_000;
type Tool = { name: string; execute?: (id: string, params: unknown, signal?: AbortSignal, ...rest: unknown[]) => Promise<unknown> };
type EffortState = { enumerationLock: MutationLock; progress: string; repeats: Map<string, number> };
const runs = new WeakMap<BenchmarkLedger, Map<string, EffortState>>();

function effort(ledger: BenchmarkLedger, code: string): EffortState {
  let challenges = runs.get(ledger);
  if (!challenges) runs.set(ledger, challenges = new Map());
  let state = challenges.get(code);
  if (!state) challenges.set(code, state = { enumerationLock: new MutationLock(), progress: "", repeats: new Map() });
  return state;
}

export function isPasswordEnumeration(params: unknown): boolean {
  const input = params as { command?: string; passwordEnumeration?: boolean } | undefined;
  if (input?.passwordEnumeration === true) return true;
  const command = input?.command ?? "";
  // Recognize dedicated online guessers; arbitrary scripts must declare their purpose.
  return (/(?:^|[\s/;&|])(hydra|medusa|ncrack)(?:\s|$)/i.test(command)
      || /(?:^|[\s/;&|])patator\s+\w+_login\b/i.test(command))
    && !/^\s*(?:\S*\/)?(?:hydra|medusa|ncrack|patator)\s+(?:--help|--version|-h|-V)\s*$/.test(command);
}

function notice(text: string, details: Record<string, unknown> = {}) {
  return { content: [{ type: "text" as const, text }], details };
}

/** Install inside execution locks, so queueing never spends a guessing budget. */
export function installPasswordEnumerationBudget(tool: Tool, ledger: BenchmarkLedger, owner: Exclude<ChallengeOwner, null>): void {
  if (tool.name !== "bash" || !tool.execute) return;
  const execute = tool.execute.bind(tool);
  tool.execute = async (id, params, signal, ...rest) => {
    if (!isPasswordEnumeration(params)) return execute(id, params, signal, ...rest);
    const active = ledger.budgetForOwner(owner);
    if (!active) return notice("Online password enumeration requires an owned benchmark challenge.", { passwordEnumerationBlocked: true });
    const code = active.challenge.uniqueCode;
    const release = await effort(ledger, code).enumerationLock.acquire(signal);
    try {
      const current = ledger.budgetForOwner(owner);
      if (current?.challenge.uniqueCode !== code || current.budget.expired) {
        return notice("The challenge was released or its first attempt ended while this call was queued. Save findings and reconcile benchmark_control.");
      }
      const remaining = PASSWORD_ENUMERATION_BUDGET_MS - current.challenge.passwordEnumerationMs;
      if (remaining <= 0) return notice("PASSWORD_ENUMERATION_BUDGET_EXHAUSTED: this challenge has used its cumulative 120 seconds of online password guessing. Reassess the evidence and investigate a different hypothesis.", { passwordEnumerationBlocked: true });
      const input = params as { timeout?: number };
      const limit = Math.max(1, Math.floor(Math.min(remaining, PASSWORD_ENUMERATION_CALL_MS, resolveBashTimeout(input.timeout) * 1000)));
      const deadline = new AbortController();
      const timer = setTimeout(() => deadline.abort(new Error("Online password enumeration time budget reached")), limit);
      const started = performance.now();
      try {
        return await execute(id, { ...input, timeout: limit / 1000 }, signal ? AbortSignal.any([signal, deadline.signal]) : deadline.signal, ...rest);
      } catch (error) {
        if (!deadline.signal.aborted || signal?.aborted) throw error;
        return { ...notice("Online password enumeration reached its time limit. The elapsed time is charged to this challenge across all workers and attempts.", { passwordEnumerationTimedOut: true }), isError: true };
      } finally {
        clearTimeout(timer);
        await ledger.recordPasswordEnumerationTime(code, Math.max(performance.now() - started, deadline.signal.aborted ? limit : 0));
      }
    } finally { release(); }
  };
}

function canonical(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(canonical);
  if (value && typeof value === "object") return Object.fromEntries(Object.entries(value).sort(([a], [b]) => a.localeCompare(b)).map(([key, item]) => [key, canonical(item)]));
  return value;
}

/** Tool content only: call ids, execution durations and bookkeeping are not progress. */
function fingerprint(tool: string, params: unknown, result: unknown): string {
  const content = (result as { content?: unknown } | undefined)?.content ?? result;
  const input = params && typeof params === "object" ? { ...params, timeout: undefined } : params;
  const text = JSON.stringify(canonical([tool, input, content]))
    .replace(/\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z/g, "[timestamp]")
    .replace(/(?:wall time|duration|elapsed)(?:\s*[:=])?\s*\d+(?:\.\d+)?\s*(?:ms|s|seconds?)/gi, "[duration]");
  return createHash("sha256").update(text).digest("hex");
}

export function installBenchmarkRepeatNotice(tool: Tool, ledger: BenchmarkLedger, owner: Exclude<ChallengeOwner, null>): void {
  if (!tool.execute || tool.name === "assign_benchmark_challenge") return;
  const execute = tool.execute.bind(tool);
  tool.execute = async (id, params, signal, ...rest) => {
    const active = ledger.budgetForOwner(owner)?.challenge;
    const code = active?.uniqueCode ?? `coordinator:${owner}`;
    const observe = (result: unknown) => {
      // Changing ownership is itself progress and belongs to another context.
      const current = ledger.budgetForOwner(owner)?.challenge;
      if (current?.uniqueCode !== active?.uniqueCode) return result;
      const state = effort(ledger, code);
      const progress = JSON.stringify([current?.lastMeaningfulProgressAt, current?.lastMeaningfulSignalContent, current?.correctFlagCount]);
      if (progress !== state.progress) { state.progress = progress; state.repeats.clear(); }
      const key = fingerprint(tool.name, params, result);
      const count = (state.repeats.get(key) ?? 0) + 1;
      state.repeats.delete(key);
      state.repeats.set(key, count);
      if (state.repeats.size > 32) state.repeats.delete(state.repeats.keys().next().value!);
      // Repetition is retained as internal bookkeeping only. Do not inject
      // advisory text into the model context: black-box scoring benefits from
      // leaving route selection to the solver.
      return result;
    };
    let result: unknown;
    try { result = await execute(id, params, signal, ...rest); }
    catch (error) {
      if (signal?.aborted) throw error;
      const message = error instanceof Error ? error.message : String(error);
      const original = notice(message);
      const observed = observe(original) as ReturnType<typeof notice>;
      if (observed === original) throw error;
      throw new Error(observed.content.map((part) => part.text).join("\n\n"), { cause: error });
    }
    return observe(result);
  };
}
