import assert from "node:assert/strict";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import {
  AgentSession, AuthStorage, createAgentSession, DefaultResourceLoader, ModelRegistry,
  SessionManager, SettingsManager, type AgentSessionEvent
} from "@mariozechner/pi-coding-agent";
import {
  createAssistantMessageEventStream, registerApiProvider, unregisterApiProviders,
  type AssistantMessage, type Context, type Model
} from "@mariozechner/pi-ai";
import { createPentestCompactionExtension } from "./pentest-compaction";
import { installMidTurnCompaction } from "./mid-turn-compaction";
import type { ContinuityContext } from "./continuity-context";
import { waitForAgentEvents } from "./pi-internals";

type HistoryMessage = {
  role: string;
  content?: string | readonly { type: string; id?: string; text?: string }[];
  summary?: string;
  toolCallId?: string;
};

function textOf(message: HistoryMessage): string {
  if (message.role === "compactionSummary") return message.summary ?? "";
  return typeof message.content === "string" ? message.content
    : (message.content ?? []).filter((part) => part.type === "text").map((part) => part.text ?? "").join("\n");
}

function assertToolPairs(messages: readonly HistoryMessage[], expectedId: string) {
  const calls = new Set<string>();
  const results = new Set<string>();
  for (const message of messages) {
    if (message.role === "assistant" && Array.isArray(message.content)) {
      for (const part of message.content) if (part.type === "toolCall") calls.add(part.id);
    }
    if (message.role === "toolResult") {
      assert.ok(calls.has(message.toolCallId!), "replayed tool results need their preceding assistant tool call");
      results.add(message.toolCallId!);
    }
  }
  assert.ok(calls.has(expectedId), "the latest completed tool call must remain available");
  assert.ok(results.has(expectedId), "the latest completed tool result must remain available");
  assert.deepEqual([...calls].sort(), [...results].sort(), "retained completed exchanges must keep both halves");
}

