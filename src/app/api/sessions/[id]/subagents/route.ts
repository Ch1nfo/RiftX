import { listSubagents } from "@/server/pi/session-manager";

export const runtime = "nodejs";

export async function GET(_request: Request, context: { params: Promise<{ id: string }> }) {
  const { id } = await context.params;
  try {
    return Response.json(await listSubagents(id));
  } catch (error) {
    const message = error instanceof Error ? error.message : "读取子 Agent 失败";
    const status = message === "Session is archived" || message === "Session does not belong to the current working directory" ? 404 : 500;
    return Response.json({ error: message }, { status });
  }
}
