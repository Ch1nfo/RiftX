import assert from "node:assert/strict";
import test from "node:test";
import { AgentSession, SessionManager, convertToLlm, type AgentSessionEvent, type CompactionResult, type ExtensionAPI, type ModelRegistry, type SessionBeforeCompactEvent } from "@mariozechner/pi-coding-agent";
import { Agent } from "@mariozechner/pi-agent-core";
import { Type } from "@sinclair/typebox";
import { createAssistantMessageEventStream, registerApiProvider, unregisterApiProviders, type AssistantMessage, type Model, type SimpleStreamOptions } from "@mariozechner/pi-ai";
import { createPentestCompactionExtension } from "./pentest-compaction";
import { PENTEST_COMPACTION_SYSTEM_PROMPT, REQUIRED_SECTIONS } from "./compaction-prompt";
import { installMidTurnCompaction } from "./mid-turn-compaction";
import { estimateMessagesContextUsage } from "./context-usage";
import { compactionBlocked } from "./compaction-retry";

for (const scenario of [
  { phase: "overflow", fail: false }, { phase: "mid-turn", fail: false },
  { phase: "overflow", fail: true }, { phase: "mid-turn", fail: true }
] as const) test(`long split turn: ${scenario.phase} with ${scenario.fail ? "failed" : "successful"} summary`, { timeout: 5000 }, async (t) => {
  const { phase } = scenario;
  let failSummary: boolean = scenario.fail;
  let now = Date.now();
  t.mock.method(Date, "now", () => now);
  const warnings: string[] = [];
  t.mock.method(console, "warn", (message: string) => { warnings.push(message); });
  const api = "riftx-compaction-recovery";
  const model: Model<typeof api> = { id: "test", name: "test", api, provider: "test", baseUrl: "http://unused", reasoning: false, input: ["text"], contextWindow: 128_000, maxTokens: 16_384, cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 } };
  const usage = { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, totalTokens: 0, cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } };
  const requests: number[] = [];
  let workerCalls = 0;
  let toolCalls = 0;
  const summary = REQUIRED_SECTIONS.map((heading) => `${heading}\nPreserved evidence; continue investigation.`).join("\n");
  const stream = (_model: Model<typeof api>, context: { systemPrompt?: string; messages: unknown[] }, options?: SimpleStreamOptions) => {
    if (context.systemPrompt !== PENTEST_COMPACTION_SYSTEM_PROMPT) {
      workerCalls += 1;
      if (failSummary) {
        assert.match(JSON.stringify(context.messages), /Evidence 0:/, "failed summary leaves old evidence in the actual worker request");
      } else {
        assert.match(JSON.stringify(context.messages), /Preserved evidence/);
        assert.doesNotMatch(JSON.stringify(context.messages), /Evidence 0:/);
      }
      const output = createAssistantMessageEventStream();
      output.end({ role: "assistant", api, provider: "test", model: "test", content: workerCalls === 1
        ? [{ type: "toolCall", id: "after-compaction", name: "probe", arguments: {} }]
        : [{ type: "text", text: "Investigation completed after compaction" }],
        stopReason: workerCalls === 1 ? "toolUse" : "stop", usage, timestamp: Date.now() });
      return output;
    }
    const cap = options?.maxTokens ?? 0;
    requests.push(cap);
    const output = createAssistantMessageEventStream();
    // Unlike the old fixture, honor the requested output limit.
    output.end({ role: "assistant", api, provider: "test", model: "test", content: [{ type: "text", text: summary.slice(0, cap * 4) }], stopReason: failSummary ? "error" : cap * 4 < summary.length ? "length" : "stop", errorMessage: failSummary ? "Synthetic upstream request failure" : undefined, usage, timestamp: Date.now() });
    return output;
  };
  registerApiProvider({ api, stream, streamSimple: stream }, api);
  try {
    const manager = SessionManager.inMemory();
    manager.appendMessage({ role: "user", content: "Investigate and continue until complete", timestamp: 1 });
    for (let i = 0; i < 60; i += 1) {
      manager.appendMessage({ role: "assistant", api, provider: "test", model: "test", content: [{ type: "toolCall", id: `tool-${i}`, name: "read", arguments: {} }], stopReason: "toolUse", usage, timestamp: i * 2 + 2 } satisfies AssistantMessage);
      manager.appendMessage({ role: "toolResult", toolCallId: `tool-${i}`, toolName: "read", content: [{ type: "text", text: `Evidence ${i}: ${"x".repeat(8000)}` }], isError: false, timestamp: i * 2 + 3 });
    }
    const agent = new Agent({ initialState: { model, messages: manager.buildSessionContext().messages, tools: [{
      name: "probe", label: "Probe", description: "Record a synthetic observation", parameters: Type.Object({}),
      execute: async () => { toolCalls += 1; return { content: [{ type: "text", text: "new observation" }], details: {} }; }
    }] }, convertToLlm });
    const state = agent.state;
    const listeners = new Set<(event: AgentSessionEvent) => void>();
    const events: AgentSessionEvent[] = [];
    let handler!: (event: SessionBeforeCompactEvent) => Promise<{ compaction?: CompactionResult; cancel?: boolean } | undefined>;
    let resumed!: () => void;
    const continued = new Promise<void>((resolve) => { resumed = resolve; });
    agent.subscribe((event) => {
      if (event.type === "message_end" && (event.message.role === "user" || event.message.role === "assistant" || event.message.role === "toolResult")) manager.appendMessage(event.message);
      if (event.type === "agent_end") resumed();
    });
    const modelRegistry = { getApiKeyAndHeaders: async () => ({ ok: true, apiKey: "test" }) } as unknown as ModelRegistry;
    const session = {
      model, thinkingLevel: "off", sessionManager: manager, _modelRegistry: modelRegistry,
      get messages() { return state.messages; },
      agent,
      getContextUsage: () => estimateMessagesContextUsage(state.messages, model.contextWindow),
      _agentEventQueue: Promise.resolve(),
      abortCompaction: AgentSession.prototype.abortCompaction,
      settingsManager: { getCompactionSettings: () => ({ enabled: true, reserveTokens: 16_384, keepRecentTokens: 12_800 }) },
      subscribe: (listener: (event: AgentSessionEvent) => void) => { listeners.add(listener); return () => listeners.delete(listener); },
      _extensionRunner: { hasHandlers: (name: string) => name === "session_before_compact", emit: async (event: SessionBeforeCompactEvent | { type: "session_compact" }) => {
        if (event.type !== "session_before_compact") return;
        assert.equal(event.preparation.isSplitTurn, true);
        assert.ok(event.preparation.turnPrefixMessages.length > 90);
        return handler(event);
      } },
      _emit: (event: AgentSessionEvent) => { events.push(event); for (const listener of listeners) listener(event); },
      _runAutoCompaction: (AgentSession.prototype as unknown as { _runAutoCompaction: (reason: string, retry: boolean) => Promise<void> })._runAutoCompaction
    } as unknown as AgentSession;
    installMidTurnCompaction(session);
    await createPentestCompactionExtension({ getSession: () => session, modelRegistry, getActiveSkills: () => [] })({ on: (_name: string, callback: typeof handler) => { handler = callback; } } as unknown as ExtensionAPI);
    if (phase === "overflow") {
      await (session as unknown as { _runAutoCompaction: (reason: string, retry: boolean) => Promise<void> })._runAutoCompaction("overflow", true);
      if (!failSummary) {
        await continued;
        await agent.waitForIdle();
      }
    } else {
      await agent.continue();
    }
    const end = events.at(-1);
    if (failSummary) {
      assert.ok(end?.type === "compaction_end");
      assert.equal(end.result, undefined);
      assert.equal(end.aborted, true);
      assert.equal(end.willRetry, false, "SDK does not schedule overflow continuation when the summary failed");
      assert.equal(manager.getBranch().filter((entry) => entry.type === "compaction").length, 0);
      assert.match(JSON.stringify(state.messages), /Evidence 0:/);
      assert.deepEqual(warnings, ["RiftX penetration compaction failed: summary model request failed; keeping the original history."]);
      assert.equal(compactionBlocked(session), true);
      if (phase === "overflow") {
        assert.equal(workerCalls, 0);
        assert.equal(toolCalls, 0);
        return;
      }
      assert.equal(workerCalls, 2, "mid-turn failure still permits the actual worker loop to finish");
      assert.equal(toolCalls, 1);
      await agent.prompt("Another message during compaction cooldown");
      assert.equal(workerCalls, 3);
      assert.equal(requests.length, 1, "cooldown skips repeated summaries while ordinary work continues");
      failSummary = false;
      now += 30_001;
      await agent.prompt("Continue after the summary provider recovers");
      assert.equal(requests.length, 2);
      assert.equal(workerCalls, 4);
      assert.equal(manager.getBranch().filter((entry) => entry.type === "compaction").length, 1);
      assert.equal(compactionBlocked(session), false);
      assert.equal(warnings.length, 1, "later successful compaction does not erase the earlier terminal warning");
      return;
    }
    assert.ok(end?.type === "compaction_end" && end.result, "long split turn must produce a valid checkpoint");
    assert.ok(requests[0] >= 1024, `summary budget was ${requests[0]}`);
    assert.ok(state.messages.length < 30, "old tool results were actually replaced");
    assert.match(JSON.stringify(state.messages), /Evidence 59/);
    assert.doesNotMatch(JSON.stringify(state.messages), /Evidence 0:/);
    assert.equal(workerCalls, 2, "the real agent loop continued through a tool call and final response");
    assert.equal(toolCalls, 1);
    assert.match(JSON.stringify(state.messages.at(-1)), /Investigation completed/);
    await agent.prompt("Continue with a second request");
    assert.equal(workerCalls, 3);
    assert.equal(requests.length, 1, "the next user message does not enter another compaction loop");
  } finally { unregisterApiProviders(api); }
});
