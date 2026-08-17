import { readConfig } from "@/server/config-store";
import { APPROVAL_MODES, type ApprovalMode } from "@/lib/types";
import { setApprovalMode } from "@/server/pi/session-manager";

export const runtime = "nodejs";

export async function GET() {
  const config = await readConfig();
  return Response.json({ approvalMode: config.approvalMode });
}

export async function PUT(request: Request) {
  const body = (await request.json()) as { approvalMode?: ApprovalMode };
  if (!body.approvalMode || !APPROVAL_MODES.includes(body.approvalMode)) {
    return Response.json({ error: "invalid approval mode" }, { status: 400 });
  }
  const config = await setApprovalMode(body.approvalMode);
  return Response.json({ approvalMode: config.approvalMode });
}
