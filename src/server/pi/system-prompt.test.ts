import assert from "node:assert/strict";
import test from "node:test";
import { buildChildPentestSystemPrompt, buildPentestSystemPrompt } from "./system-prompt";

test("benchmark prompt commands platform tools, two-pass scheduling, and the scoring objective", () => {
  const prompt = buildPentestSystemPrompt("default");
  assert.match(prompt, /TSec security benchmark/);
  assert.match(prompt, /benchmark_control/);
  assert.match(prompt, /assign_benchmark_challenge/);
  assert.match(prompt, /cumulative_score/);
  assert.match(prompt, /Submit EVERY flag immediately/);
  assert.match(prompt, /Hint is FORBIDDEN in pass 1/);
  assert.match(prompt, /8 minutes without a NEW signal/);
  assert.match(prompt, /Do not use bash\/curl against the benchmark API/);
  assert.match(prompt, /No reports, no record_finding/);
  assert.match(prompt, /browser.*proactively/s);
  assert.match(prompt, /crawl.*once you know the entry point/);
  assert.match(prompt, /max active.*close one existing container/);
  assert.match(prompt, /ResourceUnavailable.*skip to the next challenge/);
  assert.match(prompt, /assign_benchmark_challenge to dispatch SubAgents/);
  assert.match(prompt, /benchmark continuity block may appear after compaction/);
});

test("safety core is always included", () => {
  const prompt = buildPentestSystemPrompt("default");
  assert.match(prompt, /Safety, Approval, and Scope/);
  assert.match(prompt, /Do not perform destructive deletion/);
  assert.match(prompt, /minimal-impact, reversible, and auditable/);
});

test("completion boundary is benchmark-specific (no reports)", () => {
  const prompt = buildPentestSystemPrompt("default");
  assert.match(prompt, /cumulative_score, completed challenge count/);
  assert.match(prompt, /Do not generate a penetration-testing report/);
});

test("custom prompt is appended as operator constraints, benchmark protocol always present", () => {
  const prompt = buildPentestSystemPrompt("default", "My custom benchmark instructions.");
  assert.match(prompt, /My custom benchmark instructions/);
  assert.match(prompt, /Safety, Approval, and Scope/);
  assert.match(prompt, /Skill policy/);
  assert.match(prompt, /TSec security benchmark/); // benchmark protocol is never replaced
  assert.match(prompt, /Operator Constraints/);
});

test("child prompt is benchmark-specific with flag-list return format", () => {
  const prompt = buildChildPentestSystemPrompt();
  assert.match(prompt, /Benchmark SubAgent solving ONE challenge/);
  assert.match(prompt, /flag_count may be >1/);
  assert.match(prompt, /SUBMIT_STATUS/);
  assert.match(prompt, /RULED_OUT/);
  assert.match(prompt, /NEXT/);
  assert.match(prompt, /benchmark_control IS available.*ONLY for: checkpoint, submit, defer/);
  assert.match(prompt, /MOMENT you find a flag/);
  assert.match(prompt, /Do NOT use sync\/status\/acquire\/hint/);
  assert.match(prompt, /Do NOT use assign_benchmark_challenge/);
  assert.match(prompt, /Do not generate reports or evidence documentation/);
  assert.match(prompt, /Safety, Approval, and Scope/);
});
