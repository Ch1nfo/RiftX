export const DEFAULT_BASH_TIMEOUT_SECONDS = 5 * 60;
export const MAX_BASH_TIMEOUT_SECONDS = 30 * 60;

export function resolveBashTimeout(timeout?: number) {
  if (timeout === undefined || !Number.isFinite(timeout) || timeout <= 0) return DEFAULT_BASH_TIMEOUT_SECONDS;
  return Math.min(timeout, MAX_BASH_TIMEOUT_SECONDS);
}
