import { cancelSubagent } from "@/server/pi/session-manager";

export const runtime = "nodejs";

export async function POST(_request: Request, context: { params: Promise<{ id: string; taskId: string }> }) {
  const { id, taskId } = await context.params;
  return Response.json({ ok: await cancelSubagent(id, taskId) });
}
