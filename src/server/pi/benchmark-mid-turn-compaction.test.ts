import assert from "node:assert/strict";
import test from "node:test";
import type { AgentSession } from "@mariozechner/pi-coding-agent";
import { BenchmarkContextBudgetError, benchmarkInputLimit, estimateBenchmarkInputTokens } from "./compaction-budget";
import { installMidTurnCompaction } from "./mid-turn-compaction";

type FixtureMessage = Record<string, unknown>;
type FixtureEvent = { type: "compaction_end"; reason: "threshold"; result?: { summary: string }; aborted?: boolean };

function fixture(options: {
  usage?: unknown;
  enabled?: boolean;
  outcome?: "success" | "cancelled" | "ineffective";
  duringCompaction?: () => void;
  queue?: Promise<void>;
  onQueueRead?: () => void;
} = {}) {
  const listeners = new Set<(event: FixtureEvent) => void>();
  const state = {
    messages: [{ role: "user", content: `fixture-old-history ${"x".repeat(40_000)}` }] as FixtureMessage[],
    systemPrompt: "fixture-system",
    tools: []
  };
  let compactions = 0;
  let aborts = 0;
  let requests = 0;
  const session = {
    agent: {
      state,
      transformContext: async (messages: FixtureMessage[]) => [
        ...structuredClone(messages), { role: "custom", customType: "fixture_extension", content: "fixture-extension-output" }
      ]
    },
    model: { provider: "fixture", id: "fixture", contextWindow: 8_192, maxTokens: 1_024 },
    get messages() { return state.messages; },
    settingsManager: { getCompactionSettings: () => ({ enabled: options.enabled ?? true, reserveTokens: 2_048, keepRecentTokens: 819 }) },
    getContextUsage: () => options.usage ?? null,
    subscribe(listener: (event: FixtureEvent) => void) {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    abortCompaction: () => { aborts++; },
    get _agentEventQueue() {
      options.onQueueRead?.();
      return options.queue ?? Promise.resolve();
    },
    async _runAutoCompaction() {
      compactions++;
      options.duringCompaction?.();
      const outcome = options.outcome ?? "success";
      if (outcome === "success") state.messages = [{ role: "compactionSummary", summary: "fixture-compacted-history" }];
      else if (outcome === "ineffective") state.messages = structuredClone(state.messages);
      for (const listener of listeners) listener({
        type: "compaction_end", reason: "threshold", aborted: outcome === "cancelled",
        result: outcome === "cancelled" ? undefined : { summary: "fixture-compacted-history" }
      });
    }
  } as unknown as AgentSession;
  installMidTurnCompaction(session, async () => ({ investigationCapsule: "fixture-current-owner-and-pending-state" }), { samplingRefresh: true });
  return {
    session, state, listeners,
    get compactions() { return compactions; },
    get aborts() { return aborts; },
    get requests() { return requests; },
    async sample(messages: FixtureMessage[] = [...state.messages], signal?: AbortSignal) {
      const transformed = await session.agent.transformContext!(messages as never, signal) as unknown as FixtureMessage[];
      requests++;
      return transformed;
    }
  };
}

for (const usage of [null, { tokens: null, percent: null }, { tokens: 1, percent: null }]) {
  test(`benchmark estimates oversized input when SDK usage is ${JSON.stringify(usage)}`, async () => {
    const run = fixture({ usage });
    assert.ok(estimateBenchmarkInputTokens(run.session, run.state.messages) > benchmarkInputLimit(run.session));
    const sent = await run.sample();
    assert.equal(run.compactions, 1);
    assert.equal(run.requests, 1);
    assert.ok(estimateBenchmarkInputTokens(run.session, sent) <= benchmarkInputLimit(run.session));
    assert.equal(sent.some((message) => message.role === "compactionSummary"), true);
    assert.equal(sent.filter((message) => message.customType === "riftx_investigation_capsule").length, 1);
    assert.equal(sent.filter((message) => message.customType === "fixture_extension").length, 1);
    assert.equal(run.listeners.size, 0);
  });
}

for (const outcome of ["cancelled", "ineffective"] as const) {
  test(`benchmark blocks oversized sampling after ${outcome} compaction`, async () => {
    const run = fixture({ outcome });
    await assert.rejects(() => run.sample(), BenchmarkContextBudgetError);
    assert.equal(run.compactions, 1);
    assert.equal(run.requests, 0);
    assert.equal(run.listeners.size, 0);
  });
}

test("benchmark blocks oversized sampling when automatic compaction is disabled", async () => {
  const run = fixture({ enabled: false });
  await assert.rejects(() => run.sample(), BenchmarkContextBudgetError);
  assert.equal(run.compactions, 0);
  assert.equal(run.requests, 0);
});

test("benchmark propagates cancellation during compaction without sampling", async () => {
  const controller = new AbortController();
  const run = fixture({ outcome: "cancelled", duringCompaction: () => controller.abort() });
  await assert.rejects(() => run.sample(undefined, controller.signal), Error);
  assert.equal(run.compactions, 1);
  assert.equal(run.aborts, 1);
  assert.equal(run.requests, 0);
  assert.equal(run.listeners.size, 0);
});

test("benchmark replaces detached stale history after waiting for session events", async () => {
  let finishQueue!: () => void;
  let queueWasRead!: () => void;
  const queue = new Promise<void>((resolve) => { finishQueue = resolve; });
  const queueRead = new Promise<void>((resolve) => { queueWasRead = resolve; });
  const run = fixture({ queue, onQueueRead: queueWasRead });
  const staleHistory = [...run.state.messages];
  const pendingRequest = run.sample(staleHistory);
  await queueRead;
  run.state.messages = [{ role: "user", content: "fixture-settled-current-history" }];
  finishQueue();
  const sent = await pendingRequest;
  assert.equal(run.compactions, 0, "the event queue already reduced history below the input limit");
  assert.equal(run.requests, 1);
  assert.deepEqual(staleHistory, run.state.messages);
  assert.equal(sent.some((message) => message.content === "fixture-settled-current-history"), true);
  assert.equal(JSON.stringify(sent).includes("fixture-old-history"), false);
  assert.equal(sent.filter((message) => message.customType === "fixture_extension").length, 1);
  assert.equal(sent.filter((message) => message.customType === "riftx_investigation_capsule").length, 1);
  assert.ok(estimateBenchmarkInputTokens(run.session, sent) <= benchmarkInputLimit(run.session));
});
