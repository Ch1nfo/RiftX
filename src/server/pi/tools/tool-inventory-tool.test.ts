import assert from "node:assert/strict";
import test from "node:test";
import { Value } from "@sinclair/typebox/value";
import type { TSchema } from "@sinclair/typebox";
import { createToolInventoryTool } from "./tool-inventory-tool";
import { MAX_TOOL_INVENTORY_PAGE_CHARS, type RuntimeToolCatalogState } from "../../runtime-tool-catalog";

const catalog: RuntimeToolCatalogState = {
  status: "available", path: "/opt/riftx/tool-catalog.json", catalog: { schemaVersion: 1, entries: [
    { kind: "command", name: "fixture_a", category: "runtime", path: "/not/executed/a" },
    { kind: "python", name: "fixture_b", category: "math", path: "/not/imported/b.py" }
  ] }
};

test("inventory schema and direct execution reject invalid inputs", async () => {
  const tool = createToolInventoryTool(async () => catalog);
  const parameters = tool.parameters as unknown as TSchema;
  for (const input of [{ limit: 0 }, { limit: 41 }, { offset: -1 }, { offset: 1.5 }, { kind: "shell" }, { query: "a".repeat(201) }, { category: "" }, { name: "a".repeat(201) }, { command: "fixture" }]) {
    assert.equal(Value.Check(parameters, input), false);
    await assert.rejects(tool.execute("fixture", input, undefined, undefined, {} as Parameters<typeof tool.execute>[4]));
  }
  for (const input of [{ query: " " }, { query: "fixture\n" }, { offset: Number.MAX_SAFE_INTEGER + 1 }]) {
    await assert.rejects(tool.execute("fixture", input, undefined, undefined, {} as Parameters<typeof tool.execute>[4]));
  }
  assert.equal(Value.Check(parameters, { category: "runtime", kind: "command", offset: 0, limit: 20 }), true);
});

test("inventory tool shares one catalog snapshot and returns model-visible pages", async () => {
  let reads = 0;
  const tool = createToolInventoryTool(async () => { reads++; return catalog; });
  const first = await tool.execute("fixture", { limit: 1 }, undefined, undefined, {} as Parameters<typeof tool.execute>[4]);
  const firstText = (first.content[0] as { text: string }).text;
  assert.ok(firstText.length <= MAX_TOOL_INVENTORY_PAGE_CHARS);
  const firstPage = JSON.parse(firstText);
  assert.equal(firstPage.items.length, 1);
  assert.equal(firstPage.nextOffset, 1);
  const next = await tool.execute("fixture", { offset: firstPage.nextOffset }, undefined, undefined, {} as Parameters<typeof tool.execute>[4]);
  assert.equal(JSON.parse((next.content[0] as { text: string }).text).items[0].name, "fixture_b");
  assert.equal(reads, 1);
  assert.deepEqual(first.details, firstPage);
});
