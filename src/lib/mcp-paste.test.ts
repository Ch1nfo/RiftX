import test from "node:test";
import assert from "node:assert/strict";
import { parseMcpServersDraft } from "./mcp-paste";
import { mcpServersValidationError } from "@/server/mcp/config";

test("empty and whitespace drafts are empty lists, not JSON errors", () => {
  assert.deepEqual(parseMcpServersDraft(""), []);
  assert.deepEqual(parseMcpServersDraft("   \n "), []);
});

test("invalid JSON returns null", () => {
  assert.equal(parseMcpServersDraft("{oops"), null);
});

test("array form passes through unchanged", () => {
  const array = [{ name: "a", transport: "stdio", command: "x", args: [], env: {} }];
  assert.deepEqual(parseMcpServersDraft(JSON.stringify(array)), array);
});

test("the official Claude wrapper config (no type field) converts and validates", () => {
  const pasted = parseMcpServersDraft(`{
    "mcpServers": {
      "filesystem": {
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]
      }
    }
  }`);
  assert.deepEqual(pasted, [{ name: "filesystem", command: "npx", args: ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"] }]);
  // command-only entries infer stdio — the paste must survive API validation.
  assert.equal(mcpServersValidationError(pasted), null);
});

test("record form without a wrapper converts to the array form", () => {
  const pasted = parseMcpServersDraft(`{"yakit": {"type": "stdio", "command": "/bin/yak", "args": ["mcp"]}}`);
  assert.deepEqual(pasted, [{ name: "yakit", type: "stdio", command: "/bin/yak", args: ["mcp"] }]);
});
