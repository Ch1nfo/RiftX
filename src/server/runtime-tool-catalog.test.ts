import assert from "node:assert/strict";
import test from "node:test";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import {
  buildRuntimeToolIndex, getRuntimeToolCatalog, MAX_RUNTIME_TOOL_INDEX_CHARS, MAX_TOOL_INVENTORY_PAGE_CHARS,
  queryRuntimeToolCatalog, type RuntimeToolCatalogState, type RuntimeToolEntry
} from "./runtime-tool-catalog";

function state(entries: RuntimeToolEntry[]): RuntimeToolCatalogState {
  return { status: "available", path: "/opt/riftx/tool-catalog.json", catalog: { schemaVersion: 1, entries } };
}

test("catalog loader accepts only confirmed structured metadata and distinguishes missing from invalid", async () => {
  const root = await mkdtemp(join(tmpdir(), "riftx-tool-catalog-"));
  const path = join(root, "catalog.json");
  try {
    assert.equal((await getRuntimeToolCatalog(path)).status, "missing");
    await writeFile(path, JSON.stringify({ schemaVersion: 1, generatedAt: "2026-09-13T00:00:00Z", entries: [
      { kind: "wordlist", name: "fixture-list", category: "passwords", path: "/nonexistent/fixture-list.txt", sizeBytes: 42, available: true, verification: "metadata" },
      { kind: "python", name: "fixture_module", category: "math", path: "/nonexistent/fixture_module.py", available: true, verification: "import" }
    ], unavailable: [{ name: "fixture_missing" }] }));
    const loaded = await getRuntimeToolCatalog(path);
    assert.equal(loaded.status, "available");
    if (loaded.status !== "available") throw new Error("fixture_catalog_unavailable");
    assert.equal(loaded.catalog.entries.length, 2);
    assert.equal(queryRuntimeToolCatalog(loaded, { name: "fixture-list" }).items[0].path, "/nonexistent/fixture-list.txt");
    await writeFile(path, JSON.stringify({ schemaVersion: 1, entries: [{ kind: "command", name: "fixture", category: "runtime", available: false }] }));
    assert.equal((await getRuntimeToolCatalog(path)).status, "unreadable");
    await writeFile(path, "{");
    assert.equal((await getRuntimeToolCatalog(path)).status, "unreadable");
    await writeFile(path, JSON.stringify({ schemaVersion: 2, entries: [] }));
    assert.equal((await getRuntimeToolCatalog(path)).status, "unreadable");
    await writeFile(path, " ".repeat(2 * 1024 * 1024 + 1));
    const oversized = await getRuntimeToolCatalog(path);
    assert.equal(oversized.status, "unreadable");
    if (oversized.status === "unreadable") assert.equal(oversized.reason, "catalog_too_large");
  } finally { await rm(root, { recursive: true, force: true }); }
});

test("inventory filters match category, exact name, kind and text without executing entries", () => {
  const catalog = state([
    { kind: "command", name: "fixture-sql", category: "databases", path: "/does/not/exist", description: "Local SQL client", verification: "path" },
    { kind: "python", name: "fixture_sql", category: "databases", description: "SQL module", verification: "import" },
    { kind: "wordlist", name: "fixture-list", category: "passwords", path: "/not/read/fixture.txt", verification: "metadata" }
  ]);
  assert.equal(queryRuntimeToolCatalog(catalog, { category: "DATABASES" }).total, 2);
  assert.equal(queryRuntimeToolCatalog(catalog, { category: "databases", kind: "command", query: "local" }).items[0].name, "fixture-sql");
  assert.equal(queryRuntimeToolCatalog(catalog, { name: "FIXTURE_SQL" }).total, 1);
  assert.equal(queryRuntimeToolCatalog(catalog, { name: "fixture" }).total, 0);
  assert.equal(queryRuntimeToolCatalog(catalog, { query: "/not/read" }).items[0].kind, "wordlist");
});

test("inventory pages are bounded and preserve every long path without gaps", () => {
  const entries: RuntimeToolEntry[] = Array.from({ length: 47 }, (_, index) => ({
    kind: "command", name: `fixture-${index}`, category: "runtime", path: `/fixture/${"segment/".repeat(110)}${index}`, description: "fixture_".repeat(80)
  }));
  const catalog = state(entries);
  const actual: RuntimeToolEntry[] = [];
  let offset = 0;
  let pages = 0;
  for (;;) {
    const page = queryRuntimeToolCatalog(catalog, { offset, limit: 40 });
    assert.ok(JSON.stringify(page).length <= MAX_TOOL_INVENTORY_PAGE_CHARS);
    assert.ok(page.items.length > 0);
    actual.push(...page.items);
    pages++;
    if (page.nextOffset === null) break;
    assert.ok(page.nextOffset > offset);
    offset = page.nextOffset;
  }
  assert.ok(pages > 2);
  assert.deepEqual(actual, entries);
  assert.equal(queryRuntimeToolCatalog(catalog, { offset: entries.length }).items.length, 0);
  assert.throws(() => queryRuntimeToolCatalog(catalog, { offset: entries.length + 1 }), /offset/);
});

test("each challenge gets a bounded current preview and releasing it clears discovery", () => {
  const entries: RuntimeToolEntry[] = Array.from({ length: 120 }, (_, index) => ({
    kind: index % 3 === 0 ? "python" : "command", name: `fixture_tool_${index}`, category: `category_${index % 20}`
  }));
  const catalog = state(entries);
  const first = buildRuntimeToolIndex(catalog, "fixture_a");
  const next = buildRuntimeToolIndex(catalog, "fixture_b");
  assert.ok(first.length <= MAX_RUNTIME_TOOL_INDEX_CHARS && next.length <= MAX_RUNTIME_TOOL_INDEX_CHARS);
  assert.equal(JSON.parse(first).activeChallenge, "fixture_a");
  assert.equal(JSON.parse(next).activeChallenge, "fixture_b");
  assert.equal(JSON.parse(next).lookup.tool, "tool_inventory");
  assert.equal(JSON.parse(next).catalogPath, "/opt/riftx/tool-catalog.json");
  assert.equal(JSON.parse(next).previewOnly, true);
  assert.equal(buildRuntimeToolIndex(catalog), "");
  assert.equal(buildRuntimeToolIndex(catalog, null), "");
});

test("missing catalogs describe unconfirmed availability and keep the lookup entry", () => {
  const unavailable: RuntimeToolCatalogState = { status: "missing", path: "/opt/riftx/tool-catalog.json", reason: "catalog_not_found" };
  const page = queryRuntimeToolCatalog(unavailable);
  assert.equal(page.status, "missing");
  assert.match(page.note, /unconfirmed/);
  assert.deepEqual(page.items, []);
  const preview = JSON.parse(buildRuntimeToolIndex(unavailable, "fixture"));
  assert.equal(preview.catalogStatus, "missing");
  assert.equal(preview.lookup.tool, "tool_inventory");
  assert.equal(preview.counts, undefined);
});
