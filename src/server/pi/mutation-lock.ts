export class MutationLock {
  private active = false;
  private readonly waiters: Array<{ resolve: (release: () => void) => void; reject: (error: unknown) => void; signal?: AbortSignal; settled: boolean; cleanup: () => void }> = [];

  acquire(signal?: AbortSignal) {
    if (signal?.aborted) return Promise.reject(new Error("Mutation was aborted"));
    if (!this.active) {
      this.active = true;
      return Promise.resolve(() => this.release());
    }
    return new Promise<() => void>((resolve, reject) => {
      const waiter: { resolve: (release: () => void) => void; reject: (error: unknown) => void; signal?: AbortSignal; settled: boolean; cleanup: () => void } = { resolve, reject, signal, settled: false, cleanup: () => undefined };
      this.waiters.push(waiter);
      const onAbort = () => {
        const index = this.waiters.indexOf(waiter);
        if (index >= 0) this.waiters.splice(index, 1);
        if (waiter.settled || index < 0) return;
        waiter.settled = true;
        reject(new Error("Mutation was aborted"));
      };
      waiter.cleanup = () => signal?.removeEventListener("abort", onAbort);
      signal?.addEventListener("abort", onAbort, { once: true });
    });
  }

  private release() {
    const next = this.waiters.shift();
    if (!next) {
      this.active = false;
      return;
    }
    if (next.signal?.aborted) {
      next.cleanup();
      next.settled = true;
      next.reject(new Error("Mutation was aborted"));
      this.release();
      return;
    }
    next.cleanup();
    next.settled = true;
    next.resolve(() => this.release());
  }
}
