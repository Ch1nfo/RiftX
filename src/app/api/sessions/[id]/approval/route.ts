import { decideApproval } from "@/server/pi/session-manager";
import { errorResponse } from "@/server/errors";
import { parseJsonBody } from "@/lib/api-validation";

export const runtime = "nodejs";

export async function POST(request: Request, context: { params: Promise<{ id: string }> }) {
  const { id } = await context.params;
  const parsed = await parseJsonBody(request);
  if (parsed instanceof Response) return parsed;
  const body = parsed as { approvalId?: string; approved?: boolean; scope?: "once" | "task" };
  if (!body.approvalId || typeof body.approved !== "boolean") return Response.json({ error: "approvalId and approved are required" }, { status: 400 });
  try {
    return Response.json({ ok: await decideApproval(id, body.approvalId, body.approved, body.scope ?? "once") });
  } catch (error) {
    return errorResponse(error, "处理审批失败");
  }
}
