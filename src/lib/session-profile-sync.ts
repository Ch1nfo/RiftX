import type { ModelProfile, SessionSummary } from "./types";

/**
 * Optimistically apply a completed model switch to the session list. The
 * server has already switched the live session when this runs, so the UI must
 * not wait for a refetch: the SSE-reconnect effect reads
 * sessionMeta.profileId and would otherwise restore the stale profile id and
 * overwrite the selector with the pre-switch value.
 */
export function withSessionProfile(sessions: SessionSummary[], sessionId: string, profile: ModelProfile): SessionSummary[] {
  return sessions.map((session) => session.id === sessionId
    ? {
        ...session,
        profileId: profile.id,
        provider: profile.provider,
        model: profile.model,
        contextWindow: profile.contextWindow
      }
    : session);
}
