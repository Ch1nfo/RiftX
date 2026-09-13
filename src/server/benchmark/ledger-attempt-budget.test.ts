import test from "node:test";
import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { readFile, writeFile } from "node:fs/promises";
import { homedir } from "node:os";
import { join } from "node:path";
import { BenchmarkLedger, FIRST_ATTEMPT_LIMIT_MS, ATTEMPT_EXTENSION_MS, type ProgressSignalKind } from "./ledger";
import type { Challenge } from "./controller";

const MINUTE = 60_000;
const platform = (count = 0): Challenge => ({
  unique_code: "budget-fixture", description: "Synthetic budget fixture", difficulty: "easy", level: 1,
  total_score: 100, flag_count: 4, correct_flag_count: count, is_completed: false,
  container_status: "stopped", container_addr: []
});

async function setup(options: { revisit?: boolean; persistent?: boolean } = {}) {
  let now = 10_000_000;
  const id = `attempt-budget-test-${randomUUID()}`;
  const ledger = new BenchmarkLedger(id, () => now);
  if (options.persistent) await ledger.initialize();
  else Object.defineProperty(ledger, "writeStore", { value: async () => undefined });
  await ledger.syncFromPlatform([platform()], true, "fixture");
  await ledger.acquire("budget-fixture", "main", ["fixture"]);
  if (options.revisit) {
    await ledger.defer("budget-fixture", "Synthetic first attempt finished", undefined, "main", true);
    await ledger.confirmClosed("budget-fixture");
    now += MINUTE;
    await ledger.acquire("budget-fixture", "main", ["fixture"]);
  }
  const startedAt = now;
  return { ledger, id, startedAt, clock: () => now, at: (minutes: number) => { now = startedAt + minutes * MINUTE; } };
}

function stage(ledger: BenchmarkLedger, evidenceRef: string, summary = "Synthetic new stage", signalKind: ProgressSignalKind = "stage_transition") {
  return ledger.checkpoint("budget-fixture", summary, undefined, undefined, "main", { signalKind, evidenceRef });
}

test("all attempts warn at 25 minutes and expire at 30 without progress", async () => {
  for (const revisit of [false, true]) {
    const { ledger, startedAt, at } = await setup({ revisit });
    assert.equal(ledger.budgetFor("budget-fixture")?.deadlineAt, startedAt + FIRST_ATTEMPT_LIMIT_MS);
    at(24.99);
    assert.equal(ledger.attemptWarningFor("main"), undefined);
    at(25);
    assert.ok(ledger.attemptWarningFor("main"));
    assert.equal(await ledger.acknowledgeAttemptWarning("main", "budget-fixture", startedAt - 1), false);
    assert.equal(await ledger.acknowledgeAttemptWarning("main", "budget-fixture", startedAt), true);
    assert.equal(ledger.attemptWarningFor("main"), undefined);
    at(30);
    assert.equal(ledger.budgetFor("budget-fixture")?.expired, true);
    assert.equal(ledger.budgetFor("budget-fixture")?.remainingMs, 0);
  }
});

test("first attempts cannot extend from a flag or an evidenced stage", async () => {
  const { ledger, at, startedAt } = await setup();
  at(29);
  assert.equal((await stage(ledger, "fixture:first-stage")).extended, false);
  await ledger.recordSubmission("budget-fixture", "synthetic-flag", true, 25, 1, 0, "main");
  assert.equal(ledger.getChallenge("budget-fixture")?.hardDeadlineAt, startedAt + 30 * MINUTE);
  at(30);
  assert.equal(ledger.isBudgetExhausted("budget-fixture"), true);
});

