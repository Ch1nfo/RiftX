import assert from "node:assert/strict";
import test from "node:test";
import { randomUUID } from "node:crypto";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { homedir, tmpdir } from "node:os";
import { join } from "node:path";
import { SessionManager } from "@mariozechner/pi-coding-agent";
import type { BrowserManager } from "@/browser";
import { BenchmarkLedger, type BlackboardEntry, type ChallengeState } from "./ledger";
import type { BenchmarkController, Challenge } from "./controller";
import { benchmarkMemoryLocator, MAX_BENCHMARK_MEMORY_PAGE_CHARS, readBenchmarkMemory } from "./memory";
import { buildBenchmarkCompactionFallback } from "./compaction-fallback";
import { buildBenchmarkContinuity, MAX_BENCHMARK_CONTINUITY_CHARS } from "./continuity";
import { persistBenchmarkEvidenceRef } from "./evidence";
import { installBenchmarkRepeatNotice } from "./effort";
import { installBenchmarkTimeboxGate } from "./timebox";
import { createBenchmarkControlTool } from "./tools/control-tool";
import { evidenceBackedRuleOuts, selectBlackboard } from "./blackboard";

type Page = Awaited<ReturnType<typeof readBenchmarkMemory>>;

function fixtureChallenge(code = "fixture"): ChallengeState {
  const fixture: Partial<ChallengeState> = {
    uniqueCode: code, description: "fixture_description", hintContent: null, blackboard: [], approachHistory: [],
    currentApproach: "fixture_hypothesis", nextProbe: "fixture_next", triedFamilies: [], ruledOutFamilies: [],
    owner: null, status: "deferred", correctFlagCount: 0, flagCount: 4, isCompleted: false
  };
  return fixture as ChallengeState;
}

function fixtureLedger(challenges: ChallengeState[]): BenchmarkLedger {
  return {
    memorySnapshot: async (code?: string) => structuredClone(challenges.filter((challenge) => !code || challenge.uniqueCode === code)),
    allIntelForChallenge: () => [], getState: () => ({ sharedIntel: [] })
  } as unknown as BenchmarkLedger;
}

function observation(at: number, summary: string): BlackboardEntry {
  return { at, worker: "main", kind: "note", summary, evidenceRef: "artifact:fixture", approach: "", nextProbe: "", triedFamilies: [], ruledOutFamilies: [] };
}

async function allPages(ledger: BenchmarkLedger, code: string, kind?: "blackboard" | "attempts") {
  let cursor: string | undefined;
  const items: Page["items"] = [];
  let pages = 0;
  do {
    const page = await readBenchmarkMemory(ledger, code, cursor, kind);
    assert.ok(JSON.stringify(page).length <= MAX_BENCHMARK_MEMORY_PAGE_CHARS);
    items.push(...page.items);
    cursor = page.nextCursor ?? undefined;
    assert.ok(++pages < 100);
  } while (cursor);
  return { items, pages };
}

test("memory pages preserve complete records and reject only this challenge's stale cursors", async () => {
  const mine = fixtureChallenge();
  const other = fixtureChallenge("other");
  mine.blackboard = Array.from({ length: 15 }, (_, index) => observation(index, "fixture_".repeat(200)));
  const ledger = fixtureLedger([mine, other]);
  const first = await readBenchmarkMemory(ledger, "fixture", undefined, "blackboard");
  assert.ok(first.nextCursor);
  other.nextProbe = "unrelated_change";
  assert.equal((await readBenchmarkMemory(ledger, "fixture", first.nextCursor!, "blackboard")).version, first.version);
  const complete = await allPages(ledger, "fixture", "blackboard");
  assert.equal(complete.items.length, mine.blackboard.length);
  assert.deepEqual(complete.items.map((item) => item.entry), mine.blackboard);
  assert.ok(complete.pages > 1);
  mine.nextProbe = "changed_candidate";
  await assert.rejects(readBenchmarkMemory(ledger, "fixture", first.nextCursor!, "blackboard"), /Memory changed/);
});

