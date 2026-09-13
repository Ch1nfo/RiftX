import assert from "node:assert/strict";
import test from "node:test";
import { mkdtemp, readdir, rm } from "node:fs/promises";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { buildBenchmarkCompactionFallback, type BenchmarkFallbackSource } from "./compaction-fallback";
import type { BenchmarkLedger, BenchmarkState, BlackboardEntry, ChallengeState } from "./ledger";

function challenge(code: string, owner: ChallengeState["owner"], overrides: Partial<ChallengeState> = {}): ChallengeState {
  return {
    uniqueCode: code, owner, status: "running", description: "synthetic task", blackboard: [],
    triedFamilies: [], ruledOutFamilies: [], triedFlags: [], approachHistory: [],
    lastMeaningfulSignalContent: "", lastEvidenceRef: "", lastSignalKind: null,
    ...overrides
  } as ChallengeState;
}

function source(challenges: ChallengeState[], overrides: Partial<BenchmarkFallbackSource> = {}): BenchmarkFallbackSource {
  const state = { challenges: Object.fromEntries(challenges.map((item) => [item.uniqueCode, item])) } as BenchmarkState;
  return {
    ledger: { getState: () => state } as BenchmarkLedger,
    worker: "main", workingDirectory: "/synthetic/work",
    ledgerFile: "/synthetic/state.json", sessionFile: "/synthetic/session.jsonl", ...overrides
  };
}

function entry(kind: BlackboardEntry["kind"], summary: string, evidenceRef = ""): BlackboardEntry {
  return { kind, summary, evidenceRef, at: 1, worker: "main", approach: "unused-approach", nextProbe: "unused-next-probe", triedFamilies: [], ruledOutFamilies: [] };
}

test("oversized evidence paths do not discard short observations", () => {
  const oversizedReference = `/synthetic/${"long-segment/".repeat(1000)}`;
  const current = challenge("current", "main", {
    lastMeaningfulSignalContent: "core-stage-evidence", lastEvidenceRef: oversizedReference, lastSignalKind: "stage_transition",
    blackboard: [entry("credential", "core-credential-evidence", oversizedReference)]
  });
  const result = buildBenchmarkCompactionFallback(source([current]), 4000);
  const packet = JSON.parse(result);
  assert.ok(result.length <= 4000);
  assert.equal(packet.reportedProgress.summary, "core-stage-evidence");
  assert.equal(packet.reportedProgress.evidenceRef, undefined);
  assert.equal(packet.reportedFacts[0].summary, "core-credential-evidence");
  assert.equal(packet.reportedFacts[0].evidenceRef, undefined);
  assert.doesNotMatch(result, /long-segment/);
});

test("fallback keeps structured evidence and independent exclusions without live state or old plans", () => {
  const current = challenge("current", "main", {
    totalScore: 9123, scoreObtained: 4567, correctFlagCount: 1, containerAddrs: ["live-container-marker"],
    nextProbe: "old-next-probe", currentApproach: "old-approach",
    lastMeaningfulSignalContent: "synthetic privilege evidence", lastSignalKind: "privilege_change", lastEvidenceRef: "/synthetic/evidence.txt",
    triedFamilies: ["tried-only"], ruledOutFamilies: ["excluded-with-evidence"],
    blackboard: [entry("credential", "synthetic credential observation", "/synthetic/credential.txt"), entry("note", "synthetic uncertain clue")],
    approachHistory: [{ attemptNumber: 1, phase: "coverage", worker: "main", approach: "past-approach", startedAt: 1, endedAt: 2, flagsBefore: 0, flagsAfter: 0, triedFamilies: ["past-tried"], ruledOutFamilies: [], stopReason: "synthetic stop", nextDistinctApproach: "old-distinct-plan" }]
  });
  current.blackboard.push({ ...entry("decisive_rule_out", "fixture_exclusion", "artifact:exclusion"), ruledOutFamilies: ["excluded-with-evidence"] });
  const result = buildBenchmarkCompactionFallback(source([current]));
  const packet = JSON.parse(result);
  assert.equal(packet.version, 1);
  assert.equal(packet.snapshot, "partial_historical");
  assert.deepEqual(packet.binding, { worker: "main", challenge: "current" });
  assert.equal(packet.recovery.ledgerFile, "/synthetic/state.json");
  assert.match(JSON.stringify(packet.reportedFacts), /credential\.txt/);
  assert.match(JSON.stringify(packet.reportedUncertainties), /uncertain clue/);
  assert.deepEqual(packet.tried, ["tried-only"]);
  assert.deepEqual(packet.ruledOut, ["excluded-with-evidence"]);
  assert.deepEqual(packet.previousCandidate, { requiresRevalidation: true, approach: "old-approach", nextProbe: "old-next-probe" });
  assert.equal(packet.lastAttempt.flagsDelta, 0);
  assert.doesNotMatch(result, /9123|4567|live-container-marker|old-distinct-plan|unused-next-probe|unused-approach/);
  assert.doesNotMatch(result, /"(?:owner|containerAddrs|correctFlagCount|scoreObtained|activeContainers|triedFlags)":/);
});