test("benchmark fallback compactions persist, replay tool pairs and continue the same Pi session", { timeout: 60_000 }, async (t) => {
  const directory = await mkdtemp(join(tmpdir(), "riftx-benchmark-compaction-"));
  const api = "riftx-benchmark-fallback-integration";
  const model: Model<typeof api> = {
    id: "fixture", name: "fixture", api, provider: "riftx-fallback-fixture", baseUrl: "http://unused.invalid",
    input: ["text"], reasoning: false, contextWindow: 128_000, maxTokens: 8_192,
    cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 }
  };
  const usage: AssistantMessage["usage"] = {
    input: 100, output: 10, cacheRead: 0, cacheWrite: 0, totalTokens: 110,
    cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 }
  };
  const assistant = (content: AssistantMessage["content"], stopReason: AssistantMessage["stopReason"] = "stop"): AssistantMessage => ({
    role: "assistant", api, provider: model.provider, model: model.id,
    content, stopReason, usage: structuredClone(usage), timestamp: Date.now()
  });
  let mode: "error" | "invalid" | "throw" | "continue" = "error";
  let providerCalls = 0;
  let continuationSawCheckpoint = false;
  let continuationSawRequest = false;
  let warningCount = 0;
  t.mock.method(console, "warn", () => { warningCount++; });
  const stream = (_model: Model<typeof api>, context: Context) => {
    providerCalls++;
    // Failure modes do not inspect or retain the summarizer request. No prompt
    // templates, validation headings, or skill bodies are read by this fixture.
    if (mode === "throw") throw new Error("fixture provider setup failure");
    if (mode === "continue") {
      continuationSawCheckpoint = context.messages.some((message) => textOf(message).includes("fixture-benchmark-checkpoint"));
      continuationSawRequest = context.messages.some((message) => textOf(message).includes("fixture-continue-after-fallback"));
      assertToolPairs(context.messages, "fixture-tool-3");
    }
    const message = assistant([{ type: "text", text: mode === "continue" ? "fixture-continuation-complete" : "invalid-fixture-summary" }], mode === "error" ? "error" : "stop");
    if (mode === "error") message.errorMessage = "fixture summarizer error";
    const output = createAssistantMessageEventStream();
    if (message.stopReason === "error") output.push({ type: "error", reason: "error", error: message });
    else output.push({ type: "done", reason: "stop", message });
    output.end(message);
    return output;
  };
  let session: AgentSession | undefined;
  const authStorage = AuthStorage.inMemory();
  authStorage.setRuntimeApiKey(model.provider, "fixture-api-key");
  const modelRegistry = ModelRegistry.inMemory(authStorage);
  registerApiProvider({ api, stream, streamSimple: stream }, api);
  const ledger = { phase: "revisit", owner: "subagent:fixture-worker", challenge: "fixture-A", attemptCount: 2, correctFlagCount: 1 };
  const pending = new Map([["fixture-submission", { challenge: "fixture-A", candidate: "fixture-private-candidate", submitted: false }]]);
  const stateBefore = structuredClone({ ledger, pending });
  const ledgerReference = ledger;
  const pendingReference = pending;
  const continuity: ContinuityContext = {
    taskContract: "fixture-task-contract", skillContext: "",
    investigationCapsule: "fixture-continuity challenge=fixture-A owner=subagent:fixture-worker pending=1",
    progressCheckpoint: "fixture-stage-one-confirmed"
  };
  let fallbackCalls = 0;
  const manager = SessionManager.create(directory, join(directory, "sessions"));
  const settingsManager = SettingsManager.inMemory({
    compaction: { enabled: true, reserveTokens: 16_384, keepRecentTokens: 12_800 },
    retry: { enabled: false }
  });
  const extension = createPentestCompactionExtension({
    getSession: () => session, modelRegistry, getActiveSkills: () => [],
    benchmarkFallback: {
      buildSummary: (maxChars) => {
        fallbackCalls++;
        return `fixture-benchmark-checkpoint ${JSON.stringify(ledger)} pending=${pending.size}`.slice(0, maxChars);
      },
      getContinuityContext: async () => continuity
    }
  });
  const loader = new DefaultResourceLoader({
    cwd: directory, agentDir: join(directory, "agent"), settingsManager,
    noExtensions: true, noSkills: true, noPromptTemplates: true, noThemes: true, noContextFiles: true,
    extensionFactories: [extension], systemPrompt: "Offline synthetic integration fixture."
  });
  try {
    await loader.reload();
    const created = await createAgentSession({
      cwd: directory, agentDir: join(directory, "agent"), authStorage, modelRegistry, model,
      thinkingLevel: "off", noTools: "all", tools: [], resourceLoader: loader, sessionManager: manager, settingsManager
    });
    session = created.session;
    installMidTurnCompaction(session, async () => continuity, { samplingRefresh: true });
    const events: AgentSessionEvent[] = [];
    session.subscribe((event) => { if (event.type === "compaction_end") events.push(event); });
    const originalSession = session;
    const originalSessionId = session.sessionId;
    const compact = (AgentSession.prototype as unknown as {
      _runAutoCompaction(reason: "threshold", willRetry: boolean): Promise<void>;
    })._runAutoCompaction;
    const failures = ["error", "invalid", "throw", "error"] as const;
    for (let round = 0; round < failures.length; round++) {
      mode = failures[round];
      for (let index = 0; index < 48; index++) {
        manager.appendMessage({ role: "user", content: `fixture-history-${round}-${index} ${"x".repeat(4_000)}`, timestamp: Date.now() });
      }
      manager.appendMessage({ role: "user", content: `fixture-exchange-${round}`, timestamp: Date.now() });
      manager.appendMessage(assistant([{ type: "toolCall", id: `fixture-tool-${round}`, name: "fixture_tool", arguments: { round } }], "toolUse"));
      manager.appendMessage({ role: "toolResult", toolCallId: `fixture-tool-${round}`, toolName: "fixture_tool", content: [{ type: "text", text: `fixture-result-${round}` }], isError: false, timestamp: Date.now() });
      manager.appendMessage(assistant([{ type: "text", text: `fixture-finished-exchange-${round}` }]));
      session.agent.state.messages = manager.buildSessionContext().messages;
      const beforeCount = session.messages.length;
      const callsBefore = providerCalls;
      await compact.call(session, "threshold", false);
      await waitForAgentEvents(session);
      const end = events.at(-1);
      assert.ok(end?.type === "compaction_end" && end.result, "failed model summarization must yield a usable benchmark fallback");
      assert.equal(end.aborted, false);
      assert.ok(end.result.summary.includes("fixture-benchmark-checkpoint"));
      assert.equal(providerCalls, callsBefore + 1, "fallback must not request a second model summary");
      assert.equal(manager.getBranch().filter((entry) => entry.type === "compaction").length, round + 1);
      assert.ok(session.messages.length < beforeCount, "fallback must remove historical messages from the active request");
      assertToolPairs(session.messages, `fixture-tool-${round}`);
      assert.strictEqual(ledger, ledgerReference);
      assert.strictEqual(pending, pendingReference);
      assert.deepEqual({ ledger, pending }, stateBefore, "compaction must not change ownership, scoring or pending submissions");
      const sessionFile = manager.getSessionFile();
      assert.ok(sessionFile);
      const replay = SessionManager.open(sessionFile, join(directory, "reopened"));
      assert.deepEqual(replay.buildSessionContext().messages, manager.buildSessionContext().messages);
      assertToolPairs(replay.buildSessionContext().messages, `fixture-tool-${round}`);
      assert.ok(replay.buildSessionContext().messages.some((message) => textOf(message).includes("fixture-benchmark-checkpoint")));
    }
    assert.ok(fallbackCalls >= failures.length);
    mode = "continue";
    const callsBeforeContinue = providerCalls;
    await session.prompt("fixture-continue-after-fallback", { expandPromptTemplates: false });
    await waitForAgentEvents(session);
    assert.strictEqual(session, originalSession);
    assert.equal(session.sessionId, originalSessionId);
    assert.equal(providerCalls, callsBeforeContinue + 1);
    assert.equal(continuationSawCheckpoint, true);
    assert.equal(continuationSawRequest, true);
    assert.equal(textOf(session.messages.at(-1)!), "fixture-continuation-complete");
    assert.deepEqual({ ledger, pending }, stateBefore);
    const finalReplay = SessionManager.open(manager.getSessionFile()!, join(directory, "final-replay"));
    assert.deepEqual(finalReplay.buildSessionContext().messages, manager.buildSessionContext().messages);
    assert.equal(textOf(finalReplay.buildSessionContext().messages.at(-1)!), "fixture-continuation-complete");
    t.diagnostic(JSON.stringify({ fallbackRounds: failures.length, suppressedWarnings: warningCount }));
  } finally {
    session?.dispose();
    unregisterApiProviders(api);
    await rm(directory, { recursive: true, force: true });
  }
});
