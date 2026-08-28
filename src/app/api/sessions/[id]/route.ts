import { deleteArchivedSession, getSessionSnapshot } from "@/server/pi/session-manager";
import { errorResponse } from "@/server/errors";

export const runtime = "nodejs";

export async function GET(_request: Request, context: { params: Promise<{ id: string }> }) {
  const { id } = await context.params;
  try {
    return Response.json(await getSessionSnapshot(id));
  } catch (error) {
    return errorResponse(error, "读取会话失败");
  }
}

export async function DELETE(_request: Request, context: { params: Promise<{ id: string }> }) {
  const { id } = await context.params;
  try {
    return Response.json({ sessions: await deleteArchivedSession(id) });
  } catch (error) {
    return errorResponse(error, "删除归档会话失败");
  }
}
