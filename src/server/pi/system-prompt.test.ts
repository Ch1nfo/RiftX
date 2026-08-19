import assert from "node:assert/strict";
import test from "node:test";
import { buildPentestSystemPrompt } from "./system-prompt";

test("pentest prompt proactively selects browser and independent subagents", () => {
  const prompt = buildPentestSystemPrompt("default");
  assert.match(prompt, /do not wait for the user to say the word "browser"/);
  assert.match(prompt, /Use spawn_subagent proactively for broad work/);
  assert.match(prompt, /do not create three tasks by default/);
  assert.match(prompt, /configured maximum is a concurrency limit, not a target/);
  assert.match(prompt, /Tool selection is part of the task/);
});

test("aggressiveness changes delegation policy", () => {
  assert.match(buildPentestSystemPrompt("high"), /without optimizing for token cost/);
  assert.match(buildPentestSystemPrompt("default"), /Delegate on demand/);
  assert.match(buildPentestSystemPrompt("low"), /Delegate conservatively/);
});
