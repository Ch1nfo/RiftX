/** Per-connected-server call isolation: bounded concurrency, deadline, and a short circuit breaker. */

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
        reject(new Error("MCP tool call aborted while queued"));
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
  } = {}) {
    this.semaphore = new Semaphore(Math.max(1, options.maxConcurrent ?? MCP_MAX_CONCURRENT_CALLS));
  }

  async run<T>(call: (signal: AbortSignal) => Promise<T>, signal?: AbortSignal): Promise<T> {
    const release = await this.semaphore.acquire(signal);
    // The signal can abort in the microtask gap after an immediately granted
    // acquire and before the listener below is installed.
    if (signal?.aborted) {
      release();
      throw signal.reason instanceof Error ? signal.reason : new Error("MCP tool call aborted");
    }
    const now = this.options.now ?? Date.now;
    const threshold = this.options.failureThreshold ?? MCP_CIRCUIT_FAILURE_THRESHOLD;
    const cooldownMs = this.options.cooldownMs ?? MCP_CIRCUIT_COOLDOWN_MS;
    if (this.openUntil > now()) {
      release();
      throw new Error(`MCP server "${this.serverName}" circuit is open; retry after ${Math.max(1, this.openUntil - now())}ms`);
    }
    if (this.openUntil) {
      this.openUntil = 0;
      this.consecutiveFailures = 0;
      this.probing = true;
    }

    const controller = new AbortController();
    const timeoutMs = this.options.timeoutMs ?? MCP_CALL_TIMEOUT_MS;
    let timedOut = false;
    let rawSettled = false;
    const onAbort = () => controller.abort(signal?.reason);
    signal?.addEventListener("abort", onAbort, { once: true });
    const timer = setTimeout(() => {
      timedOut = true;
      controller.abort(new Error(`MCP tool call timed out after ${timeoutMs}ms`));
    }, timeoutMs);
    const raw = Promise.resolve().then(() => {
      if (controller.signal.aborted) throw controller.signal.reason instanceof Error ? controller.signal.reason : new Error("MCP tool call aborted");
      return call(controller.signal);
    }).then(
      (value) => { rawSettled = true; return value; },
      (error) => { rawSettled = true; throw error; }
    );
    const deadline = new Promise<never>((_resolve, reject) => {
      const onTimeout = () => reject(new Error(`MCP tool call on server "${this.serverName}" timed out after ${timeoutMs}ms`));
      const onControllerAbort = () => {
        if (timedOut) onTimeout();
        else reject(signal?.reason instanceof Error ? signal.reason : new Error("MCP tool call aborted"));
      };
      if (controller.signal.aborted) onControllerAbort();
      else controller.signal.addEventListener("abort", onControllerAbort, { once: true });
    });

    try {
      const value = await Promise.race([raw, deadline]);
      this.consecutiveFailures = 0;
      return value;
    } catch (error) {
      if (!signal?.aborted) {
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
      // immediately on timeout would let unbounded zombie calls accumulate.
      if (rawSettled) release();
      else void raw.then(release, release);
    }
  }
}
