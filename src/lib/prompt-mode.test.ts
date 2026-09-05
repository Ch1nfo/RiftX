import assert from "node:assert/strict";
import test from "node:test";
import { isAlreadyProcessingError, isPromptMode, preparePromptDispatch, resolvePromptMode } from "./prompt-mode";

test("queues a prompt as steer when the agent is already streaming", () => {
  assert.equal(resolvePromptMode("prompt", true), "steer");
  assert.equal(resolvePromptMode("prompt", false), "prompt");
  assert.equal(resolvePromptMode("followUp", true), "followUp");
  assert.equal(resolvePromptMode("steer", false), "prompt");
  assert.equal(resolvePromptMode("followUp", false), "prompt");
});

test("an explicit follow-up rechecks streaming only after async preparation", async () => {
  let streaming = true;
  let streamingReads = 0;
  let releasePreparation!: () => void;
  const preparationBlocked = new Promise<void>((resolve) => { releasePreparation = resolve; });
  const dispatch = preparePromptDispatch(
    "followUp",
    () => {
      streamingReads += 1;
      return streaming;
    },
    async () => {
      await preparationBlocked;
      return "prepared";
    },
    () => "raw"
  );

  // The active turn ends while a skill file is still being read. The message
  // must become a real prompt, not an idle follow-up that Pi only queues.
  assert.equal(streamingReads, 0, "mode must not be resolved before preparation finishes");
  streaming = false;
  releasePreparation();

  assert.deepEqual(await dispatch, { mode: "prompt", prepared: "prepared" });
  assert.equal(streamingReads, 1, "mode has one authoritative dispatch-time resolution");
});

test("a streaming prompt takes the synchronous steer path without preparing skills", async () => {
  let prepareCalls = 0;
  const dispatch = await preparePromptDispatch(
    "prompt",
    () => true,
    async () => {
      prepareCalls += 1;
      return "prepared";
    },
    () => "raw"
  );

  assert.deepEqual(dispatch, { mode: "steer", prepared: "raw" });
  assert.equal(prepareCalls, 0);
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
