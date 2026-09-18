import type { AgentSession } from "@mariozechner/pi-coding-agent";
import { compactionBlocked, recordCompactionOutcome, setCompactionDiagnostic } from "./compaction-retry";

type InternalAgentSession = {
  _agentEventQueue?: Promise<void>;
  _runAutoCompaction?: (reason: "threshold" | "overflow", willRetry: boolean) => Promise<void>;
};

function internalSession(session: AgentSession) {
  return session as unknown as InternalAgentSession;
}

const compactionPolicyInstalled = new WeakSet<AgentSession>();

/** Cover SDK prompt preflight, overflow recovery, turn end and our transform. */
export function installAutoCompactionRetryPolicy(session: AgentSession) {
  const internal = internalSession(session);
  const original = internal._runAutoCompaction;
  if (!original || compactionPolicyInstalled.has(session)) return;
  compactionPolicyInstalled.add(session);
  internal._runAutoCompaction = async (reason, willRetry) => {
    if (compactionBlocked(session)) return;
    setCompactionDiagnostic(session);
    let end: Parameters<typeof recordCompactionOutcome>[1];
    const unsubscribe = session.subscribe((event) => {
      if (event.type === "compaction_end") end = event;
    });
    try {
      await original.call(session, reason, willRetry);
    } finally {
      unsubscribe();
      recordCompactionOutcome(session, end);
    }
  };
}

export async function waitForAgentEvents(session: AgentSession) {
  await internalSession(session)._agentEventQueue;
}

export async function runAutoCompaction(session: AgentSession) {
  const internal = internalSession(session);
  if (!internal._runAutoCompaction) throw new Error("Auto-compaction hook is unavailable");
  await internal._runAutoCompaction("threshold", false);
}

export function replaceAgentMessages<T>(session: AgentSession, target: T[], source: readonly T[]) {
  target.splice(0, target.length, ...source);
  return session;
}

export function setAgentTransport(session: AgentSession, transport: string) {
  (session.agent as unknown as { transport: string }).transport = transport;
}
