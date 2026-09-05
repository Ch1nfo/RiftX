import { createHash } from "node:crypto";
import { readFile, stat } from "node:fs/promises";
import { resolve } from "node:path";
import { readConfig, getAppPaths, getLaunchDirectory } from "@/server/config-store";
import { RiftxError } from "@/server/errors";
import type { AppConfig, ContextUsage, ModelProfile, SessionSummary } from "@/lib/types";
import { orderSessionsByActivity } from "@/lib/session-activity";
import { emptyContextUsage, normalizeContextUsage } from "./usage";
import { estimateCompactedUsage, estimateMessagesContextUsage } from "./mid-turn-compaction";
import { isSessionRecordRunning, sessions, type SessionRecord } from "./session-registry";
import { archivedRestoreBlock } from "./session-archive";
import { textFromContent } from "./text-content";
import { isSubagentInjectionMessage } from "./session-join";

/**
 * Session snapshots, listings, and message projection: everything that reads
 * the registry or the session files to answer API requests, extracted from
 * the runtime-creation module so both can evolve independently.
 */

type SessionSnapshotCacheEntry = {
  modifiedMs: number;
  size: number;
  profileKey: string;
  snapshot: SessionSnapshot | null;
};

const sessionSnapshotCache = new Map<string, SessionSnapshotCacheEntry>();
const SESSION_SNAPSHOT_CACHE_LIMIT = 256;

type SessionSnapshot = {
  id: string;
  provider?: string;
  model?: string;
  profileId?: string;
  contextWindow: number;
  usage: ContextUsage;
};

export function summaryName(config: AppConfig, id: string, firstMessage: string, archived = false) {
  if (config.sessionTitles[id]) return config.sessionTitles[id];
  if (firstMessage) return "Untitled task";
  return archived ? "Archived session" : "New session";
}

export function usageFromRecord(record: SessionRecord): ContextUsage {
  const usage = record.session.getContextUsage();
  if (!usage) return emptyContextUsage(record.profile.contextWindow);
  if (usage.percent === null) return estimateCompactedUsage(record.session, record.profile.contextWindow);
  return normalizeContextUsage(usage, record.profile.contextWindow);
}

async function sessionSnapshotFromFile(path: string, profiles: ModelProfile[]): Promise<SessionSnapshot | null> {
  const profileKey = profiles.map((profile) => `${profile.id}:${profile.provider}:${profile.model}:${profile.contextWindow}`).join("|");
  try {
    const fileInfo = await stat(path);
    const cached = sessionSnapshotCache.get(path);
    if (cached && cached.modifiedMs === fileInfo.mtimeMs && cached.size === fileInfo.size && cached.profileKey === profileKey) {
      return cached.snapshot ? { ...cached.snapshot, usage: { ...cached.snapshot.usage } } : null;
    }
    const text = await readFile(path, "utf8");
    const lines = text.split(/\r?\n/).filter(Boolean);
    let sessionId = "";
    let provider = "";
    let model = "";
    let usage: ContextUsage | undefined;
    let postCompactionMessages: unknown[] | undefined;
    let hasPostCompactionUsage = false;
    for (const line of lines) {
      let entry: Record<string, unknown>;
      try {
        entry = JSON.parse(line) as Record<string, unknown>;
      } catch {
        continue;
      }
      if (!sessionId && entry.type === "session" && typeof entry.id === "string") sessionId = entry.id;
      if (entry.type === "model_change") {
        if (typeof entry.provider === "string") provider = entry.provider;
        if (typeof entry.modelId === "string") model = entry.modelId;
      }
      if (entry.type === "compaction") {
        postCompactionMessages = [{ role: "compactionSummary", summary: String(entry.summary ?? "") }];
        hasPostCompactionUsage = false;
      }
      if (entry.type === "branch_summary" && postCompactionMessages) {
        postCompactionMessages.push({ role: "branchSummary", summary: String(entry.summary ?? "") });
      }
      if (entry.type === "message") {
        const message = entry.message as Record<string, unknown> | undefined;
        if (postCompactionMessages && message) postCompactionMessages.push(message);
        if (message?.usage) {
          if (typeof message.provider === "string") provider = message.provider;
          if (typeof message.model === "string") model = message.model;
          const matchedProfile = profiles.find((profile) => profile.provider === provider && profile.model === model);
          const contextWindow = matchedProfile?.contextWindow ?? 0;
          usage = normalizeContextUsage(message.usage, contextWindow);
          if (postCompactionMessages && message.role === "assistant" && message.stopReason !== "aborted" && message.stopReason !== "error") {
            hasPostCompactionUsage = true;
          }
        }
      }
    }
    const matchedProfile = profiles.find((profile) => profile.provider === provider && profile.model === model);
    const contextWindow = matchedProfile?.contextWindow ?? usage?.contextWindow ?? 0;
    if (postCompactionMessages && !hasPostCompactionUsage) {
      usage = estimateMessagesContextUsage(postCompactionMessages, contextWindow);
    }
    const snapshot = {
      id: sessionId,
      provider: provider || matchedProfile?.provider,
      model: model || matchedProfile?.model,
      profileId: matchedProfile?.id,
      contextWindow,
      usage: usage ? {
        ...usage,
        contextWindow,
        remaining: usage.percent === null ? contextWindow : Math.max(0, contextWindow - usage.tokens),
        percent: usage.percent === null ? null : contextWindow > 0 ? Math.min(100, (usage.tokens / contextWindow) * 100) : null
      } : emptyContextUsage(contextWindow)
    };
    sessionSnapshotCache.set(path, { modifiedMs: fileInfo.mtimeMs, size: fileInfo.size, profileKey, snapshot });
    while (sessionSnapshotCache.size > SESSION_SNAPSHOT_CACHE_LIMIT) sessionSnapshotCache.delete(sessionSnapshotCache.keys().next().value!);
    return { ...snapshot, usage: { ...snapshot.usage } };
  } catch (error) {
    // Degrade only for a missing file or a single unparseable line —
    // other I/O errors (permissions, disk failures) must surface, not
    // silently present an empty session.
    const code = (error as NodeJS.ErrnoException).code;
    if (code === "ENOENT") return null;
    if (error instanceof SyntaxError) return null; // bad JSON line
    throw error;
  }
}

