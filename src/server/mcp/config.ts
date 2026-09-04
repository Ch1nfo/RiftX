import type { McpServerConfig } from "@/lib/types";

/** Validation + normalization for the user-configured MCP server list. Pure, no SDK imports, so it stays unit-testable. */

const NAME_PATTERN = /^[A-Za-z0-9_-]{1,32}$/;
export const MAX_MCP_SERVERS = 20;
const MAX_TOOL_FILTERS = 100;
const MAX_TOOL_FILTER_LENGTH = 256;

function stringRecord(value: unknown): Record<string, string> | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const out: Record<string, string> = {};
  for (const [key, item] of Object.entries(value)) {
    if (typeof item !== "string") return null;
    out[key] = item;
  }
  return out;
}

function httpUrlError(value: unknown): string | null {
  if (typeof value !== "string" || !value.trim()) return "http server requires a url";
  try {
    const parsed = new URL(value);
    if ((parsed.protocol !== "http:" && parsed.protocol !== "https:") || !parsed.hostname) return "http server url must be an http(s) URL with a hostname";
  } catch {
    return "http server url is not a valid URL";
  }
  return null;
}

function stringListError(value: unknown, field: string): string | null {
  if (value === undefined) return null;
  if (!Array.isArray(value) || value.length > MAX_TOOL_FILTERS || value.some((item) => typeof item !== "string" || !item.trim() || item.length > MAX_TOOL_FILTER_LENGTH)) {
    return `${field} must be an array of at most ${MAX_TOOL_FILTERS} non-empty strings (max ${MAX_TOOL_FILTER_LENGTH} chars each)`;
  }
  return null;
}

/** Ecosystem configs (Claude Code, Cursor, …) spell the transport key "type" — accept both. */
function transportOf(server: Partial<McpServerConfig> & { type?: unknown }): "stdio" | "http" | undefined {
  const value = server.transport ?? server.type;
  if (value === "stdio" || value === "http") return value;
  // Official Claude .mcp.json stdio entries carry no type at all: a command
  // implies stdio. URL-only entries are NOT inferred as http — an SSE config
  // would be silently misrouted to Streamable HTTP.
  if (value === undefined && typeof server.command === "string" && server.command.trim()) return "stdio";
  return undefined;
}

function serverError(server: unknown): string | null {
  if (!server || typeof server !== "object" || Array.isArray(server)) return "each MCP server must be an object";
  const candidate = server as Partial<McpServerConfig> & { type?: unknown };
  const transport = transportOf(candidate);
  if (typeof candidate.name !== "string" || !NAME_PATTERN.test(candidate.name)) return "MCP server name must match [A-Za-z0-9_-] and be 1-32 characters";
  if (!transport) return `MCP server "${candidate.name}": transport must be "stdio" or "http"`;
  if (candidate.visibility !== undefined && (!Array.isArray(candidate.visibility) || candidate.visibility.some((role) => role !== "main" && role !== "child") || new Set(candidate.visibility).size !== candidate.visibility.length)) {
    return `MCP server "${candidate.name}": visibility must contain unique "main" and/or "child" values`;
  }
  const includeError = stringListError(candidate.includeTools, "includeTools");
  if (includeError) return `MCP server "${candidate.name}": ${includeError}`;
  const excludeError = stringListError(candidate.excludeTools, "excludeTools");
  if (excludeError) return `MCP server "${candidate.name}": ${excludeError}`;
  if (transport === "stdio") {
    if (typeof candidate.command !== "string" || !candidate.command.trim()) return `MCP server "${candidate.name}": stdio transport requires a command`;
    if (candidate.args !== undefined && (!Array.isArray(candidate.args) || candidate.args.some((arg) => typeof arg !== "string"))) return `MCP server "${candidate.name}": args must be an array of strings`;
    if (candidate.env !== undefined && !stringRecord(candidate.env)) return `MCP server "${candidate.name}": env must be a string map`;
  } else {
    const urlError = httpUrlError(candidate.url);
    if (urlError) return `MCP server "${candidate.name}": ${urlError}`;
    if (candidate.headers !== undefined && !stringRecord(candidate.headers)) return `MCP server "${candidate.name}": headers must be a string map`;
  }
  return null;
}

/** First validation error for the whole list, or null when valid. Reject-not-mangle: the settings PUT 400s on this. */
export function mcpServersValidationError(value: unknown): string | null {
  if (value === undefined) return null;
  if (!Array.isArray(value)) return "mcpServers must be an array";
  if (value.length > MAX_MCP_SERVERS) return `mcpServers is limited to ${MAX_MCP_SERVERS} servers`;
  const names = new Set<string>();
  for (const server of value) {
    const error = serverError(server);
    if (error) return error;
    const name = (server as McpServerConfig).name;
    if (names.has(name)) return `duplicate MCP server name "${name}"`;
    names.add(name);
  }
  return null;
}

/** readConfig normalization: silently drop invalid entries (the browserScope pattern). */
export function normalizeMcpServers(value: unknown): McpServerConfig[] {
  if (!Array.isArray(value)) return [];
  const seen = new Set<string>();
  const out: McpServerConfig[] = [];
  for (const server of value) {
    if (serverError(server) !== null) continue;
    const candidate = server as Partial<McpServerConfig> & { type?: unknown };
    // serverError has validated the name; narrow it for the compiler.
    const name = candidate.name!;
    if (seen.has(name)) continue;
    seen.add(name);
    // Canonicalize "type" (ecosystem configs) to "transport" on the way in.
    const transport = transportOf(candidate)!;
    out.push({
      name,
      transport,
      ...(candidate.visibility !== undefined ? { visibility: [...candidate.visibility] } : {}),
      ...(candidate.includeTools?.length ? { includeTools: [...candidate.includeTools] } : {}),
      ...(candidate.excludeTools?.length ? { excludeTools: [...candidate.excludeTools] } : {}),
      ...(transport === "stdio"
        ? { command: candidate.command, args: candidate.args ?? [], env: candidate.env ?? {} }
        : { url: candidate.url, headers: candidate.headers ?? {} })
    });
  }
  // A hand-edited config file bypasses API validation; enforce the cap here too.
  return out.slice(0, MAX_MCP_SERVERS);
}
