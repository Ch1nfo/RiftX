import assert from "node:assert/strict";
import test, { type TestContext } from "node:test";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { randomUUID } from "node:crypto";
import { BoardStore } from "./store";
import { BoardRuntime, collaborationContext, type CollaborationActor } from "./runtime";
import type { BoardAgent, BoardMessage, BoardWork } from "@/lib/collaboration";

const sleep = (ms = 5) => new Promise((resolve) => setTimeout(resolve, ms));
async function until(check: () => boolean, message = "condition did not settle") {
  for (let n = 0; n < 200; n++) { if (check()) return; await sleep(); }
  assert.fail(message);
}
function fixture(t: TestContext, factory?: (id: string, runtime: BoardRuntime) => Promise<CollaborationActor> | CollaborationActor) {
  const dir = mkdtempSync(join(tmpdir(), "riftx-runtime-"));
  const store = new BoardStore(join(dir, "board.sqlite"), "test", { create: true, maxConcurrent: 2 });
  const runs: string[] = []; const releases: string[] = [];
  const runtime: BoardRuntime = new BoardRuntime(store, { actor: async (id): Promise<CollaborationActor> => factory ? factory(id, runtime) : ({ busy: () => false,
    run: async (packet) => { runs.push(id); runtime.included(id, packet.messageIds); return { summary: "Result" }; }, steer: async () => {}, stop: async () => true }),
    release: async (id) => { releases.push(id); }, emit: () => {} }, 5);
  t.after(async () => { await runtime.close(); rmSync(dir, { recursive: true, force: true }); });
  const create = (objective = "Investigate A") => store.apply("main", randomUUID(), "create", { objective }, { epoch: store.read().epoch }) as BoardWork;
  return { store, runtime, runs, releases, create };
}

test("idle boards never poll models; workers are reused and results await main review", async (t) => {
  const { store, runtime, runs, create } = fixture(t);
  runtime.kick(); await sleep(25); assert.equal(runs.length, 0);
  const first = create(); await until(() => store.read().tasks[0].status === "awaiting_review");
  await until(() => runs.includes("main"));
  const children = runs.filter((id) => id !== "main"); assert.equal(children.length, 1);
  const task = store.read().tasks.find((v) => v.id === first.id)!;
  store.apply("main", "accept", "manage", { taskId: task.id, version: task.version, action: "approve" }, { epoch: store.read().epoch });
  store.apply("user", "resume-next", "control", { action: "resume" });
  create("Investigate B"); await until(() => runs.filter((id) => id !== "main").length === 2);
  assert.equal(store.read().agents.filter((a) => a.role === "child").length, 1);
  await until(() => runs.filter((id) => id === "main").length === 2);
  await until(() => runtime.runningCount === 0); const count = runs.length;
  runtime.kick(); await sleep(30); assert.equal(runs.length, count, "unchanged review state does not loop");
});

test("pause during worker construction cannot start a late model round", async (t) => {
  let release!: () => void; let constructing = false; let calls = 0;
  const gate = new Promise<void>((resolve) => { release = resolve; });
  const { store, runtime, create } = fixture(t, async () => {
    constructing = true; await gate;
    return { busy: () => false, run: async () => { calls++; return {}; }, steer: async () => {}, stop: async () => true };
  });
  create(); await until(() => constructing);
  await runtime.pause(); release(); await sleep(30);
  assert.equal(calls, 0); assert.equal(store.read().status, "paused"); assert.equal(store.read().used.wakes, 0);
});

test("messages are durable through pause and budget exhaustion prevents model calls", async (t) => {
  const { store, runtime, runs } = fixture(t);
  await runtime.pause();
  store.apply("user", "message", "message", { to: "main", kind: "information", body: "Keep this" });
  await sleep(20); assert.equal(runs.length, 0); assert.equal(store.read().messages[0].status, "queued");
  store.apply("user", "resume", "control", { action: "resume" });
  await until(() => runs.length === 1); assert.equal(store.read().messages[0].status, "in_context");
  store.apply("user", "limit", "control", { action: "limits", wakes: 1 });
  store.apply("user", "message2", "message", { to: "main", kind: "information", body: "Second" });
  await until(() => store.read().status === "waiting_user"); assert.equal(runs.length, 1);
  assert.equal(store.read().used.wakes, 1); assert.equal(store.read().messages[1].status, "queued");
});

test("streaming delivery uses steer and does not spend an additional wake", async (t) => {
  let finish!: () => void; let running = false; let steers = 0;
  const done = new Promise<void>((resolve) => { finish = resolve; });
  const { store, runtime } = fixture(t, (id, rt) => ({ busy: () => running,
    run: async (packet) => { running = true; rt.included(id, packet.messageIds); await done; running = false; return {}; },
    steer: async (packet) => { steers++; rt.included(id, packet.messageIds); }, stop: async () => { finish(); return true; } }));
  store.apply("user", "start", "message", { to: "main", kind: "information", body: "Begin" });
  await until(() => running);
  store.apply("user", "steer", "message", { to: "main", kind: "information", body: "Update" });
  await until(() => steers > 0); assert.equal(store.read().used.wakes, 1);
  finish(); await until(() => runtime.runningCount === 0);
});

