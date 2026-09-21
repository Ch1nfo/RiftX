import { Worker } from "node:worker_threads";
import { fileURLToPath } from "node:url";
import assert from "node:assert/strict";
import test, { type TestContext } from "node:test";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { randomUUID } from "node:crypto";
import { BoardStore } from "./store";
import type { BoardAgent, BoardWork, BoardMessage } from "@/lib/collaboration";

function fixture(t: TestContext) {
  const dir = mkdtempSync(join(tmpdir(), "riftx-board-"));
  const path = join(dir, "board.sqlite");
  const store = new BoardStore(path, "test", { create: true, maxConcurrent: 2 });
  const connections = new Set([store]);
  const track = (connection: BoardStore) => { connections.add(connection); return connection; };
  t.after(() => { for (const connection of connections) connection.close(); rmSync(dir, { recursive: true, force: true }); });
  const fence = () => ({ epoch: store.read().epoch });
  const create = (objective = "Investigate A", dependencies: string[] = []) => store.apply("main", randomUUID(), "create", { objective, dependencies }, fence()) as BoardWork;
  const worker = () => { const a = store.apply("system", randomUUID(), "register_agent") as BoardAgent; store.apply("system", randomUUID(), "wake", { agentId: a.id }); return a; };
  return { store, path, create, worker, fence, track };
}

test("cross-connection claims are exclusive and commands are idempotent", (t) => {
  const { store, path, create, worker, fence, track } = fixture(t);
  const work = create(); const a = worker(); const b = worker();
  const other = track(new BoardStore(path, "test"));
  const claimed = store.apply(a.id, "claim", "claim", { taskId: work.id }, fence()) as BoardWork;
  assert.throws(() => other.apply(b.id, "claim", "claim", { taskId: work.id }, fence()), /unavailable/);
  assert.deepEqual(store.apply(a.id, "claim", "claim", { taskId: work.id }, fence()), claimed);
  assert.throws(() => store.apply(a.id, "claim", "claim", {}, fence()), /different input/);
  assert.equal(store.read().attempts.length, 1);
});

test("proposal acceptance, dependency release, evidence review and stale revisions", (t) => {
  const { store, create, worker, fence } = fixture(t);
  const a = worker(); const first = create();
  const second = store.apply(a.id, "proposal", "propose", { objective: "Use A", dependencies: [first.id] }, fence()) as BoardWork;
  assert.throws(() => store.apply(a.id, "approve-own", "manage", { taskId: second.id, version: 1, action: "accept" }, fence()), /coordinator/);
  const accepted = store.apply("main", "accept", "manage", { taskId: second.id, version: 1, action: "accept" }, fence()) as BoardWork;
  assert.equal(accepted.status, "blocked");
  const claimed = store.apply(a.id, "claim", "claim", { taskId: first.id }, fence()) as BoardWork;
  const submitted = store.apply(a.id, "submit", "update", { taskId: first.id, version: claimed.version, action: "submit", summary: "Evidence checked" }, { ...fence(), attemptId: claimed.attemptId }) as BoardWork;
  assert.throws(() => store.apply("main", "early", "manage", { taskId: first.id, version: submitted.version, action: "approve" }, fence()), /settle/);
  store.apply("system", "settle", "settle", { agentId: a.id });
  store.apply("main", "approve", "manage", { taskId: first.id, version: submitted.version, action: "approve" }, fence());
  assert.equal(store.read().tasks.find((v) => v.id === second.id)?.status, "ready");
  assert.throws(() => store.apply("main", "stale", "manage", { taskId: second.id, version: 1, action: "cancel" }, fence()), /changed/);
});

test("messages enforce topology, replies and strict limits", (t) => {
  const { store, worker, fence } = fixture(t); const a = worker(); const b = worker();
  assert.throws(() => store.apply(a.id, "peer", "message", { to: b.id, kind: "information", body: "hi" }, fence()), /parent\/child/);
  const q = store.apply(a.id, "question", "message", { to: "main", kind: "question", body: "Which asset?" }, fence()) as BoardMessage;
  store.apply("main", "reply", "message", { to: a.id, kind: "answer", body: "Asset A", replyTo: q.id }, fence());
  assert.equal(store.read().messages[0].status, "replied");
  assert.throws(() => store.apply("main", "long", "message", { to: a.id, kind: "information", body: "x".repeat(2001) }, fence()), /characters/);
  store.apply("user", "budget", "control", { action: "limits", messages: 2 });
  assert.throws(() => store.apply(a.id, "limit", "message", { to: "main", kind: "information", body: "hi" }, fence()), /budget/);
});

