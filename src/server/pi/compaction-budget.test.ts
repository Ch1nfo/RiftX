import assert from "node:assert/strict";
import test from "node:test";
import { SessionManager, buildSessionContext, type AgentSession, type CompactionEntry, type CompactionResult, type SessionBeforeCompactEvent } from "@mariozechner/pi-coding-agent";
import { assertBenchmarkSamplingAllowed, benchmarkInputLimit, benchmarkReserveTokens, BenchmarkContextBudgetError, blockBenchmarkSampling, clearBenchmarkCompactionFailure, estimateBenchmarkInputTokens, fitBenchmarkCompaction } from "./compaction-budget";
import { prepareCompactionWithBudget } from "./pi-internals";

async function fixture(keepRecentTokens = 8_000, reserveTokens = 8_192) {
  const manager = SessionManager.inMemory();
  for (let i = 0; i < 48; i++) {
    manager.appendMessage({ role: "user", content: "fixture_" + i + "x".repeat(3_800), timestamp: i * 3 });
    manager.appendMessage({ role: "assistant", content: [{ type: "toolCall", id: "call_" + i, name: "write", arguments: { path: "/tmp/fixture-" + i, content: "fixture" } }],
      api: "openai-completions", provider: "fixture", model: "fixture", stopReason: "toolUse", timestamp: i * 3 + 1,
      usage: { input: 0, output: 0, totalTokens: 0, cacheRead: 0, cacheWrite: 0, cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } } });
    manager.appendMessage({ role: "toolResult", toolCallId: "call_" + i, toolName: "write", content: [{ type: "text", text: "fixture_result" }], isError: false, timestamp: i * 3 + 2 });
  }
  const settings = { enabled: true, reserveTokens, keepRecentTokens };
  const branchEntries = manager.getBranch();
  const preparation = await prepareCompactionWithBudget(branchEntries, settings);
  assert(preparation);
  const state = { systemPrompt: "fixture_system", tools: [], messages: manager.buildSessionContext().messages };
  const session = {
    model: { id: "fixture", provider: "fixture", contextWindow: 64_000, maxTokens: 4_096 },
    sessionManager: manager, agent: { state }, get messages() { return state.messages; },
    settingsManager: { getCompactionSettings: () => settings }
  } as unknown as AgentSession;
  const event: SessionBeforeCompactEvent = { type: "session_before_compact", branchEntries, preparation, signal: new AbortController().signal };
  const result: CompactionResult = { summary: "fixture_fallback", firstKeptEntryId: preparation.firstKeptEntryId, tokensBefore: preparation.tokensBefore,
    details: { readFiles: [], modifiedFiles: [...preparation.fileOps.written], riftx: { version: 1, activeSkills: [], mode: "deterministic_fallback" } } };
  return { session, event, result, manager };
}

test("benchmark output reserve scales to the configured output without overwhelming small windows", () => {
  assert.equal(benchmarkReserveTokens(1_000_000, 16_384, 16_384), 65_536);
  assert.equal(benchmarkReserveTokens(128_000, 8_192, 16_384), 16_384);
  assert.equal(benchmarkReserveTokens(128_000, 40_000, 16_384), 46_400);
  assert.equal(benchmarkReserveTokens(8_192, 1_024, 16_384), 4_096);
});

test("budget preview includes the fresh context exactly once and never mutates the branch", async () => {
  const { session, event, result, manager } = await fixture();
  const before = structuredClone(manager.getBranch());
  const fitted = await fitBenchmarkCompaction(session, event, result, { investigationCapsule: "fixture_live".repeat(50) }, () => "fixture_small");
  assert.equal(fitted.firstKeptEntryId, result.firstKeptEntryId);
  assert.deepEqual(manager.getBranch(), before);
  const budget = (fitted.details as { riftx: { budget: { inputTokensAfter: number; inputTokensBefore: number; recut: boolean } } }).riftx.budget;
  assert(budget.inputTokensAfter < budget.inputTokensBefore);
  assert(budget.inputTokensAfter <= benchmarkInputLimit(session));
  assert.equal(budget.recut, false);
});

