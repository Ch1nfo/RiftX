import { mkdir, readFile, writeFile, chmod } from "node:fs/promises";
import { dirname, join } from "node:path";
import { homedir } from "node:os";
import { APPROVAL_MODES, DEFAULT_PROFILE, type AppConfig, type ModelProfile } from "@/lib/types";

const ROOT = join(homedir(), ".riftx");
const CONFIG_PATH = join(ROOT, "config.json");
const SESSION_PATH = join(ROOT, "sessions");
const PI_AGENT_PATH = join(ROOT, "pi-agent");

const defaultConfig = (): AppConfig => ({
  profiles: [DEFAULT_PROFILE],
  activeProfileId: DEFAULT_PROFILE.id,
  childProfileId: null,
  childInherit: true,
  cwd: process.cwd(),
  approvalMode: "request",
  archivedSessionIds: [],
  archivedSessions: [],
  sessionTitles: {}
});

export async function ensureAppDirs() {
  await mkdir(ROOT, { recursive: true, mode: 0o700 });
  await mkdir(SESSION_PATH, { recursive: true, mode: 0o700 });
  await mkdir(PI_AGENT_PATH, { recursive: true, mode: 0o700 });
}

export function getAppPaths() {
  return { root: ROOT, config: CONFIG_PATH, sessions: SESSION_PATH, piAgent: PI_AGENT_PATH };
}

export async function readConfig(): Promise<AppConfig> {
  await ensureAppDirs();
  try {
    const parsed = JSON.parse(await readFile(CONFIG_PATH, "utf8")) as Partial<AppConfig>;
    const profiles = Array.isArray(parsed.profiles) && parsed.profiles.length ? parsed.profiles : [DEFAULT_PROFILE];
    const approvalMode = APPROVAL_MODES.includes(parsed.approvalMode as AppConfig["approvalMode"]) ? parsed.approvalMode as AppConfig["approvalMode"] : "request";
    return {
      ...defaultConfig(),
      ...parsed,
      profiles,
      activeProfileId: parsed.activeProfileId ?? profiles[0].id,
      approvalMode,
      archivedSessionIds: Array.isArray(parsed.archivedSessionIds) ? parsed.archivedSessionIds : [],
      archivedSessions: Array.isArray(parsed.archivedSessions) ? parsed.archivedSessions : [],
      sessionTitles: parsed.sessionTitles && typeof parsed.sessionTitles === "object"
        ? Object.fromEntries(Object.entries(parsed.sessionTitles).filter(([, title]) => typeof title === "string"))
        : {}
    };
  } catch {
    const config = defaultConfig();
    await writeConfig(config);
    return config;
  }
}

export async function writeConfig(config: AppConfig) {
  await ensureAppDirs();
  await writeFile(CONFIG_PATH, `${JSON.stringify(config, null, 2)}\n`, { mode: 0o600 });
  await chmod(CONFIG_PATH, 0o600);
}

export async function updateProfiles(profiles: ModelProfile[], activeProfileId?: string) {
  const current = await readConfig();
  const next: AppConfig = {
    ...current,
    profiles: profiles.length ? profiles : [DEFAULT_PROFILE],
    activeProfileId: activeProfileId ?? profiles[0]?.id ?? DEFAULT_PROFILE.id
  };
  await writeConfig(next);
  return next;
}

export async function updateConfig(patch: Partial<AppConfig>) {
  const next = { ...(await readConfig()), ...patch };
  await writeConfig(next);
  return next;
}

export { CONFIG_PATH, PI_AGENT_PATH, SESSION_PATH };
