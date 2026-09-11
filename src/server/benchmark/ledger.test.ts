import test from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, rm } from "node:fs/promises";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { BenchmarkLedger, BENCHMARK_MAX_CONTAINERS, FIRST_ATTEMPT_LIMIT_MS, FIRST_ATTEMPT_WARNING_MS, type ChallengeState } from "./ledger";
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

test("first attempt warns at 25 minutes and expires at 30 minutes", async () => {
  let now = 1_000_000;
  const { ledger } = await setupLedger(["ch-1"], () => now);
  await ledger.acquire("ch-1", "main", ["a"]);
  assert.equal(ledger.isBudgetExhausted("ch-1"), false);
  now += FIRST_ATTEMPT_WARNING_MS;
  assert.equal(ledger.isBudgetExhausted("ch-1"), false);
  assert.equal((await ledger.consumeFirstAttemptWarning("main"))?.uniqueCode, "ch-1");
  assert.equal(await ledger.consumeFirstAttemptWarning("main"), undefined, "warning is emitted once");
  now += FIRST_ATTEMPT_LIMIT_MS - FIRST_ATTEMPT_WARNING_MS;
  assert.equal(ledger.isBudgetExhausted("ch-1"), true);
});

test("the first-attempt clock starts after the platform start is confirmed", async () => {
  let now = 1_500_000;
  const { ledger } = await setupLedger(["ch-1"], () => now);
  await ledger.reserve("ch-1", "main");
  assert.equal(ledger.getChallenge("ch-1")?.currentAttemptStartedAt, null);
  now += 10 * 60_000; // Slow control-plane start must not consume solve time.
  await ledger.confirmStarted("ch-1", ["a"], "main");
  assert.equal(ledger.getChallenge("ch-1")?.currentAttemptStartedAt, now);
  assert.equal(ledger.getChallenge("ch-1")?.hardDeadlineAt, now + FIRST_ATTEMPT_LIMIT_MS);
});

test("challenge action locks serialize one challenge without blocking another", async () => {
  const { ledger } = await setupLedger(["ch-1", "ch-2"]);
  const events: string[] = [];
  let releaseFirst!: () => void;
  const firstBlocked = new Promise<void>((resolve) => { releaseFirst = resolve; });
  const first = ledger.runChallengeAction("ch-1", async () => {
    events.push("first:start");
    await firstBlocked;
    events.push("first:end");
  });
  const sameChallenge = ledger.runChallengeAction("ch-1", async () => { events.push("same:start"); });
  const otherChallenge = ledger.runChallengeAction("ch-2", async () => { events.push("other:start"); });
  await otherChallenge;
  assert.deepEqual(events, ["first:start", "other:start"]);
  releaseFirst();
  await Promise.all([first, sameChallenge]);
  assert.deepEqual(events, ["first:start", "other:start", "first:end", "same:start"]);
});

test("challenge blackboard redacts the benchmark platform token", async () => {
  const previousToken = process.env.BENCHMARK_TOKEN;
  process.env.BENCHMARK_TOKEN = "platform-secret-token";
  try {
    const { ledger } = await setupLedger(["ch-1"]);
    await ledger.acquire("ch-1", "main", ["a"]);
    await ledger.checkpoint("ch-1", "observed platform-secret-token in copied output", undefined, "do not use platform-secret-token", "main", {
      signalKind: "note", evidenceRef: "artifact:platform-secret-token", currentApproach: "inspect platform-secret-token"
    });
    const serialized = JSON.stringify(ledger.getChallenge("ch-1")?.blackboard);
    assert.doesNotMatch(serialized, /platform-secret-token/);
    assert.match(serialized, /REDACTED_BENCHMARK_TOKEN/);
  } finally {
    if (previousToken === undefined) delete process.env.BENCHMARK_TOKEN;
    else process.env.BENCHMARK_TOKEN = previousToken;
  }
});

