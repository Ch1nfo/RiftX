import { listSubagents } from "@/server/pi/session-manager";

export const runtime = "nodejs";

export async function GET(_request: Request, context: { params: Promise<{ id: string }> }) {
  const { id } = await context.params;
  return Response.json(await listSubagents(id));
}
