/**
 * Single source for the session tool whitelist. The SDK treats the `tools`
 * option as a hard allowlist and silently drops ANY tool — built-in or
 * customTools — whose name is absent, so every custom tool's name must be
 * listed here. Kept SDK-import-free so the whitelist contract is
 * unit-testable and shared by session-manager and the web tools.
 */
export const WEB_TOOL_NAMES = ["web_search", "web_fetch"] as const;

export function sessionToolNames(subagents: boolean): string[] {
  return [
    "read", "grep", "find", "ls", "bash", "write", "edit", "browser",
    "record_finding", ...WEB_TOOL_NAMES,
    ...(subagents ? ["spawn_subagent"] : [])
  ];
}
