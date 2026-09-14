import { AsyncLocalStorage } from "node:async_hooks";
import type { BenchmarkLedger, ChallengeOwner, ChallengeState } from "./ledger";

export type AttemptFence = Readonly<{ uniqueCode: string; owner: Exclude<ChallengeOwner, null>; attemptId: string; containerEpoch: number }>;
export const attemptContext = new AsyncLocalStorage<AttemptFence>();
export class StaleAttemptError extends Error {
  readonly code = "STALE_ATTEMPT";
  constructor() { super("STALE_ATTEMPT: the challenge, worker, attempt or container changed. This operation was not authorized for the current attempt."); }
}
export function captureFence(challenge: ChallengeState | undefined, owner?: Exclude<ChallengeOwner, null>): AttemptFence | undefined {
  const worker = owner ?? challenge?.owner;
  return challenge?.attemptId && worker ? Object.freeze({ uniqueCode: challenge.uniqueCode, owner: worker,
    attemptId: challenge.attemptId, containerEpoch: challenge.containerEpoch ?? 0 }) : undefined;
}
export function assertFence(challenge: ChallengeState | undefined, fence: AttemptFence, allowReleased = false) {
  if (!challenge || challenge.uniqueCode !== fence.uniqueCode || challenge.attemptId !== fence.attemptId
    || challenge.containerEpoch !== fence.containerEpoch
    || (!allowReleased && (challenge.owner !== fence.owner || challenge.status !== "running"))) throw new StaleAttemptError();
}
export type ExecutableTool = { name: string; execute?: (id: string, params: unknown, signal?: AbortSignal, ...rest: unknown[]) => Promise<unknown> };
const coordinatorActions = new Set(["sync", "status", "acquire", "publish_intel"]);
const readOnlyTools = new Set(["read", "grep", "find", "ls", "benchmark_tool_catalog", "benchmark_skill_hint"]);
export function scopedControl(params: unknown) {
  return !coordinatorActions.has((params as { action?: string })?.action ?? "");
}

/** Per-runtime binding, updated only by an explicit acquisition or runtime recovery.
 * Never replace a stale worker's identity with the latest ledger identity. */
export class BenchmarkExecutionBinding {
  private fence?: AttemptFence;
  private revoked = false;
  private unsubscribe?: () => void;
  constructor(private readonly ledger: BenchmarkLedger, private readonly owner: Exclude<ChallengeOwner, null>, private readonly child = false) {
    this.bind(ledger.budgetForOwner(owner)?.challenge);
  }
  bind(challenge?: ChallengeState) {
    this.unsubscribe?.();
    this.fence = captureFence(challenge, this.owner);
    this.revoked = false;
    this.unsubscribe = this.fence ? this.ledger.onStateChange(() => {
      try { assertFence(this.ledger.getChallenge(this.fence!.uniqueCode), this.fence!); }
      catch { this.revoked = true; }
    }) : undefined;
  }
  dispose() { this.unsubscribe?.(); this.revoked = true; }
  snapshot() { return this.fence; }
  validate(fence = this.fence) { if (this.revoked) throw new StaleAttemptError(); if (fence) assertFence(this.ledger.getChallenge(fence.uniqueCode), fence); }

  /** Innermost check: runs after workspace, file and concurrency locks. */
  guard(tool: ExecutableTool) {
    if (!tool.execute || tool.name === "assign_benchmark_challenge") return;
    const execute = tool.execute.bind(tool);
    tool.execute = async (id, params, signal, ...rest) => {
      if (!readOnlyTools.has(tool.name)) signal?.throwIfAborted();
      const fence = attemptContext.getStore();
      if (fence && (tool.name !== "benchmark_control" || scopedControl(params))) this.validate(fence);
      return execute(id, params, signal, ...rest);
    };
  }

  /** Outermost snapshot: queued operations retain the identity they started with. */
  install(tool: ExecutableTool) {
    if (!tool.execute || tool.name === "assign_benchmark_challenge") return;
    const execute = tool.execute.bind(tool);
    tool.execute = async (id, params, signal, ...rest) => {
      const fence = this.fence;
      const scoped = tool.name !== "benchmark_control" || scopedControl(params);
      // Coordinator controls can reconcile/reacquire, but child workers cannot adopt a new generation.
      if (!scoped) return execute(id, params, signal, ...rest);
      if (!fence) {
        if (this.child || tool.name === "benchmark_control") throw new StaleAttemptError();
        return execute(id, params, signal, ...rest);
      }
      const controller = new AbortController();
      const validate = () => this.validate(fence);
      const unsubscribe = this.ledger.onStateChange(() => {
        try { validate(); } catch (error) { if (!readOnlyTools.has(tool.name) && tool.name !== "benchmark_control") controller.abort(error); }
      });
      const input = tool.name === "benchmark_control" ? { ...(params as object),
        uniqueCode: (params as { uniqueCode?: string }).uniqueCode ?? fence.uniqueCode,
        attemptId: (params as { attemptId?: string }).attemptId ?? fence.attemptId,
        containerEpoch: (params as { containerEpoch?: number }).containerEpoch ?? fence.containerEpoch } : params;
      try {
        validate();
        return await attemptContext.run(fence, () => execute(id, input,
          signal ? AbortSignal.any([signal, controller.signal]) : controller.signal, ...rest));
      } catch (error) {
        const stale = error instanceof StaleAttemptError || controller.signal.reason instanceof StaleAttemptError;
        if (stale) await this.ledger.recordAttemptIncident(fence, "harness_stale_state", "Tool rejected after ownership or generation changed", "fencing", true);
        throw stale ? new StaleAttemptError() : error;
      } finally { unsubscribe(); }
    };
  }
}
