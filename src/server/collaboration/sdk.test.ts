import assert from "node:assert/strict";
import test from "node:test";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { AuthStorage, DefaultResourceLoader, ModelRegistry, SessionManager, SettingsManager, createAgentSession, type ToolDefinition } from "@mariozechner/pi-coding-agent";
import { createAssistantMessageEventStream, registerApiProvider, unregisterApiProviders, type Context, type Model, type AssistantMessage } from "@mariozechner/pi-ai";
import { Type } from "@sinclair/typebox";
import { BoardStore } from "./store";
import { createSessionBoard, installBoardContext } from "./integration";
import { BOARD_TOOL_NAMES, createBoardTools, createBoardSpawnTool } from "./tools";
import type { BoardRuntime } from "./runtime";
import type { SessionRecord } from "@/server/pi/session-registry";
import { ApprovalGate } from "@/server/pi/approval-gate";
import { EventEmitter } from "node:events";
import { enqueueSessionAction } from "@/server/pi/session-join";

const sleep = () => new Promise((resolve) => setTimeout(resolve, 15));
async function until(predicate: () => boolean) { for (let i = 0; i < 600; i++) { if (predicate()) return; await sleep(); } assert.fail("Collaboration loop did not settle"); }

test("real SDK collaboration: shared evidence, clarification, compaction, pause/resume, review and finish", { timeout: 30000 }, async () => {
  const root = mkdtempSync(join(tmpdir(), "riftx-sdk-board-"));
  const store = new BoardStore(join(root, "board.sqlite"), "fixture", { create: true, maxConcurrent: 2 });
  let runtime!: BoardRuntime;
  const records: SessionRecord[] = [];
  const api = "riftx-collaboration-fixture";
  let resumed = false; let compactions = 0; let tools = 0; let sharedRead = false; let syntheticId = 0;
  const inFlight = new Set<string>();
  let summaryStarted!: () => void; const summaryReady = new Promise<void>((resolve) => { summaryStarted = resolve; });
  let releaseSummary!: () => void; const summaryGate = new Promise<void>((resolve) => { releaseSummary = resolve; });
  const stream = (model: Model<typeof api>, context: Context) => {
    const output = createAssistantMessageEventStream();
    const state = store.read();
    const actor = model.id;
    const content: AssistantMessage["content"] = [];
    const call = (name: string, args: Record<string, unknown>) => {
      if (syntheticId > 80) throw new Error(`Synthetic loop guard: ${actor}/${name}/${state.status}/${state.tasks.map((t) => t.status).join(",")}`);
      content.push({ type: "toolCall", id: `call-${++syntheticId}`, name, arguments: args });
    };
    if (!context.systemPrompt?.includes("Synthetic collaboration fixture")) {
      compactions++; content.push({ type: "text", text: "Investigation in progress. Restore authoritative shared board and inbox before continuing." });
    } else if (actor === "main") {
      if (state.tasks.length === 0) {
        call("task_manage", { action: "create", objective: "Inspect A", acceptance: "Publish an observation", assets: ["fixture.local"] });
        call("task_manage", { action: "create", objective: "Inspect B", acceptance: "Use A and clarify scope", assets: ["fixture.local"] });
      } else if (resumed && state.status !== "completed") {
        const review = state.tasks.find((t) => t.status === "awaiting_review" && !state.attempts.some((a) => a.taskId === t.id && a.status !== "settled"));
        const question = state.messages.find((m) => m.kind === "question" && m.status !== "replied");
        if (review) call("task_manage", { action: "approve", taskId: review.id, version: review.version });
        else if (question) call("agent_message", { to: question.from, kind: "answer", body: "Scope approved", replyTo: question.id });
        else if (state.tasks.every((t) => t.status === "done") && !state.messages.some((m) => m.status === "queued")) call("board_finish", {});
      }
    } else {
      const agent = state.agents.find((a) => a.id === actor)!;
      const task = state.tasks.find((t) => t.id === agent.taskId);
      if (task?.status === "running") {
        if (task.objective === "Inspect A") {
          if (!state.notes.some((n) => n.author === actor)) call("board_publish", { kind: "observation", body: "Asset A responds with fixture evidence", taskId: task.id, assets: ["fixture.local"], references: [{ type: "tool", id: "fixture-probe" }] });
          else call("task_update", { action: "submit", taskId: task.id, version: task.version, summary: "A verified", references: [{ type: "tool", id: "fixture-probe" }] });
        } else {
          const question = state.messages.find((m) => m.from === actor && m.kind === "question");
          if (!question) call("agent_message", { to: "main", kind: "question", body: "May I use asset A?", taskId: task.id });
          else if (question.status !== "replied") call("task_update", { action: "block", taskId: task.id, version: task.version, reason: `question:${question.id}` });
          else {
            sharedRead = JSON.stringify(context.messages).includes("fixture evidence");
            const last = context.messages.at(-1);
            if (last?.role === "toolResult" && last.toolName === "fixture_probe") call("task_update", { action: "submit", taskId: task.id, version: task.version, summary: "B verified using A" });
            else if (context.messages.some((m) => m.role === "toolResult" && m.toolName === "fixture_probe")) call("task_update", { action: "submit", taskId: task.id, version: task.version, summary: "B verified using A" });
            else call("fixture_probe", {});
          }
        }
      }
    }
    if (!content.length) content.push({ type: "text", text: "Phase complete; board state remains authoritative." });
    const response: AssistantMessage = { role: "assistant", api, provider: "fixture", model: actor, content, timestamp: Date.now(), stopReason: content.some((c) => c.type === "toolCall") ? "toolUse" : "stop",
      usage: { input: 100, output: 10, cacheRead: 0, cacheWrite: 0, totalTokens: 110, cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } } };
    if (!context.systemPrompt?.includes("Synthetic collaboration fixture")) { summaryStarted(); void summaryGate.then(() => output.end(response)); }
    else output.end(response);
    return output;
  };
  registerApiProvider({ api, stream, streamSimple: stream }, api);
  async function create(actor: string) {
    const model: Model<typeof api> = { id: actor, name: actor, api, provider: "fixture", baseUrl: "http://unused", input: ["text"], reasoning: false, contextWindow: 32000, maxTokens: 4096, cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 } };
    const auth = AuthStorage.inMemory(); auth.setRuntimeApiKey("fixture", "synthetic");
    const registry = ModelRegistry.inMemory(auth);
    const settings = SettingsManager.inMemory({ compaction: { enabled: false, reserveTokens: 4096, keepRecentTokens: 100 } });
    // Fully isolated loader: no repository prompts, skills or context files.
    const loader = new DefaultResourceLoader({ cwd: root, agentDir: root, settingsManager: settings, noExtensions: true, noSkills: true, noPromptTemplates: true, noContextFiles: true, noThemes: true, systemPrompt: "Synthetic collaboration fixture" });
    await loader.reload();
    const customTools: ToolDefinition[] = [...createBoardTools(() => runtime, actor), ...(actor === "main" ? [createBoardSpawnTool(() => runtime)] : []), {
      name: "fixture_probe", label: "Probe", description: "Synthetic probe", parameters: Type.Object({}), execute: async () => { tools++; return { content: [{ type: "text", text: "fixture evidence" }], details: {} }; }
    }];
    const { session } = await createAgentSession({ cwd: root, agentDir: root, model, authStorage: auth, modelRegistry: registry, settingsManager: settings, sessionManager: SessionManager.create(root, join(root, actor)), resourceLoader: loader, customTools, tools: [...BOARD_TOOL_NAMES, "spawn_subagent", "fixture_probe"] });
    const record = { id: session.sessionId, session, sessionManager: session.sessionManager, gate: new ApprovalGate(), profile: { provider: "fixture", model: actor }, emitter: new EventEmitter(), unsubscribe: () => {}, collaborationActor: actor } as unknown as SessionRecord;
    session.subscribe((event) => {
      if (event.type === "agent_start") { assert.ok(!inFlight.has(actor), "one model entry per actor"); inFlight.add(actor); }
      if (event.type === "agent_end") inFlight.delete(actor);
    });
    records.push(record); return record;
  }
  try {
    const main = await create("main");
    runtime = createSessionBoard(main, store, async (identity) => {
      const record = await create(identity.id); installBoardContext(record, runtime, identity.id); return record;
    });
    installBoardContext(main, runtime, "main");
    await enqueueSessionAction(main, async () => { await runtime.beginUserTurn(); try { await main.session.prompt("Investigate assets collaboratively"); } finally { runtime.userTurnEnded(); } });
    await until(() => store.read().tasks.some((w) => w.status === "blocked") && store.read().tasks.some((w) => w.status === "awaiting_review") && store.read().attempts.every((a) => a.status === "settled"));
    await runtime.pause();
    const wakes = store.read().used.wakes;
    store.apply("user", "continue-message", "message", { to: "main", kind: "information", body: "Proceed after recovery" });
    const compaction = main.session.compact();
    await summaryReady;
    store.apply("user", "during-summary", "message", { to: "main", kind: "information", body: "Update received during compaction" });
    assert.equal(store.read().messages.at(-1)?.status, "queued");
    releaseSummary(); await compaction;
    assert.equal(compactions, 1); assert.equal(store.read().used.wakes, wakes);
    resumed = true; store.apply("user", "resume", "control", { action: "resume" });
    await until(() => store.read().status === "completed");
    assert.ok(sharedRead, "sibling evidence appears in restored worker context");
    assert.equal(tools, 1); assert.equal(store.read().tasks.length, 2);
    assert.ok(store.read().tasks.every((w) => w.status === "done"));
    assert.ok(store.read().messages.every((m) => m.status !== "queued"));
    assert.equal(store.read().tasks.find((w) => w.objective === "Inspect B")?.retries, 0);
  } finally {
    releaseSummary();
    await runtime?.close();
    for (const r of records) { await r.session.abort(); r.session.dispose(); }
    store.close(); unregisterApiProviders(api); rmSync(root, { recursive: true, force: true });
  }
});
