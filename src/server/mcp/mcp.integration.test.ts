import test from "node:test";
import assert from "node:assert/strict";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { InMemoryTransport } from "@modelcontextprotocol/sdk/inMemory.js";
import { z } from "zod";
import { wireClient } from "./manager";
import { buildMcpTools } from "./tools";
import type { McpServerConfig } from "@/lib/types";
import { RIFTX_VERSION } from "@/lib/version";

/** End-to-end against the real MCP SDK over an in-process transport pair — proves the schema bridge and result mapping against the actual wire protocol. */

const config: McpServerConfig = { name: "demo", transport: "stdio", command: "unused-in-test", args: [], env: {} };

async function startDemoServer() {
  const server = new McpServer({ name: "demo", version: "1.0.0" }, { capabilities: { tools: {} } });
  // The server side registers with a Zod shape; the wire (and our client
  // bridge) still carries the plain JSON Schema that real servers send.
  server.registerTool("echo", {
    description: "Echo a message",
    inputSchema: { message: z.string() }
  }, async ({ message }) => ({ content: [{ type: "text", text: `echo: ${message}` }] }));
  server.registerTool("fail", { description: "Always fails" }, async () => ({ content: [{ type: "text", text: "kaboom" }], isError: true }));
  const [clientTransport, serverTransport] = InMemoryTransport.createLinkedPair();
  await server.connect(serverTransport);
  const client = new Client({ name: "riftx", version: RIFTX_VERSION }, { capabilities: {} });
  const handle = await wireClient(client, clientTransport, config, 2000);
  return { server, handle };
}

test("connect → listTools → buildMcpTools → execute works end to end with a raw JSON Schema", async () => {
  const { server, handle } = await startDemoServer();
  try {
    const tools = buildMcpTools({ state: "connected", key: "k", config, handle, refs: 0 });
    assert.deepEqual(tools.map((tool) => tool.name), ["mcp__demo__echo", "mcp__demo__fail"]);
    const echo = tools[0];
    assert.equal(echo.promptSnippet, "mcp__demo__echo(message)");
    // Passthrough keeps the wire JSON Schema verbatim, including the $schema and
    // additionalProperties keys the SDK's zod conversion adds.
    const parameters = echo.parameters as { properties?: unknown; required?: string[]; $schema?: string };
    assert.deepEqual(parameters.properties, { message: { type: "string" } });
    assert.deepEqual(parameters.required, ["message"]);
    assert.equal(typeof parameters.$schema, "string");
    const result = await echo.execute("call-1", { message: "hi" }, undefined, undefined, {} as never);
    assert.deepEqual(result.content, [{ type: "text", text: "echo: hi" }]);
    assert.deepEqual(result.details, { server: "demo", tool: "echo" });
    await assert.rejects(() => tools[1].execute("call-2", {}, undefined, undefined, {} as never), /kaboom/);
  } finally {
    await handle.close();
    await server.close();
  }
});

test("calls on a closed connection fail with the reopen hint", async () => {
  const { server, handle } = await startDemoServer();
  try {
    const [tool] = buildMcpTools({ state: "connected", key: "k", config, handle, refs: 0 });
    await handle.close();
    assert.equal(handle.dead, true, "client.onclose must flag the handle dead so the next acquire reconnects");
    await assert.rejects(() => tool.execute("call-1", { message: "x" }, undefined, undefined, {} as never), /reopen the session/);
  } finally {
    await server.close();
  }
});
