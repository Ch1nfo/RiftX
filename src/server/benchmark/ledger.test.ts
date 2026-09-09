import test from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, rm } from "node:fs/promises";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { BenchmarkLedger, BENCHMARK_MAX_CONTAINERS, budgetPolicyFor, type ChallengeState } from "./ledger";
import type { Challenge } from "./controller";

// Redirect homedir to a temp dir for ledger persistence tests.
const realHome = process.env.HOME;
let tempDir: string;

test.before(async () => {
  tempDir = await mkdtemp(join(tmpdir(), "riftx-benchmark-test-"));
  process.env.HOME = tempDir;
});

test.after(async () => {
  process.env.HOME = realHome;
  await rm(tempDir, { recursive: true, force: true }).catch(() => undefined);
});

function platformChallenge(code: string, overrides: Partial<Challenge> = {}): Challenge {
  return {
    unique_code: code,
    description: `Challenge ${code}`,
    difficulty: "easy",
    level: 1,
    total_score: 100,
    flag_count: 1,
    correct_flag_count: 0,
    is_completed: false,
    container_status: "stopped",
    container_addr: [],
    ...overrides
  };
}

async function setupLedger(codes: string[] = ["ch-1", "ch-2", "ch-3", "ch-4"], now?: () => number) {
  const sessionId = `test-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
  const ledger = await new BenchmarkLedger(sessionId, now).initialize();
  await ledger.syncFromPlatform(codes.map((code) => platformChallenge(code)), true, "10.0.0.1");
  return { ledger, sessionId };
}

test("initialize + sync creates pending challenges with platform metadata", async () => {
  const { ledger } = await setupLedger();
  const state = ledger.getState();
  assert.equal(state.totalChallenges, 4);
  assert.equal(state.challenges["ch-1"].status, "pending");
  assert.equal(state.challenges["ch-1"].totalScore, 100);
  assert.equal(state.vpnOk, true);
});

test("acquire sets owner and running status atomically", async () => {
  const { ledger } = await setupLedger();
  const acquired = await ledger.acquire("ch-1", "main", ["10.0.0.1:80"]);
  assert.equal(acquired.status, "running");
  assert.equal(acquired.owner, "main");
  assert.deepEqual(acquired.containerAddrs, ["10.0.0.1:80"]);
});

test("duplicate acquire by different owner is rejected and counted", async () => {
  const { ledger } = await setupLedger();
  await ledger.acquire("ch-1", "main", ["10.0.0.1:80"]);
  await assert.rejects(() => ledger.acquire("ch-1", "subagent:t1", ["10.0.0.1:80"]), /owned by main/);
  assert.equal(ledger.getMetrics().duplicateAcquires, 1);
});

test("container limit blocks the 4th concurrent acquire", async () => {
  const { ledger } = await setupLedger(["ch-1", "ch-2", "ch-3", "ch-4"]);
  await ledger.acquire("ch-1", "main", ["a"]);
  await ledger.acquire("ch-2", "subagent:t1", ["b"]);
  await ledger.acquire("ch-3", "subagent:t2", ["c"]);
  await assert.rejects(() => ledger.reserve("ch-4", "subagent:t3"), new RegExp(`Container limit.*${BENCHMARK_MAX_CONTAINERS}`));
  assert.equal(ledger.getState().activeContainers, 3);
});

test("checkpoint only resets budget on genuinely new signal", async () => {
  const { ledger } = await setupLedger();
  await ledger.acquire("ch-1", "main", ["a"]);
  const first = await ledger.checkpoint("ch-1", "found login page", undefined, undefined, "main");
  assert.equal(first.updated, true);
  assert.equal(first.extended, false, "a note without evidence is saved but does not extend time");
  const repeat = await ledger.checkpoint("ch-1", "found login page", undefined, undefined, "main");
  assert.equal(repeat.updated, false, "same signal must not reset the timer");
  const changed = await ledger.checkpoint("ch-1", "found SQL injection in search", undefined, undefined, "main");
  assert.equal(changed.updated, true);
  assert.equal(changed.extended, false);
});

test("signal budget exhaustion uses the injected clock", async () => {
  let now = 1_000_000;
  const { ledger } = await setupLedger(["ch-1"], () => now);
  await ledger.acquire("ch-1", "main", ["a"]);
  assert.equal(ledger.isBudgetExhausted("ch-1"), false);
  now += 9 * 60 * 1000;
  assert.equal(ledger.isBudgetExhausted("ch-1"), true);
});

test("evidence-backed progress extends once per stable evidence key", async () => {
  let now = 2_000_000;
  const { ledger } = await setupLedger(undefined, () => now);
  await ledger.acquire("ch-1", "main", ["a"]);
  const initialDeadline = ledger.getChallenge("ch-1")!.hardDeadlineAt!;
  now += 2 * 60 * 1000;
  const first = await ledger.checkpoint("ch-1", "obtained admin session", ["auth"], "query admin API", "main", {
    signalKind: "privilege_change", evidenceRef: "request:req-7", currentApproach: "auth bypass"
  });
  assert.equal(first.extended, true);
  assert.equal(first.challenge.hardDeadlineAt, initialDeadline + 3 * 60 * 1000);
  now += 60_000;
  const paraphrase = await ledger.checkpoint("ch-1", "admin access confirmed", ["auth"], "query admin API", "main", {
    signalKind: "privilege_change", evidenceRef: "request:req-7", currentApproach: "auth bypass"
  });
  assert.equal(paraphrase.updated, true);
  assert.equal(paraphrase.extended, false, "rewriting the same evidence must not buy more time");
  await ledger.checkpoint("ch-1", "ordinary follow-up note", undefined, "inspect audit log", "main", { signalKind: "note" });
  assert.equal(ledger.getChallenge("ch-1")?.lastMeaningfulSignalContent, "obtained admin session", "a note must not overwrite the evidence recovery brief");
});

test("a fresh recovery attempt cannot reuse evidence from the previous attempt to extend", async () => {
  let now = 2_500_000;
  const { ledger } = await setupLedger(["ch-1"], () => now);
  await ledger.acquire("ch-1", "main", ["a"]);
  await ledger.checkpoint("ch-1", "admin cookie captured", ["auth"], "inspect admin API", "main", {
    signalKind: "credential", evidenceRef: "artifact:cookie-1", currentApproach: "auth bypass"
  });
  await ledger.defer("ch-1", "coverage pass complete", "try source audit", "main");
  await ledger.confirmClosed("ch-1");
  await ledger.maybeAdvancePhase();
  now += 60_000;
  await ledger.acquire("ch-1", "subagent:fresh", ["b"]);
  const before = ledger.getChallenge("ch-1")!.hardDeadlineAt;
  const replay = await ledger.checkpoint("ch-1", "same cookie described differently", ["auth"], "inspect admin API", "subagent:fresh", {
    signalKind: "credential", evidenceRef: "artifact:cookie-1", currentApproach: "source audit"
  });
  assert.equal(replay.extended, false);
  assert.equal(replay.challenge.hardDeadlineAt, before);
});

test("a newly accepted partial flag renews progress and momentum without solving", async () => {
  let now = 3_000_000;
  const { ledger } = await setupLedger(["ch-1"], () => now);
  await ledger.syncFromPlatform([platformChallenge("ch-1", { flag_count: 3 })], true, "ip");
  await ledger.acquire("ch-1", "main", ["a"]);
  now += 10 * 60 * 1000;
  await ledger.recordSubmission("ch-1", "flag{one}", true, 30, 1, 0, "main");
  const challenge = ledger.getChallenge("ch-1")!;
  assert.equal(challenge.status, "running");
  assert.equal(challenge.correctFlagCount, 1);
  assert.equal(challenge.lastAcceptedFlagAt, now);
  assert.equal(ledger.isBudgetExhausted("ch-1"), false);
  assert.ok(challenge.hardDeadlineAt! >= now + 6 * 60 * 1000);
});

test("a restart-interrupted attempt lands in history before a fresh attempt starts", async () => {
  const { ledger, sessionId } = await setupLedger(["ch-1"]);
  await ledger.acquire("ch-1", "main", ["a"]);
  await ledger.checkpoint("ch-1", "obtained admin session", ["auth"], "query admin API", "main", {
    signalKind: "privilege_change", evidenceRef: "request:req-1", currentApproach: "auth bypass"
  });
  // Simulate a Runtime restart: initialize() orphans the running challenge
  // but leaves its open attempt in place for a possible live resume.
  const restarted = await new BenchmarkLedger(sessionId).initialize();
  assert.equal(restarted.getChallenge("ch-1")?.status, "orphaned");
  // The platform has since stopped the idle container, so the orphan cannot
  // be resumed — re-acquiring must start a fresh attempt.
  await restarted.syncFromPlatform([platformChallenge("ch-1")], true, "ip");
  await restarted.acquire("ch-1", "subagent:t1", ["a"]);

  const challenge = restarted.getChallenge("ch-1")!;
  assert.equal(challenge.attemptCount, 2, "fresh attempt after the interrupted one");
  assert.equal(challenge.approachHistory.length, 1, "the interrupted attempt is recorded, not silently dropped");
  assert.equal(challenge.approachHistory[0].approach, "auth bypass");
  assert.match(challenge.approachHistory[0].stopReason, /interrupted by restart/);
  assert.equal(challenge.approachHistory[0].worker, "main");
});

test("per-challenge cumulative scores sum into the run total (API: 该题累计总得分)", async () => {
  const { ledger } = await setupLedger(["ch-1", "ch-2"]);
  await ledger.acquire("ch-1", "main", ["a"]);
  await ledger.acquire("ch-2", "subagent:t1", ["b"]);
  // ch-1 fully solved; the platform reports THIS challenge's cumulative = 100.
  await ledger.recordSubmission("ch-1", "flag{1}", true, 100, 1, 0, "main");
  await ledger.markSolved("ch-1", 100, "main");
  // ch-2 first flag correct; the platform reports THIS challenge's cumulative
  // = 30 — treating it as a global value would clobber the run total to 30.
  await ledger.recordSubmission("ch-2", "flag{a}", true, 30, 1, 0, "subagent:t1");
  assert.equal(ledger.getState().cumulativeScore, 130, "run total must be the sum of per-challenge scores");
  assert.equal(ledger.getState().scoreExact, true);
});

test("run total becomes a lower bound when progress advances without a priced response", async () => {
  const { ledger } = await setupLedger(["ch-1"]);
  await ledger.acquire("ch-1", "main", ["a"]);
  // Duplicate/timeout reconciliation path: correct flag, no score value.
  await ledger.recordSubmission("ch-1", "flag{1}", true, undefined, 1, null, "main");
  assert.equal(ledger.getState().cumulativeScore, 0);
  assert.equal(ledger.getState().scoreExact, false, "unpriced progress keeps the total a lower bound");
});

test("platform sync invalidates a cached challenge score when flag progress changes", async () => {
  const { ledger } = await setupLedger(["ch-1"]);
  await ledger.acquire("ch-1", "main", ["a"]);
  await ledger.recordSubmission("ch-1", "flag{1}", true, 30, 1, 0, "main");
  assert.equal(ledger.getState().scoreExact, true);

  await ledger.syncFromPlatform([
    platformChallenge("ch-1", { flag_count: 2, correct_flag_count: 2, is_completed: true })
  ], true, "10.0.0.1");

  assert.equal(ledger.getState().cumulativeScore, 30, "the last known score remains a lower bound when progress increases");
  assert.equal(ledger.getState().scoreExact, false, "list progress has no matching score value");
});

test("platform sync clears a cached score when flag progress moves backwards", async () => {
  const { ledger } = await setupLedger(["ch-1"]);
  await ledger.acquire("ch-1", "main", ["a"]);
  await ledger.recordSubmission("ch-1", "flag{1}", true, 30, 1, 0, "main");

  await ledger.syncFromPlatform([platformChallenge("ch-1")], true, "10.0.0.1");

  assert.equal(ledger.getState().cumulativeScore, 0, "a score from later progress is not a safe lower bound after rollback");
  assert.equal(ledger.getState().scoreExact, true, "zero flags has an exact zero score");
});

test("submit + markSolved update state, metrics, and phase", async () => {
  const { ledger } = await setupLedger();
  await ledger.acquire("ch-1", "main", ["a"]);
  await ledger.recordSubmission("ch-1", "flag{test}", true, 100, 1, 0, "main");
  await ledger.markSolved("ch-1", 100, "main");
  await ledger.confirmClosed("ch-1");
  const state = ledger.getState();
  assert.equal(state.challenges["ch-1"].status, "solved");
  assert.equal(state.solvedCount, 1);
  assert.equal(state.cumulativeScore, 100);
  const metric = ledger.getMetrics().challenges["ch-1"];
  assert.ok(metric);
  assert.ok(metric.durationMs !== null);
});

test("defer closes container and saves recovery state", async () => {
  const { ledger } = await setupLedger();
  await ledger.acquire("ch-1", "main", ["10.0.0.1:80"]);
  await ledger.defer("ch-1", "no signal for 8 min", "try /admin", "main");
  await ledger.confirmClosed("ch-1");
  const challenge = ledger.getChallenge("ch-1") as ChallengeState;
  assert.equal(challenge.status, "deferred");
  assert.equal(challenge.owner, null);
  assert.deepEqual(challenge.containerAddrs, []);
  assert.equal(ledger.getMetrics().totalDefers, 1);
});

test("abandon is terminal", async () => {
  const { ledger } = await setupLedger();
  await ledger.acquire("ch-1", "main", ["a"]);
  await ledger.abandon("ch-1", "no path forward", "main");
  await ledger.confirmClosed("ch-1");
  assert.equal(ledger.getChallenge("ch-1")?.status, "exhausted");
  assert.equal(ledger.getState().exhaustedCount, 1);
});

test("restart recovery marks running as orphaned, keeps deferred", async () => {
  const { ledger, sessionId } = await setupLedger();
  await ledger.acquire("ch-1", "main", ["a"]);
  await ledger.acquire("ch-2", "subagent:t1", ["b"]);
  await ledger.defer("ch-2", "test", undefined, "subagent:t1");
  await ledger.confirmClosed("ch-2");
  // Simulate restart: new ledger instance on the same session.
  const restored = await new BenchmarkLedger(sessionId).initialize();
  assert.equal(restored.getChallenge("ch-1")?.status, "orphaned");
  assert.equal(restored.getChallenge("ch-1")?.owner, null);
  assert.equal(restored.getChallenge("ch-2")?.status, "deferred");
});

test("platform-stopped orphans release their container slots; live orphans still count", async () => {
  const { ledger, sessionId } = await setupLedger(["ch-1", "ch-2", "ch-3", "ch-4"]);
  await ledger.acquire("ch-1", "main", ["a"]);
  await ledger.acquire("ch-2", "subagent:t1", ["b"]);
  await ledger.acquire("ch-3", "subagent:t2", ["c"]);
  // Restart orphans all three (containers still live on the platform).
  const restarted = await new BenchmarkLedger(sessionId).initialize();
  assert.equal(restarted.getState().activeContainers, 3, "live orphans still occupy platform slots");
  // The platform stopped all three idle containers; sync observes that.
  await restarted.syncFromPlatform([
    platformChallenge("ch-1"), platformChallenge("ch-2"), platformChallenge("ch-3"), platformChallenge("ch-4")
  ], true, "ip");
  assert.equal(restarted.getState().activeContainers, 0, "stopped orphans must free their slots");
  // The freed capacity must be usable — defer/abandon cannot clear an unowned
  // orphan, so a false count here would deadlock every future acquire.
  await restarted.reserve("ch-4", "main");
  assert.equal(restarted.getChallenge("ch-4")?.status, "reserved");
});

test("platform sync overrides local stale solved state", async () => {
  const { ledger } = await setupLedger(["ch-1"]);
  await ledger.acquire("ch-1", "main", ["a"]);
  await ledger.markSolved("ch-1", 100, "main");
  await ledger.confirmClosed("ch-1");
  // Platform now says not completed.
  await ledger.syncFromPlatform([platformChallenge("ch-1", { is_completed: false })], true, "ip");
  assert.equal(ledger.getChallenge("ch-1")?.status, "deferred", "local stale solved reverts to deferred");
});

test("a small run advances directly from first-pass settlement into endgame", async () => {
  const { ledger } = await setupLedger(["ch-1"]);
  await ledger.acquire("ch-1", "main", ["a"]);
  await ledger.defer("ch-1", "first pass", undefined, "main");
  await ledger.confirmClosed("ch-1");
  const phase = await ledger.maybeAdvancePhase();
  assert.equal(phase, "endgame");
});

test("phase does not advance while first-pass workers are still active", async () => {
  const { ledger } = await setupLedger(["ch-1", "ch-2"]);
  await ledger.acquire("ch-1", "main", ["a"]);
  await ledger.acquire("ch-2", "subagent:t1", ["b"]);
  assert.equal(await ledger.maybeAdvancePhase(), "first_pass");
});

test("candidates: orphaned first, then easy→hard, then score descending", async () => {
  const sessionId = `test-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
  const ledger = await new BenchmarkLedger(sessionId).initialize();
  await ledger.syncFromPlatform([
    platformChallenge("hard-high", { difficulty: "hard", total_score: 300 }),
    platformChallenge("easy-low", { difficulty: "easy", total_score: 50 }),
    platformChallenge("easy-high", { difficulty: "easy", total_score: 200 }),
    platformChallenge("med", { difficulty: "medium", total_score: 100 })
  ], true, "ip");
  // Make one orphaned.
  const challenge = ledger.getChallenge("med") as ChallengeState;
  challenge.status = "orphaned";
  const candidates = ledger.candidates(10);
  assert.equal(candidates[0].uniqueCode, "med", "orphaned first");
  assert.equal(candidates[1].uniqueCode, "easy-high", "easy + high score");
  assert.equal(candidates[2].uniqueCode, "easy-low", "easy + lower score");
  assert.equal(candidates[3].uniqueCode, "hard-high", "hard last");
});

