import assert from "node:assert/strict";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { createBenchmarkSkillHintTool } from "./skill-hint-tool";

test("returns an explicit no-match result without changing skill state", async () => {
  const tool = createBenchmarkSkillHintTool(() => [], () => "an unrelated challenge");
  const result = await tool.execute("id", { query: "no matching topic" }, undefined, undefined, {} as Parameters<typeof tool.execute>[4]);
  assert.match((result.content[0] as { text: string }).text, /No skill is particularly relevant/);
  assert.deepEqual(result.details, { matched: [] });
});

test("returns at most two full matching skill documents", async () => {
  const dir = await mkdtemp(join(tmpdir(), "riftx-skill-hint-"));
  const filePath = join(dir, "skill.md");
  await writeFile(filePath, "---\nname: demo\n---\nUseful guidance.");
  const skills = [
    { name: "demo-sql", description: "SQL injection testing", filePath },
    { name: "demo-web", description: "Web SQL endpoint testing", filePath },
    { name: "demo-other", description: "Unrelated audio processing", filePath }
  ];
  const tool = createBenchmarkSkillHintTool(() => skills, () => "SQL injection endpoint");
  const result = await tool.execute("id", { query: "SQL injection" }, undefined, undefined, {} as Parameters<typeof tool.execute>[4]);
  assert.ok((result.details as { matched: string[] }).matched.length <= 2);
  assert.match((result.content[0] as { text: string }).text, /<skill name=/);
  await rm(dir, { recursive: true, force: true });
});

test("an obstacle query is ranked on its own domain, not the challenge's", async () => {
  const dir = await mkdtemp(join(tmpdir(), "riftx-skill-hint-domain-"));
  const filePath = join(dir, "skill.md");
  await writeFile(filePath, "---\nname: demo\n---\nUseful guidance.");
  const skills = [
    { name: "demo-web", description: "Web SQL injection sqli endpoint testing", filePath },
    { name: "demo-crypto", description: "Cryptographic attacks: padding oracle, RSA, AES, crypto weaknesses", filePath }
  ];
  // A web-domain challenge asking about a crypto technique must still reach the
  // crypto skill: the router's domain guard sees the query's domain first.
  const tool = createBenchmarkSkillHintTool(() => skills, () => "web application sql injection endpoint login form");
  const result = await tool.execute("id", { query: "padding oracle attack decrypt" }, undefined, undefined, {} as Parameters<typeof tool.execute>[4]);
  assert.ok((result.details as { matched: string[] }).matched.includes("demo-crypto"),
    `expected demo-crypto in ${(result.details as { matched: string[] }).matched}`);
  await rm(dir, { recursive: true, force: true });
});

test("oversized skill documents are omitted with a note instead of truncating mid-document", async () => {
  const dir = await mkdtemp(join(tmpdir(), "riftx-skill-hint-budget-"));
  const filePath = join(dir, "skill.md");
  await writeFile(filePath, `---\nname: demo\n---\n${"x".repeat(13_000)}`);
  const skills = [{ name: "demo-big", description: "SQL injection testing", filePath }];
  const tool = createBenchmarkSkillHintTool(() => skills, () => "SQL injection endpoint");
  const result = await tool.execute("id", {}, undefined, undefined, {} as Parameters<typeof tool.execute>[4]);
  assert.match((result.content[0] as { text: string }).text, /omitted by the size budget/);
  assert.deepEqual((result.details as { matched: string[] }).matched, []);
  await rm(dir, { recursive: true, force: true });
});
