import assert from "node:assert/strict";
import test from "node:test";
import { buildChildPentestSystemPrompt, buildPentestSystemPrompt } from "./system-prompt";

test("pentest prompt actively selects browser and targeted testing methods", () => {
  const prompt = buildPentestSystemPrompt("default");
  assert.match(prompt, /browser proactively for live pages/);
  assert.match(prompt, /Code-derived targets beat blind probing/);
  assert.match(prompt, /Do not stop at the first payload that fails or gets filtered/);
  assert.match(prompt, /Verified depth outranks count/);
  assert.match(prompt, /When blocked, change perspective instead of giving up/);
  assert.match(prompt, /Do not test only one input or one path/);
  assert.match(prompt, /small, targeted, controlled test sets/);
  assert.match(prompt, /Use the spawn_subagent tool to create child Agents/);
  assert.match(prompt, /Every spawned child is mandatory for the final assessment/);
  assert.match(prompt, /If your current turn reaches a conclusion while any child is still active/);
  assert.match(prompt, /Never use bash, sleep, tasks.json, child log files, or filesystem polling/);
  assert.match(prompt, /no optional wait mode/);
  assert.match(prompt, /configured maximum is a concurrency limit, not a target/);
  assert.match(prompt, /may be intercepted by an approval flow/);
  assert.match(prompt, /Reply in the same language the user writes in/);
  assert.match(prompt, /Only when the assessment is complete and a final conclusion is expected/);
});

test("aggressiveness changes delegation policy", () => {
  assert.match(buildPentestSystemPrompt("high"), /without optimizing for token cost/);
  assert.match(buildPentestSystemPrompt("default"), /Delegate on demand/);
  assert.match(buildPentestSystemPrompt("low"), /Delegate conservatively/);
});

test("child prompt requires a final text summary", () => {
  const prompt = buildChildPentestSystemPrompt();
  assert.match(prompt, /Always finish the delegated task with a concise plain-text final summary/);
  assert.match(prompt, /Do not stop immediately after a tool call/);
});

test("custom system prompt replaces the built-in base while retaining delegation policy", () => {
  const prompt = buildPentestSystemPrompt("default", "CUSTOM RIFTX PROMPT");
  assert.match(prompt, /CUSTOM RIFTX PROMPT/);
  assert.doesNotMatch(prompt, /You are RiftX, an authorized Web penetration testing/);
  assert.match(prompt, /Subagent delegation policy/);
  assert.match(prompt, /Do not perform destructive deletion/);
  assert.match(prompt, /Stop the related testing immediately/);
});