test("subagent exit releases challenge back to deferred", async () => {
  const { ledger } = await setupLedger(["ch-1"]);
  await ledger.acquire("ch-1", "subagent:t1", ["a"]);
  await ledger.releaseOnSubagentExit("ch-1", "subagent crashed", "subagent:t1");
  await ledger.confirmClosed("ch-1");
  assert.equal(ledger.getChallenge("ch-1")?.status, "deferred");
  assert.equal(ledger.getChallenge("ch-1")?.owner, null);
});

test("activeSubagentCount tracks non-main owners", async () => {
  const { ledger } = await setupLedger(["ch-1", "ch-2", "ch-3"]);
  await ledger.acquire("ch-1", "subagent:t1", ["a"]);
  await ledger.acquire("ch-2", "subagent:t2", ["b"]);
  await ledger.acquire("ch-3", "main", ["c"]);
  assert.equal(ledger.activeSubagentCount(), 2);
});

test("recordHint tracks usage", async () => {
  const { ledger } = await setupLedger(["ch-1"]);
  await ledger.acquire("ch-1", "main", ["a"]);
  await ledger.defer("ch-1", "first pass", undefined, "main");
  await ledger.confirmClosed("ch-1");
  await ledger.maybeAdvancePhase();
  await ledger.recordHint("ch-1", "look at /backup", "main");
  assert.equal(ledger.getChallenge("ch-1")?.hintUsed, true);
  assert.equal(ledger.getChallenge("ch-1")?.hintContent, "look at /backup");
  assert.equal(ledger.getMetrics().totalHintsUsed, 1);
});

