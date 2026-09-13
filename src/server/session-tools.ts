/**
 * Single source for the session tool whitelist. The SDK treats the `tools`
 * option as a hard allowlist and silently drops ANY tool — built-in or
 * customTools — whose name is absent, so every custom tool's name must be
 * listed here. Kept SDK-import-free so the whitelist contract is
 * unit-testable and shared by session-manager and the web tools.
 *
 * Benchmark branch: record_finding and checkpoint_progress are replaced by
 * the benchmark ledger (benchmark_control + assign_benchmark_challenge).
 */
export const WEB_TOOL_NAMES = ["web_search", "web_fetch"] as const;
export const BENCHMARK_TOOL_NAMES = ["benchmark_control", "assign_benchmark_challenge"] as const;

export function sessionToolNames(_subagents: boolean, toolInventory = false): string[] {
  // Benchmark branch: spawn_subagent is REMOVED — assign_benchmark_challenge
  // is the only dispatch path and it enforces reservation, limits, and scope.
  return [
    "read", "grep", "find", "ls", "bash", "write", "edit", "browser",
    ...BENCHMARK_TOOL_NAMES, "crawl", ...(toolInventory ? ["tool_inventory"] : [])
  ];
}
