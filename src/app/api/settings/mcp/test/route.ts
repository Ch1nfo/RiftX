import { mcpServersValidationError } from "@/server/mcp/config";
import { testMcpServers } from "@/server/mcp/manager";
import type { McpServerConfig } from "@/lib/types";
import { parseJsonBody } from "@/lib/api-validation";

export const runtime = "nodejs";

/** Probe a draft MCP server list without persisting anything. */
export async function POST(request: Request) {
  const parsed = await parseJsonBody(request);
  if (parsed instanceof Response) return parsed;
  const body = parsed as { mcpServers?: unknown };
  const error = mcpServersValidationError(body.mcpServers);
  if (error) return Response.json({ error }, { status: 400 });
  const results = await testMcpServers(Array.isArray(body.mcpServers) ? body.mcpServers as McpServerConfig[] : []);
  return Response.json({ results });
}
