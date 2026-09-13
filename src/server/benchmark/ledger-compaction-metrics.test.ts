import test, { type TestContext } from "node:test";
import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { readFile, writeFile } from "node:fs/promises";
import { homedir } from "node:os";
import { join } from "node:path";
import { BenchmarkLedger, type BenchmarkMetrics } from "./ledger";

async function createLedger(t: TestContext) {
  const sessionId = `compaction-metrics-test-${randomUUID()}`;
  t.after(() => BenchmarkLedger.destroy(sessionId));
  const ledger = await new BenchmarkLedger(sessionId).initialize();
  const metricsFile = join(homedir(), ".riftx", "benchmark", sessionId, "metrics.json");
  const readMetrics = async (): Promise<BenchmarkMetrics> => JSON.parse(await readFile(metricsFile, "utf8"));
  return { ledger, sessionId, metricsFile, readMetrics };
}

test("compaction metrics persist normal and fallback counts across ledger reloads", async (t) => {
  const { ledger, sessionId, readMetrics } = await createLedger(t);
  assert.equal(ledger.getMetrics().compactionCount, 0);
  assert.equal(ledger.getMetrics().fallbackCompactionCount, 0);

  await ledger.recordCompaction();
  await ledger.recordCompaction(false);
  assert.equal(ledger.getMetrics().compactionCount, 2);
  assert.equal(ledger.getMetrics().fallbackCompactionCount, 0);
  await Promise.all([ledger.recordCompaction(true), ledger.recordCompaction(true)]);
  assert.equal(ledger.getMetrics().compactionCount, 4);
  assert.equal(ledger.getMetrics().fallbackCompactionCount, 2);
  assert.deepEqual(await readMetrics(), ledger.getMetrics());

  const reopened = await new BenchmarkLedger(sessionId).initialize();
  assert.equal(reopened.getMetrics().compactionCount, 4);
  assert.equal(reopened.getMetrics().fallbackCompactionCount, 2);
  await reopened.recordCompaction();
  await reopened.recordCompaction(true);

  const persisted = await readMetrics();
  assert.equal(persisted.compactionCount, 6);
  assert.equal(persisted.fallbackCompactionCount, 3);
  const reloaded = await new BenchmarkLedger(sessionId).initialize();
  assert.deepEqual(reloaded.getMetrics(), persisted);
});

test("legacy metrics gain a zero fallback count and continue counting after migration", async (t) => {
  const { ledger, sessionId, metricsFile, readMetrics } = await createLedger(t);
  const legacyMetrics: Partial<BenchmarkMetrics> = {
    ...ledger.getMetrics(),
    compactionCount: 7,
    totalDefers: 3
  };
  delete legacyMetrics.fallbackCompactionCount;
  await writeFile(metricsFile, JSON.stringify(legacyMetrics), "utf8");

  const migrated = await new BenchmarkLedger(sessionId).initialize();
  assert.equal(migrated.getMetrics().compactionCount, 7);
  assert.equal(migrated.getMetrics().fallbackCompactionCount, 0);
  assert.equal(migrated.getMetrics().totalDefers, 3);
  assert.equal((await readMetrics()).fallbackCompactionCount, 0);

  await migrated.recordCompaction();
  await migrated.recordCompaction(true);
  await migrated.recordCompaction(true);
  const persisted = await readMetrics();
  assert.equal(persisted.compactionCount, 10);
  assert.equal(persisted.fallbackCompactionCount, 2);
  assert.equal(persisted.totalDefers, 3);

  const reloaded = await new BenchmarkLedger(sessionId).initialize();
  assert.deepEqual(reloaded.getMetrics(), persisted);
});
