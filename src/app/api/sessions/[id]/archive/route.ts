import { archiveSession } from "@/server/pi/session-manager";
import { errorMessage, errorStatus } from "@/server/errors";

export const runtime = "nodejs";

export async function POST(_request: Request, context: { params: Promise<{ id: string }> }) {
  const { id } = await context.params;
  try {
    return Response.json({ sessions: await archiveSession(id) });
  } catch (error) {
    return Response.json({ error: errorMessage(error, "归档会话失败") }, { status: errorStatus(error, 500) });
  }
}