test("all terminal → completed", async () => {
  const { ledger } = await setupLedger(["ch-1", "ch-2"]);
  await ledger.acquire("ch-1", "main", ["a"]);
  await ledger.markSolved("ch-1", 100, "main");
  await ledger.confirmClosed("ch-1");
  await ledger.acquire("ch-2", "main", ["b"]);
  await ledger.abandon("ch-2", "dead end", "main");
  await ledger.confirmClosed("ch-2");
  assert.equal(ledger.getState().phase, "completed");
});

test("closing challenge cannot be re-reserved and stale close cannot corrupt a later owner", async () => {
  const { ledger } = await setupLedger(["ch-1"]);
  await ledger.acquire("ch-1", "subagent:old", ["a"]);
  await ledger.releaseOnSubagentExit("ch-1", "done", "subagent:old");
  await assert.rejects(() => ledger.reserve("ch-1", "subagent:new", { isSubagent: true }), /cannot be reserved while closing/);
  await ledger.confirmClosed("ch-1");
  await ledger.maybeAdvancePhase();
  await ledger.reserve("ch-1", "subagent:new", { isSubagent: true });
  await ledger.confirmStarted("ch-1", ["b"], "subagent:new");
  await assert.rejects(() => ledger.confirmClosed("ch-1"), /not awaiting close confirmation/);
  assert.equal(ledger.getChallenge("ch-1")?.owner, "subagent:new");
  assert.deepEqual(ledger.getChallenge("ch-1")?.containerAddrs, ["b"]);
});

