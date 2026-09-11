import assert from "node:assert/strict";
import test from "node:test";
import { compactionFacts, deduplicateSummaryState, summaryIssues } from "./compaction-quality";

const headings = ["## Facts", "## Next"];

test("rejects empty sections, repeated headings and all-placeholder checkpoints", () => {
  for (const summary of ["## Facts\n## Next", "## Facts\n(none)\n## Next\n(none)", "## Facts\nEvidence\n## Facts\nOther evidence\n## Next\nContinue"]) {
    assert.ok(summaryIssues(summary, headings).length);
  }
  assert.deepEqual(summaryIssues("## Facts\nrequest:r-1 failed\n## Next\nTry another identity", headings), []);
});

test("protects short user requirements and evidence anchors across summary or surviving state", () => {
  const facts = compactionFacts([
    { role: "user", content: "只读验证，不要修改目标数据。" },
    { role: "toolResult", content: "request:r-1 returned 403 at https://target.example/api" }
  ]);
  assert.deepEqual(facts, ["只读验证，不要修改目标数据。", "request:r-1", "https://target.example/api"]);
  const summary = "## Facts\nrequest:r-1 returned 403\n## Next\nTry another identity";
  assert.equal(summaryIssues(summary, headings, facts).length, 2);
  assert.deepEqual(summaryIssues(summary, headings, facts, "只读验证，不要修改目标数据。 https://target.example/api"), []);
});

test("deduplicates only exact facts in the freshly selected state, retaining older evidence", () => {
  const fact = "The current challenge is ch-7 and request:r-20 proved read-only access.";
  const summary = `## Facts\n- ${fact}\nOld request:r-3 ruled out anonymous access.\n## Next\nCompare role B`;
  const reduced = deduplicateSummaryState(summary, `- ${fact}`);
  assert.ok(!reduced.includes(fact));
  assert.match(reduced, /Old request:r-3/);
  assert.match(reduced, /current runtime state/);
  assert.deepEqual(summaryIssues(reduced, headings), []);
});
