import { failedPromptRequestIds, getSessionMessages, promptRequestStates } from "@/server/pi/session-manager";
import { errorResponse } from "@/server/errors";

export const runtime = "nodejs";

export async function GET(_request: Request, context: { params: Promise<{ id: string }> }) {
  const { id } = await context.params;
  try {
    // The lifecycle block lets a reconnecting client recover attachments for
    // sends that failed while it was viewing another session (missed SSE).
    return Response.json({
      messages: await getSessionMessages(id),
      promptRequestStates: promptRequestStates(id),
      // Kept for compatibility with clients from the first attachment build.
      failedRequestIds: failedPromptRequestIds(id)
    });
  } catch (error) {
    return errorResponse(error, "读取消息失败");
  }
}
