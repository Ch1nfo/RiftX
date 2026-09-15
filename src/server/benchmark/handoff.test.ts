import assert from "node:assert/strict";
import test from "node:test";
import type { BrowserManager } from "@/browser";
import { evidenceBackedRuleOuts, handoffAttempt, retainBlackboard, selectBlackboard } from "./blackboard";
import { buildBenchmarkCompactionFallback } from "./compaction-fallback";
import type { BenchmarkController } from "./controller";
import type { AttemptSummary, BenchmarkLedger, BlackboardEntry, ChallengeState } from "./ledger";
import { createAssignBenchmarkChallengeTool } from "./tools/assign-tool";
import { createBenchmarkControlTool } from "./tools/control-tool";

function entry(kind: BlackboardEntry["kind"], at: number, evidenceRef = `artifact:fixture-${at}`): BlackboardEntry {
  return { kind, at, evidenceRef, worker: "main", summary: `fixture_observation_${at}`, approach: "", nextProbe: "", triedFamilies: [], ruledOutFamilies: [] };
}

function attempt(): AttemptSummary {
  return {
    attemptNumber: 2, phase: "revisit", worker: "subagent:previous", startedAt: 1_000, endedAt: 1_801_000,
    flagsBefore: 1, flagsAfter: 2, triedFamilies: ["tried_fixture"], ruledOutFamilies: ["supported_fixture", "legacy_unsupported"],
    stopReason: "fixture_no_further_progress", approach: "fixture_previous_hypothesis", nextDistinctApproach: "fixture_different_hypothesis"
  };
}

function challenge(): ChallengeState {
  const fixture: Partial<ChallengeState> = {
    uniqueCode: "fixture", owner: "main", status: "running", description: "fixture_description", totalScore: 100,
    correctFlagCount: 2, flagCount: 4, attemptCount: 3, hintUsed: false, containerAddrs: ["fixture"], containerStatus: "available",
    triedFlags: [], triedFamilies: ["tried_fixture"], ruledOutFamilies: ["supported_fixture", "legacy_unsupported"],
    attemptExtensionGrantedAt: null, seenEvidenceRefHashes: [],
    currentApproach: "", nextProbe: "fixture_different_hypothesis", approachHistory: [attempt()],
    lastMeaningfulSignalContent: "fixture_stage_observed", lastSignalKind: "stage_transition", lastEvidenceRef: "artifact:stage",
    blackboard: [entry("credential", 1), { ...entry("decisive_rule_out", 2), ruledOutFamilies: ["supported_fixture"] }]
  };
  return fixture as ChallengeState;
}

test("handoff selection preserves evidence diversity and its existing size limit", () => {
  const stage = entry("stage_transition", 1);
  const entries = [stage, ...Array.from({ length: 20 }, (_, index) => entry("credential", index + 2)),
    ...Array.from({ length: 40 }, (_, index) => entry("note", index + 22, ""))];
  const retained = retainBlackboard(entries);
  assert.equal(retained.filter((item) => !item.evidenceRef).length, 40);
  assert.equal(retained.filter((item) => item.evidenceRef).length, 21);
  for (const limit of [6, 10, 16]) {
    const selected = selectBlackboard({ blackboard: retained }, limit);
    assert.equal(selected.length, limit);
    assert.ok(selected.includes(stage));
    assert.ok(selected.some((item) => item.kind === "credential"));
  }
});

test("durable blackboard distinguishes equal wording across attempts and candidates", () => {
  const first = entry("note", 1, "");
  const second = { ...first, at: 2, worker: "subagent:second" as const };
  const updated = { ...second, nextProbe: "fixture_alternative", triedFamilies: ["fixture_family"] };
  assert.deepEqual(retainBlackboard([first, second, updated, { ...updated }]), [first, second, updated]);
});

test("repeated observations occupy one preview slot without deleting durable history", () => {
  const first = entry("credential", 1);
  const second = entry("credential", 2);
  const repeated = entry("credential", 3);
  const blackboard = retainBlackboard([first, second, ...Array.from({ length: 20 }, (_, at) => ({ ...repeated, at: at + 3 }))]);
  assert.equal(blackboard.length, 22);
  const preview = selectBlackboard({ blackboard }, 3);
  assert.equal(preview.length, 3);
  assert.ok(preview.includes(first));
  assert.ok(preview.includes(second));
  assert.equal(preview.filter((entry) => entry.evidenceRef === repeated.evidenceRef).length, 1);
});

