type Waiter = {
  resolve: (release: () => void) => void;
  reject: (error: unknown) => void;
  signal?: AbortSignal;
  onStart?: () => void;
  settled: boolean;
  cleanup: () => void;
};

function normalizeLimit(value: number) {
  return Math.max(1, Math.round(Number.isFinite(value) ? value : 1));
}

export class BashConcurrency {
  private limit: number;
  private active = 0;
  private readonly waiters: Waiter[] = [];

  constructor(limit: number) {
    this.limit = normalizeLimit(limit);
  }

  get maxConcurrent() {
    return this.limit;
  }

  get running() {
    return this.active;
  }

  setLimit(value: number) {
    this.limit = normalizeLimit(value);
    this.pump();
  }

  acquire(signal?: AbortSignal, onStart?: () => void) {
    if (signal?.aborted) return Promise.reject(new Error("Bash execution was aborted"));
    return new Promise<() => void>((resolve, reject) => {
      const waiter: Waiter = { resolve, reject, signal, onStart, settled: false, cleanup: () => undefined };
      const onAbort = () => {
        const index = this.waiters.indexOf(waiter);
        if (index >= 0) this.waiters.splice(index, 1);
        if (waiter.settled || index < 0) return;
        waiter.settled = true;
        reject(new Error("Bash execution was aborted"));
      };
      waiter.cleanup = () => signal?.removeEventListener("abort", onAbort);
      signal?.addEventListener("abort", onAbort, { once: true });
      this.waiters.push(waiter);
      this.pump();
    });
  }

  private pump() {
    while (this.active < this.limit && this.waiters.length) {
      const waiter = this.waiters.shift()!;
      if (waiter.signal?.aborted) {
        waiter.cleanup();
        waiter.settled = true;
        waiter.reject(new Error("Bash execution was aborted"));
        continue;
      }
      waiter.cleanup();
      waiter.settled = true;
      this.active += 1;
      try {
        waiter.onStart?.();
      } catch {
        // Status reporting must never prevent the command from running.
      }
      let released = false;
      waiter.resolve(() => {
        if (released) return;
        released = true;
        this.active -= 1;
        this.pump();
      });
    }
  }
}
