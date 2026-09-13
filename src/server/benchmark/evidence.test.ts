import test from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, mkdir, readFile, readdir, realpath, rm, stat, symlink, utimes, writeFile } from "node:fs/promises";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { randomUUID } from "node:crypto";
import { BrowserManager } from "@/browser";
import { RequestStore } from "@/browser/network/request-store";
import { createToolOutputStore } from "@/server/tool-output";
import { persistBenchmarkEvidenceRef, type BenchmarkEvidenceContext } from "./evidence";

async function fixture(t: test.TestContext) {
  const root = await realpath(await mkdtemp(join(tmpdir(), "riftx-benchmark-evidence-")));
  t.after(() => rm(root, { recursive: true, force: true }));
  const cwd = join(root, "work");
  const directory = join(root, "durable");
  const artifacts = join(root, "artifacts");
  const evidenceRoot = join(root, "browser-evidence");
  const sessionId = "fixture-parent";
  const evidenceDirectory = join(evidenceRoot, sessionId);
  await mkdir(cwd, { recursive: true });
  const context: BenchmarkEvidenceContext = { cwd, directory, allowedRoots: [cwd, artifacts, evidenceDirectory], evidenceDirectory };
  return { root, cwd, directory, artifacts, evidenceRoot, evidenceDirectory, sessionId, context };
}

test("request checkpoint survives browser close, another worker, and source removal", async (t) => {
  const { evidenceRoot, sessionId, evidenceDirectory, context } = await fixture(t);
  const browser = new BrowserManager({ evidenceRoot, evidenceSessionId: sessionId, scope: { rules: [] } });
  t.after(() => browser.shutdown());
  const requests = (browser as unknown as { requests: RequestStore }).requests;
  const request = requests.start({
    pageId: "fixture-page", identity: "default", method: "GET", url: "http://fixture.invalid/observed",
    resourceType: "document", requestHeaders: {}, responseBody: "fixture-response", startedAt: "2026-01-01T00:00:00Z"
  });
  const pinned = await persistBenchmarkEvidenceRef(`request:${request.ref}`, { ...context, browser });
  assert.ok(pinned.startsWith(context.directory));
  await browser.close();
  const nextWorker = new BrowserManager({ evidenceRoot, evidenceSessionId: sessionId, scope: { rules: [] } });
  t.after(() => nextWorker.shutdown());
  const repeated = await persistBenchmarkEvidenceRef(request.ref, { ...context, browser: nextWorker });
  assert.equal(repeated, pinned);
  await rm(join(evidenceDirectory, "requests"), { recursive: true, force: true });
  const retained = JSON.parse(await readFile(pinned, "utf8"));
  assert.equal(retained.url, request.url);
  assert.equal(retained.responseBody, "fixture-response");
});

test("checkpoint snapshot is independent of the rolling 64-artifact cleanup", async (t) => {
  const { artifacts, sessionId, context } = await fixture(t);
  const outputs = createToolOutputStore(artifacts, sessionId, "fixture-child");
  const contents = "fixture-important-content-".repeat(800);
  const first = await outputs.project("crawl", [contents], "fixture-summary");
  assert.ok(first.artifactPath);
  const pinned = await persistBenchmarkEvidenceRef(first.artifactPath, context);
  await utimes(first.artifactPath, new Date("2001-01-01"), new Date("2001-01-01"));
  for (let index = 0; index < 64; index++) await outputs.project("crawl", ["x".repeat(16_001)], "fixture-summary");
  await assert.rejects(() => stat(first.artifactPath!));
  assert.equal(await readFile(pinned, "utf8"), contents);
});

test("relative task files become immutable absolute snapshots with exact content", async (t) => {
  const { cwd, context } = await fixture(t);
  const name = "fixture  two-spaces.txt";
  const path = join(cwd, name);
  await writeFile(path, "fixture-before");
  const first = await persistBenchmarkEvidenceRef(`artifact:${name}`, context);
  await writeFile(path, "fixture-after");
  const second = await persistBenchmarkEvidenceRef(path, context);
  assert.notEqual(first, second);
  await rm(path);
  assert.equal(await readFile(first, "utf8"), "fixture-before");
  assert.equal(await readFile(second, "utf8"), "fixture-after");
  assert.equal(await persistBenchmarkEvidenceRef(first, context), first);
});

