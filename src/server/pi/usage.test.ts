import assert from "node:assert/strict";
import test from "node:test";
import { normalizeContextUsage } from "./usage";

test("normalizes context usage and computes remaining tokens", () => {
  const usage = normalizeContextUsage({ tokens: 50, contextWindow: 100, input: 30, output: 20 });
  assert.equal(usage.percent, 50);
  assert.equal(usage.remaining, 50);
  assert.equal(usage.input, 30);
});

test("caps context percentage at 100", () => {
  const usage = normalizeContextUsage({ tokens: 120, contextWindow: 100 });
  assert.equal(usage.percent, 100);
  assert.equal(usage.remaining, 0);
});
