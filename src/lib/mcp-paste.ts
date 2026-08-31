/**
 * Paste-friendly parsing for the settings-page MCP textarea. Pure so the
 * ecosystem-format handling is unit-testable: accepts the RiftX array form,
 * the {"mcpServers": {...}} wrapper (Claude Code / Cursor), and the bare
 * record form ({"name": {...}}). Returns null on invalid JSON.
 */

export function parseMcpServersDraft(text: string): unknown | null {
  // An empty box is an empty list, not a JSON error.
  if (!text.trim()) return [];
  let parsed: unknown;
  try {
    parsed = JSON.parse(text);
  } catch {
    return null;
  }
  if (parsed && typeof parsed === "object" && !Array.isArray(parsed) && "mcpServers" in parsed) {
    parsed = (parsed as { mcpServers: unknown }).mcpServers;
  }
  if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
    return Object.entries(parsed as Record<string, unknown>).map(([name, server]) => ({
      name,
      ...(server && typeof server === "object" ? server as Record<string, unknown> : {})
    }));
  }
  return parsed;
}
