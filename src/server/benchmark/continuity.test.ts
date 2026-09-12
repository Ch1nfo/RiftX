import test from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, rm } from "node:fs/promises";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { buildBenchmarkContinuity } from "./continuity";
import { BenchmarkLedger } from "./ledger";
import type { Challenge } from "./controller";

const realHome = process.env.HOME;
let tempDir: string;

test.before(async () => {
  tempDir = await mkdtemp(join(tmpdir(), "riftx-cont-"));
  process.env.HOME = tempDir;
});

test.after(async () => {
  process.env.HOME = realHome;
  await rm(tempDir, { recursive: true, force: true }).catch(() => undefined);
});

function platformChallenge(code: string, overrides: Partial<Challenge> = {}): Challenge {
  return {
    unique_code: code, description: `Challenge ${code}`, difficulty: "easy", level: 1,
    total_score: 100, flag_count: 1, correct_flag_count: 0, is_completed: false,
    container_status: "stopped", container_addr: [], ...overrides
  };
}

async function setup() {
  const sessionId = `cont-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
  const ledger = await new BenchmarkLedger(sessionId).initialize();
  await ledger.syncFromPlatform([
    platformChallenge("ch-1"), platformChallenge("ch-2"), platformChallenge("ch-3")
  ], true, "10.0.0.1");
  return { ledger };
}

test("empty ledger produces empty continuity", async () => {
  const sessionId = `empty-${Date.now()}`;
  const ledger = await new BenchmarkLedger(sessionId).initialize();
  assert.equal(buildBenchmarkContinuity(ledger), "");
});

test("includes run state, challenge queue, and my-challenge", async () => {
  const { ledger } = await setup();
  await ledger.acquire("ch-1", "main", ["10.0.0.1:80"]);
  await ledger.checkpoint("ch-1", "found login at /admin", ["web"], "LEGACY_NEXT_PROBE", "main", { currentApproach: "LEGACY_CURRENT_ROUTE", evidenceRef: "artifact:observation" });
  const text = buildBenchmarkContinuity(ledger);
  assert.match(text, /<riftx-benchmark-continuity>/);
  assert.match(text, /schedule=coverage/);
  assert.match(text, /solved=0\/3/);
  assert.match(text, /My challenge: ch-1/);
  assert.match(text, /addr: 10\.0\.0\.1:80/);
  assert.match(text, /found login at \/admin/);
  assert.doesNotMatch(text, /LEGACY_NEXT_PROBE|LEGACY_CURRENT_ROUTE|next_probe|current_approach/);
  assert.match(text, /artifact:observation/);
  assert.match(text, /SubAgent challenges \(0\/2\)/);
  assert.match(text, /Eligible candidates/);
  assert.match(text, /ch-2.*100pts/);
  assert.match(text, /<\/riftx-benchmark-continuity>/);
});

test("first-attempt exhaustion emits an enforced stop directive", async () => {
  let now = 1_000_000;
  const sessionId = `timeout-${Date.now()}`;
  const ledger = await new BenchmarkLedger(sessionId, () => now).initialize();
  await ledger.syncFromPlatform([platformChallenge("ch-1")], true, "ip");
  await ledger.acquire("ch-1", "main", ["a"]);
  now += 30 * 60 * 1000;
  const text = buildBenchmarkContinuity(ledger);
  assert.match(text, /FIRST_ATTEMPT_COMPLETE/);
  assert.match(text, /Solving tools are blocked/);
});

test("ordinary continuity does not expose a running countdown", async () => {
  const { ledger } = await setup();
  await ledger.acquire("ch-1", "main", ["a"]);
  await ledger.checkpoint("ch-1", "fresh signal", undefined, undefined, "main");
  const text = buildBenchmarkContinuity(ledger);
  assert.doesNotMatch(text, /elapsed|remaining|minute/i);
  assert.match(text, /fresh signal/);
});

test("final-stage recovery continuity preserves valid partial work", async () => {
  const ledger = await new BenchmarkLedger(`recovery-${Date.now()}`).initialize();
  await ledger.syncFromPlatform([platformChallenge("ch-1")], true, "10.0.0.1");
  await ledger.acquire("ch-1", "main", ["a"]);
  await ledger.checkpoint("ch-1", "sqlmap found no injectable parameters", ["SQLi"], "audit authorization", "main", {
    signalKind: "decisive_rule_out", currentApproach: "generic SQLi automation", ruledOutFamilies: ["SQLi"]
  });
  await ledger.defer("ch-1", "timebox", "audit authorization", "main");
  await ledger.confirmClosed("ch-1");
  await ledger.maybeAdvancePhase();
  await ledger.acquire("ch-1", "main", ["b"]);
  const text = buildBenchmarkContinuity(ledger);
  assert.match(text, /attempt 2/);
  assert.match(text, /#1 tried=SQLi/);
  assert.doesNotMatch(text, /audit authorization|generic SQLi automation/);
  assert.match(text, /Preserve valid partial solutions/);
  assert.match(text, /unsuccessful attempt alone does not rule out/);
  assert.match(text, /No runtime time limit/);
});

test("solved challenges are compact — no description or process detail", async () => {
  const { ledger } = await setup();
  await ledger.acquire("ch-1", "main", ["a"]);
  await ledger.markSolved("ch-1", 100, "main");
  const text = buildBenchmarkContinuity(ledger);
  // ch-1 appears as solved in run state but not in candidates or my challenge
  assert.match(text, /solved=1\/3/);
  assert.doesNotMatch(text, /My challenge: ch-1/);
  assert.doesNotMatch(text, /Eligible candidates[\s\S]*ch-1/);
});
