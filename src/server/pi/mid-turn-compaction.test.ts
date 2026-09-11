import assert from "node:assert/strict";
import test from "node:test";
import type { AgentSession } from "@mariozechner/pi-coding-agent";
import { estimateMessagesContextUsage, installMidTurnCompaction, keepRecentTokensForContext, shouldCompactBeforeSampling } from "./mid-turn-compaction";

test("caps recent history at ten percent of the current model context", () => {
  assert.equal(keepRecentTokensForContext(64_000), 6_400);
  assert.equal(keepRecentTokensForContext(128_000), 12_800);
  assert.equal(keepRecentTokensForContext(256_000), 25_600);
  assert.equal(keepRecentTokensForContext(1_000_000), 100_000);
});

test("installs the ten-percent policy where Pi computes its cut point", () => {
  const settingsManager = { getCompactionSettings: () => ({ enabled: true, reserveTokens: 16_384, keepRecentTokens: 20_000 }) };
  const session = {
    agent: { state: { messages: [] }, transformContext: undefined },
    model: { contextWindow: 128_000 },
    settingsManager,
    getContextUsage: () => undefined
  } as unknown as AgentSession;
  installMidTurnCompaction(session);
  assert.equal(settingsManager.getCompactionSettings().keepRecentTokens, 12_800);
});

test("mid-turn compaction leaves room for the next model response", () => {
  assert.equal(shouldCompactBeforeSampling(47_999, 64_000, 16_000), false);
  assert.equal(shouldCompactBeforeSampling(48_001, 64_000, 16_000), true);
});

test("unknown post-compaction usage does not trigger another compaction", () => {
  assert.equal(shouldCompactBeforeSampling(null, 64_000, 16_000), false);
  assert.equal(shouldCompactBeforeSampling(undefined, 64_000, 16_000), false);
});

test("estimated usage reflects only the compacted context", () => {
  const before = estimateMessagesContextUsage([{ role: "user", content: "x".repeat(40_000) }], 64_000);
  const after = estimateMessagesContextUsage([{ role: "compactionSummary", summary: "short summary" }], 64_000);
  assert.ok(after.tokens < before.tokens);
  assert.ok(after.percent !== null && before.percent !== null && after.percent < before.percent);
});

test("a compaction without a result keeps the active run alive", async () => {
  const listeners = new Set<(event: { type: string; reason?: string; result?: unknown }) => void>();
  const state = { messages: [{ role: "toolResult", content: "current" }] };
  const session = {
    agent: { state, transformContext: undefined },
    model: { contextWindow: 1_000 },
    messages: state.messages,
    settingsManager: { getCompactionSettings: () => ({ enabled: true, reserveTokens: 100 }) },
    getContextUsage: () => ({ tokens: 901, contextWindow: 1_000, percent: 90, input: null, output: null, cacheRead: null, cacheWrite: null, remaining: 99 }),
    subscribe: (listener: (event: { type: string; reason?: string; result?: unknown }) => void) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    abortCompaction: () => undefined,
    _agentEventQueue: Promise.resolve(),
    _runAutoCompaction: async () => {
      for (const listener of listeners) listener({ type: "compaction_end", reason: "threshold" });
    }
  } as unknown as AgentSession;

  installMidTurnCompaction(session);
  const activeMessages = state.messages;
  const transformed = await session.agent.transformContext!(activeMessages as never);
  assert.equal(transformed, activeMessages);
  assert.equal(activeMessages[0]?.content, "current");
});

