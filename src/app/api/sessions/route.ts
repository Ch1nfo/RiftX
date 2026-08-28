import { createSession, listSessions } from "@/server/pi/session-manager";
import { errorResponse } from "@/server/errors";

export const runtime = "nodejs";

export async function GET(request: Request) {
  try {
    const sessions = await listSessions();
    const archivedOnly = new URL(request.url).searchParams.get("archived") === "true";
    return Response.json(sessions.filter((session) => archivedOnly ? session.archived : !session.archived));
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
