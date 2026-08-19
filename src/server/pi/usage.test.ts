import assert from "node:assert/strict";
import test from "node:test";
import { emptyContextUsage, normalizeContextUsage } from "./usage";

test("normalizes context usage and computes remaining tokens", () => {
  const usage = normalizeContextUsage({ tokens: 50, contextWindow: 100, input: 30, output: 20 });
  assert.equal(usage.percent, 50);
  assert.equal(usage.remaining, 50);
  assert.equal(usage.input, 30);
  assert.equal(usage.output, 20);
});

test("caps context percentage at 100", () => {
  const usage = normalizeContextUsage({ tokens: 120, contextWindow: 100 });
  assert.equal(usage.percent, 100);
  assert.equal(usage.remaining, 0);
});

test("keeps context unknown when provider reports null usage after compaction", () => {
  const usage = normalizeContextUsage({ tokens: null, percent: null }, 1000);
  assert.equal(usage.tokens, 0);
  assert.equal(usage.contextWindow, 1000);
  assert.equal(usage.percent, null);
  assert.equal(usage.remaining, 1000);
});

test("builds empty usage without fake fallback window", () => {
  const usage = emptyContextUsage();
  assert.equal(usage.contextWindow, 0);
  assert.equal(usage.percent, null);
  assert.equal(usage.remaining, 0);
  assert.equal(usage.input, null);
  assert.equal(usage.cacheRead, null);
});

test("keeps missing breakdown fields unknown instead of coercing them to zero", () => {
  const usage = normalizeContextUsage({ totalTokens: 1234 }, 10000);
  assert.equal(usage.tokens, 1234);
  assert.equal(usage.input, null);
  assert.equal(usage.output, null);
  assert.equal(usage.cacheRead, null);
  assert.equal(usage.cacheWrite, null);
});
