import { mkdir, rm } from "node:fs/promises";
import { join } from "node:path";
import { randomUUID } from "node:crypto";
import type { Finding, FindingInput, FindingPatch, FindingSource, RiftxEvent } from "@/lib/types";
import { readJsonStore, writeJsonStoreAtomic } from "@/server/json-store";
import { createSerializer } from "@/server/serializer";

type FindingFile = { findings?: Finding[] };

function now() {
  return new Date().toISOString();
}

function normalize(value: string) {
  return value.trim().replace(/\s+/g, " ").toLocaleLowerCase();
}

function sanitizeEvidence(item: unknown): Finding["evidence"][number] | null {
  if (!item || typeof item !== "object") return null;
  const evidence = item as Record<string, unknown>;
  if (evidence.type === "quote" && typeof evidence.quote === "string") return { type: "quote", quote: evidence.quote };
  if (evidence.type === "tool" && typeof evidence.toolCallId === "string" && typeof evidence.toolName === "string") return { type: "tool", toolCallId: evidence.toolCallId, toolName: evidence.toolName, ...(typeof evidence.content === "string" ? { content: evidence.content } : {}) };
  if (evidence.type === "request" && typeof evidence.requestRef === "string") return {
    type: "request",
    requestRef: evidence.requestRef,
    ...(typeof evidence.method === "string" ? { method: evidence.method } : {}),
    ...(typeof evidence.url === "string" ? { url: evidence.url } : {}),
    ...(typeof evidence.status === "number" ? { status: evidence.status } : {})
  };
  if (evidence.type === "screenshot" && typeof evidence.screenshotId === "string") return { type: "screenshot", screenshotId: evidence.screenshotId, ...(typeof evidence.url === "string" ? { url: evidence.url } : {}) };
  return null;
}

function sanitizeFinding(finding: Finding): Finding {
  const source: FindingSource = finding.source === "subagent" ? "subagent" : "main";
  const sanitized = {
    ...finding,
    source,
    evidence: Array.isArray(finding.evidence)
      ? finding.evidence.map(sanitizeEvidence).filter((item): item is Finding["evidence"][number] => Boolean(item))
      : []
  };
  return sanitized;
}

function cloneFinding(finding: Finding): Finding {
  return sanitizeFinding({ ...finding, evidence: Array.isArray(finding.evidence) ? finding.evidence.map((item) => ({ ...item })) : [] });
}

function mergeEvidence(existing: Finding["evidence"], incoming: Finding["evidence"]) {
  const merged = existing.map((item) => ({ ...item }));
  for (const item of incoming) {
    const key = item.type === "quote"
      ? `quote:${item.quote}`
      : item.type === "tool"
        ? `tool:${item.toolCallId}:${item.toolName}`
        : item.type === "request"
          ? `request:${item.requestRef}`
          : `screenshot:${item.screenshotId}`;
    const duplicate = merged.some((candidate) => {
      const candidateKey = candidate.type === "quote"
        ? `quote:${candidate.quote}`
        : candidate.type === "tool"
          ? `tool:${candidate.toolCallId}:${candidate.toolName}`
          : candidate.type === "request"
            ? `request:${candidate.requestRef}`
            : `screenshot:${candidate.screenshotId}`;
      return candidateKey === key;
    });
    if (!duplicate) merged.push({ ...item });
  }
  return merged;
}

export class EvidenceStore {
  private findings: Finding[] | undefined;
  private emitter?: (event: RiftxEvent) => void;

  constructor(private readonly sessionId: string, private readonly root: string, emitter?: (event: RiftxEvent) => void) {
    this.emitter = emitter;
  }

  setEmitter(emitter: (event: RiftxEvent) => void) {
    this.emitter = emitter;
  }

  private get directory() {
    return join(this.root, this.sessionId);
  }

  private get filePath() {
    return join(this.directory, "findings.json");
  }

  private async load() {
    if (this.findings) return this.findings;
    // Corrupt or missing files start empty (corrupt ones are backed up by
    // readJsonStore); any other I/O error surfaces instead of silently
    // wiping findings.
    const parsed = await readJsonStore<FindingFile>(this.filePath);
    const rawFindings = Array.isArray(parsed?.findings) ? parsed!.findings.filter((finding) => finding?.id && finding?.title && finding?.asset) : [];
    this.findings = rawFindings.map(cloneFinding);
    // One-time migration: legacy fields are dropped from disk on load.
    if (JSON.stringify(rawFindings) !== JSON.stringify(this.findings)) await this.persist();
    return this.findings;
  }

  private async persist() {
    await mkdir(this.directory, { recursive: true, mode: 0o700 });
    await writeJsonStoreAtomic(this.filePath, { findings: this.findings ?? [] });
  }

  private readonly locked = createSerializer();
  /** Terminal state after permanent removal: no later write may resurrect the store. */
  private deleted = false;

  async list() {
    return this.locked(async () => this.deleted ? [] : (await this.load()).map(cloneFinding));
  }

