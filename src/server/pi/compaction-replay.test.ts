import assert from "node:assert/strict";
import test from "node:test";
import { AgentSession, SessionManager, type AgentSessionEvent, type CompactionResult, type ExtensionAPI, type ModelRegistry, type SessionBeforeCompactEvent } from "@mariozechner/pi-coding-agent";
import { createAssistantMessageEventStream, registerApiProvider, unregisterApiProviders, type Context, type Model, type SimpleStreamOptions } from "@mariozechner/pi-ai";
import { createPentestCompactionExtension } from "./pentest-compaction";
import { PENTEST_COMPACTION_SYSTEM_PROMPT, REQUIRED_SECTIONS } from "./compaction-prompt";
import { installMidTurnCompaction } from "./mid-turn-compaction";
import { runAutoCompaction } from "./pi-internals";
import { compactionBlocked, compactionDiagnostic } from "./compaction-retry";

test("real SDK persists valid checkpoints and cancels failed summaries without generic fallback", async (t) => {
  const api = "riftx-compaction-replay";
  const model: Model<typeof api> = { id: "fixture", name: "fixture", api, provider: "fixture", baseUrl: "http://unused", input: ["text"], reasoning: true, contextWindow: 128_000, maxTokens: 16_384, cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 } };
  let mode: "valid" | "invalid" | "error" | "aborted" | "cancel" | "length" | "throw" = "valid";
  let abortSummary = () => {};
  const warnings: string[] = [];
  let now = Date.now();
  t.mock.method(Date, "now", () => now);
  t.mock.method(console, "warn", (message: string) => { warnings.push(message); });
  const requests: Array<{ custom: boolean; reasoning?: string }> = [];
  const fact = "request:r-start ruled out anonymous access; compare role B next.";
  const stream = (_model: Model<typeof api>, context: Context, options?: SimpleStreamOptions) => {
    const custom = context.systemPrompt === PENTEST_COMPACTION_SYSTEM_PROMPT;
    requests.push({ custom, reasoning: options?.reasoning });
    if (mode === "throw") throw new Error("Synthetic request exception");
    if (mode === "cancel") abortSummary();
    const failed = mode === "error";
    const summary = REQUIRED_SECTIONS.map((heading) => `${heading}\n${fact}`).join("\n");
    const output = createAssistantMessageEventStream();
    output.end({ role: "assistant", api, provider: "fixture", model: "fixture", content: [{ type: "text", text: mode === "invalid" ? "Incomplete checkpoint" : summary }], timestamp: requests.length,
      stopReason: failed ? "error" : mode === "aborted" || mode === "cancel" ? "aborted" : mode === "length" ? "length" : "stop", errorMessage: failed ? "Synthetic provider failure" : undefined,
      usage: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, totalTokens: 0, cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } } });
    return output;
  };
  registerApiProvider({ api, stream, streamSimple: stream }, api);
  try {
    const manager = SessionManager.inMemory();
    manager.appendMessage({ role: "user", content: fact, timestamp: 1 });
    const state = { messages: manager.buildSessionContext().messages };
    const modelRegistry = { getApiKeyAndHeaders: async () => ({ ok: true, apiKey: "fixture" }) } as unknown as ModelRegistry;
    let handler!: (event: SessionBeforeCompactEvent) => Promise<{ compaction?: CompactionResult; cancel?: boolean } | undefined>;
    let preparedCut: string | undefined;
    const events: Array<{ type: string; result?: CompactionResult; aborted?: boolean }> = [];
    const listeners = new Set<(event: AgentSessionEvent) => void>();
    const session = {
      model, thinkingLevel: "high", sessionManager: manager, _modelRegistry: modelRegistry,
      abortCompaction: AgentSession.prototype.abortCompaction,
      get messages() { return state.messages; },
      agent: { state, hasQueuedMessages: () => false },
      settingsManager: { getCompactionSettings: () => ({ enabled: true, reserveTokens: 16_384, keepRecentTokens: 20_000 }) },
      _extensionRunner: {
        hasHandlers: (name: string) => name === "session_before_compact",
        emit: async (event: SessionBeforeCompactEvent | { type: "session_compact" }) => {
          if (event.type !== "session_before_compact") return;
          preparedCut = event.preparation.firstKeptEntryId;
          const before = structuredClone(event.preparation);
          const result = await handler(event);
          assert.deepEqual(event.preparation, before, "the original SDK preparation remains unchanged");
          return result;
        }
      },
      subscribe: (listener: (event: AgentSessionEvent) => void) => { listeners.add(listener); return () => listeners.delete(listener); },
      _emit: (event: AgentSessionEvent) => { events.push(event); for (const listener of listeners) listener(event); },
      _runAutoCompaction: (AgentSession.prototype as unknown as { _runAutoCompaction: (reason: string, willRetry: boolean) => Promise<void> })._runAutoCompaction
    } as unknown as AgentSession;
    abortSummary = () => session.abortCompaction();
    installMidTurnCompaction(session);
    await createPentestCompactionExtension({ getSession: () => session, getActiveSkills: () => [], modelRegistry
    })({ on: (_name: string, callback: typeof handler) => { handler = callback; } } as unknown as ExtensionAPI);
    for (let round = 0; round < 5; round += 1) {
      for (let i = 0; i < 48; i += 1) manager.appendMessage({ role: "user", timestamp: 10 + round * 100 + i, content: `Raw observation ${round}/${i}: ${"x".repeat(4000)}` });
      state.messages = manager.buildSessionContext().messages;
      const settings = session.settingsManager.getCompactionSettings();
      assert.equal(settings.keepRecentTokens, 12_800);
      await runAutoCompaction(session);
      const end = events.at(-1)!;
      assert.equal(end.type, "compaction_end");
      assert.ok(end.result);
      assert.equal(end.aborted, false);
      assert.equal(end.result.firstKeptEntryId, preparedCut);
      assert.equal(manager.getBranch().filter((entry) => entry.type === "compaction").length, round + 1);
      assert.deepEqual(requests.slice(round), [{ custom: true, reasoning: "high" }]);
      const restored = JSON.stringify(state.messages);
      assert.match(restored, /request:r-start/);
      assert.match(restored, new RegExp(`Raw observation ${round}/47`));
    }
    manager.appendMessage({ role: "user", content: "new observation ".repeat(5000), timestamp: 1000 });
    state.messages = manager.buildSessionContext().messages;
    const before = structuredClone(state.messages);
    const branchBefore = manager.getBranch();
    const expectedReasons = {
      invalid: "checkpoint validation failed",
      error: "summary model request failed",
      aborted: "summary model request failed",
      length: "summary output was truncated",
      throw: "checkpoint preparation or model request failed"
    };
    for (const failure of ["invalid", "error", "aborted", "length", "throw"] as const) {
      mode = failure;
      const callsBefore = requests.length;
      await runAutoCompaction(session);
      assert.equal(events.at(-1)?.result, undefined);
      assert.equal(events.at(-1)?.aborted, true);
      assert.deepEqual(state.messages, before, "failed summaries preserve the original context");
      assert.deepEqual(manager.getBranch(), branchBefore);
      assert.equal(requests.length, callsBefore + 1, "failure must not trigger repair or generic summary requests");
      assert.deepEqual(requests.at(-1), { custom: true, reasoning: "high" });
      assert.equal(warnings.at(-1), `RiftX penetration compaction failed: ${expectedReasons[failure]}; keeping the original history.`);
      assert.ok(compactionDiagnostic(session)?.includes(expectedReasons[failure]));
      const eventsBefore = events.length;
      await runAutoCompaction(session);
      await (session as unknown as { _runAutoCompaction: (reason: string, retry: boolean) => Promise<void> })._runAutoCompaction("overflow", true);
      assert.equal(requests.length, callsBefore + 1, "preflight and overflow must share failure backoff");
      assert.equal(events.length, eventsBefore, "backoff must not flash compaction start/end");
      now += 300_001;
    }
    assert.equal(warnings.length, 5);
    mode = "cancel";
    await runAutoCompaction(session);
    assert.equal(events.at(-1)?.aborted, true);
    assert.equal(warnings.length, 5, "explicit local cancellation must not produce a model-request-failed warning");
    assert.equal(compactionDiagnostic(session), undefined);
    assert.equal(compactionBlocked(session), false);
    assert.deepEqual(manager.getBranch(), branchBefore);
  } finally { unregisterApiProviders(api); }
});
