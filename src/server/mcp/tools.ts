import type { ToolDefinition } from "@mariozechner/pi-coding-agent";
import { mapCallResult, mcpToolName, parameterSchema, promptSnippetFor, sanitizeSegment, type McpCallResultLike } from "./bridge";
import type { McpServerEntry } from "./manager";
import type { ToolOutputStore } from "@/server/tool-output";

/**
 * Builds RiftX ToolDefinitions from connected MCP servers. Literals annotated
 * as ToolDefinition (not defineTool): parameter schemas are arbitrary JSON
 * Schema and generic inference over them is unpredictable — the same pattern
 * the wrapped bash tool uses. Tools bind the entry captured at session
 * creation (snapshot semantics); a connection closed by a later reconcile
 * makes calls fail with a reopen hint.
 */

export type McpToolAudience = "main" | "child";

function matchesPattern(name: string, pattern: string) {
  const escaped = pattern.replace(/[.+?^${}()|[\]\\]/g, "\\$&").replace(/\*/g, ".*");
  return new RegExp(`^${escaped}$`).test(name);
}

export function isMcpToolVisible(config: McpServerEntry["config"], rawName: string, audience: McpToolAudience) {
  if (!(config.visibility ?? ["main", "child"]).includes(audience)) return false;
  if (config.excludeTools?.some((pattern) => matchesPattern(rawName, pattern))) return false;
  return !config.includeTools?.length || config.includeTools.some((pattern) => matchesPattern(rawName, pattern));
}

function textualChunks(result: McpCallResultLike) {
  const chunks: string[] = [];
  for (const part of result.content ?? []) {
    if (part.type === "text" && typeof part.text === "string") chunks.push(`${part.text}\n`);
    else if (part.type === "resource" && typeof part.resource?.text === "string") chunks.push(`${part.resource.text}\n`);
    else if (part.type === "resource_link") chunks.push(`[resource] ${part.name ?? ""} (${part.uri ?? ""})\n`);
  }
  return chunks;
}

export function buildMcpTools(entry: McpServerEntry, options: { audience?: McpToolAudience; outputStore?: ToolOutputStore } = {}): ToolDefinition[] {
  if (entry.state === "error") return [];
  const audience = options.audience ?? "main";
  const transportSummary = entry.config.transport === "stdio" ? `stdio: ${entry.config.command}` : `http: ${entry.config.url}`;
  const seen = new Set<string>();
  const tools: ToolDefinition[] = [];
  for (const tool of entry.handle.tools) {
    if (!isMcpToolVisible(entry.config, tool.name, audience)) continue;
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
        const result = await entry.handle.call(tool.name, params as Record<string, unknown>, signal) as McpCallResultLike;
        const mapped = mapCallResult(result, entry.config.name, tool.name);
        const chunks = textualChunks(result);
        if (!options.outputStore || !chunks.length) return mapped;
        const projected = await options.outputStore.project(
          `mcp-${entry.config.name}-${tool.name}`,
          chunks,
          `MCP ${entry.config.name}/${tool.name}: ${result.content?.length ?? 0} content part(s).`
        );
        if (!projected.truncation) return mapped;
        return {
          content: [
            { type: "text" as const, text: projected.text },
            ...mapped.content.filter((part) => part.type === "image")
          ],
          details: { ...mapped.details, artifactPath: projected.artifactPath, truncation: projected.truncation }
        };
      }
    });
  }
  return tools;
}
