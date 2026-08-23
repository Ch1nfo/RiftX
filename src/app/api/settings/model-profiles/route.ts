import { readConfig, updateConfig, updateProfiles } from "@/server/config-store";
import { SUBAGENT_AGGRESSIVENESS, type ModelProfile, type SubagentAggressiveness } from "@/lib/types";
import { setActiveProfile, setMaxConcurrentSubagents } from "@/server/pi/session-manager";
import { parseScopeRule } from "@/browser/scope/scope-rules";

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
    maxConcurrentSubagents: number;
    subagentAggressiveness: SubagentAggressiveness;
    systemPromptEnabled: boolean;
    systemPrompt: string;
    browserScope: string[];
    browserIgnoreTlsErrors: boolean;
  }>;
  const current = await readConfig();
  // Reject invalid scope rules up front: silently dropping them would leave
  // the manager with an empty rule set that behaves like "no scope configured".
  const invalidScopeRules = Array.isArray(body.browserScope)
    ? body.browserScope.filter((rule) => typeof rule !== "string" || !rule.trim() || !parseScopeRule(rule))
    : [];
  if (invalidScopeRules.length) return Response.json({ error: `Invalid browser scope rules: ${invalidScopeRules.join(", ")}` }, { status: 400 });
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
  const activeProfileId = body.activeProfileId ?? current.activeProfileId;
  const activeProfile = profiles.find((profile) => profile.id === activeProfileId);
  if (!activeProfile) return Response.json({ error: "Model profile not found" }, { status: 400 });
  const currentActiveProfile = current.profiles.find((profile) => profile.id === current.activeProfileId);
  if (activeProfileId !== current.activeProfileId || JSON.stringify(activeProfile) !== JSON.stringify(currentActiveProfile)) {
    try {
      await setActiveProfile(activeProfile);
    } catch (error) {
      return Response.json({ error: error instanceof Error ? error.message : "Model switch failed" }, { status: 400 });
    }
  }
  const config = await updateProfiles(profiles, activeProfileId);
  const finalConfig = await updateConfig({
    childProfileId: body.childProfileId === undefined ? current.childProfileId : body.childProfileId,
    childInherit: body.childInherit === undefined ? current.childInherit : body.childInherit,
    maxConcurrentSubagents,
    subagentAggressiveness,
    systemPromptEnabled: body.systemPromptEnabled === undefined ? current.systemPromptEnabled : body.systemPromptEnabled === true,
    systemPrompt: typeof body.systemPrompt === "string" ? body.systemPrompt : current.systemPrompt,
    browserScope: Array.isArray(body.browserScope) ? body.browserScope.filter((rule): rule is string => typeof rule === "string" && Boolean(rule.trim())) : current.browserScope,
    browserIgnoreTlsErrors: body.browserIgnoreTlsErrors === undefined ? current.browserIgnoreTlsErrors : body.browserIgnoreTlsErrors === true
  });
  if (finalConfig.maxConcurrentSubagents !== current.maxConcurrentSubagents) await setMaxConcurrentSubagents(finalConfig.maxConcurrentSubagents);
  return Response.json({ ...finalConfig, profiles: finalConfig.profiles.map(publicProfile) });
}
