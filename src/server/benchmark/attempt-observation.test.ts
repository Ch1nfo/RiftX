import assert from "node:assert/strict";
import test from "node:test";
import { randomUUID } from "node:crypto";
import { BenchmarkLedger } from "./ledger";
import { captureFence } from "./fencing";
import { createPentestCompactionExtension } from "@/server/pi/pentest-compaction";
import type { ModelRegistry } from "@mariozechner/pi-coding-agent";

test("real compaction cancellation hook records failure against the captured attempt", async (t) => {
  const id = `compaction-observation-${randomUUID()}`;
  t.after(() => BenchmarkLedger.destroy(id));
  const ledger = await new BenchmarkLedger(id).initialize();
  await ledger.syncFromPlatform([{ unique_code: "A", description: "synthetic", difficulty: "easy", level: 1, total_score: 100,
    flag_count: 1, correct_flag_count: 0, is_completed: false, container_status: "stopped", container_addr: [] }], true, "ip");
  const challenge = await ledger.acquire("A", "main", ["fixture"]);
  const extension = createPentestCompactionExtension({ getSession: () => undefined, modelRegistry: {} as ModelRegistry, getActiveSkills: () => [],
    onFailure: () => { const fence = captureFence(challenge); return (reason) => ledger.recordAttemptIncident(fence, "harness_bad_compaction", reason, "compaction"); },
    getFallbackContext: async () => "ledger continuity",
    onFallbackRecovered: async () => ledger.clearAttemptIncident(captureFence(challenge), "harness_bad_compaction")
  });
  let handler!: (event: { signal: AbortSignal }) => Promise<unknown>;
  await extension({ on: (_name: string, callback: typeof handler) => { handler = callback; } } as unknown as Parameters<typeof extension>[0]);
  const result = await handler({ signal: new AbortController().signal, preparation: { fileOps: { read: new Set(), written: new Set(), edited: new Set() }, firstKeptEntryId: "keep", tokensBefore: 10 } } as never) as { compaction?: unknown };
  assert.ok(result.compaction);
  await ledger.defer("A", "unfinished", undefined, "main");
  const ended = challenge.approachHistory.at(-1)!;
  assert.equal(ended.terminationSource, "deferred");
  assert.equal(ended.compactionCount, 0);
  assert.equal(ended.activeGate, null);
});
