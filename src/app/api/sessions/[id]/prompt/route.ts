import { startPromptSession } from "@/server/pi/session-manager";

export const runtime = "nodejs";

export async function POST(request: Request, context: { params: Promise<{ id: string }> }) {
  try {
    const { id } = await context.params;
    const body = (await request.json()) as { text?: string; mode?: "prompt" | "steer" | "followUp" };
    if (!body.text?.trim()) return Response.json({ error: "text is required" }, { status: 400 });
    const session = await startPromptSession(id, body.text.trim(), body.mode ?? "prompt");
    return Response.json({ ok: true, sessionId: session.id });
  } catch (error) {
    const message = error instanceof Error ? error.message : "发送任务失败";
    const status = message === "Session does not belong to the current working directory" || message === "session not found" ? 404 : 500;
    return Response.json({ error: message }, { status });
  }
}