async function buildSessionSnapshot(id: string, path: string | undefined, config: AppConfig) {
  const live = sessions.get(id);
  if (live) {
    return {
      id,
      provider: live.profile.provider,
      model: live.profile.model,
      profileId: live.profile.id,
      contextWindow: live.profile.contextWindow,
      usage: usageFromRecord(live)
    } satisfies SessionSnapshot;
  }
  const fromFile = path ? await sessionSnapshotFromFile(path, config.profiles) : null;
  if (fromFile) return fromFile;
  const fallbackProfile = config.profiles.find((profile) => profile.id === config.activeProfileId) ?? config.profiles[0];
  return {
    id,
    provider: fallbackProfile?.provider,
    model: fallbackProfile?.model,
    profileId: fallbackProfile?.id,
    contextWindow: fallbackProfile?.contextWindow ?? 0,
    usage: emptyContextUsage(fallbackProfile?.contextWindow ?? 0)
  } satisfies SessionSnapshot;
}

export async function listWorkspaceSessionInfos(cwd: string) {
  const target = resolve(cwd);
  const { SessionManager } = await import("@mariozechner/pi-coding-agent");
  const infos = await SessionManager.list(cwd, getAppPaths().sessions);
  const launchDirectory = getLaunchDirectory();
  return infos.filter((info) => info.cwd ? resolve(info.cwd) === target : target === launchDirectory);
}

async function findSessionPath(id: string, cwd?: string) {
  const config = await readConfig();
  const rootCwd = cwd ?? config.cwd;
  const info = (await listWorkspaceSessionInfos(rootCwd)).find((item) => item.id === id);
  if (info?.path) return info.path;
  const archived = config.archivedSessions.find((item) => item.id === id);
  if (archived?.path) return archived.path;
  return "";
}

export async function getSessionSnapshot(id: string) {
  const live = sessions.get(id);
  const path = await findSessionPath(id);
  // An unknown id must 404 like every sibling route, never fabricate a
  // default-profile snapshot for a session that does not exist.
  if (!live && !path) throw new RiftxError("Session not found", "SESSION_NOT_FOUND", 404);
  const snapshot = await buildSessionSnapshot(id, path, await readConfig());
  return {
    id,
    provider: snapshot.provider ?? "",
    model: snapshot.model ?? "",
    profileId: snapshot.profileId ?? "",
    contextWindow: snapshot.contextWindow,
    usage: snapshot.usage
  };
}

/** Content-hash reference for one transcript image part: stable across snapshot
 * calls (append-only branch) and resolvable on demand by the image route, so a
 * reconciliation snapshot never re-encodes megabytes of base64 history. */
export function imageRefFor(data: string) {
  return createHash("sha256").update(data).digest("hex").slice(0, 24);
}

/**
 * Per-record incremental image index. getBranch() returns a fresh array on
 * every call, but the ENTRY objects inside are stable — so per-entry WeakSet
 * membership drives incrementality: each user image is hashed exactly once
 * no matter how many snapshots or image GETs follow. A non-append branch
 * rewrite rebuilds the index so removed images are released.
 */