test("evidence-backed progress updates the blackboard but never extends attempt 1", async () => {
  let now = 2_000_000;
  const { ledger } = await setupLedger(undefined, () => now);
  await ledger.acquire("ch-1", "main", ["a"]);
  const initialDeadline = ledger.getChallenge("ch-1")!.hardDeadlineAt!;
  now += 2 * 60 * 1000;
  const first = await ledger.checkpoint("ch-1", "obtained admin session", ["auth"], "query admin API", "main", {
    signalKind: "privilege_change", evidenceRef: "request:req-7", currentApproach: "auth bypass"
  });
  assert.equal(first.extended, false);
  assert.equal(first.challenge.hardDeadlineAt, initialDeadline);
  assert.equal(first.challenge.blackboard.at(-1)?.evidenceRef, "request:req-7");
  now += 60_000;
  const paraphrase = await ledger.checkpoint("ch-1", "admin access confirmed", ["auth"], "query admin API", "main", {
    signalKind: "privilege_change", evidenceRef: "request:req-7", currentApproach: "auth bypass"
  });
  assert.equal(paraphrase.updated, true);
  assert.equal(paraphrase.extended, false, "rewriting the same evidence must not buy more time");
  await ledger.checkpoint("ch-1", "ordinary follow-up note", undefined, "inspect audit log", "main", { signalKind: "note" });
  assert.equal(ledger.getChallenge("ch-1")?.lastMeaningfulSignalContent, "obtained admin session", "a note must not overwrite the evidence recovery brief");
});

