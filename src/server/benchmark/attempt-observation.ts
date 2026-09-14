export type FailureSource = "solver_failure" | "model_failure" | "tool_failure" | "environment_failure" | "platform_failure"
  | "harness_timeout" | "harness_over_restriction" | "harness_bad_handoff" | "harness_bad_compaction" | "harness_stale_state" | "harness_concurrency";
export type TerminationSource = FailureSource | "solved" | "deferred" | "cancelled" | "unknown";
export type AttemptIncident = { source: FailureSource; reason: string; gate: string | null; at: number };
export class HarnessGateError extends Error {
  constructor(readonly source: "harness_concurrency" | "harness_over_restriction", readonly gate: string, message: string) { super(message); }
}
export type AttemptResources = {
  toolCalls: number; wallTime: number; toolWallTime: number; estimatedTokens: number; progressEvents: number;
  repeatCount: number; toolErrorCount: number; compactionCount: number; callsWithoutProgress: number;
  lastProgressAt: number; lastProgressKind: string | null; lastWarningCall: number; progressRevision: number;
  fingerprints: Record<string, number>;
};
export function emptyResources(now = 0): AttemptResources {
  return { toolCalls: 0, wallTime: 0, toolWallTime: 0, estimatedTokens: 0, progressEvents: 0, repeatCount: 0,
    toolErrorCount: 0, compactionCount: 0, callsWithoutProgress: 0, lastProgressAt: now, lastProgressKind: null,
    lastWarningCall: 0, progressRevision: 0, fingerprints: {} };
}
export const FAILURE_PRIORITY: Record<FailureSource, number> = {
  platform_failure: 5, model_failure: 4, tool_failure: 4, environment_failure: 4,
  harness_timeout: 3, harness_over_restriction: 3, harness_stale_state: 3, harness_concurrency: 3,
  harness_bad_handoff: 2, harness_bad_compaction: 2, solver_failure: 1
};
