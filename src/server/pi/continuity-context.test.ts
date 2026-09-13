import assert from "node:assert/strict";
import test from "node:test";
import { TOOL_INVENTORY_CONTEXT_TYPE, upsertContinuityContext, upsertToolInventory } from "./continuity-context";
import { buildRuntimeToolIndex, type RuntimeToolCatalogState } from "../runtime-tool-catalog";

test("continuity replacement is ordered, idempotent, and preserves ordinary messages", () => {
  const messages: unknown[] = [
    { role: "user", content: "continue" },
    { role: "custom", customType: "riftx_task_contract", content: "stale" },
    { role: "custom", customType: "riftx_progress_checkpoint", content: "stale" }
  ];
  const context = {
    taskContract: "task",
    skillContext: "skill",
    investigationCapsule: "capsule",
    progressCheckpoint: "progress"
  };
  upsertContinuityContext(messages, context);
  upsertContinuityContext(messages, context);
  assert.equal((messages[0] as { role: string }).role, "user");
  assert.deepEqual(messages.slice(1).map((message) => (message as { customType: string }).customType), [
    "riftx_task_contract",
    "riftx_skill_context",
    "riftx_investigation_capsule",
    "riftx_progress_checkpoint"
  ]);
  assert.equal(messages.length, 5);
});

test("empty continuity blocks remove stale state instead of leaving ghosts", () => {
  const messages: unknown[] = [{ role: "custom", customType: "riftx_skill_context", content: "old" }];
  upsertContinuityContext(messages, {});
  assert.deepEqual(messages, []);
});

test("challenge switch, repeated refresh and compaction rebuild retain one current tool inventory", () => {
  const catalog: RuntimeToolCatalogState = { status: "available", path: "/fixture/catalog.json", catalog: { schemaVersion: 1, entries: [
    { kind: "command", name: "fixture_tool", category: "runtime" }
  ] } };
  const old = { role: "custom", customType: TOOL_INVENTORY_CONTEXT_TYPE, content: "stale" };
  const messages: unknown[] = [{ role: "user", content: "fixture" }, old, { ...old }];
  const inventory = () => messages.filter((message) => (message as { customType?: string }).customType === TOOL_INVENTORY_CONTEXT_TYPE) as Array<{ content: string }>;
  upsertContinuityContext(messages, { taskContract: "fixture_contract", toolInventory: buildRuntimeToolIndex(catalog, "fixture_a") });
  assert.equal(inventory().length, 1);
  assert.equal(JSON.parse(inventory()[0].content).activeChallenge, "fixture_a");
  const refreshed = { taskContract: "fixture_contract", toolInventory: buildRuntimeToolIndex(catalog, "fixture_b") };
  upsertContinuityContext(messages, refreshed);
  upsertContinuityContext(messages, refreshed);
  assert.equal(inventory().length, 1);
  assert.equal(JSON.parse(inventory()[0].content).activeChallenge, "fixture_b");
  messages.splice(0, messages.length, { role: "compactionSummary", summary: "fixture_summary" }, old, { ...old });
  upsertContinuityContext(messages, refreshed);
  assert.equal(inventory().length, 1);
  assert.equal(JSON.parse(inventory()[0].content).activeChallenge, "fixture_b");
  upsertContinuityContext(messages, { taskContract: "fixture_contract", toolInventory: buildRuntimeToolIndex(catalog) });
  assert.equal(inventory().length, 0);
  assert.equal((messages[0] as { role: string }).role, "compactionSummary");
});

test("immediate inventory replacement preserves other continuity and removes every stale copy", () => {
  const other = { role: "custom", customType: "riftx_task_contract", content: "fixture_contract" };
  const user = { role: "user", content: "fixture_user" };
  const old = { role: "custom", customType: TOOL_INVENTORY_CONTEXT_TYPE, content: "old" };
  const messages: unknown[] = [user, old, other, { ...old }];
  upsertToolInventory(messages, "current");
  upsertToolInventory(messages, "current");
  assert.deepEqual(messages.slice(0, 2), [user, other]);
  assert.equal(messages.length, 3);
  assert.equal((messages[2] as { content: string }).content, "current");
  upsertToolInventory(messages, "");
  assert.deepEqual(messages, [user, other]);
});
