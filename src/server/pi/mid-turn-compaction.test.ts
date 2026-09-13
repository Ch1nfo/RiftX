import assert from "node:assert/strict";
import test from "node:test";
import { convertToLlm, type AgentSession } from "@mariozechner/pi-coding-agent";
import { estimateMessagesContextUsage, installMidTurnCompaction, keepRecentTokensForContext, shouldCompactBeforeSampling } from "./mid-turn-compaction";
import { TOOL_INVENTORY_CONTEXT_TYPE, upsertToolInventory } from "./continuity-context";
import { BenchmarkContextBudgetError, estimateBenchmarkInputTokens } from "./compaction-budget";

function inventoryFailureFixture(getInventory: () => string, contextWindow = 16_384) {
  const messages: Array<Record<string, unknown>> = [
    { role: "assistant", content: [{ type: "toolCall", id: "fixture_call", name: "fixture_tool", arguments: {} }] },
    { role: "custom", customType: TOOL_INVENTORY_CONTEXT_TYPE, content: "fixture_stale_inventory" },
    { role: "toolResult", toolCallId: "fixture_call", toolName: "fixture_tool", content: [{ type: "text", text: "fixture_result" }] }
  ];
  const state = { messages, systemPrompt: "fixture_system", tools: [] };
  const session = {
    agent: {
      state,
      transformContext: async (input: Array<Record<string, unknown>>) => {
        const transformed = structuredClone(input);
        upsertToolInventory(transformed, getInventory());
        return transformed;
      }
    },
    model: { provider: "fixture", id: "fixture", contextWindow, maxTokens: 512 },
    get messages() { return state.messages; },
    settingsManager: { getCompactionSettings: () => ({ enabled: false, reserveTokens: 512, keepRecentTokens: 400 }) },
    getContextUsage: () => null
  } as unknown as AgentSession;
  installMidTurnCompaction(session, async () => { throw new Error("fixture_workspace_reconcile_failed"); }, { samplingRefresh: true });
  return { session, messages };
}

test("inventory refresh survives failed continuity I/O and preserves tool call/result ordering", async () => {
  let current = "fixture_current_inventory";
  const { session, messages } = inventoryFailureFixture(() => current);
  const sent = await session.agent.transformContext!(messages as never) as unknown as Array<Record<string, unknown>>;
  const inventory = sent.filter((message) => message.customType === TOOL_INVENTORY_CONTEXT_TYPE);
  assert.equal(inventory.length, 1);
  assert.equal(inventory[0].content, current);
  assert.equal(sent.some((message) => message.content === "fixture_stale_inventory"), false);
  assert.deepEqual(convertToLlm(sent as never).map((message) => message.role), ["assistant", "toolResult", "user"]);
  assert.ok(estimateBenchmarkInputTokens(session, sent) > estimateBenchmarkInputTokens(session, sent.filter((message) => message.customType !== TOOL_INVENTORY_CONTEXT_TYPE)));
  current = "";
  const released = await session.agent.transformContext!(messages as never) as unknown as Array<Record<string, unknown>>;
  assert.equal(released.some((message) => message.customType === TOOL_INVENTORY_CONTEXT_TYPE), false);
  assert.deepEqual(convertToLlm(released as never).map((message) => message.role), ["assistant", "toolResult"]);
});

test("inventory added before failed continuity refresh is included in the sampling budget gate", async () => {
  const { session, messages } = inventoryFailureFixture(() => "fixture_inventory".repeat(2_000), 4_096);
  await assert.rejects(session.agent.transformContext!(messages as never), BenchmarkContextBudgetError);
});

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

  installMidTurnCompaction(session);
  const transformed = await Promise.race([
    session.agent.transformContext!(state.messages as never),
    new Promise<never>((_, reject) => setTimeout(() => reject(new Error("waited for event queue")), 100))
  ]);
  resolveQueue();
  assert.equal(transformed, state.messages);
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

