import { patchFinding } from "@/server/pi/session-manager";
import type { FindingConfidence } from "@/lib/types";
import { errorMessage, errorStatus } from "@/server/errors";
import { parseJsonBody, validateDismissed, validateFindingConfidence } from "@/lib/api-validation";

export const runtime = "nodejs";

export async function PATCH(request: Request, context: { params: Promise<{ id: string; findingId: string }> }) {
  const { id, findingId } = await context.params;
  const parsed = await parseJsonBody(request);
  if (parsed instanceof Response) return parsed;
  const body = parsed as { confidence?: FindingConfidence; dismissed?: boolean };
  if (!validateFindingConfidence(body.confidence)) return Response.json({ error: "Invalid finding confidence" }, { status: 400 });
  if (!validateDismissed(body.dismissed)) return Response.json({ error: "Invalid dismissed value" }, { status: 400 });
  try {
    const finding = await patchFinding(id, findingId, { confidence: body.confidence, dismissed: body.dismissed });
    if (!finding) return Response.json({ error: "Finding not found" }, { status: 404 });
    return Response.json({ finding });
  } catch (error) {
    return Response.json({ error: errorMessage(error, "更新证据失败") }, { status: errorStatus(error, 500) });
  }
}
