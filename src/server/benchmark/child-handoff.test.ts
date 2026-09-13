import test from "node:test";
import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { readFile } from "node:fs/promises";
import type { SubagentTask } from "@/lib/types";
import { BenchmarkLedger } from "./ledger";
import { createBenchmarkChildHandoff } from "./child-handoff";

async function fixture(t: test.TestContext, codes = ["fixture-a", "fixture-b", "fixture-c", "fixture-d"]) {
  const parentId = `child-handoff-${randomUUID()}`;
  t.after(() => BenchmarkLedger.destroy(parentId));
  const ledger = await new BenchmarkLedger(parentId).initialize();
  await ledger.syncFromPlatform(codes.map((code) => ({
    unique_code: code, description: "fixture", difficulty: "easy", level: 1, total_score: 100,
    flag_count: 1, correct_flag_count: 0, is_completed: false, container_status: "stopped", container_addr: []
  })), true, "fixture-vpn");
  const task: SubagentTask = {
    id: "fixture-child", parentSessionId: parentId, threadId: "fixture-thread", name: "fixture", task: "fixture", status: "completed",
    model: "fixture/model", createdAt: "2026-01-01T00:00:00Z", pendingApprovalCount: 0, logs: [],
    benchmarkChallenge: "fixture-a", summary: "fixture-final-observation", delivered: false
  };
  await ledger.acquire("fixture-a", "subagent:fixture-child", ["fixture.invalid:80"]);
  return { parentId, ledger, task };
}

test("handoff persistence failure leaves ownership and environment intact until retry succeeds", async (t) => {
  const { ledger, task } = await fixture(t);
  const events: string[] = [];
  let handoffs = 0;
  const persist = ledger.recordChildHandoff.bind(ledger);
  ledger.recordChildHandoff = async (...args) => {
    handoffs++;
    events.push("persist");
    if (handoffs === 1) throw new Error("fixture-write-failure");
    await persist(...args);
  };
  const prepare = createBenchmarkChildHandoff({ ledger, controller: { closeChallenge: async (code) => {
    const handoff = ledger.getChallenge(code)!.blackboard.find((entry) => entry.kind === "handoff");
    assert.ok(handoff);
    assert.equal(await readFile(handoff.evidenceRef, "utf8"), task.summary);
    events.push("close");
    return { unique_code: code, closed: true };
  } } });
  await assert.rejects(() => prepare(task, task.summary), /fixture-write-failure/);
  assert.equal(ledger.getChallenge("fixture-a")!.owner, "subagent:fixture-child");
  assert.equal(ledger.getChallenge("fixture-a")!.status, "running");
  assert.deepEqual(events, ["persist"]);
  await prepare(task, task.summary);
  await prepare(task, task.summary);
  assert.deepEqual(events, ["persist", "persist", "close"]);
  assert.equal(ledger.getChallenge("fixture-a")!.status, "deferred");
});

test("close failure retries cleanup without writing the successful report twice", async (t) => {
  const { ledger, task } = await fixture(t);
  let handoffs = 0;
  let closes = 0;
  const persist = ledger.recordChildHandoff.bind(ledger);
  ledger.recordChildHandoff = async (...args) => { handoffs++; await persist(...args); };
  const prepare = createBenchmarkChildHandoff({ ledger, controller: { closeChallenge: async (code) => {
    if (++closes === 1) throw new Error("fixture-close-failure");
    return { unique_code: code, closed: true };
  } } });
  await assert.rejects(() => prepare(task, task.summary), /fixture-close-failure/);
  assert.equal(ledger.getChallenge("fixture-a")!.status, "closing");
  assert.equal(ledger.getChallenge("fixture-a")!.closeFailureRecorded, true);
  await prepare(task, task.summary);
  assert.equal(handoffs, 1);
  assert.equal(closes, 2);
  assert.equal(ledger.getChallenge("fixture-a")!.status, "deferred");
});

test("concurrent completion and delivery retries share one saved report and one close", async (t) => {
  const { ledger, task } = await fixture(t);
  let handoffs = 0;
  let closes = 0;
  const persist = ledger.recordChildHandoff.bind(ledger);
  ledger.recordChildHandoff = async (...args) => { handoffs++; await persist(...args); };
  const prepare = createBenchmarkChildHandoff({ ledger, controller: { closeChallenge: async (code) => {
    closes++; return { unique_code: code, closed: true };
  } } });
  await Promise.all([prepare(task, task.summary), prepare(task, task.summary)]);
  assert.equal(handoffs, 1);
  assert.equal(closes, 1);
});

test("late old-worker report cannot release or close the next worker's environment", async (t) => {
  const { ledger, task } = await fixture(t, ["fixture-a"]);
  await ledger.defer("fixture-a", "fixture-stop", undefined, "subagent:fixture-child");
  await ledger.confirmClosed("fixture-a");
  await ledger.maybeAdvancePhase();
  await ledger.acquire("fixture-a", "subagent:fixture-next-child", ["fixture.invalid:81"]);
  const prepare = createBenchmarkChildHandoff({ ledger, controller: { closeChallenge: async () => {
    assert.fail("A previous worker must not close the current environment");
  } } });
  await prepare(task, task.summary);
  const challenge = ledger.getChallenge("fixture-a")!;
  assert.equal(challenge.owner, "subagent:fixture-next-child");
  assert.equal(challenge.status, "running");
  assert.ok(challenge.blackboard.some((entry) => entry.kind === "handoff" && entry.worker === "subagent:fixture-child"));
});

test("an explicit earlier defer still receives a durable late final report", async (t) => {
  const { ledger, task, parentId } = await fixture(t);
  await ledger.defer("fixture-a", "fixture-stop", undefined, "subagent:fixture-child");
  await ledger.confirmClosed("fixture-a");
  const prepare = createBenchmarkChildHandoff({ ledger, controller: { closeChallenge: async () => {
    assert.fail("A closed environment needs no further close");
  } } });
  await prepare(task, task.summary);
  const restarted = await new BenchmarkLedger(parentId).initialize();
  const handoff = restarted.getChallenge("fixture-a")!.blackboard.find((entry) => entry.kind === "handoff");
  assert.ok(handoff);
  assert.equal(await readFile(handoff.evidenceRef, "utf8"), task.summary);
});

test("empty terminal summaries persist honest status metadata before release", async (t) => {
  const { ledger, task } = await fixture(t);
  task.status = "failed";
  task.summary = "";
  task.error = "fixture-terminal-error";
  const prepare = createBenchmarkChildHandoff({ ledger, controller: { closeChallenge: async (code) => ({ unique_code: code, closed: true }) } });
  await prepare(task);
  const handoff = ledger.getChallenge("fixture-a")!.blackboard.find((entry) => entry.kind === "handoff");
  assert.ok(handoff);
  assert.deepEqual(JSON.parse(await readFile(handoff.evidenceRef, "utf8")), {
    worker: "subagent:fixture-child", status: "failed", error: "fixture-terminal-error"
  });
});

test("unknown historical worker fails explicitly instead of claiming persistence succeeded", async (t) => {
  const { ledger, task } = await fixture(t);
  task.id = "fixture-unknown-child";
  const prepare = createBenchmarkChildHandoff({ ledger, controller: { closeChallenge: async () => {
    assert.fail("An unknown completion must not close the environment");
  } } });
  await assert.rejects(() => prepare(task, task.summary), /recorded attempt/);
  assert.equal(ledger.getChallenge("fixture-a")!.owner, "subagent:fixture-child");
});
