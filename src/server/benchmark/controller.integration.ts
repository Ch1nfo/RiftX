import test from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, rm, readFile } from "node:fs/promises";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { BenchmarkLedger } from "./ledger";
import { MockBenchmarkApi } from "../../../tests/e2e/mock-benchmark-api";

const realHome = process.env.HOME;
const realVpnUrl = process.env.BENCHMARK_VPN_URL;
let tempDir: string;

test.before(async () => {
  tempDir = await mkdtemp(join(tmpdir(), "riftx-bench-e2e-"));
  process.env.HOME = tempDir;
});

test.after(async () => {
  process.env.HOME = realHome;
  if (realVpnUrl !== undefined) process.env.BENCHMARK_VPN_URL = realVpnUrl;
  else delete process.env.BENCHMARK_VPN_URL;
  await rm(tempDir, { recursive: true, force: true }).catch(() => undefined);
});


async function makeController(mock: MockBenchmarkApi) {
  const port = mock.port;
  // Redirect the VPN check to the mock's root (which returns status: "ok").
  process.env.BENCHMARK_VPN_URL = `http://127.0.0.1:${port}`;
  const { BenchmarkController } = await import("./controller");
  return new BenchmarkController({ baseUrl: `http://127.0.0.1:${port}`, token: "e2e-token" });
}

test("bulk lifecycle: sequential acquire → submit → close with zero leaks", async () => {
  const mock = new MockBenchmarkApi();
  mock.seedChallenges(10);
  await mock.start();
  const controller = await makeController(mock);
  const sessionId = `e2e-lifecycle-${Date.now()}`;
  const ledger = await new BenchmarkLedger(sessionId).initialize();

  const vpn = await controller.checkVpn();
  const challenges = await controller.listChallenges();
  await ledger.syncFromPlatform(challenges, true, vpn.client_ip);
  assert.equal(ledger.getState().totalChallenges, 10);

  const flagsMap = (mock as unknown as { challenges: Map<string, { flags: string[] }> }).challenges;
  for (const challenge of challenges.slice(0, 3)) {
    const start = await controller.startChallenge(challenge.unique_code);
    await ledger.acquire(challenge.unique_code, "main", start.container_addr);
    const flags = flagsMap.get(challenge.unique_code)?.flags ?? [];
    for (const flag of flags) {
      const submit = await controller.submitFlag(challenge.unique_code, flag);
      await ledger.recordSubmission(challenge.unique_code, flag, submit.correct, submit.cumulative_score, submit.correct_flag_count, submit.matched_flag_index, "main");
    }
    await ledger.markSolved(challenge.unique_code, undefined, "main");
    await controller.closeChallenge(challenge.unique_code);
    await ledger.confirmClosed(challenge.unique_code);
  }

  const state = ledger.getState();
  assert.equal(state.solvedCount, 3);
  assert.equal(mock.getActiveContainers(), 0, "no leaked containers");
  const stateFile = join(tempDir, ".riftx", "benchmark", sessionId, "state.json");
  const persisted = JSON.parse(await readFile(stateFile, "utf8")) as { solvedCount: number };
  assert.equal(persisted.solvedCount, 3);
  mock.close();
});

test("container limit: 3 concurrent max, 4th rejected (distinct owners)", async () => {
  const mock = new MockBenchmarkApi();
  mock.seedChallenges(10);
  await mock.start();
  const controller = await makeController(mock);
  const ledger = await new BenchmarkLedger(`e2e-limit-${Date.now()}`).initialize();
  const challenges = await controller.listChallenges();
  await ledger.syncFromPlatform(challenges, true, "ip");

  await controller.startChallenge("ch-001");
  await ledger.acquire("ch-001", "main", ["a"]);
  await controller.startChallenge("ch-002");
  await ledger.acquire("ch-002", "subagent:t1", ["b"]);
  await controller.startChallenge("ch-003");
  await ledger.acquire("ch-003", "subagent:t2", ["c"]);
  assert.equal(mock.getActiveContainers(), 3);

  await assert.rejects(() => ledger.reserve("ch-004", "subagent:t3"), /Container limit/);
  try {
    await controller.startChallenge("ch-004");
    assert.fail("should have thrown");
  } catch (error) {
    assert.equal((error as { kind?: string }).kind, "invalid_state_max_active");
  }

  await controller.closeChallenge("ch-001");
  const start4 = await controller.startChallenge("ch-004");
  assert.ok(start4.container_addr.length > 0);
  mock.close();
});

test("duplicate flag submission is idempotent", async () => {
  const mock = new MockBenchmarkApi();
  mock.seedChallenges(1);
  await mock.start();
  const controller = await makeController(mock);
  const flags = (mock as unknown as { challenges: Map<string, { flags: string[] }> }).challenges.get("ch-001")?.flags ?? [];
  await controller.startChallenge("ch-001");
  const first = await controller.submitFlag("ch-001", flags[0]);
  assert.equal(first.correct, true);
  try {
    await controller.submitFlag("ch-001", flags[0]);
    assert.fail("should have thrown duplicate");
  } catch (error) {
    assert.equal((error as { kind?: string }).kind, "duplicate_submit");
  }
  mock.close();
});

