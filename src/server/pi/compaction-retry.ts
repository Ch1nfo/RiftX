import type { AgentSession, AgentSessionEvent } from "@mariozechner/pi-coding-agent";
import { contextModelKey } from "./context-usage";

type FailureState = { modelKey: string; failures: number; blockedUntil: number };
const failures = new WeakMap<AgentSession, FailureState>();
const extensionFailures = new WeakSet<AgentSession>();

function failureState(session: AgentSession) {
  const state = failures.get(session);
  return state?.modelKey === contextModelKey(session) ? state : undefined;
}

export function compactionBlocked(session: AgentSession) {
  return (failureState(session)?.blockedUntil ?? 0) > Date.now();
}

export function setCompactionFailed(session: AgentSession, failed: boolean) {
  if (failed) extensionFailures.add(session);
  else extensionFailures.delete(session);
}

export function clearCompactionRetry(session: AgentSession) {
  failures.delete(session);
  extensionFailures.delete(session);
}

export function recordCompactionOutcome(session: AgentSession, event?: Extract<AgentSessionEvent, { type: "compaction_end" }>) {
  const failed = extensionFailures.has(session);
  extensionFailures.delete(session);
  if (event?.result) {
    clearCompactionRetry(session);
    return;
  }
  // Pi marks both extension failures and user cancellations as aborted.
  if (event?.aborted && !failed) return;
  const count = (failureState(session)?.failures ?? 0) + 1;
  failures.set(session, { modelKey: contextModelKey(session), failures: count,
    blockedUntil: Date.now() + Math.min(300_000, 30_000 * 2 ** Math.min(6, count - 1)) });
}
