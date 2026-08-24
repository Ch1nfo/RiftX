import { mkdir, rm } from "node:fs/promises";
import { join } from "node:path";
import { randomUUID } from "node:crypto";
import type { Finding, FindingInput, FindingPatch, FindingSource, RiftxEvent } from "@/lib/types";
import { readJsonStore, writeJsonStoreAtomic } from "@/server/json-store";

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
  private operation = Promise.resolve();
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

  private async locked<T>(operation: () => Promise<T>) {
    const result = this.operation.then(operation);
    this.operation = result.then(() => undefined, () => undefined);
    return result;
  }

  async list() {
    return this.locked(async () => (await this.load()).map(cloneFinding));
  }

  async upsert(input: FindingInput, source: FindingSource, subagentId?: string) {
    return this.locked(async () => {
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
      this.findings = [];
      await rm(this.directory, { recursive: true, force: true });
    });
  }
}

const stores = new Map<string, EvidenceStore>();
const EVIDENCE_STORE_CACHE_LIMIT = 256;

export function getEvidenceStore(sessionId: string, root: string, emitter?: (event: RiftxEvent) => void) {
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
  stores.delete(sessionId);
  await rm(join(root, sessionId), { recursive: true, force: true });
}
