import type { ToolDefinition } from "@mariozechner/pi-coding-agent";
import { mapCallResult, mcpToolName, parameterSchema, promptSnippetFor, sanitizeSegment } from "./bridge";
import type { McpServerEntry } from "./manager";

/**
 * Builds RiftX ToolDefinitions from connected MCP servers. Literals annotated
 * as ToolDefinition (not defineTool): parameter schemas are arbitrary JSON
 * Schema and generic inference over them is unpredictable — the same pattern
 * the wrapped bash tool uses. Tools bind the entry captured at session
 * creation (snapshot semantics); a connection closed by a later reconcile
 * makes calls fail with a reopen hint.
 */

export function buildMcpTools(entry: McpServerEntry): ToolDefinition[] {
  if (entry.state === "error") return [];
  const transportSummary = entry.config.transport === "stdio" ? `stdio: ${entry.config.command}` : `http: ${entry.config.url}`;
  const seen = new Set<string>();
  const tools: ToolDefinition[] = [];
  for (const tool of entry.handle.tools) {
    const clean = sanitizeSegment(tool.name);
    if (!clean) continue;
    const name = mcpToolName(entry.config.name, tool.name);
    if (seen.has(name)) {
      console.warn(`RiftX MCP server "${entry.config.name}": duplicate tool name "${tool.name}" skipped`);
      continue;
    }
    seen.add(name);
    const description = `${tool.description?.trim() || "MCP tool with no description."} (external MCP server "${entry.config.name}", ${transportSummary})`;
    const parameters = parameterSchema(tool.inputSchema);
    tools.push({
      name,
      label: `${entry.config.name}: ${tool.name}`.slice(0, 80),
      description,
      promptSnippet: promptSnippetFor(name, parameters),
      parameters: parameters as unknown as ToolDefinition["parameters"],
      async execute(_toolCallId, params, signal) {
        const result = await entry.handle.call(tool.name, params as Record<string, unknown>, signal) as Parameters<typeof mapCallResult>[0];
        return mapCallResult(result, entry.config.name, tool.name);
      }
    });
  }
  return tools;
}
