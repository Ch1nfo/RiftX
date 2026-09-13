import test, { type TestContext } from "node:test";
import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { buildBenchmarkContinuity } from "./continuity";
import { BenchmarkLedger, ATTEMPT_LIMIT_MS, ATTEMPT_WARNING_MS } from "./ledger";
import type { Challenge } from "./controller";

const MINUTE = 60_000;

function platformChallenge(code: string): Challenge {
  return {
    unique_code: code, description: `Synthetic fixture ${code}`, difficulty: "easy", level: 1,
    total_score: 100, flag_count: 3, correct_flag_count: 0, is_completed: false,
    container_status: "stopped", container_addr: []
  };
}

async function setup(t: TestContext, now = () => Date.now(), codes = ["fixture", "second", "third"]) {
  const sessionId = `continuity-rotation-test-${randomUUID()}`;
  t.after(() => BenchmarkLedger.destroy(sessionId));
  const ledger = await new BenchmarkLedger(sessionId, now).initialize();
  if (codes.length) await ledger.syncFromPlatform(codes.map(platformChallenge), true, "127.0.0.1");
  return ledger;
}

async function startRevisit(ledger: BenchmarkLedger) {
  await ledger.acquire("fixture", "main", ["127.0.0.1:80"]);
  await ledger.defer("fixture", "fixture-previous-failure", "fixture-proposed-next-probe", "main");
  await ledger.confirmClosed("fixture");
  await ledger.maybeAdvancePhase();
  await ledger.acquire("fixture", "main", ["127.0.0.1:80"]);
}

test("empty ledgers have no continuity packet", async (t) => {
  assert.equal(buildBenchmarkContinuity(await setup(t, undefined, [])), "");
});

test("active continuity retains verified evidence and labels proposed plans as candidates", async (t) => {
  const ledger = await setup(t);
  await ledger.acquire("fixture", "main", ["127.0.0.1:80"]);
  await ledger.checkpoint("fixture", "fixture-confirmed-evidence", ["fixture-tried-family"], "fixture-candidate-next-probe", "main", {
    signalKind: "credential", evidenceRef: "artifact:fixture-observation", currentApproach: "fixture-candidate-approach"
  });
  const state = structuredClone(ledger.getState());
  const packet = buildBenchmarkContinuity(ledger);
  for (const marker of ["fixture-confirmed-evidence", "artifact:fixture-observation", "fixture-candidate-next-probe", "fixture-candidate-approach"]) assert.ok(packet.includes(marker));
  assert.match(packet, /Candidates to verify against the blackboard/);
  assert.match(packet, /limit=30 minutes/);
  assert.deepEqual(ledger.getState(), state, "rendering evidence cannot alter ownership, exclusion decisions or scores");
});

for (const revisit of [false, true]) {
  test(`${revisit ? "revisit" : "first attempt"} shows the generic deadline notice`, async (t) => {
    let now = 1_000_000;
    const ledger = await setup(t, () => now, ["fixture"]);
    if (revisit) await startRevisit(ledger);
    else await ledger.acquire("fixture", "main", ["127.0.0.1:80"]);
    now += ATTEMPT_LIMIT_MS;
    const packet = buildBenchmarkContinuity(ledger);
    assert.match(packet, /ATTEMPT_TIMEBOX_COMPLETE/);
    assert.match(packet, /Solving tools are blocked/);
    assert.match(packet, /submission and cleanup remain available/);
    assert.doesNotMatch(packet, /FIRST_ATTEMPT_COMPLETE|No runtime time limit/);
  });

  test(`${revisit ? "revisit" : "first attempt"} warning is sampled at twenty-five minutes without consumption`, async (t) => {
    let now = 1_000_000;
    const ledger = await setup(t, () => now, ["fixture"]);
    if (revisit) await startRevisit(ledger);
    else await ledger.acquire("fixture", "main", ["127.0.0.1:80"]);
    now += ATTEMPT_WARNING_MS;
    const warning = ledger.attemptWarningFor("main");
    assert.ok(warning);
    const packet = buildBenchmarkContinuity(ledger, "main", warning);
    assert.match(packet, /ATTEMPT_WARNING: 25 minutes/);
    assert.equal(buildBenchmarkContinuity(ledger, "main", ledger.attemptWarningFor("main")), packet);
    assert.ok(ledger.attemptWarningFor("main"));
    await ledger.acknowledgeAttemptWarning("main", warning.uniqueCode, warning.currentAttemptStartedAt);
    assert.equal(ledger.attemptWarningFor("main"), undefined);
  });
}

test("routine continuity uses a stable absolute deadline instead of a changing countdown", async (t) => {
  let now = 1_000_000;
  const ledger = await setup(t, () => now);
  await ledger.acquire("fixture", "main", ["127.0.0.1:80"]);
  const before = buildBenchmarkContinuity(ledger);
  now += MINUTE;
  assert.equal(buildBenchmarkContinuity(ledger), before);
  assert.doesNotMatch(before, /elapsed|remaining/i);
});

test("a verified revisit extension appears as forty minutes with its updated deadline", async (t) => {
  let now = 1_000_000;
  const ledger = await setup(t, () => now, ["fixture"]);
  await startRevisit(ledger);
  now += 27 * MINUTE;
  const checkpoint = await ledger.checkpoint("fixture", "fixture-verified-stage-transition", undefined, "fixture-follow-up", "main", {
    signalKind: "stage_transition", evidenceRef: "artifact:fixture-stage-transition"
  });
  assert.equal(checkpoint.extended, true);
  const budget = ledger.budgetFor("fixture")!;
  const packet = buildBenchmarkContinuity(ledger);
  assert.match(packet, /limit=40 minutes; extension=used/);
  assert.ok(packet.includes(new Date(budget.deadlineAt!).toISOString()));
});

