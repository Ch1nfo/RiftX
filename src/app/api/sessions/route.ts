import { createSession, listSessions } from "@/server/pi/session-manager";

export const runtime = "nodejs";

export async function GET(request: Request) {
  const sessions = await listSessions();
  const archivedOnly = new URL(request.url).searchParams.get("archived") === "true";
  return Response.json(sessions.filter((session) => archivedOnly ? session.archived : !session.archived));
}

export async function POST() {
  const session = await createSession();
  return Response.json({ id: session.id }, { status: 201 });
}
