import { retrySubagent } from "@/server/pi/session-manager";
import { errorResponse } from "@/server/errors";

export const runtime = "nodejs";

export async function POST(_request: Request, context: { params: Promise<{ id: string; taskId: string }> }) {
  const { id, taskId } = await context.params;
  try {
    return Response.json({ task: await retrySubagent(id, taskId) });
  } catch (error) {
    return errorResponse(error, "Unable to retry subagent", 400);
  }
}
