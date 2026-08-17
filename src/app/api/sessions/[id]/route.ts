import { deleteArchivedSession } from "@/server/pi/session-manager";

export const runtime = "nodejs";

export async function DELETE(_request: Request, context: { params: Promise<{ id: string }> }) {
  const { id } = await context.params;
  try {
    return Response.json({ sessions: await deleteArchivedSession(id) });
  } catch (error) {
    return Response.json({ error: error instanceof Error ? error.message : "删除归档会话失败" }, { status: 404 });
  }
}
