import { startPromptSession } from "@/server/pi/session-manager";

export const runtime = "nodejs";

export async function POST(request: Request, context: { params: Promise<{ id: string }> }) {
  const { id } = await context.params;
  const body = (await request.json()) as { text?: string; mode?: "prompt" | "steer" | "followUp" };
  if (!body.text?.trim()) return Response.json({ error: "text is required" }, { status: 400 });
  const session = await startPromptSession(id, body.text.trim(), body.mode ?? "prompt");
  return Response.json({ ok: true, sessionId: session.id });
}
