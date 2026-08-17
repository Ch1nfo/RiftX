import type { ApprovalRequest } from "@/lib/types";
import type { ApprovalMode } from "@/lib/types";

type PendingApproval = {
  request: ApprovalRequest;
  resolve: (approved: boolean) => void;
  timer: ReturnType<typeof setTimeout>;
};

export class ApprovalGate {
  private pending = new Map<string, PendingApproval>();
  private listeners = new Set<(request: ApprovalRequest) => void>();
  private mode: ApprovalMode;
  private taskBypass = false;

  constructor(mode: ApprovalMode = "request") {
    this.mode = mode;
  }

  get approvalMode() {
    return this.mode;
  }

  setMode(mode: ApprovalMode) {
    this.mode = mode;
    this.taskBypass = false;
    if (mode === "full") {
      for (const id of this.pending.keys()) this.decide(id, true);
    } else if (this.pending.size > 0) {
      this.rejectAll();
    }
  }

  beginTask() {
    this.taskBypass = false;
  }

  allowForTask() {
    this.taskBypass = true;
  }

  shouldBypass() {
    return this.mode === "full" || this.taskBypass;
  }

  onRequest(listener: (request: ApprovalRequest) => void) {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  waitForApproval(request: ApprovalRequest, timeoutMs = 120_000): Promise<boolean> {
    return new Promise((resolve) => {
      const timer = setTimeout(() => {
        this.pending.delete(request.id);
        resolve(false);
      }, timeoutMs);
      this.pending.set(request.id, { request, resolve, timer });
      for (const listener of this.listeners) listener(request);
    });
  }

  decide(id: string, approved: boolean) {
    const pending = this.pending.get(id);
    if (!pending) return false;
    clearTimeout(pending.timer);
    this.pending.delete(id);
    pending.resolve(approved);
    return true;
  }

  pendingRequests() {
    return [...this.pending.values()].map(({ request }) => request);
  }

  rejectAll() {
    for (const id of this.pending.keys()) this.decide(id, false);
  }
}
