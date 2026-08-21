import assert from "node:assert/strict";
import test from "node:test";
import { buildPentestSystemPrompt } from "./system-prompt";

test("pentest prompt actively selects browser and targeted testing methods", () => {
  const prompt = buildPentestSystemPrompt("default");
  assert.match(prompt, /Use browser proactively for live pages/);
  assert.match(prompt, /Do not test only one input or one path/);
  assert.match(prompt, /small, targeted, controlled test sets/);
  assert.match(prompt, /Use the spawn_subagent tool to create child Agents/);
  assert.match(prompt, /Every spawned child is mandatory for the final assessment/);
  assert.match(prompt, /never give the final conclusion until every spawned child/);
  assert.match(prompt, /Never use bash, sleep, tasks.json, child log files, or filesystem polling/);
  assert.match(prompt, /no optional wait mode/);
  assert.match(prompt, /configured maximum is a concurrency limit, not a target/);
  assert.match(prompt, /current approval mode/);
});

test("aggressiveness changes delegation policy", () => {
  assert.match(buildPentestSystemPrompt("high"), /without optimizing for token cost/);
  assert.match(buildPentestSystemPrompt("default"), /Delegate on demand/);
  assert.match(buildPentestSystemPrompt("low"), /Delegate conservatively/);
});

test("custom system prompt replaces the built-in base while retaining delegation policy", () => {
  const prompt = buildPentestSystemPrompt("default", "CUSTOM RIFTX PROMPT");
  assert.match(prompt, /CUSTOM RIFTX PROMPT/);
  assert.doesNotMatch(prompt, /You are an advanced Web penetration testing/);
  assert.match(prompt, /Subagent delegation policy/);
});