test("mutations require the current owner even when a challenge is unowned", async () => {
  const { ledger } = await setupLedger(["ch-1"]);
  await assert.rejects(
    () => ledger.checkpoint("ch-1", "stale child signal", undefined, undefined, "subagent:old"),
    /owned by nobody/
  );
});

test("failed second-pass start restores deferred scheduling state", async () => {
  const { ledger } = await setupLedger(["ch-1"]);
  await ledger.acquire("ch-1", "main", ["a"]);
  await ledger.defer("ch-1", "first pass", "try another path", "main");
  await ledger.confirmClosed("ch-1");
  await ledger.maybeAdvancePhase();
  await ledger.reserve("ch-1", "subagent:retry", { isSubagent: true });
  await ledger.releaseReservation("ch-1", "subagent:retry");
  assert.equal(ledger.getChallenge("ch-1")?.status, "deferred");
});

test("recovery partial defer preserves the live container for warm handoff", async () => {
  let now = 4_000_000;
  const { ledger } = await setupLedger(["ch-1"], () => now);
  await ledger.syncFromPlatform([platformChallenge("ch-1", { flag_count: 2 })], true, "ip");
  await ledger.acquire("ch-1", "main", ["a"]);
  await ledger.recordSubmission("ch-1", "flag{one}", true, 50, 1, 0, "main");
  await ledger.defer("ch-1", "first pass done", "try source audit", "main");
  await ledger.confirmClosed("ch-1");
  await ledger.maybeAdvancePhase();
  assert.equal(ledger.getState().phase, "endgame");

  await ledger.acquire("ch-1", "subagent:t1", ["b"]);
  await ledger.defer("ch-1", "switch approach", "inspect authorization", "subagent:t1", { preserveContainer: true });
  const handoff = ledger.getChallenge("ch-1")!;
  assert.equal(handoff.status, "handoff_waiting");
  assert.deepEqual(handoff.containerAddrs, ["b"]);
  assert.equal(handoff.containerStatus, "available");
  assert.equal(ledger.getState().activeContainers, 1);
  assert.equal(ledger.candidates(1)[0]?.uniqueCode, "ch-1");

  now += 2 * 60 * 1000;
  assert.equal(ledger.expiredHandoffs()[0]?.uniqueCode, "ch-1");
  await ledger.expireHandoff("ch-1");
  assert.equal(ledger.getChallenge("ch-1")?.status, "closing");
});

