import { abortSession } from "@/server/pi/session-manager";

export const runtime = "nodejs";

export async function POST(_request: Request, context: { params: Promise<{ id: string }> }) {
  const { id } = await context.params;
  await abortSession(id);
  return Response.json({ ok: true });
}
