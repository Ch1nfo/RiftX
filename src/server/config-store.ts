import { mkdir, rename, stat } from "node:fs/promises";
import { join, resolve } from "node:path";
import { homedir } from "node:os";
import { APPROVAL_MODES, DEFAULT_PROFILE, SUBAGENT_AGGRESSIVENESS, type AppConfig, type ModelProfile } from "@/lib/types";
import { readJsonStore, writeJsonStoreAtomic } from "@/server/json-store";

const ROOT = join(homedir(), ".riftx");
const CONFIG_PATH = join(ROOT, "config.json");
const SESSION_PATH = join(ROOT, "sessions");
const AGENT_PATH = join(ROOT, "agent");
const LEGACY_AGENT_PATH = join(ROOT, "pi-agent");
const SUBAGENT_PATH = join(ROOT, "subagents");
const EVIDENCE_PATH = join(ROOT, "evidence");
const SKILLS_PATH = join(ROOT, "skills");
let configWriteChain = Promise.resolve();

export function getLaunchDirectory() {
  return resolve(process.env.RIFTX_LAUNCH_CWD || process.cwd());
}

const defaultConfig = (): AppConfig => ({
  profiles: [DEFAULT_PROFILE],
  activeProfileId: DEFAULT_PROFILE.id,
  childProfileId: null,
  childInherit: true,
  cwd: getLaunchDirectory(),
  approvalMode: "request",
  archivedSessionIds: [],
  archivedSessions: [],
  sessionTitles: {},
  maxConcurrentSubagents: 3,
  subagentAggressiveness: "default",
  systemPromptEnabled: false,
  systemPrompt: "",
  browserScope: [],
  browserIgnoreTlsErrors: true
});

async function ensureAppDirs() {
  await mkdir(ROOT, { recursive: true, mode: 0o700 });
  await mkdir(SESSION_PATH, { recursive: true, mode: 0o700 });
  try {
    await stat(AGENT_PATH);
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error;
    await rename(LEGACY_AGENT_PATH, AGENT_PATH).catch((migrationError: NodeJS.ErrnoException) => {
      if (migrationError.code !== "ENOENT") throw migrationError;
    });
  }
  await mkdir(AGENT_PATH, { recursive: true, mode: 0o700 });
  await mkdir(SUBAGENT_PATH, { recursive: true, mode: 0o700 });
  await mkdir(EVIDENCE_PATH, { recursive: true, mode: 0o700 });
  await mkdir(SKILLS_PATH, { recursive: true, mode: 0o700 });
}

export function getAppPaths() {
  return { root: ROOT, config: CONFIG_PATH, sessions: SESSION_PATH, agent: AGENT_PATH, subagents: SUBAGENT_PATH, evidence: EVIDENCE_PATH, skills: SKILLS_PATH };
}

export async function readConfig(): Promise<AppConfig> {
  await ensureAppDirs();
  // readJsonStore treats ENOENT and unparsable files (backed up as
  // .corrupt-*) as no data; other I/O errors surface.
  const parsed = (await readJsonStore<Partial<AppConfig>>(CONFIG_PATH)) ?? {};
  const profiles = Array.isArray(parsed.profiles) && parsed.profiles.length ? parsed.profiles : [DEFAULT_PROFILE];
  const approvalMode = APPROVAL_MODES.includes(parsed.approvalMode as AppConfig["approvalMode"]) ? parsed.approvalMode as AppConfig["approvalMode"] : "request";
  const config: AppConfig = {
    ...defaultConfig(),
    ...parsed,
    profiles,
    activeProfileId: parsed.activeProfileId ?? profiles[0].id,
    approvalMode,
    archivedSessionIds: Array.isArray(parsed.archivedSessionIds) ? parsed.archivedSessionIds : [],
    archivedSessions: Array.isArray(parsed.archivedSessions) ? parsed.archivedSessions : [],
    sessionTitles: parsed.sessionTitles && typeof parsed.sessionTitles === "object"
      ? Object.fromEntries(Object.entries(parsed.sessionTitles).filter(([, title]) => typeof title === "string"))
      : {},
    maxConcurrentSubagents: Math.min(8, Math.max(1, Number.isFinite(parsed.maxConcurrentSubagents) ? Math.round(parsed.maxConcurrentSubagents as number) : 3)),
    subagentAggressiveness: SUBAGENT_AGGRESSIVENESS.includes(parsed.subagentAggressiveness as AppConfig["subagentAggressiveness"]) ? parsed.subagentAggressiveness as AppConfig["subagentAggressiveness"] : "default",
    systemPromptEnabled: parsed.systemPromptEnabled === true,
    systemPrompt: typeof parsed.systemPrompt === "string" ? parsed.systemPrompt : "",
    browserScope: Array.isArray(parsed.browserScope) ? parsed.browserScope.filter((rule): rule is string => typeof rule === "string" && Boolean(rule.trim())) : [],
    browserIgnoreTlsErrors: parsed.browserIgnoreTlsErrors !== false
  };
  if (parsed && (!Array.isArray(parsed.profiles) || !parsed.profiles.length)) await writeConfig(config);
  return config;
}

async function writeConfig(config: AppConfig) {
  await ensureAppDirs();
  await writeJsonStoreAtomic(CONFIG_PATH, config);
}

export async function updateConfig(patch: Partial<AppConfig>) {
  const result = configWriteChain.then(async () => {
    const next = { ...(await readConfig()), ...patch };
    await writeConfig(next);
    return next;
  });
  configWriteChain = result.then(() => undefined, () => undefined);
  return result;
}
