type LockMode = "exclusive" | "shared";

type Waiter = {
  mode: LockMode;
  resolve: (release: () => void) => void;
  reject: (error: unknown) => void;
  signal?: AbortSignal;
  settled: boolean;
  cleanup: () => void;
};

/** Coordinates exclusive file mutations with concurrent, non-exclusive Bash work. */
export class MutationLock {
  private exclusiveActive = false;
  private sharedActive = 0;
  private readonly waiters: Waiter[] = [];

  acquire(signal?: AbortSignal) {
    return this.enqueue("exclusive", signal, "Mutation was aborted");
  }

  acquireShared(signal?: AbortSignal) {
    return this.enqueue("shared", signal, "Bash execution was aborted");
  }

  private enqueue(mode: LockMode, signal: AbortSignal | undefined, abortMessage: string) {
    if (signal?.aborted) return Promise.reject(new Error(abortMessage));
    return new Promise<() => void>((resolve, reject) => {
      const waiter: Waiter = { mode, resolve, reject, signal, settled: false, cleanup: () => undefined };
      const onAbort = () => {
        const index = this.waiters.indexOf(waiter);
        if (index >= 0) this.waiters.splice(index, 1);
        if (waiter.settled || index < 0) return;
        waiter.settled = true;
        reject(new Error(abortMessage));
        this.pump();
      };
      waiter.cleanup = () => signal?.removeEventListener("abort", onAbort);
      signal?.addEventListener("abort", onAbort, { once: true });
      this.waiters.push(waiter);
      this.pump();
    });
  }

  private pump() {
    if (this.exclusiveActive) return;

    if (this.sharedActive > 0) {
      while (this.waiters[0]?.mode === "shared") this.start(this.waiters.shift()!);
      return;
    }

    const first = this.waiters.shift();
    if (!first) return;
    if (first.mode === "exclusive") {
      this.start(first);
      return;
    }

    this.start(first);
    while (this.waiters[0]?.mode === "shared") this.start(this.waiters.shift()!);
  }

  private start(waiter: Waiter) {
    if (waiter.signal?.aborted) {
      waiter.cleanup();
      waiter.settled = true;
      waiter.reject(new Error(waiter.mode === "shared" ? "Bash execution was aborted" : "Mutation was aborted"));
      return;
    }
    waiter.cleanup();
    waiter.settled = true;
    if (waiter.mode === "exclusive") this.exclusiveActive = true;
    else this.sharedActive += 1;
    let released = false;
    waiter.resolve(() => {
      if (released) return;
      released = true;
      if (waiter.mode === "exclusive") this.exclusiveActive = false;
      else this.sharedActive -= 1;
      this.pump();
    });
  }
}