test("a new attempt inherits blackboard evidence and reviews the previous failure and next probe", async (t) => {
  const ledger = await setup(t, undefined, ["fixture"]);
  await ledger.acquire("fixture", "main", ["127.0.0.1:80"]);
  await ledger.checkpoint("fixture", "fixture-valid-partial-evidence", ["fixture-tried-family"], "fixture-proposed-next-probe", "main", {
    signalKind: "credential", evidenceRef: "artifact:fixture-stage-one", currentApproach: "fixture-previous-approach"
  });
  await ledger.defer("fixture", "fixture-previous-failure", "fixture-proposed-next-probe", "main");
  await ledger.confirmClosed("fixture");
  await ledger.maybeAdvancePhase();
  await ledger.acquire("fixture", "main", ["127.0.0.1:80"]);
  const packet = buildBenchmarkContinuity(ledger);
  for (const marker of ["fixture-valid-partial-evidence", "artifact:fixture-stage-one", "fixture-previous-failure", "fixture-proposed-next-probe", "fixture-previous-approach"]) assert.ok(packet.includes(marker));
  assert.match(packet, /Previous attempt to review/);
  assert.match(packet, /candidates to verify/);
  assert.match(packet, /unsuccessful attempt alone does not rule out/);
  assert.deepEqual(ledger.getChallenge("fixture")!.ruledOutFamilies, []);
});

test("bounded continuity retains warnings, slots, key evidence and the immediate handoff", async (t) => {
  const ledger = await setup(t);
  await ledger.acquire("fixture", "main", ["127.0.0.1:80"]);
  const state = structuredClone(ledger.getState());
  const mine = state.challenges.fixture;
  mine.description = "d".repeat(10_000);
  mine.hintUsed = true;
  mine.hintContent = "h".repeat(10_000);
  mine.approachHistory = [{ attemptNumber: 1, phase: "coverage", worker: "main", startedAt: 1, endedAt: 2,
    flagsBefore: 0, flagsAfter: 1, triedFamilies: [], ruledOutFamilies: [], stopReason: "fixture-prior-failure",
    approach: "fixture-prior-approach", nextDistinctApproach: "fixture-prior-next-probe" }];
  mine.blackboard = Array.from({ length: 6 }, (_, index) => ({
    at: index, worker: "main" as const, kind: "credential" as const,
    summary: `fixture-evidence-${index} ` + "e".repeat(800), evidenceRef: `artifact:fixture-${index} ` + "r".repeat(250),
    approach: "", triedFamilies: [], ruledOutFamilies: [], nextProbe: ""
  }));
  state.challenges.second.status = "running";
  state.challenges.second.owner = "subagent:fixture-worker";
  const source = {
    getState: () => state, isEndgame: () => false, isBudgetExhausted: () => true,
    budgetFor: () => ledger.budgetFor("fixture"), intelForChallenge: () => [], candidates: () => [state.challenges.third]
  } as unknown as BenchmarkLedger;
  const packet = buildBenchmarkContinuity(source, "main", mine);
  assert.ok(packet.length <= 8_000);
  assert.match(packet, /ATTEMPT_WARNING/);
  assert.match(packet, /ATTEMPT_TIMEBOX_COMPLETE/);
  assert.match(packet, /fixture-prior-failure/);
  assert.match(packet, /fixture-prior-next-probe/);
  assert.match(packet, /"flagsDelta":1/);
  assert.match(packet, /"requiresRevalidation":true/);
  assert.match(packet, /SubAgent challenges \(1\/2\)/);
  for (let index = 0; index < 6; index++) assert.ok(packet.includes(`fixture-evidence-${index}`));
  assert.ok(packet.endsWith("</riftx-benchmark-continuity>"));
});

test("evidence references are preserved exactly or omitted as a complete field", async (t) => {
  const ledger = await setup(t, undefined, ["fixture"]);
  await ledger.acquire("fixture", "main", ["127.0.0.1:80"]);
  const state = structuredClone(ledger.getState());
  const reference = "artifact:/" + "fixture-long-segment/".repeat(30) + "evidence.json";
  const entry = { at: 1, worker: "main" as const, kind: "credential" as const,
    summary: "fixture-reference-evidence", evidenceRef: reference, approach: "", triedFamilies: [], ruledOutFamilies: [], nextProbe: "" };
  state.challenges.fixture.blackboard = [entry];
  const source = {
    getState: () => state, isEndgame: () => false, isBudgetExhausted: () => false,
    budgetFor: () => ledger.budgetFor("fixture"), intelForChallenge: () => [], candidates: () => []
  } as unknown as BenchmarkLedger;
  const packet = buildBenchmarkContinuity(source);
  assert.ok(packet.includes(reference));
  const hugeReference = reference.repeat(20);
  entry.evidenceRef = hugeReference;
  const bounded = buildBenchmarkContinuity(source);
  assert.ok(bounded.length <= 8_000);
  assert.ok(bounded.includes("fixture-reference-evidence"));
  assert.match(bounded, /"evidenceRefOmitted":true/);
  assert.equal(bounded.includes("fixture-long-segment"), false, "an incomplete pathname must never look usable");
});
