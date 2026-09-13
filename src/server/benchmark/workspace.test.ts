import assert from "node:assert/strict";
import test from "node:test";
import { mkdtemp, readFile, realpath, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, relative } from "node:path";
import { createServer } from "node:http";
import type { ToolDefinition } from "@mariozechner/pi-coding-agent";
import { BrowserManager } from "@/browser/runtime/browser-manager";
import { activeSkillNamesFromBranch } from "@/server/pi/skill-router";
import { BenchmarkWorkspace, benchmarkWorkspaceRoot, challengeDirectory, createWorkspaceLocalTools, benchmarkMutationLock } from "./workspace";
import { createChallengeSkillSelection } from "./challenge-skills";

test("tool inventory remains available after workspace activation fails", async (t) => {
  const root = await mkdtemp(join(tmpdir(), "riftx-inventory-workspace-"));
  t.after(() => rm(root, { recursive: true, force: true }));
  const workspace = new BenchmarkWorkspace(root, "A", async () => { throw new Error("fixture-reset-failure"); });
  const tool = { name: "tool_inventory", execute: async () => "catalog" };
  workspace.install(tool);
  await assert.rejects(workspace.activate("B"), /fixture-reset-failure/);
  assert.equal(await tool.execute(), "catalog");
});

test("relative file tools, shell cwd and temporary files follow the challenge and survive revisits", async (t) => {
  const root = await mkdtemp(join(tmpdir(), "riftx-workspace-"));
  t.after(() => rm(root, { recursive: true, force: true }));
  const workspace = new BenchmarkWorkspace(root, undefined, async () => undefined);
  const tools = createWorkspaceLocalTools(() => workspace.cwd, {});
  const call = (name: string, params: unknown) => tools.find((tool) => tool.name === name)!.execute("fixture", params, undefined, undefined, {} as Parameters<ToolDefinition["execute"]>[4]);
  await workspace.activate("A");
  await call("write", { path: "same.txt", content: "alpha" });
  await call("edit", { path: "same.txt", edits: [{ oldText: "alpha", newText: "A" }] });
  await call("bash", { command: 'printf "%s" "$PWD" > pwd.txt; printf temporary > "$TMPDIR/probe"' });
  const a = workspace.cwd;
  assert.equal(await realpath(await readFile(join(a, "pwd.txt"), "utf8")), await realpath(a));
  assert.equal(await readFile(join(a, ".tmp", "probe"), "utf8"), "temporary");
  await workspace.activate("B");
  await call("write", { path: "same.txt", content: "B" });
  assert.equal(await readFile(join(a, "same.txt"), "utf8"), "A");
  assert.equal(await readFile(join(workspace.cwd, "same.txt"), "utf8"), "B");
  await workspace.activate("A");
  const read = await call("read", { path: "same.txt" });
  assert.equal(read.content[0].type === "text" && read.content[0].text, "A");
  assert.ok(!relative(root, challengeDirectory(root, "../../escape")).startsWith(".."));
  assert.equal(benchmarkWorkspaceRoot(root, "http://platform/"), benchmarkWorkspaceRoot(root, "http://platform"));
});

test("switch waits for running tools and refuses queued calls from the old challenge", async (t) => {
  const root = await mkdtemp(join(tmpdir(), "riftx-switch-"));
  t.after(() => rm(root, { recursive: true, force: true }));
  let reset = false;
  const workspace = new BenchmarkWorkspace(root, "A", async () => { reset = true; });
  let started!: () => void;
  const ready = new Promise<void>((resolve) => { started = resolve; });
  let finish!: () => void;
  const running = new Promise<void>((resolve) => { finish = resolve; });
  const slow = { name: "bash", execute: async () => { started(); await running; return "old"; } };
  const change = { name: "benchmark_control", execute: async () => { await workspace.activate("B"); return "changed"; } };
  let writes = 0;
  const queued = { name: "write", execute: async () => { writes++; return workspace.cwd; } };
  const tools = [slow, change, queued] as Array<{ name: string; execute: (id: string, params: unknown) => Promise<unknown> }>;
  tools.forEach((tool) => workspace.install(tool));
  const first = tools[0].execute("first", {});
  await ready;
  const switchTask = tools[1].execute("switch", { action: "acquire" });
  const stale = tools[2].execute("stale", {});
  assert.equal(reset, false);
  finish();
  await Promise.all([first, switchTask]);
  assert.equal((await stale as { details: { challengeChanged: boolean } }).details.challengeChanged, true);
  assert.equal(writes, 0);
  assert.equal(await tools[2].execute("fresh", {}), workspace.cwd);
});

