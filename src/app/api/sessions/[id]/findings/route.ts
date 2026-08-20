import { listFindings } from "@/server/pi/session-manager";

export const runtime = "nodejs";

export async function GET(_request: Request, context: { params: Promise<{ id: string }> }) {
  const { id } = await context.params;
  try {
    return Response.json({ findings: await listFindings(id) });
  } catch (error) {
    return Response.json({ error: error instanceof Error ? error.message : "读取证据失败" }, { status: 404 });
  }
}
