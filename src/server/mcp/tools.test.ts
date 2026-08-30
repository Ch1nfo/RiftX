import test from "node:test";
import assert from "node:assert/strict";
import { buildMcpTools } from "./tools";
import type { McpServerEntry, McpServerHandle } from "./manager";

const stdioConfig = { name: "tools", transport: "stdio" as const, command: "run", args: [], env: {} };

function handleWith(tools: { name: string; description?: string; inputSchema?: unknown }[], calls: { name: string; args: unknown; signal?: AbortSignal }[] = []): McpServerHandle {
  return {
    tools: tools.map((tool) => ({ name: tool.name, description: tool.description, inputSchema: tool.inputSchema ?? { type: "object", properties: {} } })),
    call: async (rawName, args, signal) => {
      calls.push({ name: rawName, args, signal });
      return { content: [{ type: "text", text: `echo:${rawName}` }] };
    },
    close: async () => undefined
  };
}

test("builds named tools with prompt snippets and raw schemas; sanitize siblings stay distinct", () => {
  const entry: McpServerEntry = {
    state: "connected",
    key: "k",
    config: stdioConfig,
    handle: handleWith([
      { name: "db.query", description: "Run a query", inputSchema: { type: "object", properties: { q: { type: "string" } }, required: ["q"] } },
      { name: "db/query" },
      { name: "db?query" }
    ]),
    refs: 0
  };
  const tools = buildMcpTools(entry);
  // All three survive: the hash suffix keeps sanitized siblings distinct.
  assert.equal(tools.length, 3);
  assert.ok(tools.every((tool) => tool.name.startsWith("mcp__tools__db_query_")));
  assert.equal(new Set(tools.map((tool) => tool.name)).size, 3);
  assert.equal(tools[0].label, "tools: db.query");
  assert.match(tools[0].description, /Run a query/);
  assert.match(tools[0].description, /stdio: run/);
  assert.equal(String(tools[0].promptSnippet).slice(0, 20), "mcp__tools__db_query");
  assert.deepEqual(tools[0].parameters, { type: "object", properties: { q: { type: "string" } }, required: ["q"] });
});

test("error entries produce no tools", () => {
  const entry: McpServerEntry = { state: "error", key: "k", config: stdioConfig, error: "down", refs: 0 };
  assert.deepEqual(buildMcpTools(entry), []);
});

test("execute calls the raw tool name with params and signal, and maps the result", async () => {
  const calls: { name: string; args: unknown; signal?: AbortSignal }[] = [];
  const entry: McpServerEntry = { state: "connected", key: "k", config: stdioConfig, handle: handleWith([{ name: "scan" }], calls), refs: 0 };
  const [tool] = buildMcpTools(entry);
  const controller = new AbortController();
  const result = await tool.execute("call-1", { host: "x" }, controller.signal, undefined, {} as Parameters<typeof tool.execute>[4]);
  assert.deepEqual(result.content, [{ type: "text", text: "echo:scan" }]);
  assert.equal(calls[0].name, "scan");
  assert.deepEqual(calls[0].args, { host: "x" });
  assert.equal(calls[0].signal, controller.signal);
});

test("execute surfaces MCP isError as a thrown error", async () => {
  const entry: McpServerEntry = {
    state: "connected",
    key: "k",
    config: stdioConfig,
    handle: {
      tools: [{ name: "boom", inputSchema: { type: "object", properties: {} } }],
      call: async () => ({ isError: true, content: [{ type: "text", text: "kaboom" }] }),
      close: async () => undefined
    },
    refs: 0
  };
  const [tool] = buildMcpTools(entry);
  await assert.rejects(() => tool.execute("call-1", {}, undefined, undefined, {} as Parameters<typeof tool.execute>[4]), /kaboom/);
});
