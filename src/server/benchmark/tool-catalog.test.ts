import assert from "node:assert/strict";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { createBenchmarkToolCatalogTool } from "./tool-catalog";

type CatalogResult = { content: { text: string }[]; details: { source: string; groups: { category: string; items: { name: string; detail: string }[] }[] } };

async function execute(tool: ReturnType<typeof createBenchmarkToolCatalogTool>, params: Record<string, unknown>) {
  const ctx = {} as Parameters<typeof tool.execute>[4];
  return tool.execute("test-call", params as Parameters<typeof tool.execute>[1], undefined, undefined, ctx);
}

test("renders the image catalog with versions and wordlist paths, skipping unavailable entries", async () => {
  const dir = await mkdtemp(join(tmpdir(), "riftx-catalog-"));
  try {
    const catalogPath = join(dir, "tool-catalog.json");
    await writeFile(catalogPath, JSON.stringify({ entries: [
      { kind: "command", name: "ffuf", category: "web", path: "/usr/bin/ffuf", version: "2.3.0", available: true },
      { kind: "python", name: "pwn", category: "reverse", available: true },
      { kind: "wordlist", name: "10k-most-common.txt", category: "passwords", path: "/opt/wordlists/Passwords/10k-most-common.txt", available: true },
      { kind: "command", name: "nikto", category: "web", available: false }
    ] }));
    const tool = createBenchmarkToolCatalogTool(catalogPath);
    const result = await execute(tool, { refresh: true }) as CatalogResult;
    assert.equal(result.details.source, "image-catalog");
    assert.match(result.content[0].text, /## web\n- ffuf: command 2\.3\.0/);
    assert.match(result.content[0].text, /- pwn: python module/);
    assert.match(result.content[0].text, /10k-most-common\.txt: wordlist \/opt\/wordlists\/Passwords\/10k-most-common\.txt/);
    assert.doesNotMatch(result.content[0].text, /nikto/);
    // A second call without refresh reuses the process cache.
    const cached = await execute(tool, {}) as CatalogResult;
    assert.equal(cached.content[0].text, result.content[0].text);
  } finally {
    await rm(dir, { recursive: true, force: true });
  }
});

test("falls back to PATH probes when the image catalog is absent", async () => {
  const catalogPath = join(tmpdir(), `riftx-no-catalog-${process.pid}-${Date.now()}.json`);
  const tool = createBenchmarkToolCatalogTool(catalogPath);
  const result = await execute(tool, { refresh: true }) as CatalogResult;
  assert.equal(result.details.source, "path-probe");
  assert.match(result.content[0].text, /<riftx-benchmark-tool-catalog>/);
  assert.match(result.content[0].text, /## web/);
  // Every probed entry reports an availability verdict.
  for (const group of result.details.groups) {
    for (const item of group.items) assert.match(item.detail, /^(available|missing)$/);
  }
});
