import { createHash } from "node:crypto";
import { performance } from "node:perf_hooks";
import type { BenchmarkLedger, ChallengeOwner } from "./ledger";
import { attemptContext, captureFence, StaleAttemptError, type ExecutableTool } from "./fencing";
import { BenchmarkError } from "./controller";
import { HarnessGateError } from "./attempt-observation";

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

/** Uniform observation only; no command classification or dedicated hard budgets. */
export function installBenchmarkRepeatNotice(tool: ExecutableTool, ledger: BenchmarkLedger, owner: Exclude<ChallengeOwner, null>): void {
  if (!tool.execute || tool.name === "assign_benchmark_challenge") return;
  const execute = tool.execute.bind(tool);
  tool.execute = async (id, params, signal, ...rest) => {
    const fence = attemptContext.getStore() ?? captureFence(ledger.budgetForOwner(owner)?.challenge, owner);
    const started = performance.now();
    const observe = async (result: unknown, error: boolean) => {
      if (!fence) return result;
      const warning = await ledger.observeTool(fence, { fingerprint: fingerprint(tool.name, params, result),
        wallTime: performance.now() - started, estimatedTokens: Math.ceil(JSON.stringify([params, result]).length / 4), error });
      if (!warning || !result || typeof result !== "object") return result;
      const original = result as { content?: unknown[]; details?: object };
      return { ...original, content: [...(original.content ?? []), { type: "text", text: JSON.stringify(warning) }],
        details: { ...original.details, warning } };
    };
    let result: unknown;
    try { result = await execute(id, params, signal, ...rest); }
    catch (error) {
      if (error instanceof StaleAttemptError || signal?.aborted) throw error;
      const message = error instanceof Error ? error.message : String(error);
      await ledger.recordAttemptIncident(fence, error instanceof BenchmarkError ? "platform_failure" : error instanceof HarnessGateError ? error.source : "tool_failure",
        message, error instanceof HarnessGateError ? error.gate : null);
      const original = { content: [{ type: "text", text: message }] };
      const observed = await observe(original, true);
      if (observed === original) throw error;
      throw new Error(JSON.stringify(observed), { cause: error });
    }
    const failed = Boolean((result as { isError?: boolean })?.isError);
    if ((result as { details?: { restricted?: boolean } })?.details?.restricted) {
      await ledger.recordAttemptIncident(fence, "harness_over_restriction", `${tool.name} rejected an operation outside this worker's permissions`, "tool_permissions");
    }
    if (failed && !(result as { details?: { timeboxExpired?: boolean } }).details?.timeboxExpired) await ledger.recordAttemptIncident(fence, "tool_failure", `${tool.name} returned an error`);
    return observe(result, failed);
  };
}
