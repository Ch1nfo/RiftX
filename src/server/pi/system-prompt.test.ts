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
  assert.match(prompt, /checkpoint_progress/);
  assert.match(prompt, /Use the spawn_subagent tool to create SubAgents/);
  assert.match(prompt, /Every spawned SubAgent is mandatory for the final assessment/);
  assert.match(prompt, /If your current turn reaches a conclusion while any child is still active/);
  assert.match(prompt, /Never use bash, sleep, tasks.json, child log files, or filesystem polling/);
  assert.match(prompt, /no optional wait mode/);
  assert.match(prompt, /configured maximum is a concurrency limit, not a target/);
  assert.match(prompt, /may be intercepted by an approval flow/);
  assert.match(prompt, /Reply in the same language the user writes in/);
  assert.match(prompt, /Completion Output Boundary/);
  assert.match(prompt, /task completion must return only a concise summary/);
  assert.match(prompt, /then stop/);
  assert.match(prompt, /Do not proactively generate, draft, format, save, update, or append a penetration-testing report/);
  assert.match(prompt, /requires an explicit request from the user in the current message/);
  assert.doesNotMatch(prompt, /offering either to continue deeper validation/);
  assert.doesNotMatch(prompt, /Scope and authorization assumptions/);
  assert.doesNotMatch(prompt, /use the following structure as appropriate/);
});

test("aggressiveness changes delegation policy", () => {
  assert.match(buildPentestSystemPrompt("high"), /without optimizing for token cost/);
  assert.match(buildPentestSystemPrompt("default"), /Delegate on demand/);
  assert.match(buildPentestSystemPrompt("low"), /Delegate conservatively/);
});

test("shared board prompt teaches the coordinator loop and drops legacy subagent waits", () => {
  const prompt = buildPentestSystemPrompt("default", undefined, true);
  assert.match(prompt, /Task board delegation policy/);
  assert.match(prompt, /Delegate on demand through the shared task board/);
  assert.match(prompt, /task_manage with action=create admits structured work/);
  assert.match(prompt, /approve submitted work only after checking its summary and evidence references/);
  assert.match(prompt, /Answer worker questions with agent_message/);
  assert.match(prompt, /Publish observations relevant across tracks with board_publish/);
  assert.match(prompt, /RiftX will wake you when results, proposals, or questions need attention/);
  assert.match(prompt, /Call board_finish only when all work is accepted, rejected, or cancelled/);
  assert.doesNotMatch(prompt, /Subagent delegation policy/);
  assert.doesNotMatch(prompt, /Every spawned SubAgent is mandatory/);
  assert.doesNotMatch(prompt, /tasks\.json/);
  assert.match(buildPentestSystemPrompt("high", undefined, true), /without optimizing for token cost/);
  assert.match(buildPentestSystemPrompt("low", undefined, true), /Delegate conservatively through the shared task board/);
});

test("shared child prompt teaches board workflow; legacy child keeps parent-task wording", () => {
  const shared = buildChildPentestSystemPrompt(true);
  assert.match(shared, /Claim a ready work item with task_claim/);
  assert.match(shared, /execution tools stay locked until your claimed work is running/);
  assert.match(shared, /block your work with reason question:<messageId>/);
  assert.match(shared, /Publish observations useful to other agents with board_publish/);
  assert.match(shared, /Propose follow-up work you cannot execute yourself with task_propose/);
  assert.match(shared, /never use task_manage or board_finish/);
  assert.match(shared, /Always finish the delegated task with a concise plain-text final summary/);
  assert.doesNotMatch(shared, /delegated task from the parent RiftX Agent/);
  const legacy = buildChildPentestSystemPrompt();
  assert.match(legacy, /delegated task from the parent RiftX Agent/);
  assert.doesNotMatch(legacy, /task_claim/);
});

test("child prompt requires a final text summary", () => {
  const prompt = buildChildPentestSystemPrompt();
  assert.match(prompt, /Use checkpoint_progress at meaningful phase boundaries/);
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
  assert.match(prompt, /task completion must return only a concise summary/);
});