test("late settlement is fenced from a new execution and cancellation stays terminal", (t) => {
  const { store, create } = fixture(t); const task = create();
  const a = store.apply("system", "register", "register_agent") as BoardAgent;
  const old = store.apply("system", "wake", "wake", { agentId: a.id }) as BoardAgent;
  let work = store.apply(a.id, "claim", "claim", { taskId: task.id }, { epoch: 1, executionId: old.executionId }) as BoardWork;
  store.apply("system", "settle", "settle", { agentId: a.id, executionId: old.executionId, error: true });
  work = store.read().tasks[0];
  store.apply("main", "retry", "manage", { taskId: task.id, version: work.version, action: "retry" }, { epoch: 1 });
  const next = store.apply("system", "wake2", "wake", { agentId: a.id }) as BoardAgent;
  work = store.apply(a.id, "claim2", "claim", { taskId: task.id }, { epoch: 1, executionId: next.executionId }) as BoardWork;
  store.apply("system", "late", "settle", { agentId: a.id, executionId: old.executionId, summary: "Wrong result" });
  assert.equal(store.read().tasks[0].status, "running"); assert.equal(store.read().agents[1].status, "running");
  store.apply("user", "cancel", "manage", { taskId: task.id, version: work.version, action: "cancel" });
  store.apply("system", "finish", "settle", { agentId: a.id, executionId: next.executionId });
  work = store.read().tasks[0];
  assert.throws(() => store.apply("user", "undo", "manage", { taskId: task.id, version: work.version, action: "retry" }), /settle/);
});

test("clarification replies resume the original worker without consuming retries", (t) => {
  const { store, create } = fixture(t); create();
  const a = store.apply("system", "register", "register_agent") as BoardAgent;
  store.apply("system", "wake", "wake", { agentId: a.id });
  const work = store.apply(a.id, "claim", "claim", {}, { epoch: 1 }) as BoardWork;
  const question = store.apply(a.id, "question", "message", { to: "main", kind: "question", body: "Which asset?", taskId: work.id }, { epoch: 1, attemptId: work.attemptId }) as BoardMessage;
  store.apply(a.id, "block", "update", { taskId: work.id, version: work.version, action: "block", reason: `question:${question.id}` }, { epoch: 1, attemptId: work.attemptId });
  store.apply("main", "answer", "message", { to: a.id, kind: "answer", body: "Asset B", replyTo: question.id }, { epoch: 1 });
  store.apply("system", "settle", "settle", { agentId: a.id });
  assert.equal(store.read().tasks[0].status, "ready"); assert.equal(store.read().tasks[0].assignedTo, a.id); assert.equal(store.read().tasks[0].retries, 0);
});

test("board state and pending inbox share a bounded context budget", (t) => {
  const { store, create } = fixture(t);
  for (let i = 0; i < 30; i++) { create(`Task ${i}: ${"x".repeat(1900)}`); store.apply("user", `m${i}`, "message", { to: "main", kind: "information", body: "y".repeat(2000) }); }
  const context = collaborationContext(store.read(), "main");
  assert.ok(context.content.length <= 12000); assert.ok(context.ids.length > 0); assert.ok(context.ids.length <= 10);
  assert.match(context.content, /board_read/);
});

test("uncertain cleanup blocks reassignment until the original execution actually ends", async (t) => {
  let finish!: () => void; let running = false; let starts = 0;
  const gate = new Promise<void>((resolve) => { finish = resolve; });
  const { store, runtime, create } = fixture(t, () => ({ busy: () => running,
    run: async () => { starts++; running = true; await gate; running = false; return { summary: "late" }; },
    steer: async () => {}, stop: async () => !running }));
  store.apply("system", "one-slot", "agent_meta", { agentId: "main", maxConcurrent: 1 });
  create(); await until(() => running); await runtime.pause();
  assert.equal(store.read().attempts[0].status, "uncertain");
  store.apply("user", "resume", "control", { action: "resume" }); create("Other independent work"); await sleep(30);
  assert.equal(starts, 1, "an uncertain execution continues to occupy real capacity"); assert.equal(store.read().tasks[0].status, "blocked");
  finish(); await until(() => store.read().attempts[0].status === "settled");
  assert.notEqual(store.read().tasks[0].summary, "late", "late text cannot overwrite a paused work item");
});

test("idle eviction releases objects while preserving stable agent identity and transcript", async (t) => {
  const { store, runtime, releases, create, runs } = fixture(t);
  create(); await until(() => runs.includes("main")); await until(() => runtime.runningCount === 0);
  const actor = store.read().agents.find((a) => a.role === "child")!;
  store.apply("system", "path", "agent_meta", { agentId: actor.id, transcript: "/fixture/transcript.jsonl" });
  const then = Date.now(); t.mock.method(Date, "now", () => then + 300001);
  await runtime.maintain();
  assert.ok(releases.includes(actor.id));
  const recovered = store.read().agents.find((a) => a.id === actor.id)!;
  assert.equal(recovered.status, "sleeping"); assert.equal(recovered.transcript, "/fixture/transcript.jsonl");
});

test("queued user input waits for autonomous main execution and does not overlap or deadlock", async (t) => {
  const { enqueueSessionAction } = await import("@/server/pi/session-join");
  const queue: { promptChain?: Promise<void>; pendingActions?: number } = {};
  let release!: () => void; let autonomous = false; let userStarted = false;
  const gate = new Promise<void>((resolve) => { release = resolve; });
  const { store, runtime } = fixture(t, (id, rt) => ({ busy: () => Boolean(queue.pendingActions),
    run: (packet) => enqueueSessionAction(queue, async () => { autonomous = true; rt.included(id, packet.messageIds); await gate; autonomous = false; return {}; }),
    steer: async () => {}, stop: async () => { release(); return true; } }));
  store.apply("user", "wake-main", "message", { to: "main", kind: "information", body: "Review" });
  await until(() => autonomous);
  const user = enqueueSessionAction(queue, async () => {
    const execution = await runtime.beginUserTurn();
    assert.equal(autonomous, false); userStarted = true;
    runtime.userTurnEnded({}, execution);
  });
  await sleep(10); assert.equal(userStarted, false);
  release(); await user;
  assert.equal(userStarted, true); assert.equal(store.read().used.wakes, 1);
});
