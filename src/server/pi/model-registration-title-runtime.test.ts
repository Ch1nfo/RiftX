import assert from "node:assert/strict";
import test from "node:test";
import type { ModelProfile } from "@/lib/types";
import { memoizedTitleRuntime } from "./model-registration";

const base: ModelProfile = {
  id: "p1",
  name: "Main",
  provider: "openai",
  model: "gpt-test",
  apiKey: "sk-test",
  baseUrl: "https://api.test/v1",
  api: "openai-completions",
  transport: "auto",
  contextWindow: 128000,
  maxTokens: 4096,
  thinkingLevel: "off"
};

test("the isolated title runtime is built once per distinct profile", () => {
  let builds = 0;
  const build = () => ({ registry: `registry-${builds++}` });
  const first = memoizedTitleRuntime(base, build);
  const second = memoizedTitleRuntime({ ...base }, build);
  assert.equal(first, second);
  assert.equal(builds, 1);
});

test("a different profile gets its own runtime", () => {
  let builds = 0;
  const build = () => ({ registry: `registry-${builds++}` });
  const first = memoizedTitleRuntime({ ...base, model: "gpt-one" }, build);
  const second = memoizedTitleRuntime({ ...base, model: "gpt-two" }, build);
  assert.notEqual(first, second);
  assert.equal(builds, 2);
});