function ensureImageIndex(record: SessionRecord) {
  const branch = record.sessionManager.getBranch() as object[];
  const previousBranch = record.imageIndexedBranch;
  const appendOnly = previousBranch !== undefined
    && previousBranch.length <= branch.length
    && previousBranch.every((entry, index) => branch[index] === entry);

  // Compaction, rollback, and branch switching can remove entries. Rebuild in
  // that case so old URLs stop resolving and their base64 strings are no
  // longer strongly retained. Fresh-array snapshots with the same entries and
  // ordinary append-only growth keep the incremental fast path.
  if (!appendOnly) {
    record.imageSeenEntries = new WeakSet();
    record.imageEntryImages = new WeakMap();
    record.imageRefIndex = new Map();
  }
  record.imageSeenEntries ??= new WeakSet();
  record.imageEntryImages ??= new WeakMap();
  record.imageRefIndex ??= new Map();
  const seen = record.imageSeenEntries;
  const perEntry = record.imageEntryImages;
  const index = record.imageRefIndex;
  for (const entry of branch) {
    if (!entry || typeof entry !== "object" || seen.has(entry)) continue;
    seen.add(entry);
    const images: Array<{ ref: string; mimeType: string }> = [];
    const candidate = (entry as { type?: string; message?: { role?: string; content?: unknown } }).message;
    const role = candidate?.role;
    const content = candidate?.content;
    if ((entry as { type?: string }).type === "message" && role === "user" && Array.isArray(content)) {
      for (const part of content as Array<{ type?: string; data?: unknown; mimeType?: unknown }>) {
        if (part?.type === "image" && typeof part.data === "string" && typeof part.mimeType === "string") {
          const ref = imageRefFor(part.data);
          images.push({ ref, mimeType: part.mimeType });
          index.set(ref, { data: part.data, mimeType: part.mimeType });
        }
      }
    }
    perEntry.set(entry, images);
  }
  record.imageIndexedBranch = branch;
  // The SAME branch array this index was built from: callers walking messages
  // must use it, not a second getBranch() that can already include entries
  // written between the two reads.
  return { index, perEntry, branch };
}

/** Finds the transcript image whose content hash matches ref. */
export function findTranscriptImage(record: SessionRecord, ref: string): { bytes: Buffer; mimeType: string } | undefined {
  if (!/^[a-f0-9]{24}$/.test(ref)) return undefined;
  const indexed = ensureImageIndex(record).index.get(ref);
  return indexed ? { bytes: Buffer.from(indexed.data, "base64"), mimeType: indexed.mimeType } : undefined;
}

export async function getSessionMessages(getRecord: () => Promise<SessionRecord>) {
  // One index pass serves both the ref URLs below and later image GETs; entry
  // objects are hashed exactly once across all snapshots and image requests.
  // The walk reuses the SAME branch array the index was built from — a second
  // getBranch() could include a message written between the two reads whose
  // image refs the first pass never computed.
  const record = await getRecord();
  const { perEntry, branch } = ensureImageIndex(record);
  const messages: Array<{ id: string; role: "user" | "assistant" | "thinking" | "tool"; content: string; toolName?: string; toolCallId?: string; status?: "queued" | "running" | "done" | "error" | "cancelled"; isError?: boolean; images?: Array<{ src: string; mimeType: string }>; screenshotId?: string }> = [];
  const toolIndexes = new Map<string, number>();
  const entries = branch as Array<{ type: string; message: unknown }>;
  entries.forEach((entry, messageIndex) => {
    if (entry.type !== "message") return;
    const message = entry.message;
    const candidate = message as unknown as { role?: string; content?: unknown; toolCallId?: string; toolName?: string; isError?: boolean; details?: { screenshotId?: unknown } };
    if (candidate.role === "toolResult") {
      const toolCallId = candidate.toolCallId ?? `${record.id}-${messageIndex}`;
      const result = textFromContent(candidate.content);
      // Browser screenshots carry a stable id the UI renders via the existing
      // screenshot route; other image parts (MCP) only reach the model.
      const screenshotId = typeof candidate.details?.screenshotId === "string" ? candidate.details.screenshotId : undefined;
      const toolIndex = toolIndexes.get(toolCallId);
      if (toolIndex === undefined) {
        messages.push({ id: `${record.id}-${messageIndex}`, role: "tool", toolCallId, toolName: candidate.toolName ?? "tool", content: result, status: candidate.isError ? "error" : "done", isError: Boolean(candidate.isError), ...(screenshotId ? { screenshotId } : {}) });
      } else {
        messages[toolIndex] = { ...messages[toolIndex], content: result, status: candidate.isError ? "error" : "done", isError: Boolean(candidate.isError), ...(screenshotId ? { screenshotId } : {}) };
      }
      return;
    }

    const parts = Array.isArray(candidate.content) ? candidate.content : [{ type: "text", text: String(candidate.content ?? "") }];
    // Precomputed by the index pass: no re-hashing while building ref URLs.
    const entryImages = candidate.role === "user" ? perEntry.get(entry as object) : undefined;
    const userImages = entryImages?.length
      ? entryImages.map((image) => ({ src: `/api/sessions/${record.id}/messages/image/${image.ref}`, mimeType: image.mimeType }))
      : undefined;
    let imagesAttached = false;
    parts.forEach((part: unknown, partIndex: number) => {
      if (!part || typeof part !== "object") return;
      const item = part as { type?: string; text?: unknown; thinking?: unknown; id?: string; name?: string; arguments?: unknown };
      const id = `${record.id}-${messageIndex}-${partIndex}`;
      if (candidate.role === "user" && item.type === "text") {
        const content = String(item.text ?? "");
        if (isSubagentInjectionMessage(content)) return;
        const images = !imagesAttached && userImages?.length ? userImages : undefined;
        imagesAttached = true;
        messages.push({ id, role: "user", content, ...(images ? { images } : {}) });
      } else if (candidate.role === "assistant" && item.type === "thinking") {
        messages.push({ id, role: "thinking", content: String(item.thinking ?? ""), status: "done" });
      } else if (candidate.role === "assistant" && item.type === "text") {
        messages.push({ id, role: "assistant", content: String(item.text ?? "") });
      } else if (candidate.role === "assistant" && item.type === "toolCall") {
        const toolCallId = String(item.id ?? id);
        const toolIndex = messages.length;
        toolIndexes.set(toolCallId, toolIndex);
        messages.push({ id: toolCallId, role: "tool", toolCallId, toolName: String(item.name ?? "tool"), content: JSON.stringify(item.arguments ?? {}, null, 2), status: record.toolStatuses.get(toolCallId) ?? "cancelled" });
      }
    });
  });
  return messages;
}