test("only a new stage reference in the last five minutes grants one ten-minute extension", async () => {
  const { ledger, at, startedAt } = await setup({ revisit: true });
  at(24);
  assert.equal((await stage(ledger, "fixture:early")).extended, false);
  at(25);
  assert.equal((await stage(ledger, "fixture:early", "Paraphrased synthetic stage")).extended, false);
  for (const kind of ["note", "new_surface", "foothold", "credential", "privilege_change", "exploit_primitive"] as const) {
    assert.equal((await stage(ledger, `fixture:${kind}`, "Synthetic observation", kind)).extended, false);
    assert.equal((await stage(ledger, `fixture:${kind}`, "Relabeled synthetic observation")).extended, false);
  }
  assert.equal((await stage(ledger, "   ")).extended, false);
  assert.equal((await stage(ledger, "fixture:new-stage")).extended, true);
  assert.equal(ledger.budgetFor("budget-fixture")?.limitMs, FIRST_ATTEMPT_LIMIT_MS + ATTEMPT_EXTENSION_MS);
  assert.equal(ledger.getChallenge("budget-fixture")?.hardDeadlineAt, startedAt + 40 * MINUTE);
  at(29);
  assert.equal((await stage(ledger, "fixture:another-new-stage")).extended, false);
  await ledger.recordSubmission("budget-fixture", "synthetic-flag", true, 25, 1, 0, "main");
  at(39.99);
  assert.equal(ledger.isBudgetExhausted("budget-fixture"), false);
  at(40);
  assert.equal(ledger.isBudgetExhausted("budget-fixture"), true);
});

test("only a new accepted flag in the extension window can extend", async () => {
  for (const mode of ["submission", "sync"] as const) {
    const { ledger, at, startedAt } = await setup({ revisit: true });
    at(24);
    await ledger.recordSubmission("budget-fixture", "synthetic-first", true, 25, 1, 0, "main");
    at(25);
    await ledger.recordSubmission("budget-fixture", "synthetic-first", true, 25, 1, 0, "main");
    await ledger.recordSubmission("budget-fixture", "synthetic-wrong", false, 25, 1, null, "main");
    assert.equal(ledger.budgetFor("budget-fixture")?.extensionUsed, false);
    at(29);
    if (mode === "submission") await ledger.recordSubmission("budget-fixture", "synthetic-second", true, 50, 2, 1, "main");
    else await ledger.syncFromPlatform([{ ...platform(2), container_status: "available", container_addr: ["fixture"] }], true, "fixture");
    assert.equal(ledger.getChallenge("budget-fixture")?.hardDeadlineAt, startedAt + 40 * MINUTE);
    assert.equal(ledger.budgetFor("budget-fixture")?.extensionUsed, true);
  }
});

test("evidence and accepted flags arriving at the deadline cannot revive an attempt", async () => {
  const { ledger, at, startedAt } = await setup({ revisit: true });
  at(30);
  assert.equal((await stage(ledger, "fixture:late-stage")).extended, false);
  await ledger.recordSubmission("budget-fixture", "synthetic-late", true, 25, 1, 0, "main");
  assert.equal(ledger.getChallenge("budget-fixture")?.hardDeadlineAt, startedAt + 30 * MINUTE);
  assert.ok(await ledger.expireAttempt("budget-fixture", "main", startedAt));
});

test("expiry bypasses endgame preservation and is idempotent and identity checked", async () => {
  const { ledger, at, startedAt } = await setup({ revisit: true });
  assert.equal(ledger.isEndgame(), true);
  at(30);
  assert.equal(await ledger.expireAttempt("budget-fixture", "subagent:stale", startedAt), undefined);
  assert.equal(await ledger.expireAttempt("budget-fixture", "main", startedAt - 1), undefined);
  const [expired, duplicate] = await Promise.all([
    ledger.expireAttempt("budget-fixture", "main", startedAt),
    ledger.expireAttempt("budget-fixture", "main", startedAt)
  ]);
  assert.equal(expired?.status, "closing");
  assert.equal(expired?.pendingStatus, "deferred");
  assert.equal(expired?.owner, null);
  assert.equal(expired?.currentAttemptStartedAt, null);
  assert.equal(expired?.approachHistory.at(-1)?.attemptNumber, 2);
  assert.equal(expired?.blackboard.at(-1)?.kind, "attempt_end");
  assert.ok(expired?.revisitQueueOrder);
  assert.equal(duplicate, undefined);
  assert.equal(ledger.getMetrics().totalDefers, 2);
  await ledger.confirmClosed("budget-fixture");
  at(31);
  await ledger.acquire("budget-fixture", "main", ["fresh-fixture"]);
  const newStart = ledger.getChallenge("budget-fixture")!.currentAttemptStartedAt!;
  at(70);
  assert.equal(await ledger.expireAttempt("budget-fixture", "main", startedAt), undefined);
  assert.equal(ledger.getChallenge("budget-fixture")?.status, "running");
  assert.ok(await ledger.expireAttempt("budget-fixture", "main", newStart));
});