test("adaptive recovery policy grows as the queue shrinks", async () => {
  const many = await setupLedger(Array.from({ length: 11 }, (_, index) => `many-${index}`));
  for (let index = 0; index < 11; index += 1) {
    const code = `many-${index}`;
    await many.ledger.acquire(code, "main", [code]);
    await many.ledger.defer(code, "covered", undefined, "main");
    await many.ledger.confirmClosed(code);
  }
  await many.ledger.maybeAdvancePhase();
  assert.equal(many.ledger.getState().phase, "second_pass");
  assert.equal(budgetPolicyFor(many.ledger.getState()).label, "second_pass");

  const late = await setupLedger(Array.from({ length: 10 }, (_, index) => `late-${index}`));
  for (let index = 0; index < 10; index += 1) {
    const code = `late-${index}`;
    await late.ledger.acquire(code, "main", [code]);
    await late.ledger.defer(code, "covered", undefined, "main");
    await late.ledger.confirmClosed(code);
  }
  await late.ledger.maybeAdvancePhase();
  assert.equal(budgetPolicyFor(late.ledger.getState()).label, "late_recovery");
});

test("platform sync cannot steal an in-flight reservation", async () => {
  const { ledger } = await setupLedger(["ch-1"]);
  await ledger.reserve("ch-1", "subagent:res", { isSubagent: true });
  await ledger.syncFromPlatform([platformChallenge("ch-1", { container_status: "stopped" })], true, "ip");
  assert.equal(ledger.getChallenge("ch-1")?.status, "reserved");
  assert.equal(ledger.getChallenge("ch-1")?.owner, "subagent:res");
  await ledger.confirmStarted("ch-1", ["a"], "subagent:res");
});

