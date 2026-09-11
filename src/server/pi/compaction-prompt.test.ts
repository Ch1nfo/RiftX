import assert from "node:assert/strict";
import test from "node:test";
import { buildPentestCompactionPrompt, isValidPentestCompactionSummary, PENTEST_COMPACTION_SYSTEM_PROMPT } from "./compaction-prompt";

const headings = [
  "Goal and constraints", "Attack surface and identities", "Confirmed findings", "Active hypotheses",
  "Ruled-out hypotheses", "Work completed", "Delegated work", "Evidence and artifact references",
  "Exact next steps", "Critical context"
];

test("penetration compaction prompt preserves iterative and split-turn inputs", () => {
  const prompt = buildPentestCompactionPrompt({
    conversation: "[Tool result]: 403 for user B",
    turnPrefix: "[User]: continue the IDOR check",
    previousSummary: "request:req-4 returned 200 for user A",
    customInstructions: "Preserve exact request refs"
  });
  for (const heading of headings) assert.match(prompt, new RegExp(`## ${heading}`));
  assert.match(prompt, /<previous-checkpoint>/);
  assert.match(prompt, /<current-turn-prefix>/);
  assert.match(prompt, /request:req-4/);
  assert.doesNotMatch(prompt, /protected-facts|current-runtime-state|Repair the previous checkpoint|under \d+ tokens/);
  assert.match(PENTEST_COMPACTION_SYSTEM_PROMPT, /authorized security assessment/);
  assert.doesNotMatch(PENTEST_COMPACTION_SYSTEM_PROMPT + prompt, /TSec|benchmark|Challenge blackboard|Score optimization/);
});

test("validates minimum length and required assessment headings without strict body checks", () => {
  assert.equal(isValidPentestCompactionSummary("Looks fine but omitted state."), false);
  const valid = headings.map((heading) => `## ${heading}\n- preserved state`).join("\n\n");
  assert.equal(isValidPentestCompactionSummary(valid), true);
  assert.equal(isValidPentestCompactionSummary(valid.replace("## Exact next steps", "## Next")), false);
  assert.equal(isValidPentestCompactionSummary(headings.map((heading) => `## ${heading}`).join("\n")), true);
  assert.equal(isValidPentestCompactionSummary(valid + "\n## Critical context\nadditional reference"), true);
});