test("an evidence-backed exclusion needs no proposed next step and never extends the timebox", async () => {
  const { ledger } = await setupLedger(["ch-1"]);
  await ledger.acquire("ch-1", "main", ["a"]);
  const deadline = ledger.getChallenge("ch-1")!.hardDeadlineAt;
  await ledger.checkpoint("ch-1", "Fixture evidence rules out one tested assumption", ["fixture"], undefined, "main", {
    signalKind: "decisive_rule_out", evidenceRef: "artifact:fixture", ruledOutFamilies: ["fixture assumption"]
  });
  assert.equal(ledger.getChallenge("ch-1")!.lastMeaningfulSignalContent, "Fixture evidence rules out one tested assumption");
  assert.equal(ledger.getChallenge("ch-1")!.hardDeadlineAt, deadline);
  assert.equal(ledger.getChallenge("ch-1")!.nextProbe, "");
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

test("a newly accepted partial flag records progress without extending attempt 1", async () => {
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
  assert.equal(challenge.hardDeadlineAt, 3_000_000 + FIRST_ATTEMPT_LIMIT_MS);
  assert.equal(challenge.blackboard.at(-1)?.kind, "submission");
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
  assert.deepEqual(restarted.candidates(10).map((candidate) => candidate.uniqueCode), ["ch-1"], "the interrupted coverage attempt must remain visible");
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

test("a stale concurrent platform sync cannot move flag progress backwards", async () => {
  const { ledger } = await setupLedger(["ch-1"]);
  await ledger.acquire("ch-1", "main", ["a"]);
  await ledger.recordSubmission("ch-1", "flag{1}", true, 30, 1, 0, "main");

  await ledger.syncFromPlatform([platformChallenge("ch-1")], true, "10.0.0.1");

  assert.equal(ledger.getChallenge("ch-1")?.correctFlagCount, 1);
  assert.equal(ledger.getState().cumulativeScore, 30);
  assert.equal(ledger.getState().scoreExact, true);
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

test("a stale concurrent sync cannot erase locally confirmed completion", async () => {
  const { ledger } = await setupLedger(["ch-1"]);
  await ledger.acquire("ch-1", "main", ["a"]);
  const guard = ledger.captureSyncGuard();
  await ledger.markSolved("ch-1", 100, "main");
  await ledger.confirmClosed("ch-1");
  // A list request that started before submit may return after local completion.
  await ledger.syncFromPlatform([platformChallenge("ch-1", {
    is_completed: false,
    container_status: "available",
    container_addr: ["stale"]
  })], true, "ip", true, guard);
  assert.equal(ledger.getChallenge("ch-1")?.status, "solved");
  assert.equal(ledger.getChallenge("ch-1")?.containerStatus, "stopped");
});

test("a stale sync snapshot cannot resurrect a container closed by a concurrent defer", async () => {
  const { ledger } = await setupLedger(["ch-1"]);
  await ledger.acquire("ch-1", "main", ["a"]);
  const guard = ledger.captureSyncGuard();
  await ledger.defer("ch-1", "covered", undefined, "main");
  await ledger.confirmClosed("ch-1");

  await ledger.syncFromPlatform([platformChallenge("ch-1", {
    container_status: "available",
    container_addr: ["stale"]
  })], true, "ip", true, guard);

  assert.equal(ledger.getChallenge("ch-1")?.status, "deferred");
  assert.equal(ledger.getChallenge("ch-1")?.containerStatus, "stopped");
  assert.equal(ledger.getState().activeContainers, 0);
});

test("coverage advances to revisit only after the first attempt settles", async () => {
  const { ledger } = await setupLedger(["ch-1"]);
  await ledger.acquire("ch-1", "main", ["a"]);
  await ledger.defer("ch-1", "first pass", undefined, "main");
  await ledger.confirmClosed("ch-1");
  const phase = await ledger.maybeAdvancePhase();
  assert.equal(phase, "revisit");
});

test("phase does not advance while first-pass workers are still active", async () => {
  const { ledger } = await setupLedger(["ch-1", "ch-2"]);
  await ledger.acquire("ch-1", "main", ["a"]);
  await ledger.acquire("ch-2", "subagent:t1", ["b"]);
  assert.equal(await ledger.maybeAdvancePhase(), "coverage");
});

test("coverage candidates are ordered strictly from low score to high score", async () => {
  const sessionId = `test-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
  const ledger = await new BenchmarkLedger(sessionId).initialize();
  await ledger.syncFromPlatform([
    platformChallenge("hard-high", { difficulty: "hard", total_score: 300 }),
    platformChallenge("easy-low", { difficulty: "easy", total_score: 50 }),
    platformChallenge("easy-high", { difficulty: "easy", total_score: 200 }),
    platformChallenge("med", { difficulty: "medium", total_score: 100 })
  ], true, "ip");
  const candidates = ledger.candidates(10);
  assert.deepEqual(candidates.map((candidate) => candidate.uniqueCode), ["easy-low", "med", "easy-high", "hard-high"]);
  await assert.rejects(() => ledger.reserve("hard-high", "main"), /low score to high score/);
  await ledger.acquire("easy-low", "main", ["a"]);
  await ledger.defer("easy-low", "covered", undefined, "main");
  await ledger.confirmClosed("easy-low");
  assert.deepEqual(ledger.candidates(10).map((candidate) => candidate.uniqueCode), ["med", "easy-high", "hard-high"]);
});

test("stranded revisit live orphans stay resumable during coverage to free container slots", async () => {
  const sessionId = `test-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
  const ledger = await new BenchmarkLedger(sessionId).initialize();
  await ledger.syncFromPlatform([
    platformChallenge("a", { total_score: 50 }),
    platformChallenge("b", { total_score: 100 }),
    platformChallenge("c", { total_score: 200 })
  ], true, "ip");
  for (const [code, owner] of [["a", "main"], ["b", "subagent:t1"], ["c", "subagent:t2"]] as const) {
    await ledger.acquire(code, owner, [code]);
    await ledger.defer(code, "first attempt done", undefined, owner);
    await ledger.confirmClosed(code);
  }
  await ledger.maybeAdvancePhase();
  assert.equal(ledger.getState().phase, "revisit");

  // All three workers start revisit attempts, the platform adds an unseen
  // challenge, and the process restarts with every container still live.
  await ledger.acquire("a", "main", ["a"]);
  await ledger.acquire("b", "subagent:t1", ["b"]);
  await ledger.acquire("c", "subagent:t2", ["c"]);
  const restarted = await new BenchmarkLedger(sessionId).initialize();
  await restarted.syncFromPlatform([
    platformChallenge("a", { total_score: 50, container_status: "available", container_addr: ["a"] }),
    platformChallenge("b", { total_score: 100, container_status: "available", container_addr: ["b"] }),
    platformChallenge("c", { total_score: 200, container_status: "available", container_addr: ["c"] }),
    platformChallenge("new", { total_score: 30 })
  ], true, "ip");
  assert.equal(restarted.getState().activeContainers, 3);
  assert.equal(restarted.getState().phase, "coverage");

  // The unseen challenge is blocked by the saturated container cap...
  await assert.rejects(() => restarted.reserve("new", "main"), /Container limit/);
  // ...so the stranded revisit orphans must stay resumable (they already own
  // their slots) and visible at the back of the coverage candidates.
  const candidateCodes = restarted.candidates(10).map((challenge) => challenge.uniqueCode);
  // Stranded revisit orphans are tier 2: listed after the unseen queue,
  // ordered lowest score first within the tier.
  assert.deepEqual(candidateCodes, ["new", "a", "b", "c"]);
  const resumed = await restarted.reserve("a", "main");
  assert.equal(resumed.status, "reserved");
  assert.equal(resumed.attemptCount, 2, "resuming continues the in-flight revisit attempt, not a new one");

  // Deferring the resumed orphan frees a slot and unblocks the unseen queue.
  await restarted.defer("a", "freed the stranded slot", undefined, "main");
  await restarted.confirmClosed("a");
  await restarted.reserve("new", "main");
  assert.equal(restarted.getChallenge("new")?.status, "reserved");
});

test("coverage opens the next-lowest challenge as soon as a worker reserves the current one", async () => {
  const sessionId = `test-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
  const ledger = await new BenchmarkLedger(sessionId).initialize();
  await ledger.syncFromPlatform([
    platformChallenge("high", { total_score: 300 }),
    platformChallenge("low", { total_score: 50 }),
    platformChallenge("mid", { total_score: 100 })
  ], true, "ip");

  assert.deepEqual(ledger.candidates(10).map((challenge) => challenge.uniqueCode), ["low", "mid", "high"]);
  await ledger.reserve("low", "main");
  assert.deepEqual(ledger.candidates(10).map((challenge) => challenge.uniqueCode), ["mid", "high"]);
  await ledger.reserve("mid", "subagent:t1", { isSubagent: true });
  assert.deepEqual(ledger.candidates(10).map((challenge) => challenge.uniqueCode), ["high"]);
  await ledger.reserve("high", "subagent:t2", { isSubagent: true });

  assert.deepEqual(ledger.candidates(10), []);
});

test("resource unavailability does not consume attempt 1 or block later coverage", async () => {
  let now = 40_000_000;
  const { ledger } = await setupLedger(["low", "mid", "high"], () => now);
  Object.assign(ledger.getChallenge("low")!, { totalScore: 50 });
  Object.assign(ledger.getChallenge("mid")!, { totalScore: 100 });
  Object.assign(ledger.getChallenge("high")!, { totalScore: 300 });

  await ledger.reserve("low", "main");
  await ledger.releaseReservation("low", "main", { resourceUnavailable: true, reason: "capacity exhausted" });
  assert.equal(ledger.getChallenge("low")?.attemptCount, 0);
  assert.equal(ledger.getMetrics().challenges.low?.attempts, 0);
  assert.deepEqual(ledger.candidates(10).map((challenge) => challenge.uniqueCode), ["mid", "high"]);

  for (const code of ["mid", "high"]) {
    await ledger.acquire(code, "main", [code]);
    await ledger.defer(code, "coverage complete", undefined, "main");
    await ledger.confirmClosed(code);
  }

  assert.equal(ledger.getState().phase, "coverage");
  assert.deepEqual(ledger.candidates(10).map((challenge) => challenge.uniqueCode), ["low"]);
  now += 1_000;
  await ledger.acquire("low", "main", ["low"]);
  assert.equal(ledger.getChallenge("low")?.attemptCount, 1);
  assert.equal(ledger.getChallenge("low")?.hardDeadlineAt, now + FIRST_ATTEMPT_LIMIT_MS);
  assert.equal(ledger.getMetrics().challenges.low?.attempts, 1);
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
  await ledger.acquire("ch-1", "main", ["b"]);
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

test("automatic completion freezes run elapsed time", async () => {
  let now = 50_000_000;
  const { ledger } = await setupLedger(["ch-1"], () => now);
  now += 5_000;
  await ledger.acquire("ch-1", "main", ["a"]);
  await ledger.markSolved("ch-1", 100, "main");
  now += 2_000;
  await ledger.confirmClosed("ch-1");
  const elapsedAtCompletion = ledger.runElapsedMs();
  const completedAt = ledger.getMetrics().completedAt;

  now += 60_000;
  assert.equal(ledger.getState().phase, "completed");
  assert.equal(ledger.getMetrics().completedAt, completedAt);
  assert.equal(ledger.runElapsedMs(), elapsedAtCompletion);
});

test("session cleanup adopts and releases a live orphan", async () => {
  const { ledger, sessionId } = await setupLedger(["ch-1"]);
  await ledger.acquire("ch-1", "subagent:t1", ["a"]);
  await ledger.checkpoint("ch-1", "found admin route", ["auth"], "inspect admin API", "subagent:t1", {
    currentApproach: "auth bypass"
  });
  const restarted = await new BenchmarkLedger(sessionId).initialize();
  assert.equal(restarted.getChallenge("ch-1")?.status, "orphaned");

  await restarted.releaseForSessionCleanup("ch-1", "session archived", true);
  const closing = restarted.getChallenge("ch-1")!;
  assert.equal(closing.status, "closing");
  assert.equal(closing.pendingStatus, "deferred");
  assert.equal(closing.owner, null);
  assert.equal(closing.approachHistory.at(-1)?.worker, "subagent:t1");
  assert.match(closing.blackboard.at(-1)?.summary ?? "", /session archived/);

  await restarted.confirmClosed("ch-1");
  assert.equal(restarted.getChallenge("ch-1")?.status, "deferred");
  assert.equal(restarted.getState().activeContainers, 0);
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

test("defer always closes the container and preserves the blackboard", async () => {
  const now = 4_000_000;
  const { ledger } = await setupLedger(["ch-1"], () => now);
  await ledger.syncFromPlatform([platformChallenge("ch-1", { flag_count: 2 })], true, "ip");
  await ledger.acquire("ch-1", "main", ["a"]);
  await ledger.checkpoint("ch-1", "admin session captured", ["auth"], "query admin API", "main", {
    signalKind: "credential", evidenceRef: "artifact:cookie", currentApproach: "auth bypass"
  });
  await ledger.recordSubmission("ch-1", "flag{one}", true, 50, 1, 0, "main");
  await ledger.defer("ch-1", "first pass done", "try source audit", "main");
  assert.equal(ledger.getChallenge("ch-1")?.status, "closing");
  await ledger.confirmClosed("ch-1");
  await ledger.maybeAdvancePhase();
  assert.equal(ledger.getState().phase, "revisit");
  assert.equal(ledger.getChallenge("ch-1")?.containerStatus, "stopped");
  assert.match(ledger.getChallenge("ch-1")?.blackboard.map((entry) => entry.summary).join("\n") ?? "", /admin session captured/);
  assert.equal(ledger.candidates(1)[0]?.uniqueCode, "ch-1");
});

test("a challenge cannot be revisited until every first attempt has settled", async () => {
  const { ledger } = await setupLedger(["low", "high"]);
  Object.assign(ledger.getChallenge("low")!, { totalScore: 10 });
  Object.assign(ledger.getChallenge("high")!, { totalScore: 100 });
  await ledger.acquire("low", "main", ["a"]);
  await ledger.defer("low", "covered", undefined, "main");
  await ledger.confirmClosed("low");
  await assert.rejects(() => ledger.acquire("low", "main", ["b"]), /cannot be reserved|unseen challenges remain/);
  await ledger.acquire("high", "main", ["c"]);
  await ledger.defer("high", "covered", undefined, "main");
  await ledger.confirmClosed("high");
  assert.equal(await ledger.maybeAdvancePhase(), "revisit");
});

test("platform sync cannot steal an in-flight reservation", async () => {
  const { ledger } = await setupLedger(["ch-1"]);
  await ledger.reserve("ch-1", "subagent:res", { isSubagent: true });
  await ledger.syncFromPlatform([platformChallenge("ch-1", { container_status: "stopped" })], true, "ip");
  assert.equal(ledger.getChallenge("ch-1")?.status, "reserved");
  assert.equal(ledger.getChallenge("ch-1")?.owner, "subagent:res");
  await ledger.confirmStarted("ch-1", ["a"], "subagent:res");
});

test("the first-attempt deadline is fixed regardless of checkpoint evidence", async () => {
  let now = 10_000_000;
  const startedAt = now;
  const { ledger } = await setupLedger(["ch-1"], () => now);
  await ledger.acquire("ch-1", "main", ["a"]);
  assert.equal(ledger.getChallenge("ch-1")?.hardDeadlineAt, startedAt + FIRST_ATTEMPT_LIMIT_MS);

  for (let index = 1; index <= 2; index += 1) {
    now += 60_000;
    const result = await ledger.checkpoint("ch-1", `signal ${index}`, undefined, `probe ${index}`, "main", {
      signalKind: "foothold", evidenceRef: `request:${index}`
    });
    assert.equal(result.extended, false);
  }
  const deadlineAfterTwo = ledger.getChallenge("ch-1")!.hardDeadlineAt;
  assert.equal(deadlineAfterTwo, startedAt + FIRST_ATTEMPT_LIMIT_MS);
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
  assert.equal(ledger.budgetFor("ch-1")?.expired, true);
});

test("attempt 2 and later have no runtime time limit", async () => {
  let now = 30_000_000;
  const { ledger } = await setupLedger(["ch-1"], () => now);
  await ledger.acquire("ch-1", "main", ["a"]);
  await ledger.defer("ch-1", "first pass", "try source audit", "main");
  await ledger.confirmClosed("ch-1");
  await ledger.maybeAdvancePhase();
  assert.equal(ledger.getState().phase, "revisit");
  await ledger.acquire("ch-1", "main", ["b"]);
  now += 24 * 60 * 60_000;
  assert.equal(ledger.budgetFor("ch-1")?.firstAttempt, false);
  assert.equal(ledger.isBudgetExhausted("ch-1"), false);
});

test("revisit candidates finish the current sweep before selecting a just-deferred challenge again", async () => {
  const { ledger } = await setupLedger(["low", "mid", "high"]);
  for (const code of ["low", "mid", "high"]) {
    await ledger.acquire(code, "main", [code]);
    await ledger.defer(code, "covered", undefined, "main");
    await ledger.confirmClosed(code);
  }
  await ledger.maybeAdvancePhase();
  Object.assign(ledger.getChallenge("low")!, { totalScore: 10 });
  Object.assign(ledger.getChallenge("mid")!, { totalScore: 50 });
  Object.assign(ledger.getChallenge("high")!, { totalScore: 100 });
  assert.deepEqual(ledger.candidates(3).map((challenge) => challenge.uniqueCode), ["low", "mid", "high"]);
  await ledger.acquire("low", "main", ["low-2"]);
  await ledger.defer("low", "still stuck", "try a third approach later", "main");
  await ledger.confirmClosed("low");
  assert.deepEqual(ledger.candidates(3).map((challenge) => challenge.uniqueCode), ["mid", "high", "low"]);
});

test("revisit acquisition enforces FIFO order", async () => {
  const { ledger } = await setupLedger(["low", "high"]);
  for (const code of ["low", "high"]) {
    await ledger.acquire(code, "main", [code]);
    await ledger.defer(code, "covered", undefined, "main");
    await ledger.confirmClosed(code);
  }
  await assert.rejects(() => ledger.reserve("high", "main"), /Revisit queue is FIFO.*low before high/);
  await ledger.reserve("low", "main");
});

test("revisit queue order wins over attempt count", async () => {
  const { ledger } = await setupLedger(["older", "newer"]);
  for (const code of ["older", "newer"]) {
    await ledger.acquire(code, "main", [code]);
    await ledger.defer(code, "covered", undefined, "main");
    await ledger.confirmClosed(code);
  }
  Object.assign(ledger.getChallenge("older")!, { attemptCount: 3, revisitQueueOrder: 10 });
  Object.assign(ledger.getChallenge("newer")!, { attemptCount: 2, revisitQueueOrder: 20 });
  assert.deepEqual(ledger.candidates(2).map((challenge) => challenge.uniqueCode), ["older", "newer"]);
});
