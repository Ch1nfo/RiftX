import type { PromptRequestState } from "@/lib/prompt-request-state";

export type { PromptRequestState } from "@/lib/prompt-request-state";

export type PromptRequestOutcome = {
  state: PromptRequestState;
  error?: string;
};

type PromptRequestCarrier = {
  promptRequests?: Map<string, PromptRequestOutcome>;
};

const TERMINAL_REQUEST_LOG_CAP = 64;

/** Register a request before its detached SDK dispatch can race a snapshot.
 * Returns false when the id is already known — requestIds are idempotency
 * keys, so a replay must be rejected upstream instead of re-dispatching with
 * the stale terminal outcome of the first attempt. */
export function beginPromptRequest(record: PromptRequestCarrier, requestId: string | undefined): boolean {
  if (!requestId) return true;
  record.promptRequests ??= new Map();
  if (record.promptRequests.has(requestId)) return false;
  record.promptRequests.set(requestId, { state: "pending" });
  return true;
}

/** Move pending to its first terminal state; a later runtime failure cannot undo acceptance. */
export function settlePromptRequest(record: PromptRequestCarrier, requestId: string | undefined, state: Exclude<PromptRequestState, "pending">, error?: string) {
  if (!requestId) return undefined;
  record.promptRequests ??= new Map();
  const current = record.promptRequests.get(requestId);
  if (current && current.state !== "pending") return current;
  const outcome: PromptRequestOutcome = { state, ...(state === "failed" && error ? { error } : {}) };
  record.promptRequests.set(requestId, outcome);

  // Bound completed history without ever evicting a request whose dispatch is
  // still unresolved. Terminal responses are also echoed by POST, so keeping
  // the newest window is sufficient for reconnect reconciliation.
  let terminalCount = 0;
  for (const value of record.promptRequests.values()) if (value.state !== "pending") terminalCount += 1;
  if (terminalCount > TERMINAL_REQUEST_LOG_CAP) {
    for (const [key, value] of record.promptRequests) {
      if (value.state === "pending") continue;
      record.promptRequests.delete(key);
      terminalCount -= 1;
      if (terminalCount <= TERMINAL_REQUEST_LOG_CAP) break;
    }
  }
  return outcome;
}

export function promptRequestStates(record: PromptRequestCarrier): Record<string, PromptRequestState> {
  return Object.fromEntries([...record.promptRequests?.entries() ?? []].map(([key, value]) => [key, value.state]));
}
