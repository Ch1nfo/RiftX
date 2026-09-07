/** Per-connected-server call isolation: bounded concurrency, deadline, and a short circuit breaker. */

import { raceWithAbort } from "@/server/deadline";

export const MCP_MAX_CONCURRENT_CALLS = 2;
export const MCP_CALL_TIMEOUT_MS = 120_000;
export const MCP_CIRCUIT_FAILURE_THRESHOLD = 3;
export const MCP_CIRCUIT_COOLDOWN_MS = 30_000;

type Waiter = {
  resolve: (release: () => void) => void;
  reject: (error: Error) => void;
  signal?: AbortSignal;
  onAbort?: () => void;
};

class Semaphore {
  private active = 0;
  private readonly queue: Waiter[] = [];

  constructor(private readonly limit: number) {}

  acquire(signal?: AbortSignal): Promise<() => void> {
    if (signal?.aborted) return Promise.reject(new Error("MCP tool call aborted while queued"));
    if (this.active < this.limit) {
      this.active += 1;
      return Promise.resolve(this.releaseOnce());
    }
    return new Promise((resolve, reject) => {
      const waiter: Waiter = { resolve, reject, signal };
      waiter.onAbort = () => {
        const index = this.queue.indexOf(waiter);
        if (index >= 0) this.queue.splice(index, 1);
        reject(signal?.reason instanceof Error ? signal.reason : new Error("MCP tool call aborted while queued"));
      };
      signal?.addEventListener("abort", waiter.onAbort, { once: true });
      this.queue.push(waiter);
    });
  }

  private releaseOnce() {
    let released = false;
    return () => {
      if (released) return;
      released = true;
      const next = this.queue.shift();
      if (next) {
        next.signal?.removeEventListener("abort", next.onAbort!);
        next.resolve(this.releaseOnce());
      } else {
        this.active -= 1;
      }
    };
  }
}

export class McpCallGuard {
  private readonly semaphore: Semaphore;
  private consecutiveFailures = 0;
  private openUntil = 0;
  private probing = false;

  constructor(private readonly serverName: string, private readonly options: {
    maxConcurrent?: number;
    timeoutMs?: number;
    failureThreshold?: number;
    cooldownMs?: number;
    now?: () => number;
    /** Retire the connection when a call or queue wait exceeds its deadline. */
    onTimeout?: () => void;
  } = {}) {
    this.semaphore = new Semaphore(Math.max(1, options.maxConcurrent ?? MCP_MAX_CONCURRENT_CALLS));
  }

  async run<T>(call: (signal: AbortSignal) => Promise<T>, signal?: AbortSignal): Promise<T> {
    const now = this.options.now ?? Date.now;
    const threshold = this.options.failureThreshold ?? MCP_CIRCUIT_FAILURE_THRESHOLD;
    const cooldownMs = this.options.cooldownMs ?? MCP_CIRCUIT_COOLDOWN_MS;
    if (this.openUntil > now()) {
      throw new Error(`MCP server "${this.serverName}" circuit is open; retry after ${Math.max(1, this.openUntil - now())}ms`);
    }

    const controller = new AbortController();
    const timeoutMs = this.options.timeoutMs ?? MCP_CALL_TIMEOUT_MS;
    let timedOut = false;
    let enteredCall = false;
    let rawSettled = false;
    let release: (() => void) | undefined;
    let raw: Promise<T> | undefined;
    const onAbort = () => controller.abort(signal?.reason);
    signal?.addEventListener("abort", onAbort, { once: true });
    const timer = setTimeout(() => {
      timedOut = true;
      controller.abort(new Error(`MCP tool call timed out after ${timeoutMs}ms`));
      try { this.options.onTimeout?.(); } catch { /* retirement is best-effort */ }
    }, timeoutMs);

    try {
      // Queue wait is part of the same deadline. Otherwise ignored calls can
      // retain every slot forever and the next request never even starts its
      // timeout or reaches the circuit breaker.
      release = await this.semaphore.acquire(controller.signal);
      if (this.openUntil > now()) {
        release();
        release = undefined;
        throw new Error(`MCP server "${this.serverName}" circuit is open; retry after ${Math.max(1, this.openUntil - now())}ms`);
      }
      if (this.openUntil) {
        this.openUntil = 0;
        this.consecutiveFailures = 0;
        this.probing = true;
      }
      enteredCall = true;
      raw = Promise.resolve().then(() => {
        if (controller.signal.aborted) throw controller.signal.reason instanceof Error ? controller.signal.reason : new Error("MCP tool call aborted");
        return call(controller.signal);
      }).then(
        (value) => { rawSettled = true; return value; },
        (error) => { rawSettled = true; throw error; }
      );
      const value = await raceWithAbort(raw, controller.signal, `MCP tool call on server "${this.serverName}" aborted`);
      this.consecutiveFailures = 0;
      return value;
    } catch (error) {
      if (!signal?.aborted && (enteredCall || timedOut)) {
        // A failing half-open probe re-trips immediately: hammering a dead
        // server with threshold more full calls (each up to the timeout)
        // before re-opening buys nothing.
        if (this.probing) {
          this.consecutiveFailures = threshold;
          this.openUntil = now() + cooldownMs;
        } else {
          this.consecutiveFailures += 1;
          if (this.consecutiveFailures >= threshold) this.openUntil = now() + cooldownMs;
        }
      }
      throw error;
    } finally {
      this.probing = false;
      clearTimeout(timer);
      signal?.removeEventListener("abort", onAbort);
      // A server that ignores AbortSignal still occupies its slot. Releasing
      // immediately on timeout would let unbounded zombie calls accumulate;
      // queued callers still have their own total deadline and return.
      if (release) {
        if (!raw || rawSettled) release();
        else void raw.then(release, release);
      }
    }
  }
}
