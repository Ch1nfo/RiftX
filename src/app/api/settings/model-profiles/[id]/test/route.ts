import { readConfig } from "@/server/config-store";
import { createSessionForProfile } from "@/server/pi/session-manager";

export const runtime = "nodejs";

export async function POST(_request: Request, context: { params: Promise<{ id: string }> }) {
  const { id } = await context.params;
  const config = await readConfig();
  const profile = config.profiles.find((item) => item.id === id);
  if (!profile) return Response.json({ error: "profile not found" }, { status: 404 });
  try {
    const session = await createSessionForProfile(id);
    session.dispose();
    return Response.json({ ok: true, model: `${profile.provider}/${profile.model}` });
  } catch (error) {
    return Response.json({ ok: false, error: error instanceof Error ? error.message : "model test failed" }, { status: 400 });
  }
}
