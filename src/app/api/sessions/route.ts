import { createSession, listSessions } from "@/server/pi/session-manager";
import { errorResponse } from "@/server/errors";

export const runtime = "nodejs";

export async function GET() {
  try {
    // Archived sessions only: the active session list is served by
    // /api/bootstrap, so this endpoint exists for the settings page.
    const sessions = await listSessions();
    return Response.json(sessions.filter((session) => session.archived));
  } catch (error) {
    return errorResponse(error, "读取会话失败");
  }
}

export async function POST() {
  try {
    const session = await createSession();
    return Response.json(session, { status: 201 });
  } catch (error) {
    return errorResponse(error, "创建会话失败");
  }
}
