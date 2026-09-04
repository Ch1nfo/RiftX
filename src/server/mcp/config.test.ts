import test from "node:test";
import assert from "node:assert/strict";
import { mcpServersValidationError, normalizeMcpServers } from "./config";

test("accepts valid stdio and http servers", () => {
  const servers = [
    { name: "nmap", transport: "stdio", command: "npx", args: ["-y", "mcp-nmap"], env: { HOME: "/tmp" } },
    { name: "internal", transport: "http", url: "http://127.0.0.1:8080/mcp", headers: { Authorization: "Bearer x" } }
  ];
  assert.equal(mcpServersValidationError(servers), null);
  assert.deepEqual(normalizeMcpServers(servers), [
    { name: "nmap", transport: "stdio", command: "npx", args: ["-y", "mcp-nmap"], env: { HOME: "/tmp" } },
    { name: "internal", transport: "http", url: "http://127.0.0.1:8080/mcp", headers: { Authorization: "Bearer x" } }
  ]);
});

test("validates and preserves MCP role visibility and raw-name filters", () => {
  const servers = [{
    name: "scanner",
    transport: "stdio",
    command: "scan",
    visibility: ["child"],
    includeTools: ["scan_*", "query"],
    excludeTools: ["*_admin"]
  }];
  assert.equal(mcpServersValidationError(servers), null);
  assert.deepEqual(normalizeMcpServers(servers), [{
    ...servers[0],
    transport: "stdio",
    args: [],
    env: {}
  }]);
  assert.match(String(mcpServersValidationError([{ name: "x", command: "x", visibility: ["main", "main"] }])), /visibility/);
  assert.match(String(mcpServersValidationError([{ name: "x", command: "x", visibility: ["other"] }])), /visibility/);
  assert.match(String(mcpServersValidationError([{ name: "x", command: "x", includeTools: [""] }])), /includeTools/);
});

test("accepts the ecosystem 'type' key and canonicalizes it to 'transport'", () => {
  const pasted = [{ name: "yakit", type: "stdio", command: "/path/to/yak", args: ["mcp", "--transport", "stdio"] }];
  assert.equal(mcpServersValidationError(pasted), null);
  assert.deepEqual(normalizeMcpServers(pasted), [
    { name: "yakit", transport: "stdio", command: "/path/to/yak", args: ["mcp", "--transport", "stdio"], env: {} }
  ]);
});

test("command-only entries (official Claude stdio form) infer stdio; url-only does not infer http", () => {
  const claude = [{ name: "filesystem", command: "npx", args: ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"] }];
  assert.equal(mcpServersValidationError(claude), null);
  assert.deepEqual(normalizeMcpServers(claude), [
    { name: "filesystem", transport: "stdio", command: "npx", args: ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"], env: {} }
  ]);
  assert.match(String(mcpServersValidationError([{ name: "remote", url: "http://x/mcp" }])), /transport/);
});

test("rejects bad names, transports, commands, urls, and shapes", () => {
  assert.match(String(mcpServersValidationError([{ name: "bad name", transport: "stdio", command: "x" }])), /name/);
  assert.match(String(mcpServersValidationError([{ name: "x".repeat(33), transport: "stdio", command: "x" }])), /name/);
  assert.match(String(mcpServersValidationError([{ name: "a", transport: "grpc", command: "x" }])), /transport/);
  assert.match(String(mcpServersValidationError([{ name: "a", transport: "stdio" }])), /command/);
  assert.match(String(mcpServersValidationError([{ name: "a", transport: "stdio", command: "x", args: [1] }])), /args/);
  assert.match(String(mcpServersValidationError([{ name: "a", transport: "stdio", command: "x", env: { A: 1 } }])), /env/);
  assert.match(String(mcpServersValidationError([{ name: "a", transport: "http" }])), /url/);
  assert.match(String(mcpServersValidationError([{ name: "a", transport: "http", url: "ftp://example.com" }])), /url/);
  assert.match(String(mcpServersValidationError([{ name: "a", transport: "http", url: "not a url" }])), /url/);
  assert.match(String(mcpServersValidationError([{ name: "a", transport: "http", url: "http://x/y", headers: { A: 1 } }])), /headers/);
  assert.match(String(mcpServersValidationError("nope")), /array/);
  assert.match(String(mcpServersValidationError([{ name: "a", transport: "stdio", command: "x" }, { name: "a", transport: "http", url: "http://x" }])), /duplicate/);
});

test("rejects lists beyond the server cap", () => {
  const many = Array.from({ length: 21 }, (_, index) => ({ name: `s${index}`, transport: "stdio" as const, command: "x" }));
  assert.match(String(mcpServersValidationError(many)), /limited to 20/);
  assert.equal(mcpServersValidationError(many.slice(0, 20)), null);
});

test("normalize truncates hand-edited configs to the same cap", () => {
  const many = Array.from({ length: 25 }, (_, index) => ({ name: `s${index}`, transport: "stdio" as const, command: "x" }));
  assert.equal(normalizeMcpServers(many).length, 20);
});

test("undefined passes validation and normalizes to empty", () => {
  assert.equal(mcpServersValidationError(undefined), null);
  assert.deepEqual(normalizeMcpServers(undefined), []);
  assert.deepEqual(normalizeMcpServers("junk"), []);
});

test("normalize drops invalid entries and duplicates, fills defaults", () => {
  const out = normalizeMcpServers([
    { name: "good", transport: "stdio", command: "run" },
    { name: "bad", transport: "stdio" },
    "garbage",
    { name: "good", transport: "stdio", command: "other" }
  ]);
  assert.deepEqual(out, [{ name: "good", transport: "stdio", command: "run", args: [], env: {} }]);
});