test("restart recovery: running → orphaned, orphaned re-acquirable, signal state preserved", async () => {
  const mock = new MockBenchmarkApi();
  mock.seedChallenges(5);
  await mock.start();
  const controller = await makeController(mock);
  const sessionId = `e2e-restart-${Date.now()}`;
  const ledger = await new BenchmarkLedger(sessionId).initialize();
  const challenges = await controller.listChallenges();
  await ledger.syncFromPlatform(challenges, true, "ip");
  await controller.startChallenge("ch-001");
  await ledger.acquire("ch-001", "main", ["10.0.0.1:80"]);
  await ledger.checkpoint("ch-001", "found login", ["web"], "try sqli", "main");

  const restored = await new BenchmarkLedger(sessionId).initialize();
  const restoredChallenge = restored.getChallenge("ch-001");
  assert.ok(restoredChallenge, "challenge should exist");
  assert.equal(restoredChallenge.status, "orphaned", "running becomes orphaned on restart");
  assert.equal(restoredChallenge.lastSignalContent, "found login");
  assert.equal(restoredChallenge.nextProbe, "try sqli");
  await restored.acquire("ch-001", "main", ["10.0.0.1:80"]);
  assert.equal(restored.getChallenge("ch-001")?.status, "running");
  mock.close();
});

test("token never appears in ledger state or metrics files", async () => {
  const mock = new MockBenchmarkApi();
  mock.seedChallenges(2);
  await mock.start();
  const controller = await makeController(mock);
  const sessionId = `e2e-token-${Date.now()}`;
  const ledger = await new BenchmarkLedger(sessionId).initialize();
  const challenges = await controller.listChallenges();
  await ledger.syncFromPlatform(challenges, true, "ip");
  await controller.startChallenge("ch-001");
  await ledger.acquire("ch-001", "main", ["a"]);

  const stateContent = await readFile(join(tempDir, ".riftx", "benchmark", sessionId, "state.json"), "utf8");
  const metricsContent = await readFile(join(tempDir, ".riftx", "benchmark", sessionId, "metrics.json"), "utf8");
  assert.ok(!stateContent.includes("e2e-token"), "token must not appear in state.json");
  assert.ok(!metricsContent.includes("e2e-token"), "token must not appear in metrics.json");
  mock.close();
});

test("subagent crash: challenge released to closing then confirmed, container closed", async () => {
  const mock = new MockBenchmarkApi();
  mock.seedChallenges(5);
  await mock.start();
  const controller = await makeController(mock);
  const ledger = await new BenchmarkLedger(`e2e-crash-${Date.now()}`).initialize();
  const challenges = await controller.listChallenges();
  await ledger.syncFromPlatform(challenges, true, "ip");
  await controller.startChallenge("ch-001");
  await ledger.acquire("ch-001", "subagent:t1", ["a"]);

  await ledger.releaseOnSubagentExit("ch-001", "subagent crashed", "subagent:t1");
  assert.equal(ledger.getChallenge("ch-001")?.status, "closing");
  await controller.closeChallenge("ch-001");
  await ledger.confirmClosed("ch-001");
  assert.equal(ledger.getChallenge("ch-001")?.status, "deferred");
  assert.equal(ledger.getChallenge("ch-001")?.owner, null);
  assert.equal(mock.getActiveContainers(), 0);
  mock.close();
});

test("100-challenge coverage survives partial progress, three compactions, and Runtime restart", async () => {
  const mock = new MockBenchmarkApi();
  mock.seedChallenges(100);
  await mock.start();
  const controller = await makeController(mock);
  const sessionId = `e2e-long-run-${Date.now()}`;
  const ledger = await new BenchmarkLedger(sessionId).initialize();
  const challenges = await controller.listChallenges();
  await ledger.syncFromPlatform(challenges, true, "ip");
  const flagsMap = (mock as unknown as { challenges: Map<string, { flags: string[] }> }).challenges;

  for (let index = 0; index < challenges.length; index += 1) {
    const challenge = challenges[index];
    const start = await controller.startChallenge(challenge.unique_code);
    await ledger.acquire(challenge.unique_code, "main", start.container_addr);
    if (index === 1) {
      const flag = flagsMap.get(challenge.unique_code)?.flags[0];
      assert.ok(flag, "the multi-flag fixture must expose its first flag");
      const submit = await controller.submitFlag(challenge.unique_code, flag);
      await ledger.recordSubmission(challenge.unique_code, flag, submit.correct, submit.cumulative_score, submit.correct_flag_count, submit.matched_flag_index, "main");
      assert.equal(ledger.getChallenge(challenge.unique_code)?.correctFlagCount, 1);
      assert.equal(ledger.getChallenge(challenge.unique_code)?.isCompleted, false);
    }
    await ledger.defer(challenge.unique_code, "first-pass coverage", "use a distinct recovery approach", "main");
    await controller.closeChallenge(challenge.unique_code);
    await ledger.confirmClosed(challenge.unique_code);
    if (index === 24 || index === 49 || index === 74) await ledger.recordCompaction();
  }
  await ledger.maybeAdvancePhase();
  assert.equal(ledger.getState().phase, "second_pass");
  assert.equal(mock.getActiveContainers(), 0);

  const restored = await new BenchmarkLedger(sessionId).initialize();
  assert.equal(restored.getState().totalChallenges, 100);
  assert.equal(restored.getState().phase, "second_pass");
  assert.equal(restored.getChallenge("ch-002")?.correctFlagCount, 1);
  assert.equal(restored.getChallenge("ch-002")?.status, "deferred");
  assert.equal(restored.getMetrics().compactionCount, 3);
  assert.equal(restored.getState().activeContainers, 0);
  mock.close();
});
