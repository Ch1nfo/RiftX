import test from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, rm } from "node:fs/promises";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { BenchmarkLedger, BENCHMARK_MAX_CONTAINERS, type ChallengeState } from "./ledger";
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

async function setupLedger(codes: string[] = ["ch-1", "ch-2", "ch-3", "ch-4"]) {
  const sessionId = `test-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
  const ledger = await new BenchmarkLedger(sessionId).initialize();
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
  const repeat = await ledger.checkpoint("ch-1", "found login page", undefined, undefined, "main");
  assert.equal(repeat.updated, false, "same signal must not reset the timer");
  const changed = await ledger.checkpoint("ch-1", "found SQL injection in search", undefined, undefined, "main");
  assert.equal(changed.updated, true);
});

test("signal budget exhaustion and check", async () => {
  const { ledger } = await setupLedger();
  await ledger.acquire("ch-1", "main", ["a"]);
  assert.equal(ledger.isBudgetExhausted("ch-1"), false);
  // Simulate elapsed time by manipulating the ledger entry.
  const challenge = ledger.getChallenge("ch-1") as ChallengeState;
  challenge.lastSignalAt = Date.now() - 9 * 60 * 1000; // 9 minutes ago
  assert.equal(ledger.isBudgetExhausted("ch-1"), true);
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

test("phase advances to second_pass when no pending remain", async () => {
  const { ledger } = await setupLedger(["ch-1"]);
  await ledger.acquire("ch-1", "main", ["a"]);
  await ledger.defer("ch-1", "first pass", undefined, "main");
  await ledger.confirmClosed("ch-1");
  const phase = await ledger.maybeAdvancePhase();
  assert.equal(phase, "second_pass");
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

test("platform sync cannot steal an in-flight reservation", async () => {
  const { ledger } = await setupLedger(["ch-1"]);
  await ledger.reserve("ch-1", "subagent:res", { isSubagent: true });
  await ledger.syncFromPlatform([platformChallenge("ch-1", { container_status: "stopped" })], true, "ip");
  assert.equal(ledger.getChallenge("ch-1")?.status, "reserved");
  assert.equal(ledger.getChallenge("ch-1")?.owner, "subagent:res");
  await ledger.confirmStarted("ch-1", ["a"], "subagent:res");
});
