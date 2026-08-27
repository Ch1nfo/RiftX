import { readFile } from "node:fs/promises";
import { getAppPaths } from "@/server/config-store";
import { assertSessionInCurrentWorkspace } from "@/server/pi/session-manager";
import { getScreenshotPath } from "@/lib/evidence-path";

export const runtime = "nodejs";

export async function GET(_request: Request, context: { params: Promise<{ id: string; screenshotId: string }> }) {
  const { id, screenshotId } = await context.params;
  try {
    await assertSessionInCurrentWorkspace(id);
    const path = getScreenshotPath(getAppPaths().evidence, id, screenshotId);
    const blob = await readFile(path);
    return new Response(blob, { headers: { "Content-Type": "image/png", "Cache-Control": "no-store" } });
  } catch {
    return Response.json({ error: "Screenshot not found" }, { status: 404 });
  }
}
