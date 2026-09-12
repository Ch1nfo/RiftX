import { AsyncLocalStorage } from "node:async_hooks";

const executionGuard = new AsyncLocalStorage<() => void>();

export class BrowserExecutionBlockedError extends Error {}

/** Bind a caller's lease/budget check to its own queued browser operations. */
export function withBrowserExecutionGuard<T>(check: () => void, operation: () => Promise<T>): Promise<T> {
  return executionGuard.run(check, operation);
}

export function checkBrowserExecutionGuard() {
  executionGuard.getStore()?.();
}
