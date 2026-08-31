import { listRunningSessionIds } from "@/server/pi/session-manager";
import { errorResponse } from "@/server/errors";

export const runtime = "nodejs";

export async function GET() {
  try {
    return Response.json({ runningSessionIds: await listRunningSessionIds() });
  } catch (error) {
    return errorResponse(error, "读取会话状态失败");
  }
}
