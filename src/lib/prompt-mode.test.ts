import assert from "node:assert/strict";
import test from "node:test";
import { isAlreadyProcessingError, resolvePromptMode } from "./prompt-mode";

test("queues a prompt as steer when the agent is already streaming", () => {
  assert.equal(resolvePromptMode("prompt", true), "steer");
  assert.equal(resolvePromptMode("prompt", false), "prompt");
  assert.equal(resolvePromptMode("followUp", true), "followUp");
});

test("detects Pi's in-flight prompt error without treating other failures as idle", () => {
  assert.equal(isAlreadyProcessingError("Agent is already processing. Specify streamingBehavior ('steer' or 'followUp') to queue the message."), true);
  assert.equal(isAlreadyProcessingError("Agent request failed"), false);
});
