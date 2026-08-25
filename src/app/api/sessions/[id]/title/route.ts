import { summarizeSessionTitle } from "@/server/pi/session-manager";
import { isJsonObject, requiredText } from "@/lib/api-validation";
import { errorMessage, errorStatus } from "@/server/errors";

export const runtime = "nodejs";

export async function POST(request: Request, context: { params: Promise<{ id: string }> }) {
  const { id } = await context.params;
  let body: { text?: unknown };
  try {
    const parsed = await request.json();
    if (!isJsonObject(parsed)) return Response.json({ error: "JSON body must be an object" }, { status: 400 });
    body = parsed as typeof body;
  } catch {
    return Response.json({ error: "Invalid JSON body" }, { status: 400 });
  }
  const text = typeof body.text === "string" ? body.text.trim() : "";
  if (requiredText(text, "任务内容不能为空")) return Response.json({ error: "任务内容不能为空" }, { status: 400 });
  try {
    return Response.json(await summarizeSessionTitle(id, text));
  } catch (error) {
    return Response.json({ error: errorMessage(error, "生成任务标题失败") }, { status: errorStatus(error, 502) });
  }
}
