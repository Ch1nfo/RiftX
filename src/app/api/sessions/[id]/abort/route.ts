import { abortSession } from "@/server/pi/session-manager";
import { errorMessage, errorStatus } from "@/server/errors";

export const runtime = "nodejs";

export async function POST(_request: Request, context: { params: Promise<{ id: string }> }) {
  const { id } = await context.params;
  try {
    await abortSession(id);
    return Response.json({ ok: true });
  } catch (error) {
    return Response.json({ error: errorMessage(error, "停止会话失败") }, { status: errorStatus(error, 500) });
  }
}
