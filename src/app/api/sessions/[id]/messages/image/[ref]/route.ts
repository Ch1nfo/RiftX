import { getTranscriptImage } from "@/server/pi/session-manager";
import { errorResponse } from "@/server/errors";

export const runtime = "nodejs";

/** Serves one transcript image by content-hash reference, on demand. */
export async function GET(_request: Request, context: { params: Promise<{ id: string; ref: string }> }) {
  try {
    const { id, ref } = await context.params;
    const image = await getTranscriptImage(id, ref);
    if (!image) return Response.json({ error: "Image not found" }, { status: 404 });
    return new Response(new Uint8Array(image.bytes), {
      headers: { "Content-Type": image.mimeType, "Cache-Control": "private, max-age=86400, immutable" }
    });
  } catch (error) {
    return errorResponse(error, "读取图片失败");
  }
}
