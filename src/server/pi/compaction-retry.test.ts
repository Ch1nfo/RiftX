import assert from "node:assert/strict";
import test from "node:test";
import type { AgentSession, AgentSessionEvent } from "@mariozechner/pi-coding-agent";
import { compactionBlocked, compactionEndError, setCompactionDiagnostic } from "./compaction-retry";
import { installAutoCompactionRetryPolicy, runAutoCompaction } from "./pi-internals";

test("automatic entry points share backoff, recover after success and respect model changes and cancellation", async (t) => {
  let now = 1000;
  t.mock.method(Date, "now", () => now);
  let calls = 0;
  let mode: "failure" | "success" | "cancel" = "failure";
  const listeners = new Set<(event: AgentSessionEvent) => void>();
  const model = { id: "a", provider: "test", contextWindow: 128_000 };
  const session = {
    model,
    subscribe: (listener: (event: AgentSessionEvent) => void) => { listeners.add(listener); return () => listeners.delete(listener); },
    _runAutoCompaction: async (reason: "threshold" | "overflow", willRetry: boolean) => {
      calls += 1;
      if (mode === "failure") setCompactionDiagnostic(session, "Checkpoint validation failed");
      for (const listener of listeners) listener({ type: "compaction_end", reason,
        result: mode === "success" ? { summary: "valid", firstKeptEntryId: "kept", tokensBefore: 1 } : undefined,
        aborted: mode !== "success", willRetry: mode === "success" && willRetry });
    }
  } as unknown as AgentSession;
  installAutoCompactionRetryPolicy(session);
  installAutoCompactionRetryPolicy(session);
  const auto = session as unknown as { _runAutoCompaction: (reason: "threshold" | "overflow", retry: boolean) => Promise<void> };
  await runAutoCompaction(session);
  now += 29_999;
  await auto._runAutoCompaction("overflow", true);
  assert.equal(calls, 1);
  now += 1;
  await auto._runAutoCompaction("overflow", true);
  assert.equal(calls, 2);
  now += 30_000;
  assert.equal(compactionBlocked(session), true, "second failure uses a longer cooldown");
  now += 30_000;
  mode = "success";
  await runAutoCompaction(session);
  assert.equal(compactionBlocked(session), false);
  mode = "failure";
  await runAutoCompaction(session);
  now += 30_000;
  assert.equal(compactionBlocked(session), false, "success resets the failure counter");
  await runAutoCompaction(session);
  model.id = "b";
  assert.equal(compactionBlocked(session), false, "a different model can recover immediately");
  mode = "cancel";
  await runAutoCompaction(session);
  assert.equal(compactionBlocked(session), false, "user cancellation does not count as a failed provider request");
  assert.equal(listeners.size, 0, "attempt listeners are always removed");
});

test("SDK failure text is sanitized and intentional cancellation is silent", () => {
  const session = {} as AgentSession;
  const event = { type: "compaction_end" as const, reason: "overflow" as const, result: undefined, aborted: false, willRetry: false, errorMessage: "provider failed: Authorization=SECRET" };
  assert.match(compactionEndError(session, event)!, /original history/);
  assert.doesNotMatch(compactionEndError(session, event)!, /SECRET/);
  assert.equal(compactionEndError(session, { ...event, aborted: true, errorMessage: undefined }), undefined);
});
