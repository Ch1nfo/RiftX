import { readConfig, updateConfig, updateProfiles } from "@/server/config-store";
import { SUBAGENT_AGGRESSIVENESS, type ModelProfile, type SubagentAggressiveness } from "@/lib/types";
import { setActiveProfile, setMaxConcurrentSubagents } from "@/server/pi/session-manager";

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
    maxConcurrentSubagents: number;
    subagentAggressiveness: SubagentAggressiveness;
  }>;
  const current = await readConfig();
  const incoming = Array.isArray(body.profiles) ? body.profiles : current.profiles;
  const requestedMax = Number(body.maxConcurrentSubagents);
  const maxConcurrentSubagents = body.maxConcurrentSubagents === undefined || !Number.isFinite(requestedMax)
    ? current.maxConcurrentSubagents
    : Math.min(8, Math.max(1, Math.round(requestedMax)));
  const subagentAggressiveness = SUBAGENT_AGGRESSIVENESS.includes(body.subagentAggressiveness as SubagentAggressiveness)
    ? body.subagentAggressiveness as SubagentAggressiveness
    : current.subagentAggressiveness;
  const profiles = incoming.map((profile) => ({
    ...profile,
    apiKey: profile.apiKey && profile.apiKey !== "••••••••" ? profile.apiKey : current.profiles.find((item) => item.id === profile.id)?.apiKey
  }));
  const config = await updateProfiles(profiles, body.activeProfileId ?? current.activeProfileId);
  const finalConfig = await updateConfig({
    childProfileId: body.childProfileId === undefined ? current.childProfileId : body.childProfileId,
    childInherit: body.childInherit === undefined ? current.childInherit : body.childInherit,
    cwd: body.cwd?.trim() || current.cwd,
    maxConcurrentSubagents,
    subagentAggressiveness
  });
  if (finalConfig.maxConcurrentSubagents !== current.maxConcurrentSubagents) await setMaxConcurrentSubagents(finalConfig.maxConcurrentSubagents);
  if (finalConfig.activeProfileId !== current.activeProfileId) await setActiveProfile(finalConfig.activeProfileId);
  return Response.json({ ...finalConfig, profiles: finalConfig.profiles.map(publicProfile) });
}
