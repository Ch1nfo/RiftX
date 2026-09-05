import { startPromptSession } from "@/server/pi/session-manager";
import { errorResponse } from "@/server/errors";
import { badRequest, parseJsonBody, requiredText } from "@/lib/api-validation";
import { isPromptMode } from "@/lib/prompt-mode";
import { promptAttachmentsError, promptImagesError, type PromptAttachment, type PromptImage } from "@/lib/attachments";

export const runtime = "nodejs";

export async function POST(request: Request, context: { params: Promise<{ id: string }> }) {
  try {
    const { id } = await context.params;
    const parsed = await parseJsonBody(request);
    if (parsed instanceof Response) return parsed;
    const body = parsed as { text?: string; mode?: "prompt" | "steer" | "followUp"; images?: unknown; attachments?: unknown; requestId?: unknown };
    const requestId = typeof body.requestId === "string" && body.requestId.length <= 100 ? body.requestId : undefined;
    const text = typeof body.text === "string" ? body.text.trim() : "";
    const textError = requiredText(text, "text is required");
    if (textError) return badRequest(textError);
    const mode = body.mode ?? "prompt";
    // An unrecognized mode must be rejected here: passing it through would
    // bypass the prompt serialization gate and break the running turn with a
    // raw SDK "already processing" rejection.
    if (!isPromptMode(mode)) return Response.json({ error: "mode must be one of prompt, steer, followUp" }, { status: 400 });
    const imageError = promptImagesError(body.images);
    if (imageError) return badRequest(`Invalid images: ${imageError}`);
    const attachmentError = promptAttachmentsError(body.attachments);
    if (attachmentError) return badRequest(`Invalid attachments: ${attachmentError}`);
    const { record, composedText, requestState } = await startPromptSession(id, text, mode, {
      images: body.images === undefined ? undefined : body.images as PromptImage[],
      attachments: body.attachments === undefined ? undefined : body.attachments as PromptAttachment[],
      requestId
    });
    return Response.json({ ok: true, sessionId: record.id, composedText, requestState });
  } catch (error) {
    return errorResponse(error, "发送任务失败");
  }
}
