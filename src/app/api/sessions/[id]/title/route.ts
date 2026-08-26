import { summarizeSessionTitle } from "@/server/pi/session-manager";
import { parseJsonBody, requiredText } from "@/lib/api-validation";
import { errorMessage, errorStatus } from "@/server/errors";

export const runtime = "nodejs";

export async function POST(request: Request, context: { params: Promise<{ id: string }> }) {
  const { id } = await context.params;
  const parsed = await parseJsonBody(request);
  if (parsed instanceof Response) return parsed;
  const body = parsed as { text?: unknown };
  const text = typeof body.text === "string" ? body.text.trim() : "";
  if (requiredText(text, "任务内容不能为空")) return Response.json({ error: "任务内容不能为空" }, { status: 400 });
  try {
    return Response.json(await summarizeSessionTitle(id, text));
  } catch (error) {
    return Response.json({ error: errorMessage(error, "生成任务标题失败") }, { status: errorStatus(error, 502) });
  }
}