test("screenshots preserve binary bytes and latest resolves before the browser disappears", async (t) => {
  const { evidenceDirectory, context } = await fixture(t);
  const screenshotId = `s-${randomUUID()}`;
  const source = join(evidenceDirectory, "shots", `${screenshotId}.png`);
  await mkdir(join(evidenceDirectory, "shots"), { recursive: true });
  const bytes = Buffer.from([137, 80, 78, 71, 0, 255, 128]);
  await writeFile(source, bytes);
  const browser = { screenshotEvidence: async () => ({ screenshotId }) } as unknown as BenchmarkEvidenceContext["browser"];
  const pinned = await persistBenchmarkEvidenceRef("screenshot:latest", { ...context, browser });
  await rm(source);
  assert.ok(pinned.endsWith(".png"));
  assert.deepEqual(await readFile(pinned), bytes);
});

test("tool evidence is snapshotted so a later worker needs no previous session messages", async (t) => {
  const { context } = await fixture(t);
  const pinned = await persistBenchmarkEvidenceRef("tool:fixture-call", {
    ...context, resolveToolEvidence: () => ({ toolName: "bash", content: "fixture-observation" })
  });
  assert.deepEqual(JSON.parse(await readFile(pinned, "utf8")), {
    toolCallId: "fixture-call", toolName: "bash", content: "fixture-observation"
  });
});

test("tool snapshots pin the full large-output artifact along with its inline preview", async (t) => {
  const { cwd, context } = await fixture(t);
  const artifactPath = join(cwd, "fixture-full-output.txt");
  const full = "fixture-full-evidence".repeat(2_000);
  await writeFile(artifactPath, full);
  const pinned = await persistBenchmarkEvidenceRef("tool:fixture-call", {
    ...context, resolveToolEvidence: () => ({ toolName: "crawl", content: "fixture-preview", artifactPath })
  });
  await rm(artifactPath);
  const snapshot = JSON.parse(await readFile(pinned, "utf8"));
  assert.equal(snapshot.content, "fixture-preview");
  assert.notEqual(snapshot.artifactPath, artifactPath);
  assert.equal(await readFile(snapshot.artifactPath, "utf8"), full);
});

test("missing temporary refs and browser element refs fail instead of becoming evidence", async (t) => {
  const { context } = await fixture(t);
  for (const ref of ["artifact:missing", "request:missing", `r-${randomUUID()}-1`, "screenshot:latest", "tool:missing", "call_missing", "e12", "page:old-page", "missing.json"]) {
    await assert.rejects(() => persistBenchmarkEvidenceRef(ref, context), /./, ref);
  }
});

test("rejects files outside task scope and symlinks escaping task roots", async (t) => {
  const { root, cwd, context } = await fixture(t);
  const outside = join(root, "outside.txt");
  await writeFile(outside, "fixture-outside");
  await symlink(outside, join(cwd, "link.txt"));
  await assert.rejects(() => persistBenchmarkEvidenceRef(outside, context), /inside this task/);
  await assert.rejects(() => persistBenchmarkEvidenceRef("link.txt", context), /inside this task/);
  await assert.rejects(() => persistBenchmarkEvidenceRef(cwd, context), /regular files/);
});

test("plain descriptions and platform references retain their existing meaning", async (t) => {
  const { context } = await fixture(t);
  assert.equal(await persistBenchmarkEvidenceRef(undefined, context), "");
  for (const ref of ["fixture plain note", "platform:flag:fixture", "flag:fixture", "https://fixture.invalid/evidence"]) {
    assert.equal(await persistBenchmarkEvidenceRef(ref, context), ref);
  }
});

test("long source references and long destinations are rejected without truncation", async (t) => {
  const { cwd, context } = await fixture(t);
  await assert.rejects(() => persistBenchmarkEvidenceRef("x".repeat(501), context), /exceeds 500/);
  const source = join(cwd, "source.txt");
  await writeFile(source, "fixture");
  const directory = join(context.directory, ...Array.from({ length: 5 }, () => "d".repeat(90)));
  await assert.rejects(() => persistBenchmarkEvidenceRef(source, { ...context, directory }), /exceeds 500/);
});

test("concurrent checkpoints deduplicate complete snapshots without temporary leftovers", async (t) => {
  const { cwd, directory, context } = await fixture(t);
  const source = join(cwd, "source.txt");
  await writeFile(source, "fixture".repeat(20_000));
  const paths = await Promise.all(Array.from({ length: 4 }, () => persistBenchmarkEvidenceRef(source, context)));
  assert.equal(new Set(paths).size, 1);
  assert.equal((await readFile(paths[0], "utf8")).length, 140_000);
  assert.deepEqual(await readdir(directory), [paths[0].slice(directory.length + 1)]);
});