test("one oversized legacy record is recoverable as lossless JSON fragments", async () => {
  const mine = fixtureChallenge();
  const original = observation(1, 'fixture_"\\\n中'.repeat(4_000));
  original.evidenceRef = `/fixture/${"long_reference_segment/".repeat(30)}evidence.json`;
  mine.blackboard = [original, observation(2, "following_record")];
  const result = await allPages(fixtureLedger([mine]), "fixture", "blackboard");
  const chunks = result.items.filter((item) => item.kind === "record_chunk");
  assert.ok(chunks.length > 1);
  const recovered = JSON.parse(chunks.sort((left, right) => Number(left.part) - Number(right.part)).map((item) => item.jsonFragment).join(""));
  assert.deepEqual(recovered, { kind: "blackboard", entry: original });
  assert.equal((result.items.at(-1)?.entry as BlackboardEntry).summary, "following_record");
});

test("superseded evidence stays readable as inactive history without returning as an effective conclusion", async () => {
  const mine = fixtureChallenge();
  const old = { ...observation(1, "fixture_old_conclusion"), kind: "decisive_rule_out" as const, ruledOutFamilies: ["fixture_family"] };
  const current = observation(2, "fixture_correction");
  current.evidenceRef = "artifact:fixture_new";
  mine.blackboard = [current];
  mine.ruledOutFamilies = ["fixture_family"];
  mine.supersededBlackboard = [{ entry: old, supersededAt: 2, replacementEvidenceRef: current.evidenceRef }];
  const ledger = fixtureLedger([mine]);
  const board = await allPages(ledger, "fixture", "blackboard");
  assert.equal(board.items.length, 2);
  assert.deepEqual(board.items.find((item) => item.kind === "historical_superseded"), {
    kind: "historical_superseded", effective: false, ...mine.supersededBlackboard[0]
  });
  const overview = await readBenchmarkMemory(ledger, "fixture", undefined, "overview");
  assert.ok(overview.items.every((item) => item.kind !== "blackboard" && item.kind !== "historical_superseded"));
  assert.deepEqual(evidenceBackedRuleOuts(mine), []);
  assert.deepEqual(selectBlackboard(mine, 8), [current]);
});

test("memory exposes the complete matching shared-intel history beyond context preview limits", async () => {
  const entries = Array.from({ length: 40 }, (_, index) => ({
    scope: "global" as const, target: "", intel: `fixture_intel_${index}`, publishedAt: index
  }));
  const ledger = fixtureLedger([fixtureChallenge()]);
  ledger.allIntelForChallenge = () => entries;
  const complete = await allPages(ledger, "fixture");
  assert.deepEqual(complete.items.filter((item) => item.kind === "shared_intel"), entries.map((entry) => ({ kind: "shared_intel", ...entry })));
});

test("checkpoint returns canonical references and preserves an already matched superseded pointer", async () => {
  const mine = fixtureChallenge();
  mine.blackboard = [{ ...observation(1, "fixture_old"), evidenceRef: "/fixture/canonical-old.txt" }];
  const copies: Array<string | undefined> = [];
  const writes: Array<{ evidenceRef?: string; supersedesEvidenceRef?: string }> = [];
  const ledger = {
    runChallengeAction: async (_code: string, action: () => Promise<unknown>) => action(),
    assertOwned: async () => mine,
    checkpoint: async (_code: string, _signal: string, _families: string[], _next: string, _owner: string, options: { evidenceRef?: string; supersedesEvidenceRef?: string }) => {
      writes.push(options);
      return { updated: true, extended: false, challenge: mine };
    },
    budgetFor: () => undefined,
    getState: () => ({ phase: "coverage" })
  } as unknown as BenchmarkLedger;
  const writer = createBenchmarkControlTool({} as BenchmarkController, ledger, {} as BrowserManager, () => "main", undefined, undefined, undefined,
    async (reference) => {
      copies.push(reference);
      return reference === "artifact:old" ? "/fixture/canonical-old.txt" : "/fixture/canonical-new.txt";
    });
  const params = { action: "checkpoint" as const, uniqueCode: "fixture", signal: "fixture_correction", evidenceRef: "artifact:new", supersedesEvidenceRef: "artifact:old" };
  const first = await writer.execute("fixture_call", params, undefined, undefined, {} as Parameters<typeof writer.execute>[4]);
  assert.deepEqual(copies, ["artifact:new", "artifact:old"]);
  assert.equal(writes[0].evidenceRef, "/fixture/canonical-new.txt");
  assert.equal(writes[0].supersedesEvidenceRef, "/fixture/canonical-old.txt");
  const canonical = JSON.parse((first.content[1] as { text: string }).text);
  assert.deepEqual(canonical, { evidenceRef: "/fixture/canonical-new.txt", supersedesEvidenceRef: "/fixture/canonical-old.txt" });
  copies.length = 0;
  await writer.execute("fixture_next", { ...params, supersedesEvidenceRef: canonical.supersedesEvidenceRef }, undefined, undefined, {} as Parameters<typeof writer.execute>[4]);
  assert.deepEqual(copies, ["artifact:new"]);
  assert.equal(writes[1].supersedesEvidenceRef, "/fixture/canonical-old.txt");
});

