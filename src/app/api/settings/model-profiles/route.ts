import { readConfig, updateConfig } from "@/server/config-store";
import { SUBAGENT_AGGRESSIVENESS, clampConcurrency, type McpServerConfig, type ModelProfile, type SubagentAggressiveness } from "@/lib/types";
import { setActiveProfile, setMaxConcurrentSubagents } from "@/server/pi/session-manager";
import { parseScopeRule } from "@/lib/scope-rules";
import { errorResponse } from "@/server/errors";
import { parseJsonBody } from "@/lib/api-validation";
import { MASKED_API_KEY, publicWebSearch, resolveProfileApiKey } from "@/server/profile-api-key";
import { mcpServersValidationError, normalizeMcpServers } from "@/server/mcp/config";

export const runtime = "nodejs";

function publicProfile(profile: ModelProfile) {
  const { apiKey, ...rest } = profile;
  return { ...rest, apiKey: apiKey ? MASKED_API_KEY : "" };
}

export async function GET() {
  const config = await readConfig();
  return Response.json({ ...config, profiles: config.profiles.map(publicProfile), webSearch: publicWebSearch(config.webSearch) });
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
  const parsed = await parseJsonBody(request);
  if (parsed instanceof Response) return parsed;
  const body = parsed as Partial<{
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
    webSearch?: { tavilyApiKey?: string };
    mcpServers: McpServerConfig[];
 }>;
  // Reject invalid scope rules up front: silently dropping them would leave
  // the manager with an empty rule set that behaves like "no scope configured".
  const invalidScopeRules = Array.isArray(body.browserScope)
    ? body.browserScope.filter((rule) => typeof rule !== "string" || !rule.trim() || !parseScopeRule(rule))
    : [];
  if (invalidScopeRules.length) return Response.json({ error: `Invalid browser scope rules: ${invalidScopeRules.join(", ")}` }, { status: 400 });
  const mcpError = mcpServersValidationError(body.mcpServers);
  if (mcpError) return Response.json({ error: mcpError }, { status: 400 });
  // Only validate profiles the request actually carries: stored ones were
  // validated on write, and re-reading config here would race the update.
  const incoming = Array.isArray(body.profiles) ? body.profiles : [];
  const invalidProfiles = incoming.map(profileValidationError).filter((error): error is string => Boolean(error));
  if (invalidProfiles.length) return Response.json({ error: `Invalid model profiles: ${invalidProfiles.join("; ")}` }, { status: 400 });
  const duplicateIds = incoming.map((profile) => profile.id).filter((id, index, all) => all.indexOf(id) !== index);
  if (duplicateIds.length) return Response.json({ error: `Duplicate profile ids: ${[...new Set(duplicateIds)].join(", ")}` }, { status: 400 });
  const requestedMax = Number(body.maxConcurrentSubagents);
  // Persist FIRST in a single atomic write, then switch the live session. If
  // the disk write fails, the live session was never touched; if the switch
  // then fails, only the stored default changed (a recoverable state) and the
  // caller gets an explicit error — a failed request can never leave the UI,
  // config, and live session in three different states.
  let finalConfig;
  try {
    finalConfig = await updateConfig((latest) => {
      const latestIncoming = Array.isArray(body.profiles) ? body.profiles : latest.profiles;
      const profiles = latestIncoming.map((profile) => {
        const apiKey = resolveProfileApiKey(profile, latest.profiles.find((item) => item.id === profile.id));
        return apiKey === profile.apiKey ? profile : { ...profile, apiKey };
      });
      // A per-session switch (sessionId present) must not rewrite the global
      // default — only the settings UI (no sessionId) changes what new
      // sessions start on.
      const targetProfileId = body.activeProfileId ?? latest.activeProfileId;
      const activeProfileId = body.sessionId ? latest.activeProfileId : targetProfileId;
      if (!profiles.some((profile) => profile.id === targetProfileId)) throw new Error("Model profile not found");
      const latestMax = body.maxConcurrentSubagents === undefined || !Number.isFinite(requestedMax)
        ? latest.maxConcurrentSubagents
        : clampConcurrency(requestedMax);
      return {
        // undefined or a masked echo keeps the stored key; an empty string
        // clears it (falling back to the keyless default provider).
        webSearch: {
          tavilyApiKey: body.webSearch?.tavilyApiKey === undefined || body.webSearch.tavilyApiKey === MASKED_API_KEY
            ? (latest.webSearch?.tavilyApiKey ?? "")
            : body.webSearch.tavilyApiKey
        },
        profiles,
        activeProfileId,
        childProfileId: body.childProfileId === undefined ? latest.childProfileId : body.childProfileId,
        childInherit: body.childInherit === undefined ? latest.childInherit : body.childInherit,
        maxConcurrentSubagents: latestMax,
        subagentAggressiveness: SUBAGENT_AGGRESSIVENESS.includes(body.subagentAggressiveness as SubagentAggressiveness) ? body.subagentAggressiveness as SubagentAggressiveness : latest.subagentAggressiveness,
        systemPromptEnabled: body.systemPromptEnabled === undefined ? latest.systemPromptEnabled : body.systemPromptEnabled === true,
        systemPrompt: typeof body.systemPrompt === "string" ? body.systemPrompt : latest.systemPrompt,
        browserScope: Array.isArray(body.browserScope) ? body.browserScope.filter((rule): rule is string => typeof rule === "string" && Boolean(rule.trim())) : latest.browserScope,
        browserIgnoreTlsErrors: body.browserIgnoreTlsErrors === undefined ? latest.browserIgnoreTlsErrors : body.browserIgnoreTlsErrors === true,
        // Canonicalize on persist so a pasted ecosystem entry ("type" key) is
        // stored in RiftX's own "transport" shape.
        mcpServers: body.mcpServers === undefined ? latest.mcpServers : normalizeMcpServers(body.mcpServers)
      };
    });
  } catch (error) {
    if (error instanceof Error && error.message === "Model profile not found") return Response.json({ error: error.message }, { status: 400 });
    throw error;
  }
  // Switching is an explicit per-session operation (composer sends sessionId)
  // and is decided against the target session's current profile, so a request
  // can never report success without actually switching.
  if (typeof body.sessionId === "string" && body.sessionId) {
    try {
      const activeProfile = finalConfig.profiles.find((profile) => profile.id === (body.activeProfileId ?? finalConfig.activeProfileId));
      if (!activeProfile) throw new Error("Model profile not found");
      await setActiveProfile(activeProfile, body.sessionId);
    } catch (error) {
      return errorResponse(error, "Model switch failed", 400);
    }
  }
  // Apply the limit whenever the request explicitly carries it — comparing
  // against a pre-write snapshot would race a concurrent config write.
  if (body.maxConcurrentSubagents !== undefined && Number.isFinite(requestedMax)) await setMaxConcurrentSubagents(finalConfig.maxConcurrentSubagents);
  return Response.json({ ...finalConfig, profiles: finalConfig.profiles.map(publicProfile), webSearch: publicWebSearch(finalConfig.webSearch) });
}
