export type SessionDrafts = Readonly<Record<string, string>>;

/** Read the composer draft owned by one session. No active session has no draft. */
export function sessionDraft(drafts: SessionDrafts, sessionId: string): string {
  return sessionId ? drafts[sessionId] ?? "" : "";
}

/** Immutably update one session without leaking its composer text into another. */
export function withSessionDraft(drafts: SessionDrafts, sessionId: string, value: string): SessionDrafts {
  if (!sessionId || sessionDraft(drafts, sessionId) === value) return drafts;
  if (value) return { ...drafts, [sessionId]: value };
  const next = { ...drafts };
  delete next[sessionId];
  return next;
}
