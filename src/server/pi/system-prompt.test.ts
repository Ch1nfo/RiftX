import assert from "node:assert/strict";
import test from "node:test";
import { buildChildPentestSystemPrompt, buildPentestSystemPrompt } from "./system-prompt";

test("main and child benchmark prompts allow relevant skills", () => {
  for (const prompt of [buildPentestSystemPrompt("default"), buildChildPentestSystemPrompt()]) {
    assert.match(prompt, /Use relevant available Agent Skills/);
    assert.match(prompt, /if none clearly fits, proceed without loading one/);
    assert.doesNotMatch(prompt, /Do not load skills|do not load Agent Skills/);
  }
});

test("commander prompt is verbatim: role, principles, workflow, endgame report", () => {
  const prompt = buildPentestSystemPrompt("default");
  assert.match(prompt, /# ROLE\nYou are an elite CTF player and field commander/);
  assert.match(prompt, /Score = flags captured\. You are never idle/);
  assert.match(prompt, /1\. Optimize points-per-minute, not elegance/);
  assert.match(prompt, /never babysit or poll them/);
  assert.match(prompt, /Only report flags observed verbatim in tool output/);
  assert.match(prompt, /Never attack the scoring\/harness\n   infrastructure/);
  assert.match(prompt, /# SUB-AGENT RETURN FORMAT\n- FLAG: exact string if captured, else NONE\./);
  // Handoff contract: successors get evidence and open questions, never a plan.
  assert.match(prompt, /- UNCERTAINTIES: unresolved questions and limits of the evidence\. Do not/);
  assert.doesNotMatch(prompt, /NEXT: best remaining hypotheses/);
  // Phase 0 must not fight the harness-enforced low-score-first coverage order.
  assert.match(prompt, /from the currently eligible lowest score tier/);
  assert.doesNotMatch(prompt, /highest-value or\s+hardest challenge/);
  // Password-enumeration budget must match the harness-enforced effort.ts values.
  assert.match(prompt, /120 seconds of\n  wall-clock time in total, each guessing call capped at 30 seconds/);
  assert.match(prompt, /# PLAYBOOK BY CATEGORY/);
  assert.match(prompt, /# TENACITY/);
  assert.match(prompt, /Final report: every solved challenge with its method, plus each captured flag/);
  assert.match(prompt, /never re-derive or guess a flag string/);
  assert.match(prompt, /Credential guessing is evidence-gated/);
  assert.match(prompt, /shared across you, every sub-agent, and every tool switch/);
  assert.match(prompt, /"Try another wordlist" or "run it a\n  bit longer" is not new evidence/);
  assert.match(prompt, /Submit each flag the moment it is confirmed/);
});

test("harness mechanics appendix names the runtime-enforced rules", () => {
  const prompt = buildPentestSystemPrompt("default");
  assert.match(prompt, /## Harness mechanics/);
  assert.match(prompt, /benchmark_control/);
  assert.match(prompt, /assign_benchmark_challenge/);
  assert.match(prompt, /at most 2 sub-agents and 3 live containers/i);
  assert.match(prompt, /low score to high/i);
  assert.match(prompt, /EVERY attempt is capped at 30 minutes\s*\n\s*with ONE notice at 25/);
  assert.match(prompt, /ONE 10-minute extension, to 40 minutes total/);
  assert.match(prompt, /stage_transition checkpoint citing evidence not recorded before/);
  // The runtime enforces the deadline on every attempt; no prompt may promise
  // unlimited revisits (stale wording once contradicted the harness).
  assert.doesNotMatch(prompt, /no\s+runtime\s+limit|not\s+time-limited|unlimited/);
  assert.match(prompt, /never\n  bash\/curl the benchmark API/);
  assert.match(prompt, /"Parked" = defer/);
  assert.match(prompt, /benchmark_control\(action="status"\) IS the live board/);
  assert.match(prompt, /tool_inventory/);
});

test("benchmark scope boundary permits challenge exploitation but protects platform infrastructure", () => {
  const prompt = buildPentestSystemPrompt("default");
  assert.match(prompt, /Benchmark Scope and Approval Boundary/);
  assert.match(prompt, /shells inside the disposable target/);
  assert.match(prompt, /Never attack, fuzz, enumerate.*Benchmark API/s);
  assert.match(prompt, /Do not use denial of service/);
});

test("custom prompt is appended as operator constraints, commander protocol never replaced", () => {
  const prompt = buildPentestSystemPrompt("default", "My custom benchmark instructions.");
  assert.match(prompt, /My custom benchmark instructions/);
  assert.match(prompt, /Benchmark Scope and Approval Boundary/);
  assert.match(prompt, /Skill policy/);
  assert.match(prompt, /# ROLE/); // commander protocol is never replaced
  assert.match(prompt, /Operator Constraints/);
});

test("child prompt carries the commander return format, playbook, and tool restrictions", () => {
  const prompt = buildChildPentestSystemPrompt();
  assert.match(prompt, /CTF sub-agent solving ONE challenge/);
  assert.match(prompt, /flag_count may be >1/);
  assert.match(prompt, /Return format \(mandatory\):\nFLAG: exact captured flag string/);
  assert.match(prompt, /FINDINGS: creds, access gained/);
  assert.match(prompt, /RULED_OUT: approaches tried and why they failed/);
  assert.match(prompt, /UNCERTAINTIES: unresolved questions and limits of the evidence; no proposed next steps/);
  assert.match(prompt, /benchmark_control\(action="submit",\n  flag="\.\.\."\) — that is the submission method/);
  assert.match(prompt, /ONLY for: checkpoint, submit, defer, reset_environment,\n  publish_intel/);
  assert.match(prompt, /Never permanently abandon this challenge/);
  assert.match(prompt, /Do NOT use sync\/status\/acquire\/\n  hint/);
  assert.match(prompt, /Do NOT use assign_benchmark_challenge \(commander-only\)/);
  assert.match(prompt, /# PLAYBOOK BY CATEGORY/);
  assert.match(prompt, /steghide \(bruteforce passphrase with rockyou/);
  assert.match(prompt, /- tool_inventory: check command, Python module and wordlist availability/);
  assert.match(prompt, /Credential testing is evidence-gated/);
  assert.match(prompt, /if the blackboard shows it was already spent, do not restart it/);
  assert.match(prompt, /Benchmark Scope and Approval Boundary/);
});
