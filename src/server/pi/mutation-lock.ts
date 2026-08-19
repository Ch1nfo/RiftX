export class MutationLock {
  private active = false;
  private readonly waiters: Array<{ resolve: (release: () => void) => void; reject: (error: unknown) => void; signal?: AbortSignal; settled: boolean }> = [];

  acquire(signal?: AbortSignal) {
    if (signal?.aborted) return Promise.reject(new Error("Mutation was aborted"));
    if (!this.active) {
      this.active = true;
      return Promise.resolve(() => this.release());
    }
    return new Promise<() => void>((resolve, reject) => {
      const waiter: { resolve: (release: () => void) => void; reject: (error: unknown) => void; signal?: AbortSignal; settled: boolean } = { resolve, reject, signal, settled: false };
      this.waiters.push(waiter);
      signal?.addEventListener("abort", () => {
        const index = this.waiters.indexOf(waiter);
        if (index >= 0) this.waiters.splice(index, 1);
        if (waiter.settled || index < 0) return;
        waiter.settled = true;
        reject(new Error("Mutation was aborted"));
      }, { once: true });
    });
  }

  private release() {
    const next = this.waiters.shift();
    if (!next) {
      this.active = false;
      return;
    }
    if (next.signal?.aborted) {
      next.settled = true;
      next.reject(new Error("Mutation was aborted"));
      this.release();
      return;
    }
    next.settled = true;
    next.resolve(() => this.release());
  }
}