test("challenge skill selection clears on release/unrelated tasks and persists re-selection", async (t) => {
  const root = await mkdtemp(join(tmpdir(), "riftx-selection-"));
  t.after(() => rm(root, { recursive: true, force: true }));
  const filePath = join(root, "SKILL.md");
  await writeFile(filePath, "---\nname: widget\n---\nSYNTHETIC_WIDGET_GUIDANCE");
  const entries: unknown[] = [];
  const active = new Set<string>();
  const select = createChallengeSkillSelection([{ name: "widget", description: "Synthetic widget inspection", filePath }], active,
    (content) => { entries.push({ type: "custom_message", customType: "riftx_skill_context", content }); });
  await select("Inspect a synthetic widget");
  assert.deepEqual([...active], ["widget"]);
  await select("Analyze an ELF binary");
  assert.deepEqual([...active], []);
  assert.deepEqual(activeSkillNamesFromBranch(entries), []);
  await select("Inspect a synthetic widget");
  assert.deepEqual(activeSkillNamesFromBranch(entries), ["widget"]);
  await select();
  assert.deepEqual([...active], []);
  assert.deepEqual(activeSkillNamesFromBranch(entries), []);
});

test("switching browser challenges removes cookies, storage, identity headers, tabs and grants", { timeout: 30_000 }, async (t) => {
  const root = await mkdtemp(join(tmpdir(), "riftx-browser-switch-"));
  let lastHeader: string | string[] | undefined;
  const server = createServer((req, res) => { lastHeader = req.headers["x-fixture"]; res.setHeader("Content-Type", "text/html"); res.end("<html>fixture</html>"); });
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  const origin = `http://127.0.0.1:${(server.address() as { port: number }).port}`;
  const browser = new BrowserManager({});
  t.after(async () => {
    await browser.shutdown();
    server.closeAllConnections();
    await new Promise<void>((resolve) => server.close(() => resolve()));
    await rm(root, { recursive: true, force: true });
  });
  const workspace = new BenchmarkWorkspace(root, "A", () => browser.run(() => browser.close()));
  browser.grantScope(origin, true);
  browser.grantScope("http://old-challenge.test:8080", true);
  await browser.navigate(origin);
  await browser.setExtraHeaders({ "X-Fixture": "previous" });
  await browser.evaluate('document.cookie="fixture=old"; localStorage.setItem("fixture", "old"); sessionStorage.setItem("fixture", "old");');
  await workspace.activate("B");
  browser.grantScope(origin, true);
  assert.equal(browser.checkNavigationScope("http://old-challenge.test:8080").allowed, false);
  assert.equal(browser.continuitySnapshot(), undefined);
  await browser.navigate(origin);
  assert.deepEqual(JSON.parse(await browser.evaluate('[document.cookie, localStorage.getItem("fixture"), sessionStorage.getItem("fixture")]')), ["", null, null]);
  assert.equal(lastHeader, undefined);
});


test("a long command and queued write in one challenge do not block another challenge", async () => {
  const run = {};
  const a = benchmarkMutationLock(run, "a");
  const b = benchmarkMutationLock(run, "b");
  assert.equal(benchmarkMutationLock(run, "a"), a);
  assert.notEqual(benchmarkMutationLock({}, "a"), a);
  const releaseA = await a.acquireShared();
  let wroteA = false;
  const pendingA = a.acquire().then((release) => { wroteA = true; release(); });
  const releaseB = await b.acquire();
  assert.equal(wroteA, false);
  releaseB();
  releaseA();
  await pendingA;
  assert.equal(wroteA, true);
});


test("submission reaches the platform before a long tool ends, but workspace teardown waits", async (t) => {
  const root = await mkdtemp(join(tmpdir(), "riftx-submit-barrier-"));
  t.after(() => rm(root, { recursive: true, force: true }));
  let reset = false;
  const workspace = new BenchmarkWorkspace(root, "A", async () => { reset = true; });
  const oldCwd = workspace.cwd;
  let finish!: () => void;
  let started!: () => void;
  const ready = new Promise<void>((resolve) => { started = resolve; });
  const running = new Promise<void>((resolve) => { finish = resolve; });
  const bash = { name: "bash", execute: async (_id: string, _params: unknown) => { started(); await running; } };
  let received!: () => void;
  const receipt = new Promise<void>((resolve) => { received = resolve; });
  const control = { name: "benchmark_control", execute: async (_id: string, _params: unknown) => { received(); await workspace.activate(); } };
  workspace.install(bash);
  workspace.install(control);
  const command = bash.execute("bash", {});
  await ready;
  const submission = control.execute("submit", { action: "submit" });
  await receipt;
  assert.equal(reset, false);
  assert.equal(workspace.cwd, oldCwd);
  finish();
  await Promise.all([command, submission]);
  assert.equal(reset, true);
  assert.notEqual(workspace.cwd, oldCwd);
});
