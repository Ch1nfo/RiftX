import { readConfig } from "@/server/config-store";
import { listSessions } from "@/server/pi/session-manager";

export const runtime = "nodejs";

export async function GET() {
  const config = await readConfig();
  const sessions = await listSessions();
  const activeSessions = sessions.filter((item) => !item.archived);
  return Response.json({
    cwd: config.cwd,
    activeSessionId: activeSessions[0]?.id ?? "",
    sessions: activeSessions,
    profiles: config.profiles.map(({ apiKey, ...profile }) => ({ ...profile, apiKey: apiKey ? "••••••••" : "" })),
    activeProfileId: config.activeProfileId,
    approvalMode: config.approvalMode,
    childProfileId: config.childProfileId,
    childInherit: config.childInherit,
    maxConcurrentSubagents: config.maxConcurrentSubagents,
    subagentAggressiveness: config.subagentAggressiveness
  });
}