test("one smaller SDK cut preserves tool pairs and newly summarized file references", async () => {
  const { session, event, result } = await fixture(42_000, 32_000);
  const fitted = await fitBenchmarkCompaction(session, event, result, {}, () => "fixture_small");
  assert.notEqual(fitted.firstKeptEntryId, result.firstKeptEntryId);
  const details = fitted.details as { modifiedFiles: string[]; riftx: { budget: { recut: boolean; inputTokensAfter: number } } };
  assert.equal(details.riftx.budget.recut, true);
  assert(details.riftx.budget.inputTokensAfter <= benchmarkInputLimit(session));
  assert(details.modifiedFiles.length > (result.details as { modifiedFiles: string[] }).modifiedFiles.length);
  const entry: CompactionEntry = { type: "compaction", id: "fitted", parentId: event.branchEntries.at(-1)!.id, timestamp: new Date().toISOString(), ...fitted };
  const messages = buildSessionContext([...event.branchEntries, entry]).messages;
  const calls = new Set<string>();
  for (const message of messages) {
    if (message.role === "assistant") for (const part of message.content) if (part.type === "toolCall") calls.add(part.id);
    if (message.role === "toolResult") assert(calls.has(message.toolCallId));
  }
  assert(messages.some((message) => message.role === "toolResult" && message.toolCallId === "call_47"));
});

test("fixed context that cannot fit is rejected without appending or changing history", async () => {
  const { session, event, result, manager } = await fixture();
  session.agent.state.systemPrompt = "x".repeat(64_000 * 4);
  const before = structuredClone(manager.getBranch());
  await assert.rejects(fitBenchmarkCompaction(session, event, result, {}, () => "fixture_small"), BenchmarkContextBudgetError);
  assert.deepEqual(manager.getBranch(), before);
});

test("normal model summaries cannot silently discard an additional unsummarized tail", async () => {
  const { session, event, result } = await fixture(42_000, 32_000);
  await assert.rejects(fitBenchmarkCompaction(session, event, result, {}), BenchmarkContextBudgetError);
});

test("stale SDK usage from an earlier compaction cannot make context growth pass validation", async () => {
  const { session, manager } = await fixture();
  const retainedId = manager.appendMessage({ role: "assistant", content: [{ type: "text", text: "fixture_retained" }],
    api: "openai-completions", provider: "fixture", model: "fixture", stopReason: "stop", timestamp: 200,
    usage: { input: 100_000, output: 5, totalTokens: 100_005, cacheRead: 0, cacheWrite: 0, cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } } });
  manager.appendCompaction("tiny", retainedId, 100_005);
  manager.appendMessage({ role: "user", content: "continue", timestamp: 201 });
  session.agent.state.messages = manager.buildSessionContext().messages;
  const branchEntries = manager.getBranch();
  const preparation = await prepareCompactionWithBudget(branchEntries, session.settingsManager.getCompactionSettings());
  assert(preparation);
  const actualBefore = estimateBenchmarkInputTokens(session, session.messages);
  assert(actualBefore < 100);
  assert(preparation.tokensBefore > 100_000);
  const event: SessionBeforeCompactEvent = { type: "session_before_compact", branchEntries, preparation, signal: new AbortController().signal };
  const result = { summary: "s".repeat(2_000), firstKeptEntryId: preparation.firstKeptEntryId, tokensBefore: preparation.tokensBefore };
  await assert.rejects(fitBenchmarkCompaction(session, event, result, {}), BenchmarkContextBudgetError);
});

test("sampling failure stays blocked until configuration recovery or a model change", async () => {
  const { session } = await fixture();
  const failure = new Error("fixture_failure");
  blockBenchmarkSampling(session, failure);
  assert.throws(() => assertBenchmarkSamplingAllowed(session), (error) => error === failure);
  assert.throws(() => assertBenchmarkSamplingAllowed(session), (error) => error === failure);
  clearBenchmarkCompactionFailure(session);
  assert.doesNotThrow(() => assertBenchmarkSamplingAllowed(session));
  blockBenchmarkSampling(session, failure);
  session.model!.id = "fixture_changed";
  assert.doesNotThrow(() => assertBenchmarkSamplingAllowed(session));
});
