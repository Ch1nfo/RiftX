import { retrySubagent } from "@/server/pi/session-manager";

export const runtime = "nodejs";

export async function POST(_request: Request, context: { params: Promise<{ id: string; taskId: string }> }) {
  const { id, taskId } = await context.params;
  try {
    return Response.json({ task: await retrySubagent(id, taskId) });
  } catch (error) {
    return Response.json({ error: error instanceof Error ? error.message : "Unable to retry subagent" }, { status: 400 });
  }
}
