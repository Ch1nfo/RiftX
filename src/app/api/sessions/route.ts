import { createSession, listSessions } from "@/server/pi/session-manager";
import { errorMessage, errorStatus } from "@/server/errors";

export const runtime = "nodejs";

export async function GET(request: Request) {
  try {
    const sessions = await listSessions();
    const archivedOnly = new URL(request.url).searchParams.get("archived") === "true";
    return Response.json(sessions.filter((session) => archivedOnly ? session.archived : !session.archived));
  } catch (error) {
    return Response.json({ error: errorMessage(error, "读取会话失败") }, { status: errorStatus(error, 500) });
  }
}

export async function POST() {
  try {
    const session = await createSession();
    return Response.json({ id: session.id }, { status: 201 });
  } catch (error) {
    return Response.json({ error: errorMessage(error, "创建会话失败") }, { status: errorStatus(error, 500) });
  }
}
