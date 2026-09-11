import assert from "node:assert/strict";
import test from "node:test";
import { compactionBudget } from "./compaction-budget";

test("allocates recent history and summary within a total target across window sizes", () => {
  for (const window of [128_000, 256_000, 1_000_000]) {
    for (const ratio of [1, 1.5, 3]) {
      const fixed = 2_000;
      const budget = compactionBudget(window, fixed, ratio);
      const total = budget.keepRecentTokens * ratio + budget.summaryTokens + fixed;
      assert.ok(total <= window * 0.13, `${window}: ${total}`);
      assert.ok(total >= window * 0.10, `${window}: ${total}`);
      assert.ok(budget.keepRecentTokens * ratio <= window * 0.10);
      assert.equal(budget.summaryTokens, Math.max(8192, window * 0.05));
    }
  }
  const budget = compactionBudget(256_000, 10_000);
  assert.equal(budget.summaryTokens, 12_800);
  assert.equal(budget.keepRecentTokens, 10_480);
  assert.equal(budget.targetTokens, 33_280);
});

test("summary budget uses five percent or 8192, whichever is larger", () => {
  assert.equal(compactionBudget(128_000).summaryTokens, 8_192);
  assert.equal(compactionBudget(256_000).summaryTokens, 12_800);
  assert.equal(compactionBudget(1_000_000).summaryTokens, 50_000);
  // Honor the summary minimum even when a small window cannot fit the total target.
  for (const window of [32_000, 64_000]) {
    const budget = compactionBudget(window, 2_000);
    assert.equal(budget.summaryTokens, 8_192);
    assert.equal(budget.keepRecentTokens, 1);
  }
});

test("shrinks raw history for large fixed prompts without reducing the summary budget", () => {
  assert.ok(compactionBudget(256_000, 20_000).keepRecentTokens < compactionBudget(256_000, 2_000).keepRecentTokens);
  assert.equal(compactionBudget(256_000, 20_000).summaryTokens, 12_800);
  // Fixed overhead may make the target impossible; never use a negative SDK budget.
  assert.equal(compactionBudget(8_000, 4_000).keepRecentTokens, 1);
});
