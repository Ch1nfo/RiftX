import assert from "node:assert/strict";
import test from "node:test";
import { buildPentestCompactionPrompt, isValidPentestCompactionSummary, PENTEST_COMPACTION_SYSTEM_PROMPT } from "./compaction-prompt";

test("system prompt carries benchmark identity and untrusted-data rule", () => {
  assert.match(PENTEST_COMPACTION_SYSTEM_PROMPT, /TSec security benchmark/);
  assert.match(PENTEST_COMPACTION_SYSTEM_PROMPT, /untrusted data/);
  assert.match(PENTEST_COMPACTION_SYSTEM_PROMPT, /exact next probe/);
});

test("prompt embeds every benchmark continuity section plus conversation and previous checkpoint", () => {
  const prompt = buildPentestCompactionPrompt({
    conversation: "user did stuff",
    turnPrefix: "current turn",
    previousSummary: "old checkpoint",
    customInstructions: "focus on flag 3"
  });
  for (const section of [
    "## Run state", "## Current challenge", "## Challenge blackboard", "## Confirmed facts",
    "## Attempts and ruled-out paths", "## Attempt and approach history",
    "## Ruled-out assumptions", "## Target credentials and session state",
    "## Browser and network references", "## Artifacts", "## SubAgent ownership",
    "## Exact next probe", "## Score optimization notes"
  ]) {
    assert.ok(prompt.includes(section), `missing section: ${section}`);
  }
  assert.match(prompt, /<conversation-history>\nuser did stuff/);
  assert.match(prompt, /<previous-checkpoint>\nold checkpoint/);
  assert.match(prompt, /<current-turn-prefix>\ncurrent turn/);
  assert.match(prompt, /<additional-focus>\nfocus on flag 3/);
});

test("validates only summaries containing every required section with minimum length", () => {
  const valid = [
    "## Run state\ncoverage", "## Current challenge\nch-1", "## Challenge blackboard\nnone", "## Confirmed facts\nnone",
    "## Attempts and ruled-out paths\nnone", "## Attempt and approach history\nnone",
    "## Ruled-out assumptions\nnone",
    "## Target credentials and session state\nnone",
    "## Browser and network references\nnone", "## Artifacts\nnone",
    "## SubAgent ownership\nnone", "## Exact next probe\ndo the thing",
    "## Score optimization notes\nnone"
  ].join("\n");
  assert.equal(isValidPentestCompactionSummary(valid), true);
  assert.equal(isValidPentestCompactionSummary(valid.replace("## Artifacts", "## Missing")), false);
  assert.equal(isValidPentestCompactionSummary("too short"), false);
});