test("pause fences old execution, waits for settlement, and preserves messages", (t) => {
  const { store, create, worker, fence } = fixture(t); const work = create(); const a = worker(); const old = fence();
  const claim = store.apply(a.id, "claim", "claim", { taskId: work.id }, old) as BoardWork;
  store.apply("user", "stop", "control", { action: "pause" });
  assert.throws(() => store.apply(a.id, "late", "update", { taskId: work.id, version: claim.version, action: "submit", summary: "late" }, { ...old, attemptId: claim.attemptId }), /generation/);
  store.apply("user", "message", "message", { to: a.id, kind: "information", body: "Keep this queued" });
  store.apply("user", "resume", "control", { action: "resume" });
  assert.equal(store.read().tasks[0].status, "blocked");
  store.apply("system", "settle", "settle", { agentId: a.id });
  store.apply("user", "resume-again", "control", { action: "resume" });
  assert.equal(store.read().tasks[0].status, "ready");
  assert.equal(store.read().messages[0].status, "queued");
});

test("restart never starts work or guesses that uncertain execution stopped", (t) => {
  const { store, path, create, worker, fence, track } = fixture(t); const work = create(); const a = worker();
  store.apply(a.id, "claim", "claim", { taskId: work.id }, fence()); store.close();
  const restored = track(new BoardStore(path, "test", { recover: true }));
  assert.equal(restored.read().status, "paused");
  restored.apply("user", "resume", "control", { action: "resume" });
  assert.equal(restored.read().tasks[0].blockedReason, "execution_uncertain");
  assert.throws(() => new BoardStore(path + "missing", "test"), /missing/);
});

test("cycles, rollback, missing notifications, and receipt replay preserve event consistency", (t) => {
  const { store, create, fence } = fixture(t);
  const first = create(); const second = create("Dependent", [first.id]);
  const before = store.read();
  assert.throws(() => store.apply("main", "cycle", "manage", { action: "edit", taskId: first.id, version: first.version, dependencies: [second.id] }, fence()), /cycle/);
  assert.deepEqual(store.read(), before, "invalid mutations rollback the complete transaction");
  store.subscribe(() => { throw new Error("synthetic notification loss"); });
  const result = store.apply("main", "unique-command", "manage", { action: "edit", taskId: first.id, version: first.version, priority: 10 }, fence());
  const snapshot = store.snapshot(before.revision, 10);
  assert.equal(snapshot.mode, "shared");
  if (snapshot.mode === "shared") { assert.equal(snapshot.events.length, 1); assert.equal(snapshot.events[0].seq, snapshot.state.revision); }
  assert.deepEqual(store.apply("main", "unique-command", "manage", { action: "edit", taskId: first.id, version: first.version, priority: 10 }, { ...fence(), attemptId: "changed" }), result);
});

test("failed dependencies do not release work; retry limits and cancellation are terminal", (t) => {
  const { store, create, worker, fence } = fixture(t);
  const first = create(); const second = create("Dependent", [first.id]); const a = worker();
  for (let attempt = 0; attempt < 3; attempt++) {
    store.apply(a.id, `claim-${attempt}`, "claim", { taskId: first.id }, fence());
    store.apply("system", `settle-${attempt}`, "settle", { agentId: a.id, error: true });
    const work = store.read().tasks[0];
    if (attempt < 2) {
      store.apply("main", `retry-${attempt}`, "manage", { action: "retry", taskId: first.id, version: work.version }, fence());
      store.apply("system", `wake-${attempt}`, "wake", { agentId: a.id });
    } else assert.throws(() => store.apply("main", "exhausted", "manage", { action: "retry", taskId: first.id, version: work.version }, fence()), /retry limit/);
  }
  assert.equal(store.read().tasks.find((work) => work.id === second.id)?.status, "blocked");
  assert.equal(store.read().used.tasks, 2);
});

test("unknown senders, cross-board dependencies and main spoofing are rejected", (t) => {
  const { store, create, worker, fence } = fixture(t); const work = create(); const a = worker();
  assert.throws(() => store.apply("foreign", "forged", "message", { to: "main", kind: "information", body: "forged" }, fence()), /Unknown agent/);
  assert.throws(() => store.apply(a.id, "manage", "manage", { action: "cancel", taskId: work.id, version: work.version }, fence()), /coordinator/);
  assert.throws(() => store.apply("main", "dependency", "create", { objective: "Foreign", dependencies: ["another-board-work"] }, fence()), /Dependencies/);
  assert.equal(store.read().tasks.length, 1);
});

