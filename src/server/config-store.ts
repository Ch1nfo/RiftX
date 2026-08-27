import { mkdir, rename, stat } from "node:fs/promises";
import { join, resolve } from "node:path";
import { homedir } from "node:os";
import { APPROVAL_MODES, DEFAULT_PROFILE, SUBAGENT_AGGRESSIVENESS, clampConcurrency, type AppConfig, type ModelProfile } from "@/lib/types";
import { readJsonStore, writeJsonStoreAtomic } from "@/server/json-store";
import { createSerializer } from "@/server/serializer";

const ROOT = join(homedir(), ".riftx");
const CONFIG_PATH = join(ROOT, "config.json");
const SESSION_PATH = join(ROOT, "sessions");
const AGENT_PATH = join(ROOT, "agent");
const LEGACY_AGENT_PATH = join(ROOT, "pi-agent");
const SUBAGENT_PATH = join(ROOT, "subagents");
const EVIDENCE_PATH = join(ROOT, "evidence");
const SKILLS_PATH = join(ROOT, "skills");
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
  // The six directories are independent; only the agent-dir migration must
  // finish before its own mkdir. Sequential awaits cost ~6 round trips on
  // every readConfig call.
  const migrateAgentDir = (async () => {
    try {
      await stat(AGENT_PATH);
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error;
      await rename(LEGACY_AGENT_PATH, AGENT_PATH).catch((migrationError: NodeJS.ErrnoException) => {
        if (migrationError.code !== "ENOENT") throw migrationError;
      });
    }
  })();
  await Promise.all([
    mkdir(ROOT, { recursive: true, mode: 0o700 }),
    mkdir(SESSION_PATH, { recursive: true, mode: 0o700 }),
    migrateAgentDir.then(() => mkdir(AGENT_PATH, { recursive: true, mode: 0o700 })),
    mkdir(SUBAGENT_PATH, { recursive: true, mode: 0o700 }),
    mkdir(EVIDENCE_PATH, { recursive: true, mode: 0o700 }),
    mkdir(SKILLS_PATH, { recursive: true, mode: 0o700 })
  ]);
}

export function getAppPaths() {
  return { root: ROOT, config: CONFIG_PATH, sessions: SESSION_PATH, agent: AGENT_PATH, subagents: SUBAGENT_PATH, evidence: EVIDENCE_PATH, skills: SKILLS_PATH };
}

export async function readConfig(repair = true): Promise<AppConfig> {
  await ensureAppDirs();
  // readJsonStore treats ENOENT and unparsable files (backed up as
  // .corrupt-*) as no data; other I/O errors surface.
  const parsed = (await readJsonStore<Partial<AppConfig>>(CONFIG_PATH)) ?? {};
  const profiles = Array.isArray(parsed.profiles) && parsed.profiles.length ? parsed.profiles : [DEFAULT_PROFILE];
  const approvalMode = APPROVAL_MODES.includes(parsed.approvalMode as AppConfig["approvalMode"]) ? parsed.approvalMode as AppConfig["approvalMode"] : "request";
  const config: AppConfig = {
    ...defaultConfig(),
    ...parsed,
    webSearch: parsed.webSearch && typeof parsed.webSearch === "object"
      ? { tavilyApiKey: typeof parsed.webSearch.tavilyApiKey === "string" ? parsed.webSearch.tavilyApiKey : "" }
      : undefined,
    profiles,
    activeProfileId: parsed.activeProfileId ?? profiles[0].id,
    approvalMode,
    archivedSessionIds: Array.isArray(parsed.archivedSessionIds) ? parsed.archivedSessionIds : [],
    archivedSessions: Array.isArray(parsed.archivedSessions) ? parsed.archivedSessions : [],
    sessionTitles: parsed.sessionTitles && typeof parsed.sessionTitles === "object"
      ? Object.fromEntries(Object.entries(parsed.sessionTitles).filter(([, title]) => typeof title === "string"))
      : {},
    maxConcurrentSubagents: Number.isFinite(parsed.maxConcurrentSubagents) ? clampConcurrency(parsed.maxConcurrentSubagents as number) : 3,
    subagentAggressiveness: SUBAGENT_AGGRESSIVENESS.includes(parsed.subagentAggressiveness as AppConfig["subagentAggressiveness"]) ? parsed.subagentAggressiveness as AppConfig["subagentAggressiveness"] : "default",
    systemPromptEnabled: parsed.systemPromptEnabled === true,
    systemPrompt: typeof parsed.systemPrompt === "string" ? parsed.systemPrompt : "",
    browserScope: Array.isArray(parsed.browserScope) ? parsed.browserScope.filter((rule): rule is string => typeof rule === "string" && Boolean(rule.trim())) : [],
    browserIgnoreTlsErrors: parsed.browserIgnoreTlsErrors !== false
  };
  if (repair && parsed && (!Array.isArray(parsed.profiles) || !parsed.profiles.length)) {
    await enqueueConfigWrite(() => writeConfig(config));
  }
  return config;
}

async function writeConfig(config: AppConfig) {
  await ensureAppDirs();
  await writeJsonStoreAtomic(CONFIG_PATH, config);
}

export type ConfigPatch = Partial<AppConfig> | ((current: AppConfig) => Partial<AppConfig> | Promise<Partial<AppConfig>>);

const enqueueConfigWrite = createSerializer();

export async function updateConfig(patch: ConfigPatch) {
  return enqueueConfigWrite(async () => {
    const current = await readConfig(false);
    const resolvedPatch = typeof patch === "function" ? await patch(current) : patch;
    const next = { ...current, ...resolvedPatch };
    await writeConfig(next);
    return next;
  });
}
