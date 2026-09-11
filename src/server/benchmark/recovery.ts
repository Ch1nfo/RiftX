/** Additional run-level recovery after the provider SDK has exhausted its retries. */
export function recoverableModelError(message: string): boolean {
  if (/\b(?:401|403)\b|invalid.*(?:api.?key|credential)|authentication|unauthorized|insufficient.quota|billing|model.*not.found|context.{0,20}(?:length|limit)|aborted|cancelled|canceled/i.test(message)) return false;
  return /\b(?:408|429|500|502|503|504|529)\b|rate.limit|overload|temporar|service.unavailable|internal.server.error|timed?\s*out|timeout|connection|network|fetch failed|socket|ECONN|EPIPE|EAI_AGAIN|stream.*(?:closed|ended|reset)|without using tools/i.test(message);
}

export class ModelRecovery {
  private attempts = 0;
  private retryAt = 0;
  private error = "";
  constructor(private readonly now: () => number = Date.now, private readonly delays = [5_000, 15_000, 30_000]) {}

  failed(message: string) {
    this.error = message;
    this.retryAt = 0;
  }

  succeeded() { this.attempts = 0; this.error = ""; this.retryAt = 0; }

  decision(childrenActive: boolean): { action: "continue" | "wait" | "retry" | "fail"; error: string; attempt: number } {
    const result = { error: this.error, attempt: this.attempts };
    if (!this.error) return { ...result, action: "continue" };
    if (!recoverableModelError(this.error)) return { ...result, action: "fail" };
    if (this.attempts >= this.delays.length) return { ...result, action: childrenActive ? "wait" : "fail" };
    this.retryAt ||= this.now() + this.delays[this.attempts];
    return { ...result, action: this.now() >= this.retryAt ? "retry" : "wait" };
  }

  dispatched() { this.attempts++; this.error = ""; this.retryAt = 0; }
}
