import assert from "node:assert/strict";
import test from "node:test";
import type { AgentSession } from "@mariozechner/pi-coding-agent";
import { contextTokenRatio, estimateCompactedUsage, estimateMessagesContextUsage, estimateStaticContextTokens, installContextUsageTracking } from "./context-usage";

test("calibrates the actual transformed input including cached tokens, and survives a cut", async () => {
  const state = { systemPrompt: "system ".repeat(400), tools: [{ name: "read", description: "Read evidence", parameters: { type: "object" } }], messages: [{ role: "user", content: "hello", timestamp: 1 }] as unknown[] };
  const model = { provider: "fixture", id: "m1", contextWindow: 128_000 };
  const session = { model, get messages() { return state.messages; }, agent: {
    state, transformContext: async (messages: unknown[]) => [...messages, { role: "custom", content: "runtime packet ".repeat(200) }]
  }, sessionManager: { getBranch: () => [] } } as unknown as AgentSession;
  installContextUsageTracking(session);
  const sent = await session.agent.transformContext!(state.messages as never);
  const rawInput = estimateStaticContextTokens(session) + estimateMessagesContextUsage(sent, 0).tokens;
  state.messages.push({ role: "assistant", timestamp: 2, stopReason: "stop", content: "done", usage: { input: rawInput, cacheRead: rawInput, cacheWrite: 0, output: 100_000 } });
  assert.equal(contextTokenRatio(session), 2, "reasoning/output tokens are not part of the preceding request input");
  state.messages = [{ role: "compactionSummary", summary: "short checkpoint" }];
  const usage = estimateCompactedUsage(session, model.contextWindow);
  assert.equal(usage.tokens, 2 * (estimateStaticContextTokens(session) + estimateMessagesContextUsage(state.messages, 0).tokens));
  assert.equal(usage.source, "estimated");
  model.id = "m2";
  assert.equal(contextTokenRatio(session), 1, "do not reuse another model's calibration");
});