test("expiry rechecks an extension granted since the caller sampled the deadline", async () => {
  const { ledger, at, startedAt } = await setup({ revisit: true });
  const originalDeadline = ledger.budgetFor("budget-fixture")!.deadlineAt!;
  at(29);
  assert.equal((await stage(ledger, "fixture:new-stage")).extended, true);
  at(30);
  assert.equal(originalDeadline, startedAt + 30 * MINUTE);
  assert.equal(await ledger.expireAttempt("budget-fixture", "main", startedAt), undefined);
  assert.equal(ledger.getChallenge("budget-fixture")?.status, "running");
});

test("defer and natural child exit cannot preserve an expired endgame environment", async () => {
  for (const extended of [false, true]) {
    for (const expired of [false, true]) {
      for (const operation of ["defer", "child-exit"] as const) {
        const { ledger, at } = await setup({ revisit: true });
        if (extended) {
          at(25);
          await stage(ledger, "fixture:stage");
        }
        const deadlineMinutes = extended ? 40 : 30;
        at(deadlineMinutes - (expired ? 0 : 1));
        await ledger.bindOwner("budget-fixture", "main", "subagent:fixture");
        const challenge = operation === "defer"
          ? await ledger.defer("budget-fixture", "Synthetic stop", undefined, "subagent:fixture")
          : await ledger.releaseOnSubagentExit("budget-fixture", "Synthetic stop", "subagent:fixture");
        assert.equal(challenge.status, expired ? "closing" : "orphaned");
        assert.equal(challenge.pendingStatus, expired ? "deferred" : undefined);
      }
    }
  }
});

test("owner rebinding and restart preserve deadline, warning and used extension; new attempts reset them", async () => {
  const { ledger, id, clock, at, startedAt } = await setup({ revisit: true, persistent: true });
  try {
    at(25);
    await ledger.acknowledgeAttemptWarning("main", "budget-fixture", startedAt);
    await stage(ledger, "fixture:persisted-stage");
    await ledger.bindOwner("budget-fixture", "main", "subagent:replacement");
    assert.equal(ledger.budgetForOwner("subagent:replacement")?.budget.deadlineAt, startedAt + 40 * MINUTE);
    at(29);
    const restored = await new BenchmarkLedger(id, clock).initialize();
    await restored.acquire("budget-fixture", "main", ["fixture"]);
    assert.equal(restored.getChallenge("budget-fixture")?.attemptCount, 2);
    assert.equal(restored.budgetFor("budget-fixture")?.deadlineAt, startedAt + 40 * MINUTE);
    assert.equal(restored.budgetFor("budget-fixture")?.extensionUsed, true);
    assert.equal(restored.attemptWarningFor("main"), undefined);
    assert.equal((await stage(restored, "fixture:another-stage")).extended, false);
    await restored.defer("budget-fixture", "Synthetic finished revisit", undefined, "main", true);
    await restored.confirmClosed("budget-fixture");
    at(41);
    await restored.acquire("budget-fixture", "main", ["fresh-fixture"]);
    assert.equal(restored.getChallenge("budget-fixture")?.attemptCount, 3);
    assert.equal(restored.budgetFor("budget-fixture")?.deadlineAt, clock() + 30 * MINUTE);
    assert.equal(restored.budgetFor("budget-fixture")?.extensionUsed, false);
    assert.equal(restored.getChallenge("budget-fixture")?.firstAttemptWarningIssuedAt, null);
  } finally {
    await BenchmarkLedger.destroy(id);
  }
});

