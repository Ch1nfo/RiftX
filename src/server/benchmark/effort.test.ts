import assert from "node:assert/strict";
import test from "node:test";
import type { BenchmarkLedger, ChallengeState } from "./ledger";
import { installBenchmarkRepeatNotice, installPasswordEnumerationBudget, isPasswordEnumeration, PASSWORD_ENUMERATION_BUDGET_MS } from "./effort";

function fixture(spent = 0) {
  const challenge = { uniqueCode: "fixture", passwordEnumerationMs: spent, lastMeaningfulProgressAt: 1, correctFlagCount: 0 } as ChallengeState;
  const ledger = {
    budgetForOwner: () => ({ challenge, budget: { expired: false } }),
    recordPasswordEnumerationTime: async (_code: string, ms: number) => { challenge.passwordEnumerationMs += ms; }
  } as unknown as BenchmarkLedger;
  return { ledger, challenge };
}

test("online guessing classification leaves offline work and ordinary login checks alone", () => {
  for (const command of ["hydra -L users -P passwords target ssh", "sudo /usr/bin/medusa -h fixture", "python custom.py"]) {
    assert.ok(isPasswordEnumeration({ command, ...(command.startsWith("python") ? { passwordEnumeration: true } : {}) }));
  }
  for (const command of ["hydra --help", "hashcat hashes.txt", "patator unzip_pass archive=fixture.zip", "curl https://fixture/login", "python analyze.py"]) assert.equal(isPasswordEnumeration({ command }), false);
  assert.ok(isPasswordEnumeration({ command: "ncrack fixture", passwordEnumeration: false }));
});

test("guessing timeout aborts execution and a concurrent fresh worker shares the remaining budget", async () => {
  const { ledger, challenge } = fixture(PASSWORD_ENUMERATION_BUDGET_MS - 20);
  let executions = 0;
  const makeTool = () => ({ name: "bash", execute: async (_id: string, input: unknown, signal?: AbortSignal): Promise<unknown> => {
    executions++;
    assert.ok((input as { timeout: number }).timeout <= 0.020);
    return await new Promise<void>((_resolve, reject) => {
      if (signal?.aborted) reject(signal.reason);
      else signal?.addEventListener("abort", () => reject(signal.reason), { once: true });
    });
  } });
  const first = makeTool();
  const second = makeTool();
  installPasswordEnumerationBudget(first, ledger, "main");
  installPasswordEnumerationBudget(second, ledger, "subagent:replacement");
  const [a, b] = await Promise.all([first.execute("a", { passwordEnumeration: true }), second.execute("b", { passwordEnumeration: true })]);
  assert.ok(JSON.stringify(a).includes("passwordEnumerationTimedOut"));
  assert.ok(JSON.stringify(b).includes("passwordEnumerationBlocked"));
  assert.equal(executions, 1);
  assert.ok(challenge.passwordEnumerationMs >= PASSWORD_ENUMERATION_BUDGET_MS);
});

test("ordinary commands execute after enumeration budget exhaustion", async () => {
  const { ledger } = fixture(PASSWORD_ENUMERATION_BUDGET_MS);
  let calls = 0;
  const tool = { name: "bash", execute: async (_id: string, _params: unknown) => { calls++; return { content: [] }; } };
  installPasswordEnumerationBudget(tool, ledger, "main");
  await tool.execute("guess", { command: "hydra fixture" });
  await tool.execute("analysis", { command: "python inspect.py" });
  assert.equal(calls, 1);
});

test("repeated identical operations do not alter tool output", async () => {
  const { ledger, challenge } = fixture();
  let tick = 0;
  const make = () => ({ name: "bash", execute: async (_id: string, _params: unknown) => ({ content: [{ type: "text", text: `unchanged 2026-09-11T10:00:0${tick++}.000Z` }], details: { duration: tick } }) });
  const a = make(), b = make();
  installBenchmarkRepeatNotice(a, ledger, "main");
  installBenchmarkRepeatNotice(b, ledger, "subagent:replacement");
  await a.execute("1", { command: "inspect", timeout: 30 });
  await b.execute("2", { timeout: 90, command: "inspect" });
  await a.execute("other", { command: "other" });
  assert.doesNotMatch(JSON.stringify(await b.execute("3", { command: "inspect" })), /REPEATED_WITHOUT_NEW_INFORMATION/);
  challenge.lastMeaningfulProgressAt++;
  assert.doesNotMatch(JSON.stringify(await a.execute("4", { command: "inspect" })), /REPEATED_WITHOUT_NEW_INFORMATION/);
});

test("different results and active computation do not produce repetition warnings", async () => {
  const { ledger } = fixture();
  let value = 0;
  const tool = { name: "read", execute: async (_id: string, _params: unknown) => ({ content: [{ type: "text", text: `new value ${value++}` }] }) };
  installBenchmarkRepeatNotice(tool, ledger, "main");
  for (let i = 0; i < 5; i++) assert.doesNotMatch(JSON.stringify(await tool.execute(String(i), {})), /REPEATED_WITHOUT_NEW_INFORMATION/);
});


test("repeated thrown tool errors preserve the original failure", async () => {
  const { ledger } = fixture();
  const failure = new Error("fixture operation failed");
  const tool = { name: "bash", execute: async (_id: string, _params: unknown): Promise<unknown> => { throw failure; } };
  installBenchmarkRepeatNotice(tool, ledger, "main");
  await assert.rejects(tool.execute("1", {}), (error) => error === failure);
  await assert.rejects(tool.execute("2", {}), (error) => error === failure);
  await assert.rejects(tool.execute("3", {}), (error) => error === failure);
});
