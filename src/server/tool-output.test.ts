import assert from "node:assert/strict";
import test from "node:test";
import { mkdtemp, readFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { createToolOutputStore, listToolArtifacts, toolArtifactDir, TOOL_OUTPUT_INLINE_CHARS } from "./tool-output";

test("keeps short tool output inline without creating an artifact", async () => {
  const root = await mkdtemp(join(tmpdir(), "riftx-output-"));
  const projected = await createToolOutputStore(root, "session-1").project("web_fetch", ["short"], "summary");
  assert.deepEqual(projected, { text: "short" });
});

test("stores complete long output and returns a bounded head/tail pointer", async () => {
  const root = await mkdtemp(join(tmpdir(), "riftx-output-"));
  const chunks = ["A".repeat(12_000), "B".repeat(12_000)];
  const projected = await createToolOutputStore(root, "../session").project("mcp/tool", chunks, "MCP output summary");
  assert.ok(projected.artifactPath?.startsWith(join(root, "___session")));
  assert.equal(await readFile(projected.artifactPath!, "utf8"), chunks.join(""));
  assert.match(projected.text, /MCP output summary/);
  assert.match(projected.text, /Full output/);
  assert.ok(projected.text.length < TOOL_OUTPUT_INLINE_CHARS);
  assert.deepEqual(projected.truncation, { truncated: true, totalChars: 24_000, shownChars: 14_000 });
  const refs = await listToolArtifacts(root, "../session");
  assert.equal(refs.length, 1);
  assert.equal(refs[0].path, projected.artifactPath);
  assert.equal(refs[0].size, 24_000);
});

test("child artifacts stay in a nested directory and survive parent listing", async () => {
  const root = await mkdtemp(join(tmpdir(), "riftx-output-"));
  const chunks = ["C".repeat(17_000)];
  const parent = await createToolOutputStore(root, "parent.session").project("crawl", chunks, "parent crawl");
  const child = await createToolOutputStore(root, "parent.session", "child/1").project("mcp-scan", chunks, "child scan");
  assert.ok(parent.artifactPath?.startsWith(join(root, "parent_session")));
  assert.ok(child.artifactPath?.startsWith(join(root, "parent_session", "child-child_1")));
  assert.notEqual(parent.artifactPath, child.artifactPath);
  const refs = await listToolArtifacts(root, "parent.session");
  assert.equal(refs.length, 2);
  assert.deepEqual(new Set(refs.map((item) => item.path)), new Set([parent.artifactPath, child.artifactPath]));
});

test("delete path uses the same sanitized session directory as writes", () => {
  const root = "/tmp/riftx-artifacts";
  assert.equal(toolArtifactDir(root, "../session"), join(root, "___session"));
  assert.equal(toolArtifactDir(root, "parent.session", "child/1"), join(root, "parent_session", "child-child_1"));
});
