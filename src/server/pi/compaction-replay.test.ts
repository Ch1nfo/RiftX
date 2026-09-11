import assert from "node:assert/strict";
import test from "node:test";
import { SessionManager, buildSessionContext, type AgentSession, type CompactionResult, type ExtensionAPI, type ModelRegistry, type SessionBeforeCompactEvent } from "@mariozechner/pi-coding-agent";
import { createAssistantMessageEventStream, registerApiProvider, unregisterApiProviders, type AssistantMessage, type Context, type Model } from "@mariozechner/pi-ai";
import { createPentestCompactionExtension } from "./pentest-compaction";
import { PENTEST_COMPACTION_SYSTEM_PROMPT, REQUIRED_SECTIONS } from "./compaction-prompt";
import { estimateCompactedUsage } from "./context-usage";
import { upsertContinuityContext } from "./continuity-context";

test("five compactions retain evidence and constraints, restore raw detail and fail atomically", async () => {
  const api = "riftx-compaction-replay";
  const model: Model<typeof api> = { id: "fixture", name: "fixture", api, provider: "fixture", baseUrl: "http://unused", input: ["text"], reasoning: false, contextWindow: 128_000, maxTokens: 1000, cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 } };
  const keyFacts = ["Only perform read-only checks; never modify target data.", "request:r-start ruled out anonymous access.", "Compare role B using the same endpoint next."];
  const liveFact = "Current owner is main and the active challenge has exactly one running container.";
  const continuity = { investigationCapsule: `## Current task\n${liveFact}`, skillContext: "LIVE_SKILL_CONTEXT_DO_NOT_SUMMARIZE" };
  let calls = 0;
  let mode: "repair" | "stop" | "length" | "missing" | "abort" = "repair";
  let controller = new AbortController();
  const stream = (_model: Model<typeof api>, context: Context) => {
    calls += 1;
    assert.equal(context.systemPrompt, PENTEST_COMPACTION_SYSTEM_PROMPT, "repair keeps the domain contract");
    const prompt = JSON.stringify(context.messages);
    assert.doesNotMatch(prompt, /STALE_SKILL_CONTEXT|LIVE_SKILL_CONTEXT/);
    const request = context.messages[0].content;
    const text = typeof request === "string" ? request : request.filter((part) => part.type === "text").map((part) => part.text).join("\n");
    const protectedFacts = JSON.parse(/<protected-facts>\n([\s\S]*?)\n<\/protected-facts>/.exec(text)?.[1] ?? "[]") as string[];
    let content = REQUIRED_SECTIONS.map((heading) => `${heading}\nRecorded checkpoint state.`).join("\n");
    if (mode !== "missing") content += "\n" + [...new Set([...keyFacts.filter((fact) => text.includes(fact)), ...protectedFacts])].join("\n");
    content += `\n${liveFact}`;
    if (mode === "repair" && calls === 1) content = REQUIRED_SECTIONS.join("\n");
    if (mode === "abort") controller.abort();
    const output = createAssistantMessageEventStream();
    output.end({ role: "assistant", api, provider: "fixture", model: "fixture", content: [{ type: "text", text: content }], timestamp: calls,
      stopReason: mode === "length" ? "length" : "stop", usage: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, totalTokens: 0, cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } } } as AssistantMessage);
    return output;
  };
  registerApiProvider({ api, stream, streamSimple: stream }, api);
  try {
    const manager = SessionManager.inMemory();
    manager.appendMessage({ role: "user", content: keyFacts.join("\n"), timestamp: 1 });
    manager.appendCustomMessageEntry("riftx_skill_context", "STALE_SKILL_CONTEXT", false);
    const state = { systemPrompt: "system ".repeat(2000), tools: [], messages: manager.buildSessionContext().messages };
    const session = { model, thinkingLevel: "off", sessionManager: manager, agent: { state }, get messages() { return state.messages; } } as unknown as AgentSession;
    let handler!: (event: SessionBeforeCompactEvent) => Promise<{ compaction?: CompactionResult; cancel?: boolean } | undefined>;
    await createPentestCompactionExtension({ getSession: () => session, getContinuityContext: async () => continuity, getActiveSkills: () => [],
      modelRegistry: { getApiKeyAndHeaders: async () => ({ ok: true, apiKey: "fixture" }) } as unknown as ModelRegistry
    })({ on: (_name: string, callback: typeof handler) => { handler = callback; } } as unknown as ExtensionAPI);
    const makeEvent = (): SessionBeforeCompactEvent => {
      const branch = manager.getBranch();
      return { type: "session_before_compact", signal: controller.signal, branchEntries: branch, preparation: {
        firstKeptEntryId: branch.at(-12)!.id, messagesToSummarize: buildSessionContext(branch, branch.at(-13)!.id).messages,
        turnPrefixMessages: [], isSplitTurn: false, tokensBefore: 100_000, fileOps: { read: new Set(), written: new Set(), edited: new Set() },
        settings: { enabled: true, reserveTokens: 16384, keepRecentTokens: 12800 }
      } };
    };
    for (let round = 0; round < 5; round += 1) {
      for (let i = 0; i < 48; i += 1) manager.appendMessage({ role: "user", timestamp: 10 + round * 100 + i, content: `Raw observation ${round}/${i}: ${"x".repeat(4000)}` });
      state.messages = manager.buildSessionContext().messages;
      upsertContinuityContext(state.messages, continuity);
      const event = makeEvent();
      const result = await handler(event);
      assert.ok(result?.compaction);
      const compaction = result.compaction;
      const budget = (compaction.details as { riftx: { budget: { recentTokens: number; postCompactionTokens: number; keepRecentTokens: number; initialKeepRecentTokens: number } } }).riftx.budget;
      assert.ok(budget.recentTokens <= model.contextWindow * 0.1);
      assert.ok(budget.postCompactionTokens <= model.contextWindow * 0.13);
      assert.ok(budget.keepRecentTokens > budget.initialKeepRecentTokens);
      manager.appendCompaction(compaction.summary, compaction.firstKeptEntryId, compaction.tokensBefore, compaction.details);
      state.messages = manager.buildSessionContext().messages;
      assert.equal(estimateCompactedUsage(session, model.contextWindow).tokens, budget.postCompactionTokens,
        "compaction_end includes the packet even before asynchronous restoration");
      upsertContinuityContext(state.messages, continuity);
      const restored = JSON.stringify(state.messages);
      for (const fact of keyFacts) assert.ok(restored.includes(fact), `round ${round} lost ${fact}`);
      assert.equal(restored.split(liveFact).length - 1, 1, "fresh state is not duplicated into the summary");
      assert.match(restored, new RegExp(`Raw observation ${round}/47`));
      assert.equal(estimateCompactedUsage(session, model.contextWindow).tokens, budget.postCompactionTokens);
    }
    assert.equal(calls, 6, "only the malformed first response needed a repair");
    for (const failure of ["length", "missing", "abort"] as const) {
      mode = failure;
      controller = new AbortController();
      const before = manager.getBranch();
      const beforeCalls: number = calls;
      const event = makeEvent();
      const preparation = structuredClone(event.preparation);
      assert.deepEqual(await handler(event), { cancel: true });
      assert.deepEqual(manager.getBranch(), before);
      assert.deepEqual(event.preparation, preparation);
      assert.equal(calls - beforeCalls, failure === "abort" ? 1 : 2);
    }
  } finally { unregisterApiProviders(api); }
});
