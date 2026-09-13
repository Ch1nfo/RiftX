import type { AgentSession, SessionBeforeCompactEvent, SessionEntry, CompactionSettings } from "@mariozechner/pi-coding-agent";
import { existsSync } from "node:fs";
import { join } from "node:path";
import { pathToFileURL } from "node:url";

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

type PrepareCompaction = (entries: SessionEntry[], settings: CompactionSettings) => SessionBeforeCompactEvent["preparation"] | undefined;
let prepareCompactionModule: Promise<{ prepareCompaction: PrepareCompaction }> | undefined;
let warnedMissingCompactionModule = false;

// Pi does not export preparation from its package root. Keep this version-sensitive
// access beside the other SDK internals, and reuse its tool-safe cut and file tracking.
export async function prepareCompactionWithBudget(entries: SessionEntry[], settings: CompactionSettings) {
  // Resolve from the deployed application, not a build-time source URL.
  // Webpack does not preserve Node's import.meta.resolve implementation.
  if (!prepareCompactionModule) {
    const packageName = "@mariozechner/pi-coding-agent";
    // Preserve native resolution instead of Webpack's createRequire parser.
    const { createRequire } = await import(/* webpackIgnore: true */ "node:module");
    // Pi publishes import-only exports, so require.resolve(packageName) fails.
    // Prefer the installed module's dependencies over files in the workspace.
    // The bundled web app can fall back to its application working directory.
    const searchPaths = [...new Set([
      ...(createRequire(import.meta.url).resolve.paths(packageName) ?? []),
      ...(createRequire(join(process.cwd(), "package.json")).resolve.paths(packageName) ?? [])
    ])];
    const modulePath = searchPaths
      .map((directory) => join(directory, packageName, "dist/core/compaction/compaction.js"))
      .find((candidate) => existsSync(candidate));
    if (!modulePath) {
      const error = new Error("Installed Pi SDK compaction module could not be located");
      if (!warnedMissingCompactionModule) {
        warnedMissingCompactionModule = true;
        console.warn("[riftx] PI_COMPACTION_MODULE_UNAVAILABLE", { packageName, cwd: process.cwd(), reason: error.message });
      }
      throw error;
    }
    prepareCompactionModule = import(/* webpackIgnore: true */ pathToFileURL(modulePath).href);
  }
  const sdk = await prepareCompactionModule;
  if (typeof sdk.prepareCompaction !== "function") throw new Error("Pi compaction preparation is unavailable");
  return sdk.prepareCompaction(entries, settings);
}
