import { startPromptSession } from "@/server/pi/session-manager";
import { errorMessage, errorStatus } from "@/server/errors";
import { parseJsonBody, requiredText } from "@/lib/api-validation";
import { isPromptMode } from "@/lib/prompt-mode";

export const runtime = "nodejs";

export async function POST(request: Request, context: { params: Promise<{ id: string }> }) {
  try {
    const { id } = await context.params;
    const parsed = await parseJsonBody(request);
    if (parsed instanceof Response) return parsed;
    const body = parsed as { text?: string; mode?: "prompt" | "steer" | "followUp" };
    const text = typeof body.text === "string" ? body.text.trim() : "";
    if (requiredText(text, "text is required")) return Response.json({ error: "text is required" }, { status: 400 });
    const mode = body.mode ?? "prompt";
    // An unrecognized mode must be rejected here: passing it through would
    // bypass the prompt serialization gate and break the running turn with a
    // raw SDK "already processing" rejection.
    if (!isPromptMode(mode)) return Response.json({ error: "mode must be one of prompt, steer, followUp" }, { status: 400 });
    const session = await startPromptSession(id, text, mode);
    return Response.json({ ok: true, sessionId: session.id });
  } catch (error) {
    return Response.json({ error: errorMessage(error, "发送任务失败") }, { status: errorStatus(error, 500) });
  }
}
