import { getSessionMessages } from "@/server/pi/session-manager";
import { errorResponse } from "@/server/errors";

export const runtime = "nodejs";

export async function GET(_request: Request, context: { params: Promise<{ id: string }> }) {
  const { id } = await context.params;
  try {
    return Response.json(await getSessionMessages(id));
  } catch (error) {
    return errorResponse(error, "读取消息失败");
  }
}
