import assert from "node:assert/strict";
import test from "node:test";
import { upsertContinuityContext } from "./continuity-context";

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
