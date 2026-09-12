import type { RecordedRequest } from "../types";
import { randomUUID } from "node:crypto";
import { mkdir, readFile } from "node:fs/promises";
import { join } from "node:path";
import { writeJsonStoreAtomic } from "@/server/json-store";

export const MAX_CAPTURE_BYTES = 256 * 1024;

/** Benchmark target traffic is evidence, including challenge credentials. */
export function boundedBody(body: string | null | undefined) {
  if (body == null) return undefined;
  const bytes = Buffer.from(body);
  return bytes.length > MAX_CAPTURE_BYTES ? `${bytes.subarray(0, MAX_CAPTURE_BYTES).toString("utf8")}\n[truncated]` : body;
}

export class RequestStore {
  private records = new Map<string, RecordedRequest>();
  private counter = 0;
  private readonly runId = randomUUID();

  constructor(private readonly directory?: string) {}

  start(input: Omit<RecordedRequest, "ref">) {
    const ref = `r-${this.runId}-${++this.counter}`;
    const record = { ...input, ref };
    this.records.set(ref, record);
    if (this.records.size > 200) this.records.delete(this.records.keys().next().value!);
    return record;
  }

  update(ref: string, patch: Partial<RecordedRequest>) {
    const current = this.records.get(ref);
    if (current) this.records.set(ref, { ...current, ...patch });
  }

  list() {
    return [...this.records.values()];
  }

  get(ref: string) {
    return this.records.get(ref);
  }

  /** Explicit inspection pins a point-in-time snapshot, outside the 200-entry
   * rolling log. Unique refs survive browser restart and concurrent workers. */
  async snapshot(ref: string) {
    if (!/^r-[a-f0-9-]{36}-\d+$/.test(ref)) throw new Error(`Unknown request ref ${ref}`);
    const artifactPath = this.directory ? join(this.directory, `${ref}.json`) : undefined;
    let record = this.records.get(ref);
    if (!record && artifactPath) {
      try { record = JSON.parse(await readFile(artifactPath, "utf8")) as RecordedRequest; }
      catch (error) { if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error; }
    }
    if (!record) throw new Error(`Unknown request ref ${ref}`);
    if (artifactPath && this.records.has(ref)) {
      await mkdir(this.directory!, { recursive: true, mode: 0o700 });
      await writeJsonStoreAtomic(artifactPath, record);
    }
    return { ...record, artifactPath };
  }

  clear() {
    this.records.clear();
  }
}