test("empty context can recover old notes, zero-flag attempts and durable evidence using only a bound memory locator", async () => {
  const id = `memory-test-${randomUUID()}`;
  const work = await mkdtemp(join(tmpdir(), "riftx-memory-test-"));
  const evidenceDirectory = join(homedir(), ".riftx", "benchmark", id, "evidence");
  const originalFile = join(work, "fixture-evidence.txt");
  await writeFile(originalFile, "fixture_durable_evidence", "utf8");
  let now = 1_000_000;
  let durableReference = "";
  try {
    const original = await new BenchmarkLedger(id, () => now).initialize();
    const platform: Challenge = {
      unique_code: "fixture", description: "fixture_task_description", difficulty: "easy", level: 1,
      total_score: 100, flag_count: 4, correct_flag_count: 0, is_completed: false, container_status: "stopped", container_addr: []
    };
    await original.syncFromPlatform([platform], true, "fixture_ip");
    for (let round = 1; round <= 9; round++) {
      await original.acquire("fixture", "subagent:old", ["fixture_container"]);
      if (round === 1) {
        const writer = createBenchmarkControlTool({} as BenchmarkController, original, {} as BrowserManager, () => "subagent:old", "fixture", undefined, undefined,
          (reference) => persistBenchmarkEvidenceRef(reference, { directory: evidenceDirectory, cwd: work, allowedRoots: [work] }));
        const saved = await writer.execute("fixture_evidence_call", {
          action: "checkpoint", signal: "fixture_old_evidence", signalKind: "credential", evidenceRef: originalFile,
          nextProbe: "fixture_old_candidate", currentApproach: "fixture_old_hypothesis"
        }, undefined, undefined, {} as Parameters<typeof writer.execute>[4]);
        durableReference = (saved.details as { evidenceRef: string }).evidenceRef;
        assert.ok(durableReference.startsWith(evidenceDirectory));
        for (let index = 0; index < 45; index++) {
          now++;
          await original.checkpoint("fixture", `ordinary_note_${index}`, undefined, `candidate_${index}`, "subagent:old");
        }
      }
      now += 1_000;
      await original.defer("fixture", `round_${round}_no_flag`, `round_${round}_candidate`, "subagent:old", true);
      await original.confirmClosed("fixture");
      await original.maybeAdvancePhase();
    }
    await rm(originalFile);
    const restored = await new BenchmarkLedger(id, () => now).initialize();
    const emptyConversation = SessionManager.inMemory();
    assert.equal(emptyConversation.getBranch().length, 0);
    const bootstrap = JSON.parse(buildBenchmarkCompactionFallback({
      ledger: restored, worker: "subagent:new", assignedChallenge: "fixture", workingDirectory: work
    }, 4_000));
    assert.equal(bootstrap.binding.challenge, null);
    assert.equal(bootstrap.memory.arguments.action, "read_memory");
    const reader = createBenchmarkControlTool({} as BenchmarkController, restored, {} as BrowserManager, () => "subagent:new", "fixture");
    installBenchmarkTimeboxGate(reader as Parameters<typeof installBenchmarkTimeboxGate>[0], restored, "subagent:new", "fixture");
    installBenchmarkRepeatNotice(reader as Parameters<typeof installBenchmarkRepeatNotice>[0], restored, "subagent:new");
    const items: Page["items"] = [];
    let cursor: string | number | undefined = bootstrap.memory.arguments.cursor;
    let pages = 0;
    do {
      const result = await reader.execute("fixture_read_call", { action: "read_memory", cursor }, undefined, undefined, {} as Parameters<typeof reader.execute>[4]);
      const text = (result.content[0] as { text: string }).text;
      assert.ok(text.length <= MAX_BENCHMARK_MEMORY_PAGE_CHARS);
      const page: Page = JSON.parse(text);
      assert.equal(page.uniqueCode, "fixture");
      items.push(...page.items);
      cursor = page.nextCursor ?? undefined;
      assert.ok(++pages < 100);
    } while (cursor);
    assert.ok(pages > 1);
    const notes = items.filter((item) => item.kind === "blackboard").map((item) => item.entry as BlackboardEntry);
    assert.ok(notes.some((entry) => entry.summary === "ordinary_note_0" && entry.nextProbe === "candidate_0"));
    assert.ok(notes.some((entry) => entry.summary === "ordinary_note_44"));
    assert.ok(notes.some((entry) => entry.evidenceRef === durableReference));
    const attempts = items.filter((item) => item.kind === "attempt");
    assert.equal(attempts.length, 9);
    assert.ok(attempts.every((item) => item.flagsDelta === 0));
    assert.equal(await readFile(durableReference, "utf8"), "fixture_durable_evidence");
    assert.equal(emptyConversation.getBranch().length, 0);
    const wrong = await reader.execute("fixture_wrong", { action: "read_memory", uniqueCode: "other" }, undefined, undefined, {} as Parameters<typeof reader.execute>[4]);
    assert.equal((wrong.details as { restricted: boolean }).restricted, true);
    assert.equal((await restored.memorySnapshot("fixture"))[0].owner, null);
  } finally {
    await BenchmarkLedger.destroy(id);
    await rm(work, { recursive: true, force: true });
  }
});

