import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { readFile } from "node:fs/promises";
import { homedir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { writeJsonStoreAtomic } from "../json-store";
import { startBenchmarkAttemptWatchdog } from "./attempt-watchdog";
import { BenchmarkLedger, type BenchmarkState } from "./ledger";

function deferred() {
  let resolve!: () => void;
  const promise = new Promise<void>((done) => { resolve = done; });
  return { promise, resolve };
}

test("watchdog preserves durable memory through a blocked and failed expiry write before retrying close", async () => {
  const id = `watchdog-persistent-memory-test-${randomUUID()}`;
  const statePath = join(homedir(), ".riftx", "benchmark", id, "state.json");
  const entered = deferred();
  const release = deferred();
  let now = 1000;
  let writeFault = true;
  let pendingCheck: Promise<void> | undefined;
  let watchdog: ReturnType<typeof startBenchmarkAttemptWatchdog> | undefined;
  try {
    const ledger = await new BenchmarkLedger(id, () => now).initialize();
    await ledger.syncFromPlatform([{
      unique_code: "fixture", description: "Synthetic persistent memory fixture", difficulty: "easy", level: 1,
      total_score: 100, flag_count: 4, correct_flag_count: 0, is_completed: false,
      container_status: "stopped", container_addr: []
    }], true, "fixture");
    await ledger.acquire("fixture", "main", ["fixture"]);
    await ledger.checkpoint("fixture", "Synthetic valuable observation without a flag", ["route-a"], "Synthetic unresolved next probe", "main");
    const durableBefore = await readFile(statePath, "utf8");
    const originalBoard = structuredClone(ledger.getChallenge("fixture")!.blackboard);
    const events: string[] = [];
    const statesAtClose: BenchmarkState[] = [];
    Object.defineProperty(ledger, "writeStore", { value: async (path: string, data: unknown, mode?: number) => {
      if (path === statePath && writeFault) {
        entered.resolve();
        await release.promise;
        events.push("state_write_failed");
        throw new Error("SYNTHETIC_EXPIRY_STATE_WRITE_FAILURE");
      }
      await writeJsonStoreAtomic(path, data, mode);
      if (path === statePath) events.push(`state_commit:${(data as BenchmarkState).challenges.fixture.status}`);
    }});
    watchdog = startBenchmarkAttemptWatchdog({
      ledger, owner: "main", pollMs: 2_147_483_647,
      controller: { closeChallenge: async (code) => {
        statesAtClose.push(JSON.parse(await readFile(statePath, "utf8")) as BenchmarkState);
        events.push("platform_close");
        return { unique_code: code, closed: true };
      }},
      stopWorker: async () => { events.push("stop_worker"); },
      isStopping: () => false,
      warn: async () => undefined
    });
    now += 30 * 60_000;
    pendingCheck = watchdog.check();
    await entered.promise;
    const coalescedCheck = watchdog.check();
    assert.equal(statesAtClose.length, 0);
    assert.equal(ledger.getChallenge("fixture")?.status, "running");
    assert.deepEqual(ledger.getChallenge("fixture")?.blackboard, originalBoard);
    assert.equal(await readFile(statePath, "utf8"), durableBefore);

    // Reconstruct restart state from the real file while leaving its recovery
    // resave disabled, so the live watchdog remains the only file writer.
    const recoveredWhileBlocked = new BenchmarkLedger(id, () => now);
    Object.defineProperty(recoveredWhileBlocked, "writeStore", { value: async () => undefined });
    await recoveredWhileBlocked.initialize();
    assert.equal(recoveredWhileBlocked.getChallenge("fixture")?.status, "orphaned");
    assert.equal(recoveredWhileBlocked.getChallenge("fixture")?.currentAttemptStartedAt, 1000);
    assert.deepEqual(recoveredWhileBlocked.getChallenge("fixture")?.blackboard, originalBoard);

    release.resolve();
    await Promise.all([pendingCheck, coalescedCheck]);
    assert.equal(statesAtClose.length, 0);
    assert.equal(ledger.getChallenge("fixture")?.owner, "main");
    assert.equal(ledger.getChallenge("fixture")?.status, "running");
    assert.deepEqual(ledger.getChallenge("fixture")?.blackboard, originalBoard);
    assert.equal(await readFile(statePath, "utf8"), durableBefore);

    writeFault = false;
    await watchdog.check();
    assert.equal(statesAtClose.length, 1);
    const committedBeforeClose = statesAtClose[0].challenges.fixture;
    assert.equal(committedBeforeClose.status, "closing");
    assert.equal(committedBeforeClose.owner, null);
    assert.deepEqual(committedBeforeClose.blackboard.slice(0, -1), originalBoard);
    assert.equal(committedBeforeClose.blackboard.at(-1)?.kind, "attempt_end");
    assert.equal(committedBeforeClose.blackboard.at(-1)?.nextProbe, "Synthetic unresolved next probe");
    assert.equal(committedBeforeClose.approachHistory.length, 1);
    assert.equal(committedBeforeClose.approachHistory[0].flagsAfter, 0);
    assert.deepEqual(events, ["stop_worker", "state_write_failed", "stop_worker", "state_commit:closing", "platform_close", "state_commit:deferred"]);
    assert.equal(ledger.getChallenge("fixture")?.status, "deferred");

    const recoveredAfterClose = await new BenchmarkLedger(id, () => now).initialize();
    assert.equal(recoveredAfterClose.getChallenge("fixture")?.status, "deferred");
    assert.equal(recoveredAfterClose.getChallenge("fixture")?.blackboard.length, originalBoard.length + 1);
    assert.deepEqual(recoveredAfterClose.getChallenge("fixture")?.blackboard.slice(0, -1), originalBoard);
    assert.equal(recoveredAfterClose.getMetrics().totalDefers, 1);
  } finally {
    watchdog?.dispose();
    release.resolve();
    await pendingCheck;
    await BenchmarkLedger.destroy(id);
  }
});