test("first-pass signal extensions are bounded and report only real deadline movement", async () => {
  let now = 10_000_000;
  const startedAt = now;
  const { ledger } = await setupLedger(["ch-1"], () => now);
  await ledger.acquire("ch-1", "main", ["a"]);
  assert.equal(ledger.getChallenge("ch-1")?.hardDeadlineAt, startedAt + 12 * 60_000);

  for (let index = 1; index <= 2; index += 1) {
    now += 60_000;
    const result = await ledger.checkpoint("ch-1", `signal ${index}`, undefined, `probe ${index}`, "main", {
      signalKind: "foothold", evidenceRef: `request:${index}`
    });
    assert.equal(result.extended, true);
  }
  const deadlineAfterTwo = ledger.getChallenge("ch-1")!.hardDeadlineAt;
  assert.equal(deadlineAfterTwo, startedAt + 18 * 60_000);
  now += 60_000;
  const capped = await ledger.checkpoint("ch-1", "third distinct signal", undefined, "probe 3", "main", {
    signalKind: "exploit_primitive", evidenceRef: "request:3"
  });
  assert.equal(capped.extended, false);
  assert.equal(capped.challenge.hardDeadlineAt, deadlineAfterTwo);
});

test("accepted flags cannot extend a first-pass worker beyond its 30-minute cap", async () => {
  let now = 20_000_000;
  const startedAt = now;
  const { ledger } = await setupLedger(["ch-1"], () => now);
  await ledger.syncFromPlatform([platformChallenge("ch-1", { flag_count: 3 })], true, "ip");
  await ledger.acquire("ch-1", "main", ["a"]);
  now += 29 * 60_000;
  await ledger.recordSubmission("ch-1", "flag{late}", true, 50, 1, 0, "main");
  assert.equal(ledger.getChallenge("ch-1")?.hardDeadlineAt, startedAt + 30 * 60_000);
  now += 60_000;
  assert.equal(ledger.budgetFor("ch-1")?.hardExpired, true);
});

