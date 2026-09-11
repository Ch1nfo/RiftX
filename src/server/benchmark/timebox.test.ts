import assert from "node:assert/strict";
import test from "node:test";
import { mkdtemp, rm } from "node:fs/promises";
import { join } from "node:path";
import { tmpdir } from "node:os";
import type { Challenge } from "./controller";
import { BenchmarkLedger, FIRST_ATTEMPT_LIMIT_MS } from "./ledger";
import { installBenchmarkTimeboxGate } from "./timebox";

const realHome = process.env.HOME;
let tempDir: string;

test.before(async () => {
  tempDir = await mkdtemp(join(tmpdir(), "riftx-timebox-"));
  process.env.HOME = tempDir;
});

test.after(async () => {
  process.env.HOME = realHome;
  await rm(tempDir, { recursive: true, force: true }).catch(() => undefined);
});

function challenge(): Challenge {
  return { unique_code: "ch-1", description: "test", difficulty: "easy", level: 1, total_score: 100, flag_count: 1, correct_flag_count: 0, is_completed: false, container_status: "stopped", container_addr: [] };
}

test("expired attempts block solving tools but never block benchmark control", async () => {
  let now = 1_000_000;
  const ledger = await new BenchmarkLedger(`gate-${Date.now()}`, () => now).initialize();
  await ledger.syncFromPlatform([challenge()], true, "ip");
  await ledger.acquire("ch-1", "main", ["a"]);
  let executions = 0;
  const browser = { name: "browser", execute: async (_id: string, _params: unknown) => { executions += 1; return { content: [] }; } };
  const control = { name: "benchmark_control", execute: async (_id: string, _params: unknown) => { executions += 1; return { content: [] }; } };
  installBenchmarkTimeboxGate(browser, ledger, "main");
  installBenchmarkTimeboxGate(control, ledger, "main");

  await browser.execute("before", {});
  now += 30 * 60 * 1000;
  const blocked = await browser.execute("after", {}) as { details?: { timeboxExpired?: boolean } };
  await control.execute("control", {});

  assert.equal(blocked.details?.timeboxExpired, true);
  assert.equal(executions, 2, "the expired browser call must not reach its implementation, while control remains callable");
});

test("attempt 2 remains unblocked regardless of elapsed time", async () => {
  let now = 2_000_000;
  const ledger = await new BenchmarkLedger(`gate-revisit-${Date.now()}`, () => now).initialize();
  await ledger.syncFromPlatform([challenge()], true, "ip");
  await ledger.acquire("ch-1", "main", ["a"]);
  await ledger.defer("ch-1", "covered", "different approach", "main");
  await ledger.confirmClosed("ch-1");
  await ledger.maybeAdvancePhase();
  await ledger.acquire("ch-1", "main", ["b"]);
  let executions = 0;
  const browser = { name: "browser", execute: async () => { executions += 1; return { content: [] }; } };
  installBenchmarkTimeboxGate(browser, ledger, "main");
  now += 24 * 60 * 60_000;
  await browser.execute();
  assert.equal(executions, 1);
});

test("a child cannot keep solving after it released its assigned challenge", async () => {
  const ledger = await new BenchmarkLedger(`gate-child-${Date.now()}`).initialize();
  await ledger.syncFromPlatform([challenge()], true, "ip");
  await ledger.acquire("ch-1", "subagent:t1", ["a"]);
  let executions = 0;
  const bash = { name: "bash", execute: async () => { executions += 1; return { content: [] }; } };
  installBenchmarkTimeboxGate(bash, ledger, "subagent:t1", "ch-1");
  await ledger.defer("ch-1", "timebox", "fresh approach", "subagent:t1");

  const blocked = await bash.execute() as { details?: { challengeReleased?: boolean } };
  assert.equal(blocked.details?.challengeReleased, true);
  assert.equal(executions, 0);
});


test("bash is interrupted at the remaining first-attempt deadline", async () => {
  let now = 1_000_000;
  const ledger = await new BenchmarkLedger(`deadline-${Date.now()}`, () => now).initialize();
  await ledger.syncFromPlatform([challenge()], true, "ip");
  await ledger.acquire("ch-1", "main", ["a"]);
  now += FIRST_ATTEMPT_LIMIT_MS - 40;
  let aborted = false;
  const bash = { name: "bash", execute: async (_id: string, params: unknown, signal?: AbortSignal): Promise<unknown> => {
    assert.equal((params as { timeout: number }).timeout, 0.04);
    return new Promise((_resolve, reject) => signal!.addEventListener("abort", () => { aborted = true; reject(signal!.reason); }, { once: true }));
  } };
  installBenchmarkTimeboxGate(bash, ledger, "main");
  const result = await bash.execute("fixture", { timeout: 1800 });
  assert.equal(aborted, true);
  assert.match(JSON.stringify(result), /FIRST_ATTEMPT_COMPLETE/);
});

test("deadline wrapper preserves explicit cancellation", async () => {
  const ledger = await new BenchmarkLedger(`cancel-${Date.now()}`).initialize();
  await ledger.syncFromPlatform([challenge()], true, "ip");
  await ledger.acquire("ch-1", "main", ["a"]);
  const cancel = new AbortController();
  cancel.abort(new Error("fixture cancellation"));
  const bash = { name: "bash", execute: async (_id: string, _params: unknown, signal?: AbortSignal): Promise<unknown> => { signal!.throwIfAborted(); return undefined; } };
  installBenchmarkTimeboxGate(bash, ledger, "main");
  await assert.rejects(bash.execute("fixture", {}, cancel.signal), /fixture cancellation/);
});
