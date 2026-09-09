import test from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, rm } from "node:fs/promises";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { buildBenchmarkContinuity } from "./continuity";
import { BenchmarkLedger, type ChallengeState } from "./ledger";
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
  await ledger.checkpoint("ch-1", "found login at /admin", ["web"], "try sqli on search", "main");
  const text = buildBenchmarkContinuity(ledger);
  assert.match(text, /<riftx-benchmark-continuity>/);
  assert.match(text, /phase=first_pass/);
  assert.match(text, /solved=0\/3/);
  assert.match(text, /My challenge: ch-1/);
  assert.match(text, /addr: 10\.0\.0\.1:80/);
  assert.match(text, /found login at \/admin/);
  assert.match(text, /tried: web/);
  assert.match(text, /next_probe: try sqli on search/);
  assert.match(text, /SubAgent challenges \(0\/2\)/);
  assert.match(text, /Next candidates:/);
  assert.match(text, /ch-2.*easy/);
  assert.match(text, /<\/riftx-benchmark-continuity>/);
});

test("8-minute budget exhaustion emits defer directive", async () => {
  const { ledger } = await setup();
  await ledger.acquire("ch-1", "main", ["a"]);
  const challenge = ledger.getChallenge("ch-1") as ChallengeState;
  challenge.lastSignalAt = Date.now() - 9 * 60 * 1000;
  const text = buildBenchmarkContinuity(ledger);
  assert.match(text, /BUDGET EXHAUSTED/);
  assert.match(text, /defer.*ch-1/);
});

test("fresh signal shows elapsed time without warning", async () => {
  const { ledger } = await setup();
  await ledger.acquire("ch-1", "main", ["a"]);
  await ledger.checkpoint("ch-1", "fresh signal", undefined, undefined, "main");
  const text = buildBenchmarkContinuity(ledger);
  assert.doesNotMatch(text, /BUDGET EXHAUSTED/);
  assert.match(text, /\(0m ago\)/);
});

test("solved challenges are compact — no description or process detail", async () => {
  const { ledger } = await setup();
  await ledger.acquire("ch-1", "main", ["a"]);
  await ledger.markSolved("ch-1", 100, "main");
  const text = buildBenchmarkContinuity(ledger);
  // ch-1 appears as solved in run state but not in candidates or my challenge
  assert.match(text, /solved=1\/3/);
  assert.doesNotMatch(text, /My challenge: ch-1/);
  assert.doesNotMatch(text, /Next candidates:[\s\S]*ch-1/);
});
