import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { mkdir, readFile, readdir, rename, rm, writeFile } from "node:fs/promises";
import { homedir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { BenchmarkLedger, type BenchmarkState, type ChallengeOwner } from "./ledger";
import { evidenceBackedRuleOuts, selectBlackboard } from "./blackboard";

async function fixture(owner: Exclude<ChallengeOwner, null> = "main") {
  let now = 1000;
  const id = `ledger-persistence-test-${randomUUID()}`;
  const ledger = await new BenchmarkLedger(id, () => now).initialize();
  await ledger.syncFromPlatform([{
    unique_code: "fixture", description: "Synthetic persistence fixture", difficulty: "easy", level: 1,
    total_score: 100, flag_count: 4, correct_flag_count: 0, is_completed: false,
    container_status: "stopped", container_addr: []
  }], true, "fixture");
  await ledger.acquire("fixture", owner, ["fixture"]);
  return { ledger, id, directory: join(homedir(), ".riftx", "benchmark", id), clock: () => now,
    at: (minutes: number) => { now = 1000 + minutes * 60_000; } };
}

test("failed state writes roll back checkpoint and lifecycle mutations, leaving retries possible", async (t) => {
  for (const operation of ["checkpoint", "defer", "expire", "child-exit"] as const) {
    await t.test(operation, async () => {
      const owner = operation === "child-exit" ? "subagent:fixture" : "main";
      const f = await fixture(owner);
      try {
        f.at(30);
        const stateBefore = structuredClone(f.ledger.getState());
        const metricsBefore = structuredClone(f.ledger.getMetrics());
        const persistedBefore = await readFile(join(f.directory, "state.json"), "utf8");
        const realPersist = (f.ledger as unknown as { persist(): Promise<void> }).persist.bind(f.ledger);
        Object.defineProperty(f.ledger, "persist", {
          configurable: true,
          value: async () => { throw new Error("SYNTHETIC_STATE_WRITE_FAILURE"); }
        });
        const mutate = () => {
          switch (operation) {
            case "checkpoint": return f.ledger.checkpoint("fixture", "Synthetic valuable observation", ["route"], "Synthetic next probe", "main");
            case "defer": return f.ledger.defer("fixture", "Synthetic stop", "Synthetic next probe", "main");
            case "expire": return f.ledger.expireAttempt("fixture", "main", 1000);
            case "child-exit": return f.ledger.releaseOnSubagentExit("fixture", "Synthetic child stop", "subagent:fixture");
          }
        };
        await assert.rejects(mutate(), /SYNTHETIC_STATE_WRITE_FAILURE/);
        assert.deepEqual(f.ledger.getState(), stateBefore);
        assert.deepEqual(f.ledger.getMetrics(), metricsBefore);
        assert.equal(await readFile(join(f.directory, "state.json"), "utf8"), persistedBefore);
        assert.equal(f.ledger.getChallenge("fixture")?.owner, owner);
        assert.equal(f.ledger.getChallenge("fixture")?.status, "running");
        Object.defineProperty(f.ledger, "persist", { configurable: true, value: realPersist });
        await mutate();
        const restored = await new BenchmarkLedger(f.id, f.clock).initialize();
        assert.equal(restored.getChallenge("fixture")?.blackboard.at(-1)?.kind, operation === "checkpoint" ? "note" : "attempt_end");
        assert.equal(restored.getChallenge("fixture")?.approachHistory.length, operation === "checkpoint" ? 0 : 1);
      } finally {
        await BenchmarkLedger.destroy(f.id);
      }
    });
  }
});

test("a real atomic state replacement error keeps the old durable attempt recoverable", async () => {
  const f = await fixture();
  const path = join(f.directory, "state.json");
  const saved = join(f.directory, "state-before-test.json");
  try {
    const before = structuredClone(f.ledger.getState());
    await rename(path, saved);
    await mkdir(path);
    await assert.rejects(f.ledger.checkpoint("fixture", "Synthetic uncommitted observation", undefined, undefined, "main"), /EISDIR|ENOTDIR|directory/i);
    assert.deepEqual(f.ledger.getState(), before);
    await rm(path, { recursive: true });
    await rename(saved, path);
    const restored = await new BenchmarkLedger(f.id, f.clock).initialize();
    assert.equal(restored.getChallenge("fixture")?.blackboard.length, 0);
    assert.equal(restored.getChallenge("fixture")?.currentAttemptStartedAt, 1000);
  } finally {
    await BenchmarkLedger.destroy(f.id);
  }
});

test("public readers see committed state while writes are pending and memorySnapshot waits for rollback", async () => {
  for (const failWrite of [false, true]) {
    const f = await fixture();
    try {
      f.at(30);
      const previousView = f.ledger.getChallenge("fixture")!;
      let entered!: () => void;
      let release!: () => void;
      const writing = new Promise<void>((resolve) => { entered = resolve; });
      const resume = new Promise<void>((resolve) => { release = resolve; });
      const realPersist = (f.ledger as unknown as { persist(): Promise<void> }).persist.bind(f.ledger);
      Object.defineProperty(f.ledger, "persist", { configurable: true, value: async () => {
        entered();
        await resume;
        if (failWrite) throw new Error("SYNTHETIC_STATE_WRITE_FAILURE");
        await realPersist();
      }});
      const expiry = f.ledger.expireAttempt("fixture", "main", 1000);
      const settledExpiry = expiry.then(() => "committed", () => "rejected");
      await writing;
      assert.equal(f.ledger.getState().challenges.fixture.status, "running");
      assert.equal(f.ledger.getChallenge("fixture")?.currentAttemptStartedAt, 1000);
      assert.equal(f.ledger.getMetrics().totalDefers, 0);
      assert.equal(f.ledger.budgetForOwner("main")?.challenge.status, "running");
      assert.equal(previousView.status, "running");
      let snapshotReturned = false;
      const snapshot = f.ledger.memorySnapshot("fixture").then((memory) => { snapshotReturned = true; return memory; });
      await Promise.resolve();
      assert.equal(snapshotReturned, false);
      release();
      assert.equal(await settledExpiry, failWrite ? "rejected" : "committed");
      const memory = await snapshot;
      assert.equal(memory[0].status, failWrite ? "running" : "closing");
      assert.equal(f.ledger.getChallenge("fixture")?.status, memory[0].status);
      assert.equal(previousView.status, "running");
      memory[0].description = "Changed detached test snapshot";
      assert.notEqual(f.ledger.getChallenge("fixture")?.description, memory[0].description);
      assert.equal((await f.ledger.memorySnapshot()).length, 1);
      await assert.rejects(f.ledger.memorySnapshot("missing"), /Unknown benchmark challenge/);
    } finally {
      await BenchmarkLedger.destroy(f.id);
    }
  }
});

test("a successful early return cannot publish or carry an uncommitted draft", async () => {
  const f = await fixture();
  try {
    const internal = f.ledger as unknown as { state: BenchmarkState; serialize<T>(operation: () => Promise<T>): Promise<T> };
    await internal.serialize(async () => { internal.state.challenges.fixture.status = "closing"; });
    assert.equal(f.ledger.getChallenge("fixture")?.status, "running");
    await f.ledger.recordVpnCheck(true, "fixture");
    const restored = await new BenchmarkLedger(f.id, f.clock).initialize();
    assert.equal(restored.getChallenge("fixture")?.status, "orphaned");
    assert.equal(restored.getChallenge("fixture")?.currentAttemptStartedAt, 1000);
  } finally {
    await BenchmarkLedger.destroy(f.id);
  }
});

test("evidence pointers reject overlong inputs and retain internal spaces across restart", async () => {
  const f = await fixture();
  try {
    for (const key of ["evidenceRef", "supersedesEvidenceRef"] as const) {
      const before = structuredClone(f.ledger.getState());
      await assert.rejects(f.ledger.checkpoint("fixture", "Synthetic observation", undefined, undefined, "main", {
        [key]: "a".repeat(501)
      }), /Evidence references must be at most 500/);
      assert.deepEqual(f.ledger.getState(), before);
    }
    const evidenceRef = "/synthetic  artifact/folder with  spaces/result.json";
    await f.ledger.checkpoint("fixture", "Synthetic linked observation", undefined, undefined, "main", { evidenceRef });
    const restored = await new BenchmarkLedger(f.id, f.clock).initialize();
    assert.equal(restored.getChallenge("fixture")?.blackboard.at(-1)?.evidenceRef, evidenceRef);
  } finally {
    await BenchmarkLedger.destroy(f.id);
  }
});

test("metrics write failures warn without rolling back committed state, and retry on the next write", async (t) => {
  const f = await fixture();
  const metricsPath = join(f.directory, "metrics.json");
  const saved = join(f.directory, "metrics-before-test.json");
  const warning = t.mock.method(console, "warn", () => undefined);
  try {
    await rename(metricsPath, saved);
    await mkdir(metricsPath);
    const ended = await f.ledger.defer("fixture", "Synthetic durable handoff", undefined, "main");
    assert.equal(ended.status, "closing");
    assert.equal(f.ledger.getMetrics().totalDefers, 1);
    const durable = JSON.parse(await readFile(join(f.directory, "state.json"), "utf8"));
    assert.equal(durable.challenges.fixture.status, "closing");
    assert.equal(durable.challenges.fixture.approachHistory.length, 1);
    assert.equal(warning.mock.callCount(), 1);
    assert.match(String(warning.mock.calls[0].arguments[0]), /metrics write failed.*canonical state is committed/i);
    await rm(metricsPath, { recursive: true });
    await rename(saved, metricsPath);
    await f.ledger.recordVpnCheck(true, "fixture");
    assert.equal(warning.mock.callCount(), 1);
    const restored = await new BenchmarkLedger(f.id, f.clock).initialize();
    assert.equal(restored.getChallenge("fixture")?.status, "closing");
    assert.equal(restored.getChallenge("fixture")?.approachHistory.length, 1);
    assert.equal(restored.getMetrics().totalDefers, 1);
  } finally {
    await BenchmarkLedger.destroy(f.id);
  }
});

test("restart retains all attempts and ordinary observations, including a late report from the earliest worker", async () => {
  const f = await fixture("subagent:first");
  try {
    for (let round = 1; round <= 8; round++) {
      const owner = round === 1 ? "subagent:first" : "main";
      for (let note = 1; note <= 5; note++) await f.ledger.checkpoint("fixture", `Synthetic round ${round} observation ${note}`, [`route-${round}`], `Synthetic next ${round}`, owner);
      await f.ledger.defer("fixture", `Synthetic stop ${round}`, undefined, owner, true);
      await f.ledger.confirmClosed("fixture");
      f.at(round);
      if (round < 8) await f.ledger.acquire("fixture", "main", ["fixture"]);
    }
    await f.ledger.recordChildHandoff("fixture", "subagent:first", "FINDINGS: Synthetic late finding from the first attempt");
    const restored = await new BenchmarkLedger(f.id, f.clock).initialize();
    const challenge = restored.getChallenge("fixture")!;
    assert.deepEqual(challenge.approachHistory.map((attempt) => attempt.attemptNumber), [1, 2, 3, 4, 5, 6, 7, 8]);
    assert.equal(challenge.blackboard.filter((entry) => entry.kind === "note").length, 40);
    assert.equal(challenge.blackboard.filter((entry) => entry.kind === "attempt_end").length, 8);
    assert.ok(challenge.blackboard.some((entry) => entry.summary === "Synthetic round 1 observation 1"));
    const handoff = challenge.blackboard.find((entry) => entry.kind === "handoff" && entry.worker === "subagent:first");
    assert.ok(handoff);
    assert.match(await readFile(handoff.evidenceRef, "utf8"), /Synthetic late finding from the first attempt/);
  } finally {
    await BenchmarkLedger.destroy(f.id);
  }
});

test("malformed stored state rejects restart without renaming or replacing existing memory", async () => {
  for (const malformed of ["{invalid-json", "{}", "null", '{"challenges":{"fixture":null}}']) {
    const id = `ledger-invalid-state-test-${randomUUID()}`;
    const directory = join(homedir(), ".riftx", "benchmark", id);
    const path = join(directory, "state.json");
    try {
      await mkdir(directory, { recursive: true });
      await writeFile(path, malformed);
      await assert.rejects(new BenchmarkLedger(id).initialize(), /Invalid benchmark state/);
      assert.equal(await readFile(path, "utf8"), malformed);
      assert.deepEqual(await readdir(directory), ["state.json"]);
    } finally {
      await BenchmarkLedger.destroy(id);
    }
  }
});

test("a genuinely missing state creates and reloads a new empty ledger", async () => {
  const id = `ledger-missing-state-test-${randomUUID()}`;
  try {
    const ledger = await new BenchmarkLedger(id).initialize();
    assert.deepEqual(ledger.getState().challenges, {});
    const reloaded = await new BenchmarkLedger(id).initialize();
    assert.deepEqual(reloaded.getState().challenges, {});
    assert.equal(reloaded.getState().phase, ledger.getState().phase);
  } finally {
    await BenchmarkLedger.destroy(id);
  }
});

test("canonical shared intelligence and complete checkpoint families survive reload beyond display limits", async () => {
  const f = await fixture();
  try {
    for (let i = 0; i < 40; i++) await f.ledger.publishIntel("global", "*", `Synthetic shared observation ${i}`);
    await f.ledger.publishIntel("target", "another-target", "Synthetic unrelated target observation");
    const tried = Array.from({ length: 20 }, (_, i) => `route-${i}`);
    const ruledOut = Array.from({ length: 20 }, (_, i) => `excluded-${i}`);
    await f.ledger.checkpoint("fixture", "Synthetic complete exclusion batch", tried, undefined, "main", {
      signalKind: "decisive_rule_out", evidenceRef: "fixture:decisive-evidence", ruledOutFamilies: ruledOut
    });
    await f.ledger.checkpoint("fixture", "Synthetic later route", ["route-20"], undefined, "main");
    const restored = await new BenchmarkLedger(f.id, f.clock).initialize();
    const challenge = restored.getChallenge("fixture")!;
    assert.equal(restored.getState().sharedIntel.length, 41);
    assert.equal(restored.allIntelForChallenge(challenge).length, 40);
    assert.equal(restored.allIntelForChallenge(challenge)[0].intel, "Synthetic shared observation 0");
    assert.equal(restored.intelForChallenge(challenge).length, 8);
    assert.deepEqual(challenge.blackboard[0].triedFamilies, tried);
    assert.deepEqual(challenge.blackboard[0].ruledOutFamilies, ruledOut);
    assert.deepEqual(challenge.blackboard[1].triedFamilies, ["route-20"]);
  } finally {
    await BenchmarkLedger.destroy(f.id);
  }
});

test("a handoff report remains intact and retryable when its ledger write fails", async () => {
  const f = await fixture("subagent:fixture");
  const summary = "FINDINGS: Synthetic durable report\nUNCERTAINTIES: Synthetic unresolved question";
  try {
    const realPersist = (f.ledger as unknown as { persist(): Promise<void> }).persist.bind(f.ledger);
    Object.defineProperty(f.ledger, "persist", { configurable: true, value: async () => { throw new Error("SYNTHETIC_STATE_WRITE_FAILURE"); } });
    await assert.rejects(f.ledger.recordChildHandoff("fixture", "subagent:fixture", summary), /SYNTHETIC_STATE_WRITE_FAILURE/);
    assert.equal(f.ledger.getChallenge("fixture")?.blackboard.length, 0);
    const directory = join(f.directory, "handoffs");
    const files = await readdir(directory);
    assert.equal(files.length, 1);
    assert.ok(!files[0].includes(".tmp-"));
    assert.equal(await readFile(join(directory, files[0]), "utf8"), summary);
    Object.defineProperty(f.ledger, "persist", { configurable: true, value: realPersist });
    await f.ledger.recordChildHandoff("fixture", "subagent:fixture", summary);
    await f.ledger.recordChildHandoff("fixture", "subagent:fixture", summary);
    assert.deepEqual(await readdir(directory), files);
    const restored = await new BenchmarkLedger(f.id, f.clock).initialize();
    const handoffs = restored.getChallenge("fixture")!.blackboard.filter((entry) => entry.kind === "handoff");
    assert.equal(handoffs.length, 2);
    assert.equal(await readFile(handoffs[0].evidenceRef, "utf8"), summary);
  } finally {
    await BenchmarkLedger.destroy(f.id);
  }
});

test("successive corrections preserve their history after restart without reviving invalid conclusions", async () => {
  const f = await fixture();
  try {
    await f.ledger.checkpoint("fixture", "Synthetic original exclusion", ["route"], undefined, "main", {
      signalKind: "decisive_rule_out", evidenceRef: "fixture:evidence-a", ruledOutFamilies: ["route"]
    });
    f.at(1);
    await f.ledger.checkpoint("fixture", "Synthetic first correction", undefined, undefined, "main", {
      supersedesEvidenceRef: "fixture:evidence-a", evidenceRef: "fixture:evidence-b"
    });
    f.at(2);
    await f.ledger.checkpoint("fixture", "Synthetic second correction", undefined, undefined, "main", {
      supersedesEvidenceRef: "fixture:evidence-b", evidenceRef: "fixture:evidence-c"
    });
    const restored = await new BenchmarkLedger(f.id, f.clock).initialize();
    const [challenge] = await restored.memorySnapshot("fixture");
    assert.deepEqual(challenge.supersededBlackboard?.map((archived) => archived.entry.evidenceRef), ["fixture:evidence-a", "fixture:evidence-b"]);
    assert.deepEqual(challenge.supersededBlackboard?.map((archived) => archived.replacementEvidenceRef), ["fixture:evidence-b", "fixture:evidence-c"]);
    assert.deepEqual(challenge.supersededBlackboard?.map((archived) => archived.supersededAt), [61_000, 121_000]);
    assert.equal(challenge.supersededBlackboard?.[0].entry.summary, "Synthetic original exclusion");
    assert.deepEqual(challenge.blackboard.map((entry) => entry.evidenceRef), ["fixture:evidence-c"]);
    assert.deepEqual(challenge.ruledOutFamilies, []);
    assert.deepEqual(evidenceBackedRuleOuts(challenge), []);
    assert.ok(selectBlackboard(challenge, 6).every((entry) => entry.evidenceRef === "fixture:evidence-c"));
  } finally {
    await BenchmarkLedger.destroy(f.id);
  }
});
