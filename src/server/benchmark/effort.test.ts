import assert from "node:assert/strict";
import test from "node:test";
import { randomUUID } from "node:crypto";
import { BenchmarkLedger } from "./ledger";
import { installBenchmarkRepeatNotice } from "./effort";
import { captureFence } from "./fencing";

async function fixture(t: test.TestContext) {
  let now = 1_000_000;
  const id = `observation-${randomUUID()}`;
  t.after(() => BenchmarkLedger.destroy(id));
  const ledger = await new BenchmarkLedger(id, () => now).initialize();
  await ledger.syncFromPlatform([{ unique_code: "fixture", description: "synthetic", difficulty: "easy", level: 1, total_score: 100,
    flag_count: 4, correct_flag_count: 0, is_completed: false, container_status: "stopped", container_addr: [] }], true, "ip");
  const challenge = await ledger.acquire("fixture", "main", ["fixture"]);
  return { ledger, challenge, advance: (ms: number) => { now += ms; } };
}

test("repeat warning does not block tools; evidence, credential, foothold and stage reset counters", async (t) => {
  const { ledger, challenge } = await fixture(t);
  let calls = 0;
  const tool = { name: "bash", execute: async (_id: string, _params: unknown): Promise<unknown> => { calls++; return { content: [{ type: "text", text: "unchanged" }] }; } };
  installBenchmarkRepeatNotice(tool, ledger, "main");
  for (const kind of ["note", "credential", "foothold", "stage_transition"] as const) {
    for (let i = 0; i < 3; i++) {
      const result = await tool.execute(String(i), { command: "synthetic", timeout: 100 + i });
      assert.equal(JSON.stringify(result).includes("REPEATED_WITHOUT_NEW_INFORMATION"), i === 2);
    }
    await ledger.checkpoint("fixture", `new ${kind}`, [], undefined, "main", { signalKind: kind, evidenceRef: `synthetic:${kind}` });
    assert.equal(challenge.resources?.repeatCount, 0);
    assert.equal(challenge.resources?.callsWithoutProgress, 0);
  }
  assert.equal(calls, 12);
  assert.equal(challenge.resources?.progressEvents, 4);
  await ledger.recordSubmission("fixture", "synthetic flag", true, 25, 1, 0, "main");
  assert.equal(challenge.resources?.callsWithoutProgress, 0);
  assert.equal(challenge.resources?.repeatCount, 0);
});

test("no-progress warning covers bash, browser and crawl without interrupting any call", async (t) => {
  const { ledger, advance } = await fixture(t);
  advance(6 * 60_000);
  let calls = 0;
  for (const name of ["bash", "browser", "crawl"]) {
    const tool = { name, execute: async (_id: string, _params: unknown): Promise<unknown> => ({ content: [{ type: "text", text: `value-${++calls}` }] }) };
    installBenchmarkRepeatNotice(tool, ledger, "main");
    let warned = false;
    for (let i = 0; i < 10; i++) {
      const result = await tool.execute(String(i), { i });
      warned = JSON.stringify(result).includes("NO_DURABLE_PROGRESS") || warned;
    }
    assert.equal(warned, true);
    await tool.execute("after", {});
  }
  assert.equal(calls, 33);
  assert.equal(ledger.getChallenge("fixture")?.status, "running");
});

test("password enumeration retains requested timeout and has no independent hard gate", async (t) => {
  const { ledger, challenge } = await fixture(t);
  challenge.passwordEnumerationMs = 1_000_000;
  let calls = 0;
  const tool = { name: "bash", execute: async (_id: string, params: unknown): Promise<unknown> => {
    assert.equal((params as { timeout: number }).timeout, 600); calls++; return { content: [] };
  } };
  installBenchmarkRepeatNotice(tool, ledger, "main");
  for (let i = 0; i < 4; i++) assert.doesNotMatch(JSON.stringify(await tool.execute(String(i), { command: "hydra synthetic", timeout: 600 })), /passwordEnumerationBlocked/);
  assert.equal(calls, 4);
});

test("thrown errors preserve failure and increment attempt metrics, warnings remain soft", async (t) => {
  const { ledger, challenge } = await fixture(t);
  const tool = { name: "crawl", execute: async (_id: string, _params: unknown): Promise<unknown> => { throw new Error("synthetic tool failure"); } };
  installBenchmarkRepeatNotice(tool, ledger, "main");
  await assert.rejects(tool.execute("1", {}), /synthetic tool failure/);
  await assert.rejects(tool.execute("2", {}), /synthetic tool failure/);
  await assert.rejects(tool.execute("3", {}), /REPEATED_WITHOUT_NEW_INFORMATION/);
  await ledger.recordCompaction(captureFence(challenge));
  await ledger.defer("fixture", "unfinished", undefined, "main");
  const ended = challenge.approachHistory.at(-1)!;
  assert.equal(ended.terminationSource, "tool_failure");
  assert.equal(ended.toolErrorCount, 3);
  assert.equal(ended.compactionCount, 1);
  assert.equal(ledger.getMetrics().resources.toolCalls, 3);
});
