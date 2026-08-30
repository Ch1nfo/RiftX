import test from "node:test";
import assert from "node:assert/strict";
import { mapCallResult, mcpToolName, parameterSchema, promptSnippetFor, sanitizeSegment } from "./bridge";

test("sanitize replaces invalid characters and keeps the charset", () => {
  assert.equal(sanitizeSegment("db.query:v2"), "db_query_v2");
  assert.equal(sanitizeSegment("工具"), "__");
  assert.equal(sanitizeSegment("ok-name_1"), "ok-name_1");
});

test("tool names carry the reserved prefix and cap at 64 chars", () => {
  assert.equal(mcpToolName("nmap", "scan_host"), "mcp__nmap__scan_host");
  assert.ok(mcpToolName("s".repeat(32), "t".repeat(80)).length <= 64);
  assert.ok(mcpToolName("s".repeat(32), "t".repeat(80)).startsWith("mcp__"));
});

test("names the sanitizer rewrote get a hash suffix so siblings never collide", () => {
  const dotted = mcpToolName("s", "db.query");
  const slashed = mcpToolName("s", "db/query");
  const questioned = mcpToolName("s", "db?query");
  assert.ok(dotted.startsWith("mcp__s__db_query_"), dotted);
  assert.notEqual(dotted, slashed);
  assert.notEqual(dotted, questioned);
  assert.notEqual(slashed, questioned);
  assert.equal(dotted, mcpToolName("s", "db.query"), "deterministic across sessions");
});

test("over-length names stay unique: readable prefix plus a stable hash of the full name", () => {
  const long = (tool: string) => mcpToolName("s".repeat(32), tool);
  const first = long("t".repeat(80));
  const second = long("t".repeat(79));
  assert.equal(first.length, 64);
  assert.equal(second.length, 64);
  assert.notEqual(first, second);
  assert.equal(first, long("t".repeat(80)), "hash is deterministic across sessions");
});

test("parameterSchema passes raw JSON Schema through as a clone", () => {
  const input = { type: "object", properties: { q: { type: "string" } }, required: ["q"], $defs: { x: { type: "string" } } };
  const out = parameterSchema(input);
  assert.deepEqual(out, input);
  (out.properties as Record<string, unknown>).q = { type: "number" };
  assert.deepEqual((input.properties as Record<string, unknown>).q, { type: "string" });
});

test("parameterSchema falls back for missing or non-object schemas", () => {
  assert.deepEqual(parameterSchema(undefined), { type: "object", properties: {} });
  assert.deepEqual(parameterSchema({ type: "string" }), { type: "object", properties: {} });
  assert.deepEqual(parameterSchema({ type: "object" }), { type: "object", properties: {} });
  assert.deepEqual(parameterSchema({ type: "object", properties: "junk" }), { type: "object", properties: {} });
});

test("promptSnippet lists required first and marks optional with ?", () => {
  const schema = { type: "object", properties: { limit: { type: "number" }, query: { type: "string" }, mode: { type: "string" } }, required: ["query"] };
  assert.equal(promptSnippetFor("mcp__db__search", schema), "mcp__db__search(query, limit?, mode?)");
  const many = { type: "object", properties: Object.fromEntries("abcdefg".split("").map((key) => [key, { type: "string" }])) };
  assert.equal(promptSnippetFor("t", many), "t(a?, b?, c?, d?, e?, f?, …)");
  assert.equal(promptSnippetFor("t", {}), "t()");
});

test("mapCallResult maps text, image, resource_link, and embedded resources", () => {
  const mapped = mapCallResult({
    content: [
      { type: "text", text: "hello" },
      { type: "image", data: "aGk=", mimeType: "image/png" },
      { type: "resource_link", name: "notes", uri: "file:///tmp/notes.txt" },
      { type: "resource", resource: { uri: "file:///tmp/x", mimeType: "text/plain", text: "embedded text" } },
      { type: "resource", resource: { uri: "file:///tmp/y", mimeType: "application/pdf", blob: "" } },
      { type: "audio", data: "" }
    ]
  }, "srv", "t");
  assert.deepEqual(mapped.content[0], { type: "text", text: "hello" });
  assert.deepEqual(mapped.content[1], { type: "image", data: "aGk=", mimeType: "image/png" });
  assert.equal(mapped.content[2]?.type, "text");
  assert.ok((mapped.content[2] as { text: string }).text.includes("notes"));
  assert.deepEqual(mapped.content[3], { type: "text", text: "embedded text" });
  assert.ok((mapped.content[4] as { text: string }).text.includes("resource blob"));
  assert.deepEqual(mapped.content[5], { type: "text", text: "[unsupported audio content]" });
  assert.deepEqual(mapped.details, { server: "srv", tool: "t" });
});

test("mapCallResult uses structuredContent for empty results", () => {
  const mapped = mapCallResult({ content: [], structuredContent: { rows: 2 } }, "srv", "t");
  assert.equal(mapped.content[0]?.type, "text");
  assert.equal((mapped.content[0] as { text: string }).text, '{"rows":2}');
  assert.deepEqual(mapped.details, { server: "srv", tool: "t" });
  assert.deepEqual(mapCallResult({}, "srv", "t").content, [{ type: "text", text: "(no content)" }]);
});

test("mapCallResult throws on MCP isError", () => {
  assert.throws(() => mapCallResult({ isError: true, content: [{ type: "text", text: "boom" }] }, "srv", "t"), /failed: boom/);
});

test("whole-result budgets cap cumulative text, images, and part count", () => {
  // Cumulative text: three 40k parts exceed the 100k total → the third is cut.
  const chunk = "x".repeat(40_000);
  const text = mapCallResult({ content: [{ type: "text", text: chunk }, { type: "text", text: chunk }, { type: "text", text: chunk }] }, "srv", "t");
  const textOut = text.content.map((part) => (part as { text?: string }).text ?? "").join("");
  assert.ok(textOut.length < 120_000, `expected truncation, got ${textOut.length}`);
  assert.match(textOut, /truncated/);
  // Cumulative images: 3M + 3M base64 exceeds the 5M shared budget.
  const image = mapCallResult({ content: [
    { type: "image", data: "a".repeat(3_000_000), mimeType: "image/png" },
    { type: "image", data: "b".repeat(3_000_000), mimeType: "image/png" }
  ] }, "srv", "t");
  assert.equal(image.content.filter((part) => part.type === "image").length, 1);
  assert.match((image.content[1] as { text: string }).text, /image omitted/);
  // Part count: 60 tiny parts → 50 kept, rest noted.
  const many = mapCallResult({ content: Array.from({ length: 60 }, () => ({ type: "text", text: "p" })) }, "srv", "t");
  assert.equal(many.content.length, 51);
  assert.match((many.content[50] as { text: string }).text, /10 further content parts omitted/);
  // Structured output over the budget is omitted without serializing it fully.
  const structured = mapCallResult({ content: [], structuredContent: { blob: "z".repeat(150_000) } }, "srv", "t");
  assert.match((structured.content[0] as { text: string }).text, /structured output omitted/);
  // The isError path bounds the joined text too.
  assert.throws(() => mapCallResult({ isError: true, content: [{ type: "text", text: "e".repeat(150_000) }] }, "srv", "t"), /failed: e{100000}/);
  let thrownLength = 0;
  try {
    mapCallResult({ isError: true, content: Array.from({ length: 10 }, () => ({ type: "text", text: "f".repeat(60_000) })) }, "srv", "t");
  } catch (error) {
    thrownLength = (error as Error).message.length;
  }
  assert.ok(thrownLength > 0 && thrownLength < 105_000, `bounded error message, got ${thrownLength}`);
});