test("budgets persist across pause/restart and reset only after a completed run", (t) => {
  const { store, path, create, fence } = fixture(t);
  store.apply("user", "limit", "control", { action: "limits", tasks: 1 }); const work = create();
  assert.throws(() => create("Beyond limit"), /budget/);
  store.apply("main", "cancel", "manage", { action: "cancel", taskId: work.id, version: work.version }, fence());
  store.apply("main", "finish", "finish", {}, fence());
  assert.equal(store.read().used.tasks, 1);
  store.apply("system", "begin", "begin_run"); assert.equal(store.read().used.tasks, 0);
  create("New run"); store.close();
  const restored = new BoardStore(path, "test", { recover: true });
  try { assert.equal(restored.read().used.tasks, 1); assert.equal(restored.read().limits.tasks, 1); }
  finally { restored.close(); }
});


test("simultaneous native connections serialize claims and message budget reservations", async (t) => {
  const { store, path, create, worker } = fixture(t);
  const task = create(); const actors = [worker(), worker()];
  const storeModule = fileURLToPath(new URL("./store.ts", import.meta.url));
  async function race(op: string, input: Record<string, unknown>) {
    const children = actors.map((actor) => new Worker(`
      require("tsx/cjs");
      const { parentPort, workerData: d } = require("node:worker_threads");
      const { BoardStore } = require(d.module);
      const store = new BoardStore(d.path, "test");
      parentPort.once("message", () => {
        try { store.apply(d.actor, d.op, d.op, d.input, { epoch: 1 }); parentPort.postMessage("ok"); }
        catch (e) { parentPort.postMessage(e.code); }
        finally { store.close(); }
      });
      parentPort.postMessage("ready");
    `, { eval: true, workerData: { module: storeModule, path, actor: actor.id, op, input } }));
    const errors = children.map((child) => new Promise<never>((_, reject) => child.once("error", reject)));
    await Promise.all(children.map((child, i) => Promise.race([new Promise((resolve) => child.once("message", resolve)), errors[i]])));
    const results = children.map((child, i) => Promise.race([new Promise<string>((resolve) => child.once("message", resolve)), errors[i]]));
    const exits = children.map((child) => new Promise((resolve) => child.once("exit", resolve)));
    children.forEach((child) => child.postMessage("go"));
    const values = await Promise.all(results); await Promise.all(exits); return values;
  }
  assert.deepEqual((await race("claim", { taskId: task.id })).sort(), ["STATE_NOT_ALLOWED", "ok"]);
  store.apply("user", "limit", "control", { action: "limits", messages: 1 });
  assert.deepEqual((await race("message", { to: "main", kind: "information", body: "Bounded" })).sort(), ["BUDGET_EXHAUSTED", "ok"]);
  assert.equal(store.read().used.messages, 1); assert.equal(store.read().attempts.length, 1);
});

test("explicit confirmation clears a recovered assignment and can settle terminal cancellation", (t) => {
  const { store, create, worker, fence } = fixture(t); create(); const a = worker();
  store.apply(a.id, "claim-old", "claim", {}, fence());
  store.apply("system", "recover", "control", { action: "recover" });
  let task = store.read().tasks[0];
  assert.throws(() => store.apply("user", "retry-unknown", "manage", { action: "retry", taskId: task.id, version: task.version }), /settle/);
  store.apply("user", "retry-confirmed", "manage", { action: "retry", taskId: task.id, version: task.version, confirmStopped: true });
  assert.equal(store.read().agents.find((v) => v.id === a.id)?.taskId, undefined);
  store.apply("user", "resume", "control", { action: "resume" });
  store.apply("system", "wake-new", "wake", { agentId: a.id });
  store.apply(a.id, "claim-new", "claim", {}, fence());
  store.apply("system", "recover-again", "control", { action: "recover" });
  task = store.read().tasks[0];
  store.apply("user", "cancel", "manage", { action: "cancel", taskId: task.id, version: task.version });
  task = store.read().tasks[0];
  store.apply("user", "confirm-cancel", "manage", { action: "cancel", taskId: task.id, version: task.version, confirmStopped: true });
  assert.equal(store.read().tasks[0].status, "cancelled"); assert.ok(store.read().attempts.every((v) => v.status === "settled"));
});