test("every bounded preview keeps the authoritative memory locator", () => {
  const mine = fixtureChallenge();
  Object.assign(mine, {
    owner: "main", status: "running", description: "fixture_long_description".repeat(2_000),
    containerAddrs: [], attemptCount: 1, currentAttemptPhase: "coverage", passwordEnumerationMs: 0,
    lastMeaningfulSignalContent: "fixture_observation", lastEvidenceRef: "", triedFlags: []
  });
  const ledger = {
    getState: () => ({ challenges: { fixture: mine }, totalChallenges: 1, phase: "coverage", sharedIntel: [] }),
    isEndgame: () => false, budgetFor: () => undefined, isBudgetExhausted: () => false, intelForChallenge: () => [], candidates: () => []
  } as unknown as BenchmarkLedger;
  const preview = buildBenchmarkContinuity(ledger, "main");
  assert.ok(preview.length <= MAX_BENCHMARK_CONTINUITY_CHARS);
  assert.ok(preview.includes(JSON.stringify(benchmarkMemoryLocator("fixture"))));
  const childPreview = buildBenchmarkContinuity(ledger, "subagent:new");
  assert.ok(childPreview.includes(JSON.stringify(benchmarkMemoryLocator())));
  const fallback = buildBenchmarkCompactionFallback({ ledger, worker: "main", workingDirectory: "" }, 4_000);
  assert.ok(fallback.length <= 4_000);
  assert.deepEqual(JSON.parse(fallback).memory, benchmarkMemoryLocator("fixture").memory);
});

test("a failed durable evidence copy never acknowledges a saved checkpoint", async () => {
  let copyCalls = 0;
  let writes = 0;
  const ledger = {
    runChallengeAction: async (_code: string, action: () => Promise<unknown>) => action(),
    assertOwned: async () => fixtureChallenge(),
    checkpoint: async () => { writes++; }
  } as unknown as BenchmarkLedger;
  const writer = createBenchmarkControlTool({} as BenchmarkController, ledger, {} as BrowserManager, () => "main", undefined, undefined, undefined,
    async () => { copyCalls++; throw new Error("fixture_copy_failed"); });
  await assert.rejects(writer.execute("fixture_call", { action: "checkpoint", uniqueCode: "fixture", signal: "fixture_observed", evidenceRef: "artifact:fixture" }, undefined, undefined, {} as Parameters<typeof writer.execute>[4]), /fixture_copy_failed/);
  assert.equal(copyCalls, 1);
  assert.equal(writes, 0);
  ledger.assertOwned = async () => { throw new Error("fixture_wrong_owner"); };
  await assert.rejects(writer.execute("fixture_call", { action: "checkpoint", uniqueCode: "fixture", signal: "fixture_observed", evidenceRef: "artifact:fixture" }, undefined, undefined, {} as Parameters<typeof writer.execute>[4]), /fixture_wrong_owner/);
  assert.equal(copyCalls, 1);
});
