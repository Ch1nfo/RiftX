import { archiveSession, restoreArchivedSession } from "@/server/pi/session-manager";
import { errorResponse } from "@/server/errors";

export const runtime = "nodejs";

export async function POST(_request: Request, context: { params: Promise<{ id: string }> }) {
  const { id } = await context.params;
  try {
    return Response.json({ sessions: await archiveSession(id) });
  } catch (error) {
    return errorResponse(error, "归档会话失败");
  }
}

export async function DELETE(_request: Request, context: { params: Promise<{ id: string }> }) {
  const { id } = await context.params;
  try {
    return Response.json({ sessions: await restoreArchivedSession(id) });
  } catch (error) {
    return errorResponse(error, "恢复归档会话失败");
  }
}