export async function listSessions(): Promise<SessionSummary[]> {
  const config = await readConfig();
  const archived = new Set(config.archivedSessionIds);
  const infos = await listWorkspaceSessionInfos(config.cwd);
  const persisted = await Promise.all(infos.map(async (info) => {
    const snapshot = await buildSessionSnapshot(info.id, info.path, config);
    const liveRecord = sessions.get(info.id);
    return {
      id: info.id,
      path: info.path,
      name: summaryName(config, info.id, info.firstMessage, archived.has(info.id)),
      firstMessage: info.firstMessage,
      updatedAt: info.modified.toISOString(),
      archived: archived.has(info.id),
      profileId: snapshot.profileId,
      provider: snapshot.provider,
      model: snapshot.model,
      contextWindow: snapshot.contextWindow,
      usage: snapshot.usage,
      running: liveRecord ? isSessionRecordRunning(liveRecord) : false
    } satisfies SessionSummary;
  }));
  const seen = new Set(persisted.map((item) => item.id));
  const live = [...sessions.values()]
    .filter((session) => resolve(session.cwd) === resolve(config.cwd) && !seen.has(session.id))
    .map((session) => ({
      id: session.id,
      path: session.session.sessionFile ?? "",
      name: summaryName(config, session.id, "", archived.has(session.id)),
      firstMessage: "",
      updatedAt: new Date().toISOString(),
      archived: archived.has(session.id),
      profileId: session.profile.id,
      provider: session.profile.provider,
      model: session.profile.model,
      contextWindow: session.profile.contextWindow,
      usage: usageFromRecord(session),
      running: isSessionRecordRunning(session)
    } satisfies SessionSummary));
  const archivedMetadata = await Promise.all(config.archivedSessions
    .filter((session) => !seen.has(session.id) && !live.some((item) => item.id === session.id))
    .map(async (session) => {
      const sessionFileExists = Boolean(session.path) && await stat(session.path).then(() => true, () => false);
      return {
        ...session,
        name: summaryName(config, session.id, session.firstMessage, true),
        archived: true,
        restoreBlock: archivedRestoreBlock(false, sessionFileExists)
      } satisfies SessionSummary;
    }));
  const archivedFallback = config.archivedSessionIds
    .filter((id) => !seen.has(id) && !archivedMetadata.some((session) => session.id === id))
    .map((id) => ({
      id,
      path: "",
      name: summaryName(config, id, "", true),
      firstMessage: "",
      updatedAt: new Date().toISOString(),
      archived: true,
      restoreBlock: archivedRestoreBlock(false, false)
    } satisfies SessionSummary));
  return orderSessionsByActivity([...live, ...persisted, ...archivedMetadata, ...archivedFallback]);
}

export async function listRunningSessionIds() {
  const config = await readConfig();
  return [...sessions.values()]
    .filter((record) => resolve(record.cwd) === resolve(config.cwd) && isSessionRecordRunning(record))
    .map((record) => record.id);
}
