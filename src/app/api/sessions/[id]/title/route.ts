import { summarizeSessionTitle } from "@/server/pi/session-manager";
import { requiredText } from "@/lib/api-validation";

export const runtime = "nodejs";

export async function POST(request: Request, context: { params: Promise<{ id: string }> }) {
  const { id } = await context.params;
  const body = (await request.json()) as { text?: unknown };
  const text = typeof body.text === "string" ? body.text.trim() : "";
  if (requiredText(text, "任务内容不能为空")) return Response.json({ error: "任务内容不能为空" }, { status: 400 });
  try {
    return Response.json(await summarizeSessionTitle(id, text));
  } catch (error) {
    return Response.json({ error: error instanceof Error ? error.message : "生成任务标题失败" }, { status: 502 });
  }
}
