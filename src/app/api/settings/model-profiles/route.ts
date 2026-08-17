import { readConfig, updateConfig, updateProfiles } from "@/server/config-store";
import type { ModelProfile } from "@/lib/types";
import { setActiveProfile } from "@/server/pi/session-manager";

export const runtime = "nodejs";

function publicProfile(profile: ModelProfile) {
  const { apiKey, ...rest } = profile;
  return { ...rest, apiKey: apiKey ? "••••••••" : "" };
}

export async function GET() {
  const config = await readConfig();
  return Response.json({ ...config, profiles: config.profiles.map(publicProfile) });
}

export async function PUT(request: Request) {
  const body = (await request.json()) as Partial<{
    profiles: ModelProfile[];
    activeProfileId: string;
    childProfileId: string | null;
    childInherit: boolean;
    cwd: string;
  }>;
  const current = await readConfig();
  const incoming = Array.isArray(body.profiles) ? body.profiles : current.profiles;
  const profiles = incoming.map((profile) => ({
    ...profile,
    apiKey: profile.apiKey && profile.apiKey !== "••••••••" ? profile.apiKey : current.profiles.find((item) => item.id === profile.id)?.apiKey
  }));
  const config = await updateProfiles(profiles, body.activeProfileId ?? current.activeProfileId);
  const finalConfig = await updateConfig({
    childProfileId: body.childProfileId === undefined ? current.childProfileId : body.childProfileId,
    childInherit: body.childInherit === undefined ? current.childInherit : body.childInherit,
    cwd: body.cwd?.trim() || current.cwd
  });
  if (finalConfig.activeProfileId !== current.activeProfileId) await setActiveProfile(finalConfig.activeProfileId);
  return Response.json({ ...finalConfig, profiles: finalConfig.profiles.map(publicProfile) });
}