test("does not wait for the event queue below the compaction threshold", async () => {
  let continuityRefreshes = 0;
  let resolveQueue!: () => void;
  const eventQueue = new Promise<void>((resolve) => { resolveQueue = resolve; });
  const state = { messages: [{ role: "toolResult", content: "current" }] };
  const session = {
    agent: { state, transformContext: undefined },
    model: { contextWindow: 1_000 },
    messages: state.messages,
    settingsManager: { getCompactionSettings: () => ({ enabled: true, reserveTokens: 100 }) },
    getContextUsage: () => ({ tokens: 100, contextWindow: 1_000, percent: 10, input: null, output: null, cacheRead: null, cacheWrite: null, remaining: 900 }),
    _agentEventQueue: eventQueue
  } as unknown as AgentSession;

  installMidTurnCompaction(session, async () => {
    continuityRefreshes += 1;
    return {};
  });
  const transformed = await Promise.race([
    session.agent.transformContext!(state.messages as never),
    new Promise<never>((_, reject) => setTimeout(() => reject(new Error("waited for event queue")), 100))
  ]);
  resolveQueue();
  assert.equal(transformed, state.messages);
  assert.equal(continuityRefreshes, 0, "main refreshes continuity after compaction, not on ordinary sampling turns");
});

test("mid-turn compaction replaces the active loop context in place", async () => {
  const listeners = new Set<(event: { type: string; reason?: string; result?: unknown }) => void>();
  const state = { messages: [{ role: "toolResult", content: "old" }] };
  const agent = {
    state,
    transformContext: undefined as ((messages: typeof state.messages, signal?: AbortSignal) => Promise<typeof state.messages>) | undefined
  };
  const session = {
    agent,
    model: { contextWindow: 1_000 },
    messages: state.messages,
    settingsManager: { getCompactionSettings: () => ({ enabled: true, reserveTokens: 100 }) },
    getContextUsage: () => ({ tokens: 901, contextWindow: 1_000, percent: 90, input: null, output: null, cacheRead: null, cacheWrite: null, remaining: 99 }),
    subscribe: (listener: (event: { type: string; reason?: string; result?: unknown }) => void) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    abortCompaction: () => undefined,
    _agentEventQueue: Promise.resolve(),
    _runAutoCompaction: async () => {
      state.messages = [{ role: "compactionSummary", content: "summary" }];
      for (const listener of listeners) listener({ type: "compaction_end", reason: "threshold", result: { summary: "summary" } });
    }
  } as unknown as AgentSession;

  installMidTurnCompaction(session);
  const activeMessages = state.messages;
  const transformed = await session.agent.transformContext!(activeMessages as never);
  assert.equal(transformed, activeMessages);
  assert.equal(activeMessages[0]?.content, "summary");
});

test("mid-turn compaction restores the ordered continuity packet to detached and future contexts", async () => {
  const listeners = new Set<(event: { type: string; reason?: string; result?: unknown }) => void>();
  const state: { messages: Array<Record<string, unknown>> } = { messages: [{ role: "toolResult", content: "old" }] };
  const session = {
    agent: { state, transformContext: undefined },
    model: { contextWindow: 1_000 },
    messages: state.messages,
    settingsManager: { getCompactionSettings: () => ({ enabled: true, reserveTokens: 100 }) },
    getContextUsage: () => ({ tokens: 901, contextWindow: 1_000, percent: 90, input: null, output: null, cacheRead: null, cacheWrite: null, remaining: 99 }),
    subscribe: (listener: (event: { type: string; reason?: string; result?: unknown }) => void) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    abortCompaction: () => undefined,
    _agentEventQueue: Promise.resolve(),
    _runAutoCompaction: async () => {
      state.messages = [{ role: "compactionSummary", summary: "summary" }];
      for (const listener of listeners) listener({ type: "compaction_end", reason: "threshold", result: { summary: "summary" } });
    }
  } as unknown as AgentSession;

  installMidTurnCompaction(session, async () => ({
    taskContract: "task",
    skillContext: "skill",
    investigationCapsule: "capsule",
    progressCheckpoint: "checkpoint"
  }));
  const activeMessages = session.messages as unknown as Array<Record<string, unknown>>;
  const transformed = await session.agent.transformContext!(activeMessages as never) as unknown as Array<Record<string, unknown>>;
  const expected = ["riftx_task_contract", "riftx_skill_context", "riftx_investigation_capsule", "riftx_progress_checkpoint"];
  assert.deepEqual(transformed.slice(-4).map((message) => message.customType), expected);
  assert.deepEqual(state.messages.slice(-4).map((message) => message.customType), expected);
  for (const type of expected) assert.equal(transformed.filter((message) => message.customType === type).length, 1);
});
