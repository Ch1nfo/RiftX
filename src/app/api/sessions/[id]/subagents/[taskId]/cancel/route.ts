import { cancelSubagent } from "@/server/pi/session-manager";
import { errorMessage, errorStatus } from "@/server/errors";

export const runtime = "nodejs";

export async function POST(_request: Request, context: { params: Promise<{ id: string; taskId: string }> }) {
  const { id, taskId } = await context.params;
  try {
    return Response.json({ ok: await cancelSubagent(id, taskId) });
  } catch (error) {
    return Response.json({ error: errorMessage(error, "取消子 Agent 失败") }, { status: errorStatus(error, 500) });
  }
}