test("handoff separates supported exclusions, attempt results and unverified candidates", () => {
  const state = challenge();
  state.blackboard.push({ ...entry("note", 3), ruledOutFamilies: ["legacy_unsupported"] });
  const exclusions = evidenceBackedRuleOuts(state);
  assert.deepEqual(exclusions, ["supported_fixture"]);
  const result = handoffAttempt(attempt(), exclusions);
  assert.equal(result.flagsDelta, 1);
  assert.equal(result.flagsBefore, 1);
  assert.equal(result.flagsAfter, 2);
  assert.equal(result.endedAt - result.startedAt, 1_800_000);
  assert.deepEqual(result.triedFamilies, ["tried_fixture"]);
  assert.deepEqual(result.ruledOutFamilies, ["supported_fixture"]);
  assert.equal(result.stopReason, "fixture_no_further_progress");
  assert.equal(result.terminationSource, "solver");
  assert.equal(result.previousCandidate?.requiresRevalidation, true);
  assert.equal(result.previousCandidate?.nextProbe, "fixture_different_hypothesis");
});

test("assigned worker receives full evidence references and prior attempt outcomes", async () => {
  const state = challenge();
  const reference = `artifact:${"fixture_segment/".repeat(22)}result.json`;
  state.blackboard[0].evidenceRef = reference;
  let brief = "";
  const ledger = {
    runChallengeAction: async (_code: string, action: () => Promise<unknown>) => action(),
    getChallenge: () => state, reserve: async () => state, confirmStarted: async () => state,
    getState: () => ({ phase: "revisit" }), isEndgame: () => false, intelForChallenge: () => []
  } as unknown as BenchmarkLedger;
  const controller = { startChallenge: async () => ({ container_addr: ["fixture"] }) } as unknown as BenchmarkController;
  const tool = createAssignBenchmarkChallengeTool(controller, ledger, async (task) => { brief = task; return { taskId: "fixture_child" }; });
  const result = await tool.execute("fixture_call", { uniqueCode: "fixture" }, undefined, undefined, {} as Parameters<typeof tool.execute>[4]);
  assert.equal((result.details as { assigned: boolean }).assigned, true);
  assert.ok(brief.includes(reference));
  assert.ok(!brief.includes("legacy_unsupported"));
  const packets = brief.split("\n").filter((line) => line.startsWith("{")).map((line) => JSON.parse(line));
  const history = packets.find((packet) => packet.attemptNumber === 2);
  assert.equal(history.flagsDelta, 1);
  assert.equal(history.previousCandidate.requiresRevalidation, true);
  assert.equal(history.stopReason, "fixture_no_further_progress");
});

test("checkpoint accepts bounded candidate fields and forwards them to the ledger", async () => {
  let capturedNext: string | undefined;
  let capturedApproach: string | undefined;
  const state = challenge();
  const ledger = {
    runChallengeAction: async (_code: string, action: () => Promise<unknown>) => action(),
    checkpoint: async (_code: string, _signal: string, _tried: string[] | undefined, next: string | undefined, _owner: string, options: { currentApproach?: string }) => {
      capturedNext = next; capturedApproach = options.currentApproach;
      return { updated: true, extended: false, challenge: state };
    },
    getState: () => ({ phase: "revisit" }), budgetFor: () => ({ firstAttempt: false, expired: false })
  } as unknown as BenchmarkLedger;
  const tool = createBenchmarkControlTool({} as BenchmarkController, ledger, {} as BrowserManager, () => "main");
  const schema = tool.parameters as { properties: Record<string, { maxLength?: number }> };
  assert.equal(schema.properties.currentApproach.maxLength, 300);
  assert.equal(schema.properties.nextProbe.maxLength, 1_000);
  await tool.execute("fixture_call", { action: "checkpoint", uniqueCode: "fixture", signal: "fixture_observed", currentApproach: "fixture_hypothesis", nextProbe: "fixture_next" }, undefined, undefined, {} as Parameters<typeof tool.execute>[4]);
  assert.equal(capturedApproach, "fixture_hypothesis");
  assert.equal(capturedNext, "fixture_next");
});

test("bounded compaction fallback retains the last attempt outcome separately from its candidate", () => {
  const state = challenge();
  const output = buildBenchmarkCompactionFallback({
    ledger: { getState: () => ({ challenges: { fixture: state } }) } as unknown as BenchmarkLedger,
    worker: "main", workingDirectory: "/fixture/work", ledgerFile: "/fixture/state.json"
  }, 4_000);
  assert.ok(output.length <= 4_000);
  const packet = JSON.parse(output);
  assert.equal(packet.lastAttempt.flagsDelta, 1);
  assert.equal(packet.lastAttempt.flagsBefore, 1);
  assert.equal(packet.lastAttempt.flagsAfter, 2);
  assert.equal(packet.lastAttempt.stopReason, "fixture_no_further_progress");
  assert.equal(packet.previousCandidate.requiresRevalidation, true);
  assert.equal(packet.previousCandidate.approach, "");
  assert.equal(packet.previousCandidate.nextProbe, "fixture_different_hypothesis");
  assert.deepEqual(packet.tried, ["tried_fixture"]);
  assert.deepEqual(packet.ruledOut, ["supported_fixture"]);
  assert.ok(!output.includes("legacy_unsupported"));
});