test("endgame starts new approach epochs but requires a fresh worker after 30 minutes without a flag", async () => {
  let now = 30_000_000;
  const { ledger } = await setupLedger(["ch-1"], () => now);
  await ledger.acquire("ch-1", "main", ["a"]);
  await ledger.defer("ch-1", "first pass", "try source audit", "main");
  await ledger.confirmClosed("ch-1");
  await ledger.maybeAdvancePhase();
  assert.equal(ledger.getState().phase, "endgame");
  await ledger.acquire("ch-1", "main", ["b"]);
  const attemptStart = now;

  now += 12 * 60_000;
  assert.equal(ledger.isBudgetExhausted("ch-1"), true);
  const reset = await ledger.checkpoint("ch-1", "switching to source audit", undefined, "inspect source bundle", "main", {
    signalKind: "note", currentApproach: "source audit"
  });
  assert.equal(reset.extended, true);
  assert.equal(ledger.isBudgetExhausted("ch-1"), false);

  now = attemptStart + 30 * 60_000;
  assert.equal(ledger.budgetFor("ch-1")?.workerRotationDue, true);
});

test("endgame candidates prioritize one remaining flag, then other partial progress", async () => {
  const { ledger } = await setupLedger(["one-left", "partial-many", "no-flags"]);
  for (const code of ["one-left", "partial-many", "no-flags"]) {
    await ledger.acquire(code, "main", [code]);
    await ledger.defer(code, "covered", undefined, "main");
    await ledger.confirmClosed(code);
  }
  await ledger.maybeAdvancePhase();
  assert.equal(ledger.getState().phase, "endgame");
  Object.assign(ledger.getChallenge("one-left")!, { flagCount: 3, correctFlagCount: 2, totalScore: 100 });
  Object.assign(ledger.getChallenge("partial-many")!, { flagCount: 5, correctFlagCount: 2, totalScore: 500 });
  Object.assign(ledger.getChallenge("no-flags")!, { flagCount: 3, correctFlagCount: 0, totalScore: 1_000 });
  assert.deepEqual(ledger.candidates(3).map((challenge) => challenge.uniqueCode), ["one-left", "partial-many", "no-flags"]);
});
