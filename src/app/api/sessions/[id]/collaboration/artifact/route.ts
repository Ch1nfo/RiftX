import { readFile, realpath } from "node:fs/promises";
import { basename, isAbsolute, relative } from "node:path";
import { getCollaboration } from "@/server/pi/session-manager";
import { getAppPaths } from "@/server/config-store";
import { listToolArtifacts, toolArtifactDir } from "@/server/tool-output";
export const runtime = "nodejs";
export async function GET(request: Request, context: { params: Promise<{ id: string }> }) {
  try {
    const { id } = await context.params;
    if (!await getCollaboration(id)) return Response.json({ error: "Legacy session" }, { status: 404 });
    const path = new URL(request.url).searchParams.get("path");
    const paths = getAppPaths();
    const artifact = (await listToolArtifacts(paths.artifacts, id, 10000)).find((item) => item.path === path);
    if (!artifact) return Response.json({ error: "Artifact not found" }, { status: 404 });
    const real = await realpath(artifact.path);
    const rel = relative(await realpath(toolArtifactDir(paths.artifacts, id)), real);
    if (rel.startsWith("..") || isAbsolute(rel)) return Response.json({ error: "Artifact outside session" }, { status: 403 });
    return new Response(await readFile(real, "utf8"), { headers: { "Content-Type": "text/plain; charset=utf-8", "X-Content-Type-Options": "nosniff", "Cache-Control": "no-store", "Content-Disposition": `attachment; filename="${basename(real).replaceAll('"', '')}"` } });
  } catch { return Response.json({ error: "Artifact unavailable" }, { status: 404 }); }
}
