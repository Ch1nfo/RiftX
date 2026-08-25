import { decideApproval } from "@/server/pi/session-manager";
import { errorMessage, errorStatus } from "@/server/errors";
import { isJsonObject } from "@/lib/api-validation";

export const runtime = "nodejs";

export async function POST(request: Request, context: { params: Promise<{ id: string }> }) {
  const { id } = await context.params;
  let body: { approvalId?: string; approved?: boolean; scope?: "once" | "task" };
  try {
    const parsed = await request.json();
    if (!isJsonObject(parsed)) return Response.json({ error: "JSON body must be an object" }, { status: 400 });
    body = parsed as typeof body;
  } catch {
    return Response.json({ error: "Invalid JSON body" }, { status: 400 });
  }
  if (!body.approvalId || typeof body.approved !== "boolean") return Response.json({ error: "approvalId and approved are required" }, { status: 400 });
  try {
    return Response.json({ ok: await decideApproval(id, body.approvalId, body.approved, body.scope ?? "once") });
  } catch (error) {
    return Response.json({ error: errorMessage(error, "处理审批失败") }, { status: errorStatus(error, 500) });
  }
}
