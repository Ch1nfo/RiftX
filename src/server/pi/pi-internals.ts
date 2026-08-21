import type { AgentSession } from "@mariozechner/pi-coding-agent";

type InternalAgentSession = {
  _agentEventQueue?: Promise<void>;
  _runAutoCompaction?: (reason: "threshold", willRetry: boolean) => Promise<void>;
};

function internalSession(session: AgentSession) {
  return session as unknown as InternalAgentSession;
}

export async function waitForAgentEvents(session: AgentSession) {
  await internalSession(session)._agentEventQueue;
}

export async function runAutoCompaction(session: AgentSession) {
  const internal = internalSession(session);
  if (!internal._runAutoCompaction) throw new Error("Pi auto-compaction hook is unavailable");
  await internal._runAutoCompaction("threshold", false);
}

export function replaceAgentMessages<T>(session: AgentSession, target: T[], source: readonly T[]) {
  target.splice(0, target.length, ...source);
  return session;
}

export function setAgentTransport(session: AgentSession, transport: string) {
  (session.agent as unknown as { transport: string }).transport = transport;
}
