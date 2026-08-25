import { startPromptSession } from "@/server/pi/session-manager";
import { errorMessage, errorStatus } from "@/server/errors";
import { isJsonObject, requiredText } from "@/lib/api-validation";

export const runtime = "nodejs";

export async function POST(request: Request, context: { params: Promise<{ id: string }> }) {
  try {
    const { id } = await context.params;
    let body: { text?: string; mode?: "prompt" | "steer" | "followUp" };
    try {
      const parsed = await request.json();
      if (!isJsonObject(parsed)) return Response.json({ error: "JSON body must be an object" }, { status: 400 });
      body = parsed as typeof body;
    } catch {
      return Response.json({ error: "Invalid JSON body" }, { status: 400 });
    }
    const text = typeof body.text === "string" ? body.text.trim() : "";
    if (requiredText(text, "text is required")) return Response.json({ error: "text is required" }, { status: 400 });
    const session = await startPromptSession(id, text, body.mode ?? "prompt");
    return Response.json({ ok: true, sessionId: session.id });
  } catch (error) {
    return Response.json({ error: errorMessage(error, "发送任务失败") }, { status: errorStatus(error, 500) });
  }
}
