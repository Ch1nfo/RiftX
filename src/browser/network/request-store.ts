import type { RecordedRequest } from "../types";

const SENSITIVE = /authorization|cookie|set-cookie|token|secret|password|api[-_]?key/i;
const MAX_BODY = 64 * 1024;

export function redactHeaders(headers: Record<string, string>) {
  return Object.fromEntries(Object.entries(headers).map(([key, value]) => [key, SENSITIVE.test(key) ? "[REDACTED]" : value]));
}

export function redactBody(body: string | null | undefined) {
  if (!body) return undefined;
  if (SENSITIVE.test(body)) return "[REDACTED]";
  return body.length > MAX_BODY ? `${body.slice(0, MAX_BODY)}\n[truncated]` : body;
}

export class RequestStore {
  private records = new Map<string, RecordedRequest>();
  private counter = 0;

  start(input: Omit<RecordedRequest, "ref">) {
    const ref = `r${++this.counter}`;
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

  clear() {
    this.records.clear();
  }
}
