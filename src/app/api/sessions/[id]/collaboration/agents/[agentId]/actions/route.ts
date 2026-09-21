import { collaborationResponse } from "@/server/collaboration/http";

export const runtime = "nodejs";

export async function POST(request: Request, context: { params: Promise<{ id: string; agentId: string; }> }) {
  const params = await context.params;
  return collaborationResponse(request, params.id, "agent", params.agentId);
}
