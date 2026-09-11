import assert from "node:assert/strict";
import test from "node:test";
import { benchmarkDefaultApproval, benchmarkProfile, modelBaseUrl } from "./environment";

test("hosted model endpoints preserve paths, rewrite once, and enforce the whitelist", () => {
  assert.equal(modelBaseUrl("https://open.bigmodel.cn/api/v1", true), "http://open.bigmodel.cn.tsecbench.gw/api/v1");
  assert.equal(modelBaseUrl("http://api.deepseek.com.tsecbench.gw/v1", true), "http://api.deepseek.com.tsecbench.gw/v1");
  assert.equal(modelBaseUrl("https://example.maas.aliyuncs.com/compatible-mode/v1", true), "http://example.maas.aliyuncs.com.tsecbench.gw/compatible-mode/v1");
  for (const url of ["https://api.openai.com/v1", "https://dashscope.aliyuncs.com/v1", "https://open.bigmodel.cn.evil.test/v1", "https://open.bigmodel.cn:8443/v1"]) {
    assert.throws(() => modelBaseUrl(url, true));
  }
  assert.equal(modelBaseUrl("http://127.0.0.1:1234/v1", false), "http://127.0.0.1:1234/v1");
});

test("model parameters validate and child overrides inherit all unspecified fields", () => {
  const env = { RIFTX_LLM_BASE_URL: "https://open.bigmodel.cn/api/v1", RIFTX_LLM_API_KEY: "test-key", RIFTX_LLM_MODEL: "test-model", RIFTX_HOSTED: "1", RIFTX_LLM_CONTEXT_WINDOW: "200000", RIFTX_LLM_MAX_TOKENS: "10000", RIFTX_LLM_THINKING: "high", RIFTX_LLM_IMAGES: "1" };
  const parent = benchmarkProfile(env);
  const child = benchmarkProfile({ ...env, RIFTX_CHILD_LLM_MODEL: "child-model", RIFTX_CHILD_LLM_MAX_TOKENS: "4000" }, true);
  assert.equal(parent.contextWindow, 200000);
  assert.equal(parent.supportsImages, true);
  assert.equal(child.model, "child-model");
  assert.equal(child.maxTokens, 4000);
  assert.equal(child.apiKey, parent.apiKey);
  assert.equal(child.baseUrl, parent.baseUrl);
  assert.equal(child.thinkingLevel, "high");
  for (const thinking of ["max", "ultra"]) {
    assert.equal(benchmarkProfile({ ...env, RIFTX_LLM_THINKING: thinking }).thinkingLevel, thinking);
    assert.equal(benchmarkProfile({ ...env, RIFTX_CHILD_LLM_THINKING: thinking }, true).thinkingLevel, thinking);
  }
  for (const patch of [{ RIFTX_LLM_API_KEY: "" }, { RIFTX_LLM_MAX_TOKENS: "200000" }, { RIFTX_LLM_CONTEXT_WINDOW: "NaN" }, { RIFTX_LLM_IMAGES: "yes" }, { RIFTX_LLM_THINKING: "invalid" }, { RIFTX_HOSTED: "true" }]) {
    assert.throws(() => benchmarkProfile({ ...env, ...patch }));
  }
  assert.equal(benchmarkDefaultApproval({ BENCHMARK_BASE_URL: "http://platform", BENCHMARK_TOKEN: "test" }), "full");
  assert.equal(benchmarkDefaultApproval({}), "request");
});