test("samplingRefresh injects continuity into the transformed provider context on ordinary sampling", async () => {
  const input: Array<Record<string, unknown>> = [{ role: "user", content: "task" }];
  const session = {
    agent: {
      state: { messages: input },
      // Pi's base transform returns a structured clone rather than the input
      // array. The continuity packet must be added to this returned array.
      transformContext: async (messages: Array<Record<string, unknown>>) => structuredClone(messages)
    },
    model: { contextWindow: 1_000 },
    settingsManager: { getCompactionSettings: () => ({ enabled: true, reserveTokens: 100 }) },
    getContextUsage: () => ({ tokens: 100, contextWindow: 1_000, percent: 10, input: null, output: null, cacheRead: null, cacheWrite: null, remaining: 900 })
  } as unknown as AgentSession;

  installMidTurnCompaction(session, async () => ({ investigationCapsule: "fresh benchmark state" }), { samplingRefresh: true });
  const transformed = await session.agent.transformContext!(input as never) as unknown as Array<Record<string, unknown>>;

  assert.notEqual(transformed, input);
  assert.equal(input.some((message) => message.customType === "riftx_investigation_capsule"), false);
  assert.equal(transformed.at(-1)?.customType, "riftx_investigation_capsule");
  assert.equal(transformed.at(-1)?.content, "fresh benchmark state");
});

test("without samplingRefresh, ordinary sampling leaves the provider context untouched", async () => {
  const input: Array<Record<string, unknown>> = [{ role: "user", content: "task" }];
  let refreshCalls = 0;
  const session = {
    agent: {
      state: { messages: input },
      transformContext: async (messages: Array<Record<string, unknown>>) => structuredClone(messages)
    },
    model: { contextWindow: 1_000 },
    settingsManager: { getCompactionSettings: () => ({ enabled: true, reserveTokens: 100 }) },
    getContextUsage: () => ({ tokens: 100, contextWindow: 1_000, percent: 10, input: null, output: null, cacheRead: null, cacheWrite: null, remaining: 900 })
  } as unknown as AgentSession;

  installMidTurnCompaction(session, async () => { refreshCalls += 1; return { investigationCapsule: "stale" }; });
  const transformed = await session.agent.transformContext!(input as never) as unknown as Array<Record<string, unknown>>;

  assert.equal(refreshCalls, 0, "ordinary sessions must not pay the continuity refresh on every sampling");
  assert.deepEqual(transformed, input);
});

for (const succeeds of [false, true]) {
  test(`mid-turn compaction ${succeeds ? "success" : "cancellation"} preserves the final context transform and pending notice`, async () => {
    const listeners = new Set<(event: { type: string; reason?: string; result?: unknown }) => void>();
    const input: Array<Record<string, unknown>> = [{ role: "user", content: "fixture_input" }];
    const state = { messages: input };
    let transformCalls = 0;
    let refreshCalls = 0;
    const session = {
      agent: {
        state,
        transformContext: async (messages: Array<Record<string, unknown>>) => {
          transformCalls += 1;
          return [...structuredClone(messages), { role: "custom", customType: "fixture_extension", content: "extension_result" }];
        }
      },
      model: { contextWindow: 1_000 },
      settingsManager: { getCompactionSettings: () => ({ enabled: true, reserveTokens: 100 }) },
      getContextUsage: () => ({ tokens: 950, percent: 95 }),
      subscribe: (listener: (event: { type: string; reason?: string; result?: unknown }) => void) => {
        listeners.add(listener);
        return () => listeners.delete(listener);
      },
      abortCompaction: () => undefined,
      _agentEventQueue: Promise.resolve(),
      _runAutoCompaction: async () => {
        if (succeeds) state.messages = [{ role: "compactionSummary", summary: "fixture_summary" }];
        for (const listener of listeners) listener({
          type: "compaction_end", reason: "threshold", result: succeeds ? { summary: "fixture_summary" } : undefined
        });
      }
    } as unknown as AgentSession;
    installMidTurnCompaction(session, async () => {
      refreshCalls += 1;
      return { investigationCapsule: "pending_notice" };
    }, { samplingRefresh: true });

    const sent = await session.agent.transformContext!(input as never) as unknown as Array<Record<string, unknown>>;
    assert.equal(sent.filter((message) => message.content === "pending_notice").length, 1);
    assert.equal(sent.filter((message) => message.customType === "fixture_extension").length, 1);
    assert.equal(transformCalls, succeeds ? 2 : 1);
    assert.equal(refreshCalls, succeeds ? 2 : 1);
    assert.equal(sent.some((message) => message.role === "compactionSummary"), succeeds);
    if (!succeeds) assert.deepEqual(input, [{ role: "user", content: "fixture_input" }]);
  });
}
