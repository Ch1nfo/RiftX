import type { ModelProfile } from "@/lib/types";
import { RiftxError } from "@/server/errors";

/**
 * Switch the model profile of exactly one live session.
 *
 * Kept free of SDK imports so the switching contract is unit-testable. The
 * decision is always based on the target session's current profile — never on
 * the global default — so a request can never "succeed" without switching.
 * Streaming or shutting-down sessions are explicit 409s; an identical profile
 * is a successful no-op.
 */

export type ProfileSwitchLock = {
  /** In-flight profile registration/switch; used as a per-session mutex. */
  profileSwitch?: Promise<unknown>;
};

/**
 * Per-session mutex around profile registration and switching. Two switches
 * racing on one session otherwise interleave their capture/commit/rollback
 * phases, and the loser's rollback undoes the winner's already-committed
 * result. A concurrent switch is a SESSION_BUSY 409. The critical section
 * runs from state capture through commit or rollback.
 */
export async function withProfileSwitchLock<T>(record: ProfileSwitchLock, run: () => Promise<T>): Promise<T> {
  if (record.profileSwitch) {
    throw new RiftxError("A model switch is already in progress for this session", "SESSION_BUSY", 409);
  }
  const operation = (async () => run())();
  record.profileSwitch = operation;
  try {
    return await operation;
  } finally {
    if (record.profileSwitch === operation) record.profileSwitch = undefined;
  }
}

export type ProfileSwitchRecord = {
  id: string;
  profile: ModelProfile;
  model: unknown;
  shutdownPromise?: Promise<void>;
  profileSwitch?: Promise<unknown>;
  session: {
    isStreaming: boolean;
    setModel(model: unknown): Promise<unknown>;
    setThinkingLevel(level: ModelProfile["thinkingLevel"]): void;
  };
  settingsManager: { setTransport(transport: ModelProfile["transport"]): void };
};

export type ProfileSwitchDeps = {
  /**
   * Registers the profile's model on the record's registries and returns the
   * model handle plus a rollback that undoes the runtime side effects (the
   * runtime API key override) when the switch fails.
   */
  prepareModel(record: unknown, profile: ModelProfile): { model: unknown; rollback?: () => unknown };
  hasConfiguredAuth(model: unknown): boolean;
  applyTransport(session: ProfileSwitchRecord["session"], transport: ModelProfile["transport"]): void;
};

function sameProfile(left: ModelProfile, right: ModelProfile) {
  // Every field that reaches registerProfileModel or changes agent behavior
  // must participate: endpoints, protocols, credentials, and image support
  // are all runtime-relevant even when id and model name are unchanged.
  // (The display name is the only field safe to ignore.)
  return left.id === right.id
    && left.provider === right.provider
    && left.model === right.model
    && left.transport === right.transport
    && left.thinkingLevel === right.thinkingLevel
    && left.contextWindow === right.contextWindow
    && left.maxTokens === right.maxTokens
    && left.baseUrl === right.baseUrl
    && left.api === right.api
    && left.apiKey === right.apiKey
    && left.supportsImages === right.supportsImages;
}

export async function switchSessionProfile(record: ProfileSwitchRecord, profile: ModelProfile, deps: ProfileSwitchDeps): Promise<boolean> {
  if (record.shutdownPromise) throw new RiftxError("Session is shutting down", "SESSION_BUSY", 409);
  if (record.session.isStreaming) throw new RiftxError("Session is running; switch models when it is idle", "SESSION_BUSY", 409);
  if (sameProfile(record.profile, profile)) return false;
  const prepared = deps.prepareModel(record as unknown, profile);
  const previousProfile = record.profile;
  const previousModel = record.model;
  try {
    if (!deps.hasConfiguredAuth(prepared.model)) throw new RiftxError(`No API key for ${profile.provider}/${profile.model}`, "MODEL_AUTH_MISSING", 400);
    await record.session.setModel(prepared.model);
    record.session.setThinkingLevel(profile.thinkingLevel);
    record.settingsManager.setTransport(profile.transport);
    deps.applyTransport(record.session, profile.transport);
    record.profile = profile;
    record.model = prepared.model;
    return true;
  } catch (error) {
    // Rollback restores this session's state (model, thinking level,
    // transport, record) and the record's runtime API key override set by
    // prepareModel, so a failed switch never leaves the session sending the
    // old model's requests with the new profile's credentials. The registry
    // registration itself is kept: it stays valid for retries and a later
    // switch to the same profile.
    try { await prepared.rollback?.(); } catch { /* rollback is best-effort */ }
    await record.session.setModel(previousModel).catch(() => undefined);
    record.session.setThinkingLevel(previousProfile.thinkingLevel);
    record.settingsManager.setTransport(previousProfile.transport);
    deps.applyTransport(record.session, previousProfile.transport);
    record.profile = previousProfile;
    record.model = previousModel;
    throw error;
  }
}
