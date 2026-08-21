import { decideApproval } from "@/server/pi/session-manager";
import { errorMessage, errorStatus } from "@/server/errors";

export const runtime = "nodejs";

export async function POST(request: Request, context: { params: Promise<{ id: string }> }) {
  const { id } = await context.params;
  const body = (await request.json()) as { approvalId?: string; approved?: boolean; scope?: "once" | "task" };
  if (!body.approvalId || typeof body.approved !== "boolean") return Response.json({ error: "approvalId and approved are required" }, { status: 400 });
  try {
    return Response.json({ ok: await decideApproval(id, body.approvalId, body.approved, body.scope ?? "once") });
  } catch (error) {
    return Response.json({ error: errorMessage(error, "处理审批失败") }, { status: errorStatus(error, 500) });
  }
}
