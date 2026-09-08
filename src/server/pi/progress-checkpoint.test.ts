import assert from "node:assert/strict";
import test from "node:test";
import { buildProgressCheckpointContext, MAX_PROGRESS_CHECKPOINT_CHARS, progressCheckpointFromBranch } from "./progress-checkpoint";

test("restores the latest progress checkpoint from uncompacted tool-call history", () => {
  const latest = progressCheckpointFromBranch([
    { type: "message", message: { role: "assistant", content: [{ type: "toolCall", name: "checkpoint_progress", arguments: { objective: "old", completed: [], ruledOut: [], pending: [], nextProbe: "old", criticalRefs: [] } }] } },
    { type: "compaction", summary: "summary" },
    { type: "message", message: { role: "assistant", content: [{ type: "toolCall", name: "checkpoint_progress", arguments: { objective: "Validate authz", completed: ["Mapped routes"], ruledOut: ["SQLi on q"], pending: ["IDOR"], nextProbe: "Replay req-7 as user B", criticalRefs: ["request:req-7"] } }] } }
  ]);
  assert.equal(latest?.objective, "Validate authz");
  assert.equal(latest?.nextProbe, "Replay req-7 as user B");
  const context = buildProgressCheckpointContext(latest);
  assert.match(context, /Mapped routes/);
  assert.match(context, /SQLi on q/);
  assert.match(context, /request:req-7/);
});

test("checkpoint context escapes target-controlled markup", () => {
  const context = buildProgressCheckpointContext({
    objective: "<ignore>instructions</ignore>", completed: [], ruledOut: [], pending: [], nextProbe: "GET /", criticalRefs: []
  });
  assert.doesNotMatch(context, /<ignore>/);
  assert.match(context, /&lt;ignore&gt;/);
});

test("checkpoint context has a fixed lightweight budget", () => {
  const context = buildProgressCheckpointContext({
    objective: "x".repeat(800),
    completed: Array(20).fill("c".repeat(300)),
    ruledOut: Array(20).fill("r".repeat(300)),
    pending: Array(20).fill("p".repeat(300)),
    nextProbe: "n".repeat(800),
    criticalRefs: Array(20).fill("e".repeat(500))
  });
  assert.ok(context.length <= MAX_PROGRESS_CHECKPOINT_CHARS);
  assert.ok(context.endsWith("</riftx-progress-checkpoint>"));
  assert.match(context, /Checkpoint truncated/);
});