  async upsert(input: FindingInput, source: FindingSource, subagentId?: string) {
    return this.locked(async () => {
      // Checked inside the locked operation: an upsert queued behind a
      // pending remove() must be refused when it finally runs, not recreate
      // findings.json for a session whose evidence was permanently deleted.
      if (this.deleted) throw new Error("This session's evidence has been permanently deleted; new findings are refused.");
      const findings = await this.load();
      const timestamp = now();
      const evidence = input.evidence.map(sanitizeEvidence).filter((item): item is Finding["evidence"][number] => Boolean(item));
      const existing = findings.find((finding) => normalize(finding.asset) === normalize(input.asset) && normalize(finding.title) === normalize(input.title));
      if (existing) {
        existing.title = input.title.trim();
        existing.asset = input.asset.trim();
        existing.confidence = input.confidence;
        existing.impact = input.impact.trim();
        existing.reproduction = input.reproduction.trim();
        existing.evidence = mergeEvidence(existing.evidence, evidence);
        if (existing.source === undefined) existing.source = source;
        if (existing.subagentId === undefined && subagentId !== undefined) existing.subagentId = subagentId;
        existing.updatedAt = timestamp;
        await this.persist();
        const finding = cloneFinding(existing);
        this.emitter?.({ type: "finding", finding });
        return finding;
      }
      const finding: Finding = {
        id: randomUUID(),
        title: input.title.trim(),
        asset: input.asset.trim(),
        confidence: input.confidence,
        status: "open",
        impact: input.impact.trim(),
        reproduction: input.reproduction.trim(),
        evidence: mergeEvidence([], evidence),
        source,
        subagentId,
        createdAt: timestamp,
        updatedAt: timestamp
      };
      findings.push(finding);
      await this.persist();
      const result = cloneFinding(finding);
      this.emitter?.({ type: "finding", finding: result });
      return result;
    });
  }

  async patch(id: string, patch: Omit<FindingPatch, "id">) {
    return this.locked(async () => {
      if (this.deleted) return null;
      const finding = (await this.load()).find((item) => item.id === id);
      if (!finding) return null;
      if (patch.confidence !== undefined) finding.confidence = patch.confidence;
      if (patch.status !== undefined) finding.status = patch.status;
      finding.updatedAt = now();
      await this.persist();
      const findingPatch: FindingPatch = { id, confidence: finding.confidence, status: finding.status, updatedAt: finding.updatedAt };
      this.emitter?.({ type: "findingPatch", findingPatch });
      return cloneFinding(finding);
    });
  }

  async remove() {
    return this.locked(async () => {
      await rm(this.directory, { recursive: true, force: true });
      // Commit the terminal state only after the removal actually succeeded:
      // a failed rm (permissions, transient I/O) must leave the store fully
      // usable instead of wedged in a half-deleted state.
      this.deleted = true;
      this.findings = [];
    });
  }
}

const stores = new Map<string, EvidenceStore>();
const EVIDENCE_STORE_CACHE_LIMIT = 256;
/**
 * Per-session deletion state, process-wide. `inflight` counts removeEvidence()
 * calls currently running for the id; `deleted` is the terminal state once any
 * of them succeeded. getEvidenceStore refuses an id while a deletion is
 * in-flight OR terminal: the per-instance `deleted` flag only covers writes
 * queued on the same instance, and once the cache entry is dropped a fresh
 * store would happily recreate findings.json.
 */
type DeletionState = { inflight: number; deleted: boolean };
const deletionStates = new Map<string, DeletionState>();

export function getEvidenceStore(sessionId: string, root: string, emitter?: (event: RiftxEvent) => void) {
  if (deletionStates.has(sessionId)) {
    throw new Error("This session's evidence has been permanently deleted; no store can be created for it.");
  }
  const existing = stores.get(sessionId);
  if (existing) {
    if (emitter) existing.setEmitter(emitter);
    return existing;
  }
  const store = new EvidenceStore(sessionId, root, emitter);
  stores.set(sessionId, store);
  while (stores.size > EVIDENCE_STORE_CACHE_LIMIT) stores.delete(stores.keys().next().value!);
  return store;
}

export async function removeEvidence(sessionId: string, root: string) {
  // Register FIRST, before any store is fetched or removed: from this point
  // on, getEvidenceStore refuses the id entirely, so a request that validated
  // before the deletion cannot obtain a fresh writable instance mid-flight.
  let state = deletionStates.get(sessionId);
  if (!state) {
    state = { inflight: 0, deleted: false };
    deletionStates.set(sessionId, state);
  }
  state.inflight += 1;
  try {
    const store = stores.get(sessionId);
    if (store) {
      // Route through the cached store's serialization chain: a queued upsert
      // that finishes after a bare rm would re-create findings.json.
      await store.remove();
    } else {
      await rm(join(root, sessionId), { recursive: true, force: true });
    }
    // Any success commits the terminal state — including for concurrent
    // callers still winding down: a later failure must not roll back a
    // deletion that already happened.
    state.deleted = true;
    stores.delete(sessionId);
  } finally {
    state.inflight -= 1;
    // The guard is only lifted when EVERY in-flight deletion has finished and
    // none of them succeeded — one failed request rolling back while another
    // is still deleting would re-open the resurrection window.
    if (state.inflight === 0 && !state.deleted) deletionStates.delete(sessionId);
  }
}
