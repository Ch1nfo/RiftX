import { summarizeSessionTitle } from "@/server/pi/session-manager";
import { badRequest, parseJsonBody, requiredText } from "@/lib/api-validation";
import { errorResponse } from "@/server/errors";

export const runtime = "nodejs";

export async function POST(request: Request, context: { params: Promise<{ id: string }> }) {
  const { id } = await context.params;
  const parsed = await parseJsonBody(request);
  if (parsed instanceof Response) return parsed;
  const body = parsed as { text?: unknown };
  const text = typeof body.text === "string" ? body.text.trim() : "";
  const textError = requiredText(text, "任务内容不能为空");
  if (textError) return badRequest(textError);
  try {
    return Response.json(await summarizeSessionTitle(id, text));
  } catch (error) {
    return errorResponse(error, "生成任务标题失败", 502);
  }
}
