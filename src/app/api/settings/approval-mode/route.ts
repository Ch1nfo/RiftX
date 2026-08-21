import type { ApprovalMode } from "@/lib/types";
import { setApprovalMode } from "@/server/pi/session-manager";
import { validateApprovalMode } from "@/lib/api-validation";
import { errorMessage, errorStatus } from "@/server/errors";

export const runtime = "nodejs";

export async function PUT(request: Request) {
  const body = (await request.json()) as { approvalMode?: ApprovalMode };
  if (!validateApprovalMode(body.approvalMode)) {
    return Response.json({ error: "invalid approval mode" }, { status: 400 });
  }
  try {
    const config = await setApprovalMode(body.approvalMode);
    return Response.json({ approvalMode: config.approvalMode });
  } catch (error) {
    return Response.json({ error: errorMessage(error, "更新审批模式失败") }, { status: errorStatus(error, 500) });
  }
}
