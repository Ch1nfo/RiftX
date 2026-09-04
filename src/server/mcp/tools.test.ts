import test from "node:test";
import assert from "node:assert/strict";
import { buildMcpTools, isMcpToolVisible } from "./tools";
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

test("filters tools by Agent role and raw-name include/exclude patterns", () => {
  const config = { ...stdioConfig, visibility: ["child" as const], includeTools: ["scan_*", "query"], excludeTools: ["*_admin"] };
  assert.equal(isMcpToolVisible(config, "scan_host", "main"), false);
  assert.equal(isMcpToolVisible(config, "scan_host", "child"), true);
  assert.equal(isMcpToolVisible(config, "scan_admin", "child"), false);
  assert.equal(isMcpToolVisible(config, "unrelated", "child"), false);
  const entry: McpServerEntry = {
    state: "connected",
    key: "k",
    config,
    handle: handleWith([{ name: "scan_host" }, { name: "scan_admin" }, { name: "query" }, { name: "other" }]),
    refs: 0
  };
  assert.deepEqual(buildMcpTools(entry, { audience: "child" }).map((tool) => tool.label), ["tools: scan_host", "tools: query"]);
  assert.deepEqual(buildMcpTools(entry, { audience: "main" }), []);
});

test("externalizes long MCP text while retaining image content", async () => {
  const writes: string[][] = [];
  const entry: McpServerEntry = {
    state: "connected",
    key: "k",
    config: stdioConfig,
    handle: {
      tools: [{ name: "large", inputSchema: { type: "object", properties: {} } }],
      call: async () => ({ content: [{ type: "text", text: "x".repeat(20_000) }, { type: "image", data: "aGk=", mimeType: "image/png" }] }),
      close: async () => undefined
    },
    refs: 0
  };
  const [tool] = buildMcpTools(entry, { outputStore: {
    project: async (_name, chunks) => {
      writes.push([...chunks]);
      return { text: "summary + /artifact/path", artifactPath: "/artifact/path", truncation: { truncated: true, totalChars: 20_001, shownChars: 100 } };
    }
  } });
  const result = await tool.execute("call", {}, undefined, undefined, {} as Parameters<typeof tool.execute>[4]);
  assert.equal(writes[0].join("").length, 20_001);
  assert.deepEqual(result.content[0], { type: "text", text: "summary + /artifact/path" });
  assert.equal(result.content[1].type, "image");
  assert.equal((result.details as { artifactPath?: string }).artifactPath, "/artifact/path");
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
