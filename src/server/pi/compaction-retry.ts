import type { AgentSession, AgentSessionEvent } from "@mariozechner/pi-coding-agent";
import { contextModelKey } from "./context-usage";

type FailureState = { modelKey: string; failures: number; blockedUntil: number };
const failures = new WeakMap<AgentSession, FailureState>();
const diagnostics = new WeakMap<AgentSession, string>();

function failureState(session: AgentSession) {
  const state = failures.get(session);
  return state?.modelKey === contextModelKey(session) ? state : undefined;
}

export function compactionBlocked(session: AgentSession) {
  return (failureState(session)?.blockedUntil ?? 0) > Date.now();
}

export function setCompactionDiagnostic(session: AgentSession, message?: string) {
  if (message) diagnostics.set(session, message);
  else diagnostics.delete(session);
}

export function compactionDiagnostic(session: AgentSession) {
  return diagnostics.get(session);
}

export function recordCompactionOutcome(session: AgentSession, event?: Extract<AgentSessionEvent, { type: "compaction_end" }>) {
  if (event?.result) {
    failures.delete(session);
    diagnostics.delete(session);
    return;
  }
  // The SDK marks both extension failures and user cancellations as aborted.
  // Only the former carry our diagnostic and should enter retry backoff.
  if (event?.aborted && !diagnostics.has(session)) return;
  const count = (failureState(session)?.failures ?? 0) + 1;
  failures.set(session, { modelKey: contextModelKey(session), failures: count,
    blockedUntil: Date.now() + Math.min(300_000, 30_000 * 2 ** Math.min(6, count - 1)) });
}

export function compactionEndError(session: AgentSession, event: Extract<AgentSessionEvent, { type: "compaction_end" }>) {
  if (event.result) return undefined;
  // Provider exception text may contain credentials or request contents.
  return compactionDiagnostic(session) ?? (event.errorMessage || !event.aborted
    ? "Context compaction failed; original history was kept. Automatic retries are temporarily paused."
    : undefined);
}