test("4000 characters retain binding and core evidence despite long descriptions and repeated notes", () => {
  const current = challenge("current", "subagent:worker", {
    description: "description-noise ".repeat(10_000),
    lastMeaningfulSignalContent: "core-stage-evidence", lastEvidenceRef: "/synthetic/stage.txt", lastSignalKind: "stage_transition",
    blackboard: [entry("credential", "core-credential-evidence", "/synthetic/credential.txt"), ...Array.from({ length: 200 }, (_, i) => ({ ...entry("note", `noise-${i} ${"x".repeat(2000)}`), at: i + 2 }))],
    triedFamilies: ["tried-family"], ruledOutFamilies: ["excluded-family"]
  });
  current.blackboard.push({ ...entry("decisive_rule_out", "fixture_exclusion", "artifact:exclusion"), ruledOutFamilies: ["excluded-family"] });
  const input = source([current], { worker: "subagent:worker", assignedChallenge: "current" });
  const result = buildBenchmarkCompactionFallback(input, 4000);
  const packet = JSON.parse(result);
  assert.ok(result.length <= 4000);
  assert.equal(packet.binding.challenge, "current");
  assert.match(result, /core-stage-evidence/);
  assert.match(result, /core-credential-evidence/);
  assert.deepEqual(packet.tried, ["tried-family"]);
  assert.deepEqual(packet.ruledOut, ["excluded-family"]);
  assert.equal(result, buildBenchmarkCompactionFallback(input, 4000));
  assert.ok(buildBenchmarkCompactionFallback(input, 100_000).length <= 16_000);
});

test("only the real active worker binding supplies facts, and idle main copies no challenge payloads", () => {
  const own = challenge("own", "subagent:worker", { lastMeaningfulSignalContent: "own-fact" });
  const other = challenge("other", "main", { lastMeaningfulSignalContent: "other-fact" });
  const child = buildBenchmarkCompactionFallback(source([own, other], { worker: "subagent:worker", assignedChallenge: "own" }));
  assert.match(child, /own-fact/);
  assert.doesNotMatch(child, /other-fact/);
  const mismatched = JSON.parse(buildBenchmarkCompactionFallback(source([own, other], { worker: "subagent:worker", assignedChallenge: "other" })));
  assert.equal(mismatched.binding.challenge, null);
  assert.equal(mismatched.binding.assignedChallenge, "other");
  assert.equal(mismatched.reportedProgress, undefined);
  own.owner = null;
  own.status = "deferred";
  const idle = buildBenchmarkCompactionFallback(source([own], { worker: "main" }));
  assert.equal(JSON.parse(idle).binding.challenge, null);
  assert.doesNotMatch(idle, /own-fact|synthetic task/);
});

test("redaction removes the benchmark token and tried flag hashes even inside copied observations and paths", () => {
  const previous = process.env.BENCHMARK_TOKEN;
  process.env.BENCHMARK_TOKEN = "synthetic-token-secret";
  try {
    const result = buildBenchmarkCompactionFallback(source([challenge("current", "main", {
      triedFlags: ["synthetic-flag-hash"],
      lastMeaningfulSignalContent: "synthetic-token-secret synthetic-flag-hash",
      lastEvidenceRef: "/synthetic/synthetic-token-secret",
      blackboard: [entry("credential", "synthetic-flag-hash", "/synthetic/synthetic-token-secret")]
    })], { workingDirectory: "/synthetic/synthetic-token-secret" }));
    assert.doesNotMatch(result, /synthetic-token-secret|synthetic-flag-hash|triedFlags/);
    assert.match(result, /REDACTED/);
    JSON.parse(result);
  } finally {
    if (previous === undefined) delete process.env.BENCHMARK_TOKEN;
    else process.env.BENCHMARK_TOKEN = previous;
  }
});

test("small budgets preserve complete binding JSON or fail clearly, never clipping references", () => {
  const input = source([challenge("current", "main", { description: "quotation \" and emoji 🧪 ".repeat(1000) })]);
  const minimum = buildBenchmarkCompactionFallback({ ...input, workingDirectory: "", ledgerFile: undefined, sessionFile: undefined }, 600);
  const smallest = buildBenchmarkCompactionFallback(input, minimum.length);
  assert.equal(smallest, minimum);
  assert.equal(JSON.parse(smallest).binding.challenge, "current");
  assert.throws(() => buildBenchmarkCompactionFallback(input, minimum.length - 1), RangeError);
  for (const size of [0, -1, 2.5, NaN, Infinity]) assert.throws(() => buildBenchmarkCompactionFallback(input, size), RangeError);
  for (const size of [600, 1000, 4000]) {
    const result = buildBenchmarkCompactionFallback(input, size);
    assert.ok(result.length <= size);
    const packet = JSON.parse(result);
    if (packet.recovery?.sessionFile) assert.equal(packet.recovery.sessionFile, input.sessionFile);
  }
});

test("fallback does not mutate its ledger or create/read recovery files", async () => {
  const directory = await mkdtemp(join(tmpdir(), "riftx-fallback-"));
  try {
    const current = challenge("current", "main", { blackboard: [entry("credential", "synthetic fact", "/synthetic/evidence")] });
    const freeze = (value: unknown): void => {
      if (!value || typeof value !== "object" || Object.isFrozen(value)) return;
      for (const item of Object.values(value)) freeze(item);
      Object.freeze(value);
    };
    freeze(current);
    const before = JSON.stringify(current);
    const input = source([current], { workingDirectory: join(directory, "missing-work"), ledgerFile: join(directory, "missing-ledger"), sessionFile: join(directory, "missing-session") });
    buildBenchmarkCompactionFallback(input);
    assert.equal(JSON.stringify(current), before);
    assert.deepEqual(await readdir(directory), []);
  } finally { await rm(directory, { recursive: true, force: true }); }
});
