import assert from "node:assert/strict";
import test from "node:test";
import type { AgentSession, AgentSessionEvent } from "@mariozechner/pi-coding-agent";
import { clearCompactionRetry, compactionBlocked, setCompactionFailed } from "./compaction-retry";
import { installAutoCompactionRetryPolicy, runAutoCompaction } from "./pi-internals";

test("automatic entry points share bounded backoff and recover after success or configuration changes", async (t) => {
  let now = 1000;
  t.mock.method(Date, "now", () => now);
  let calls = 0;
  let mode: "failure" | "success" | "cancel" | "sdk_failure" | "throw" = "failure";
  const listeners = new Set<(event: AgentSessionEvent) => void>();
  const model = { id: "a", provider: "test", contextWindow: 128_000 };
  const session = {
    model,
    subscribe: (listener: (event: AgentSessionEvent) => void) => { listeners.add(listener); return () => listeners.delete(listener); },
    _runAutoCompaction: async (reason: "threshold" | "overflow", willRetry: boolean) => {
      calls += 1;
      if (mode === "throw") throw new Error("fixture failure");
      if (mode === "failure") setCompactionFailed(session, true);
      for (const listener of listeners) listener({ type: "compaction_end", reason,
        result: mode === "success" ? { summary: "valid", firstKeptEntryId: "kept", tokensBefore: 1 } : undefined,
        aborted: mode === "failure" || mode === "cancel", willRetry: mode === "success" && willRetry });
    }
  } as unknown as AgentSession;
  installAutoCompactionRetryPolicy(session);
  installAutoCompactionRetryPolicy(session);
  const auto = session as unknown as { _runAutoCompaction: (reason: "threshold" | "overflow", retry: boolean) => Promise<void> };
  for (const wait of [30_000, 60_000, 120_000, 240_000, 300_000, 300_000]) {
    const before = calls;
    await runAutoCompaction(session);
    assert.equal(calls, before + 1);
    now += wait - 1;
    await auto._runAutoCompaction("overflow", true);
    assert.equal(calls, before + 1, "overflow must share the threshold cooldown");
    now += 1;
    assert.equal(compactionBlocked(session), false);
  }
  mode = "success";
  await auto._runAutoCompaction("overflow", true);
  assert.equal(compactionBlocked(session), false);
  mode = "failure";
  await runAutoCompaction(session);
  now += 30_000;
  assert.equal(compactionBlocked(session), false, "success resets the failure count");
  await runAutoCompaction(session);
  model.id = "b";
  assert.equal(compactionBlocked(session), false, "a different model can recover immediately");
  mode = "cancel";
  await runAutoCompaction(session);
  assert.equal(compactionBlocked(session), false, "intentional cancellation is not a failure");
  mode = "sdk_failure";
  await runAutoCompaction(session);
  assert.equal(compactionBlocked(session), true, "SDK failures before the extension also back off");
  clearCompactionRetry(session);
  assert.equal(compactionBlocked(session), false, "corrected credentials can retry with the same model");
  mode = "throw";
  await assert.rejects(runAutoCompaction(session), /fixture failure/);
  assert.equal(compactionBlocked(session), true);
  assert.equal(listeners.size, 0, "attempt listeners are always removed");
});
