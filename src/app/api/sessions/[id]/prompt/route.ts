import { startPromptSession } from "@/server/pi/session-manager";
import { errorMessage, errorStatus } from "@/server/errors";
import { requiredText } from "@/lib/api-validation";

export const runtime = "nodejs";

export async function POST(request: Request, context: { params: Promise<{ id: string }> }) {
  try {
    const { id } = await context.params;
    const body = (await request.json()) as { text?: string; mode?: "prompt" | "steer" | "followUp" };
    const text = typeof body.text === "string" ? body.text.trim() : "";
    if (requiredText(text, "text is required")) return Response.json({ error: "text is required" }, { status: 400 });
    const session = await startPromptSession(id, text, body.mode ?? "prompt");
    return Response.json({ ok: true, sessionId: session.id });
  } catch (error) {
    return Response.json({ error: errorMessage(error, "发送任务失败") }, { status: errorStatus(error, 500) });
  }
}
