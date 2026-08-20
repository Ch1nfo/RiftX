import { patchFinding } from "@/server/pi/session-manager";
import type { FindingConfidence } from "@/lib/types";

export const runtime = "nodejs";

const CONFIDENCES: FindingConfidence[] = ["confirmed", "likely", "suspected", "not_reproducible"];

export async function PATCH(request: Request, context: { params: Promise<{ id: string; findingId: string }> }) {
  const { id, findingId } = await context.params;
  const body = await request.json() as { confidence?: FindingConfidence; dismissed?: boolean };
  if (body.confidence !== undefined && !CONFIDENCES.includes(body.confidence)) return Response.json({ error: "Invalid finding confidence" }, { status: 400 });
  if (body.dismissed !== undefined && typeof body.dismissed !== "boolean") return Response.json({ error: "Invalid dismissed value" }, { status: 400 });
  try {
    const finding = await patchFinding(id, findingId, { confidence: body.confidence, dismissed: body.dismissed });
    if (!finding) return Response.json({ error: "Finding not found" }, { status: 404 });
    return Response.json({ finding });
  } catch (error) {
    return Response.json({ error: error instanceof Error ? error.message : "更新证据失败" }, { status: 404 });
  }
}
