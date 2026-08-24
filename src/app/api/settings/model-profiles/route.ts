import { readConfig, updateConfig } from "@/server/config-store";
import { SUBAGENT_AGGRESSIVENESS, type ModelProfile, type SubagentAggressiveness } from "@/lib/types";
import { setActiveProfile, setMaxConcurrentSubagents } from "@/server/pi/session-manager";
import { parseScopeRule } from "@/browser/scope/scope-rules";
import { errorStatus } from "@/server/errors";

export const runtime = "nodejs";

function publicProfile(profile: ModelProfile) {
  const { apiKey, ...rest } = profile;
  return { ...rest, apiKey: apiKey ? "••••••••" : "" };
}

export async function GET() {
  const config = await readConfig();
  return Response.json({ ...config, profiles: config.profiles.map(publicProfile) });
}

const API_TYPES_LIST = ["openai-completions", "openai-responses", "anthropic-messages", "google-generative-ai"] as const;
const TRANSPORTS_LIST = ["auto", "sse", "websocket"] as const;
const THINKING_LEVELS = ["off", "minimal", "low", "medium", "high", "xhigh"] as const;

/** Validate profile input at the API boundary: bad values must not reach config storage. */
function profileValidationError(profile: Partial<ModelProfile>): string | null {
  if (!profile || typeof profile !== "object") return "profiles must be objects";
  for (const field of ["id", "name", "provider", "model", "baseUrl"] as const) {
    if (typeof profile[field] !== "string" || !profile[field]!.trim()) return `${field} is required`;
  }
  if (!(API_TYPES_LIST as readonly string[]).includes(String(profile.api))) return `api must be one of ${API_TYPES_LIST.join(", ")}`;
  if (!(TRANSPORTS_LIST as readonly string[]).includes(String(profile.transport))) return `transport must be one of ${TRANSPORTS_LIST.join(", ")}`;
  if (!(THINKING_LEVELS as readonly string[]).includes(String(profile.thinkingLevel))) return `thinkingLevel must be one of ${THINKING_LEVELS.join(", ")}`;
  try {
    const parsed = new URL(profile.baseUrl!);
    if ((parsed.protocol !== "http:" && parsed.protocol !== "https:") || !parsed.hostname) throw new Error("bad url");
  } catch {
    return "baseUrl must be a valid http(s) URL";
  }
  for (const field of ["contextWindow", "maxTokens"] as const) {
    const value = profile[field];
    if (typeof value !== "number" || !Number.isFinite(value) || value < 1) return `${field} must be a finite positive number`;
  }
  if (profile.apiKey !== undefined && typeof profile.apiKey !== "string") return "apiKey must be a string";
  if (profile.supportsImages !== undefined && typeof profile.supportsImages !== "boolean") return "supportsImages must be a boolean";
  return null;
}

export async function PUT(request: Request) {
  const body = (await request.json()) as Partial<{
    profiles: ModelProfile[];
    activeProfileId: string;
    sessionId: string;
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
  const invalidProfiles = incoming.map(profileValidationError).filter((error): error is string => Boolean(error));
  if (invalidProfiles.length) return Response.json({ error: `Invalid model profiles: ${invalidProfiles.join("; ")}` }, { status: 400 });
  const duplicateIds = incoming.map((profile) => profile.id).filter((id, index, all) => all.indexOf(id) !== index);
  if (duplicateIds.length) return Response.json({ error: `Duplicate profile ids: ${[...new Set(duplicateIds)].join(", ")}` }, { status: 400 });
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
  // Persist FIRST in a single atomic write, then switch the live session. If
  // the disk write fails, the live session was never touched; if the switch
  // then fails, only the stored default changed (a recoverable state) and the
  // caller gets an explicit error — a failed request can never leave the UI,
  // config, and live session in three different states.
  const finalConfig = await updateConfig({
    profiles,
    activeProfileId,
    childProfileId: body.childProfileId === undefined ? current.childProfileId : body.childProfileId,
    childInherit: body.childInherit === undefined ? current.childInherit : body.childInherit,
    maxConcurrentSubagents,
    subagentAggressiveness,
    systemPromptEnabled: body.systemPromptEnabled === undefined ? current.systemPromptEnabled : body.systemPromptEnabled === true,
    systemPrompt: typeof body.systemPrompt === "string" ? body.systemPrompt : current.systemPrompt,
    browserScope: Array.isArray(body.browserScope) ? body.browserScope.filter((rule): rule is string => typeof rule === "string" && Boolean(rule.trim())) : current.browserScope,
    browserIgnoreTlsErrors: body.browserIgnoreTlsErrors === undefined ? current.browserIgnoreTlsErrors : body.browserIgnoreTlsErrors === true
  });
  // Switching is an explicit per-session operation (composer sends sessionId)
  // and is decided against the target session's current profile, so a request
  // can never report success without actually switching.
  if (typeof body.sessionId === "string" && body.sessionId) {
    try {
      await setActiveProfile(activeProfile, body.sessionId);
    } catch (error) {
      return Response.json({ error: error instanceof Error ? error.message : "Model switch failed" }, { status: errorStatus(error, 400) });
    }
  }
  if (finalConfig.maxConcurrentSubagents !== current.maxConcurrentSubagents) await setMaxConcurrentSubagents(finalConfig.maxConcurrentSubagents);
  return Response.json({ ...finalConfig, profiles: finalConfig.profiles.map(publicProfile) });
}
