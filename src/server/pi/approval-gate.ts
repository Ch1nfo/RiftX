import type { ApprovalRequest } from "@/lib/types";
import type { ApprovalMode } from "@/lib/types";

type ApprovalDecision = { approved: boolean; task: boolean };

type PendingApproval = {
  request: ApprovalRequest;
  resolve: (decision: ApprovalDecision) => void;
  timer: ReturnType<typeof setTimeout>;
  abortCleanup?: () => void;
};

type ApprovalInput = Pick<ApprovalRequest, "toolName" | "input">;
type ApprovalDecisionListener = (request: ApprovalRequest, approved: boolean) => void;

function stableSerialize(value: unknown): string {
  if (value === null || typeof value !== "object") return JSON.stringify(value) ?? String(value);
  if (Array.isArray(value)) return `[${value.map(stableSerialize).join(",")}]`;
  const entries = Object.entries(value as Record<string, unknown>).sort(([left], [right]) => left.localeCompare(right));
  return `{${entries.map(([key, entry]) => `${JSON.stringify(key)}:${stableSerialize(entry)}`).join(",")}}`;
}

function approvalKey(request: ApprovalInput) {
  return `${request.toolName}:${stableSerialize(request.input)}`;
}

export class ApprovalGate {
  private pending = new Map<string, PendingApproval>();
  private decisionListeners = new Set<ApprovalDecisionListener>();
  private mode: ApprovalMode;
  private taskBypass = new Set<string>();

  constructor(mode: ApprovalMode = "request") {
    this.mode = mode;
  }

  get approvalMode() {
    return this.mode;
  }

  setMode(mode: ApprovalMode) {
    this.mode = mode;
    this.taskBypass.clear();
    if (mode === "full") {
      for (const id of this.pending.keys()) this.decide(id, true);
    }
  }

  beginTask() {
    this.taskBypass.clear();
  }

  allowForTask(request: ApprovalInput) {
    this.taskBypass.add(approvalKey(request));
  }

  shouldBypass(request: ApprovalInput) {
    return this.mode === "full" || this.taskBypass.has(approvalKey(request));
  }

  onDecision(listener: ApprovalDecisionListener) {
    this.decisionListeners.add(listener);
    return () => this.decisionListeners.delete(listener);
  }

  waitForApproval(request: ApprovalRequest, timeoutMs = 120_000, signal?: AbortSignal): Promise<ApprovalDecision> {
    return new Promise((resolve) => {
      const denied: ApprovalDecision = { approved: false, task: false };
      if (signal?.aborted) {
        for (const listener of this.decisionListeners) listener(request, false);
        resolve(denied);
        return;
      }
      const timer = setTimeout(() => {
        this.pending.delete(request.id);
        signal?.removeEventListener("abort", onAbort);
        for (const listener of this.decisionListeners) listener(request, false);
        resolve(denied);
      }, timeoutMs);
      const onAbort = () => this.decide(request.id, false);
      signal?.addEventListener("abort", onAbort, { once: true });
      this.pending.set(request.id, { request, resolve, timer, abortCleanup: () => signal?.removeEventListener("abort", onAbort) });
    });
  }

  decide(id: string, approved: boolean, task = false) {
    const pending = this.pending.get(id);
    if (!pending) return false;
    clearTimeout(pending.timer);
    pending.abortCleanup?.();
    this.pending.delete(id);
    for (const listener of this.decisionListeners) listener(pending.request, approved);
    pending.resolve({ approved, task });
    return true;
  }

  pendingRequests() {
    return [...this.pending.values()].map(({ request }) => request);
  }

  rejectAll() {
    for (const id of this.pending.keys()) this.decide(id, false);
  }
}
