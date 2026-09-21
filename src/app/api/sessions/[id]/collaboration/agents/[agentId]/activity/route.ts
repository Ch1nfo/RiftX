import { getCollaborationActivity } from "@/server/pi/session-manager";
import { BoardError } from "@/server/collaboration/store";
import { errorResponse } from "@/server/errors";
export const runtime = "nodejs";
export async function GET(_request: Request, context: { params: Promise<{ id: string; agentId: string }> }) {
  const { id, agentId } = await context.params;
  try { return Response.json(await getCollaborationActivity(id, agentId), { headers: { "Cache-Control": "no-store" } }); }
  catch (error) {
    if (error instanceof BoardError) return Response.json({ error: error.message, code: error.code }, { status: error.status });
    return errorResponse(error, "Unable to read agent activity");
  }
}
