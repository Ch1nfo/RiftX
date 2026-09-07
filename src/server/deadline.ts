/** Shared AbortSignal deadlines for tool and network operations. */

export type Deadline = {
  signal: AbortSignal;
  cleanup: () => void;
};

function abortReason(signal: AbortSignal, fallback: string) {
  return signal.reason instanceof Error ? signal.reason : new Error(fallback);
}

/**
 * Combine a caller cancellation signal with a finite deadline. The returned
 * signal is suitable for APIs that support AbortSignal; cleanup must be called
 * once the operation settles.
 */
export function createDeadline(parentSignal: AbortSignal | undefined, timeoutMs: number, timeoutMessage: string): Deadline {
  const controller = new AbortController();
  const onAbort = () => controller.abort(parentSignal?.reason ?? new Error("Operation aborted"));
  if (parentSignal?.aborted) onAbort();
  else parentSignal?.addEventListener("abort", onAbort, { once: true });
  const timer = setTimeout(() => controller.abort(new Error(timeoutMessage)), timeoutMs);
  return {
    signal: controller.signal,
    cleanup: () => {
      clearTimeout(timer);
      parentSignal?.removeEventListener("abort", onAbort);
    }
  };
}

/** Bound a promise even when its implementation ignores AbortSignal. */
export function raceWithAbort<T>(operation: Promise<T>, signal: AbortSignal, fallback = "Operation aborted"): Promise<T> {
  if (signal.aborted) return Promise.reject(abortReason(signal, fallback));
  return new Promise<T>((resolve, reject) => {
    let settled = false;
    const finish = (complete: () => void) => {
      if (settled) return;
      settled = true;
      signal.removeEventListener("abort", onAbort);
      complete();
    };
    const onAbort = () => finish(() => reject(abortReason(signal, fallback)));
    signal.addEventListener("abort", onAbort, { once: true });
    operation.then(
      (value) => finish(() => resolve(value)),
      (error) => finish(() => reject(error))
    );
  });
}

/** Run an operation with a child signal and a caller-visible hard deadline. */
export async function runWithDeadline<T>(
  operation: (signal: AbortSignal) => Promise<T>,
  options: { signal?: AbortSignal; timeoutMs: number; timeoutMessage: string }
): Promise<T> {
  const deadline = createDeadline(options.signal, options.timeoutMs, options.timeoutMessage);
  try {
    const raw = Promise.resolve().then(() => operation(deadline.signal));
    return await raceWithAbort(raw, deadline.signal);
  } finally {
    deadline.cleanup();
  }
}
