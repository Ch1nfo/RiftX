import type { EventEmitter } from "node:events";
import type { AuthStorage, ModelRegistry, SessionManager as AgentSessionManager, SettingsManager, AgentSession } from "@mariozechner/pi-coding-agent";
import type { Model } from "@mariozechner/pi-ai";
import type { EvidenceStore } from "./evidence-store";
import { ApprovalGate } from "./approval-gate";
import { BrowserManager } from "@/browser";
import { MutationLock } from "./mutation-lock";
import { BashConcurrency } from "./bash-concurrency";
import { SubagentManager } from "./subagent-manager";
import type { SkillDescriptor } from "./skill-router";
import type { ProviderRegistrations } from "./model-registration";

/**
 * Process-global session registry: the live SessionRecord map, the in-flight
 * creation dedup map, and the shared record type. Extracted so snapshot,
 * listing, and lifecycle modules can read the registry without importing the
 * runtime-creation god module (and vice versa).
 */

export type SessionRecord = {
  id: string;
  cwd: string;
  profile: import("@/lib/types").ModelProfile;
  authStorage: AuthStorage;
  model: Model<any>;
  modelRegistry: ModelRegistry;
  settingsManager: SettingsManager;
  sessionManager: AgentSessionManager;
  session: AgentSession;
  gate: ApprovalGate;
  emitter: EventEmitter;
  toolStatuses: Map<string, "queued" | "running">;
  unsubscribe: () => void;
  browser?: BrowserManager;
  mutationLock: MutationLock;
  bashConcurrency: BashConcurrency;
  subagents?: SubagentManager;
  evidenceStore: EvidenceStore;
  shutdownPromise?: Promise<void>;
  runtimeVersion?: number;
  abortPromise?: Promise<void>;
  aborting?: boolean;
  abortEpoch?: number;
  waitingForSubagents?: boolean;
  promptChain?: Promise<void>;
  subagentDeliveryInProgress?: boolean;
  deliveredSubagentResults: Set<string>;
  deliveringSubagentResults: Set<string>;
  skills: SkillDescriptor[];
  loadedSkills: Set<string>;
  providerRegistrations: ProviderRegistrations;
  profileSwitch?: Promise<unknown>;
};

export type RuntimeDeps = {
  evidenceStore: EvidenceStore;
  evidenceSessionId: string;
};

/** Bump to force process-global session objects to rebuild from disk. */
export const RUNTIME_VERSION = 27;

declare global {
  // eslint-disable-next-line no-var
  var __riftxSessions: Map<string, SessionRecord> | undefined;
  // eslint-disable-next-line no-var
  var __riftxSessionCreation: Map<string, Promise<SessionRecord>> | undefined;
}

export const sessions = globalThis.__riftxSessions ?? (globalThis.__riftxSessions = new Map<string, SessionRecord>());
export const sessionCreation = globalThis.__riftxSessionCreation ?? (globalThis.__riftxSessionCreation = new Map<string, Promise<SessionRecord>>());
