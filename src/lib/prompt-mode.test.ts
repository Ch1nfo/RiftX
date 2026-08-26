import assert from "node:assert/strict";
import test from "node:test";
import { isAlreadyProcessingError, isPromptMode, resolvePromptMode } from "./prompt-mode";

test("queues a prompt as steer when the agent is already streaming", () => {
  assert.equal(resolvePromptMode("prompt", true), "steer");
  assert.equal(resolvePromptMode("prompt", false), "prompt");
  assert.equal(resolvePromptMode("followUp", true), "followUp");
  assert.equal(resolvePromptMode("steer", false), "prompt");
  assert.equal(resolvePromptMode("followUp", false), "prompt");
});

test("detects the in-flight prompt error without treating other failures as idle", () => {
  assert.equal(isAlreadyProcessingError("Agent is already processing. Specify streamingBehavior ('steer' or 'followUp') to queue the message."), true);
  assert.equal(isAlreadyProcessingError("Agent request failed"), false);
});

test("isPromptMode accepts only the known modes", () => {
  assert.equal(isPromptMode("prompt"), true);
  assert.equal(isPromptMode("steer"), true);
  assert.equal(isPromptMode("followUp"), true);
  assert.equal(isPromptMode("Prompt"), false);
  assert.equal(isPromptMode("chat"), false);
  assert.equal(isPromptMode(undefined), false);
  assert.equal(isPromptMode(7), false);
});