test("legacy active revisits migrate from their original start and cannot reuse persisted evidence", async () => {
  const { ledger, id, at, clock, startedAt } = await setup({ revisit: true, persistent: true });
  try {
    at(5);
    await stage(ledger, "fixture:legacy-stage");
    const path = join(homedir(), ".riftx", "benchmark", id, "state.json");
    const stored = JSON.parse(await readFile(path, "utf8"));
    const challenge = stored.challenges["budget-fixture"];
    challenge.hardDeadlineAt = null;
    delete challenge.attemptExtensionGrantedAt;
    delete challenge.seenEvidenceRefHashes;
    await writeFile(path, JSON.stringify(stored));
    at(29);
    const restored = await new BenchmarkLedger(id, clock).initialize();
    await restored.acquire("budget-fixture", "main", ["fixture"]);
    assert.equal(restored.budgetFor("budget-fixture")?.deadlineAt, startedAt + 30 * MINUTE);
    assert.equal((await stage(restored, "FIXTURE:LEGACY-STAGE", "Synthetic paraphrase")).extended, false);
    at(30);
    assert.equal(restored.isBudgetExhausted("budget-fixture"), true);
  } finally {
    await BenchmarkLedger.destroy(id);
  }
});

test("superseding and blackboard retention cannot make the same reference new again", async () => {
  const { ledger, at } = await setup({ revisit: true });
  at(5);
  await stage(ledger, "fixture:old-stage");
  await ledger.checkpoint("budget-fixture", "Synthetic correction", undefined, undefined, "main", { supersedesEvidenceRef: "fixture:old-stage" });
  for (let i = 0; i < 35; i++) await stage(ledger, `fixture:observation-${i}`, "Synthetic retained observation", "note");
  at(29);
  assert.equal((await stage(ledger, "fixture:old-stage", "Synthetic paraphrase")).extended, false);
  assert.equal((await stage(ledger, "fixture:truly-new-stage")).extended, true);
});

test("formal exclusions require decisive evidence and rejected checkpoints are atomic", async () => {
  const { ledger } = await setup();
  for (const options of [
    { signalKind: "note" as const, evidenceRef: "fixture:note" },
    { signalKind: "new_surface" as const, evidenceRef: "fixture:surface" },
    { signalKind: "decisive_rule_out" as const, evidenceRef: " \n " }
  ]) {
    const before = structuredClone(ledger.getChallenge("budget-fixture"));
    await assert.rejects(ledger.checkpoint("budget-fixture", "Synthetic attempted route", ["route"], "Synthetic next probe", "main", {
      ...options, ruledOutFamilies: ["route"], supersedesEvidenceRef: "fixture:existing"
    }), /ruledOutFamilies requires decisive_rule_out/);
    assert.deepEqual(ledger.getChallenge("budget-fixture"), before);
  }
  await ledger.checkpoint("budget-fixture", "Synthetic tried route", ["route"], undefined, "main");
  assert.deepEqual(ledger.getChallenge("budget-fixture")?.triedFamilies, ["route"]);
  assert.deepEqual(ledger.getChallenge("budget-fixture")?.ruledOutFamilies, []);
  for (const evidenceRef of ["fixture:evidence-a", "fixture:evidence-b"]) await ledger.checkpoint("budget-fixture", "Synthetic decisive exclusion", undefined, undefined, "main", {
    signalKind: "decisive_rule_out", evidenceRef, ruledOutFamilies: ["route"]
  });
  await ledger.checkpoint("budget-fixture", "Synthetic correction", undefined, undefined, "main", { supersedesEvidenceRef: "fixture:evidence-a" });
  assert.deepEqual(ledger.getChallenge("budget-fixture")?.ruledOutFamilies, ["route"]);
  await ledger.checkpoint("budget-fixture", "Synthetic second correction", undefined, undefined, "main", { supersedesEvidenceRef: "fixture:evidence-b" });
  assert.deepEqual(ledger.getChallenge("budget-fixture")?.ruledOutFamilies, []);
});
