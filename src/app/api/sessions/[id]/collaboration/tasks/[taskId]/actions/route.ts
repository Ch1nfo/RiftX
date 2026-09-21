import { collaborationResponse } from "@/server/collaboration/http";

export const runtime = "nodejs";

export async function POST(request: Request, context: { params: Promise<{ id: string; taskId: string; }> }) {
  const params = await context.params;
  return collaborationResponse(request, params.id, "task", params.taskId);
}
