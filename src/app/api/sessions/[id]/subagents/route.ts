import { listSubagents } from "@/server/pi/session-manager";
import { errorMessage, errorStatus } from "@/server/errors";

export const runtime = "nodejs";

export async function GET(_request: Request, context: { params: Promise<{ id: string }> }) {
  const { id } = await context.params;
  try {
    return Response.json(await listSubagents(id));
  } catch (error) {
    return Response.json({ error: errorMessage(error, "读取子 Agent 失败") }, { status: errorStatus(error, 500) });
  }
}
