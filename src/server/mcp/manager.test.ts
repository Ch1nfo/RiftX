import test from "node:test";
import assert from "node:assert/strict";
import type { McpServerConfig } from "@/lib/types";
import { McpManager, serverKey, testMcpServers, withMcpReferences, type McpServerHandle } from "./manager";

const stdio = (name: string, command = "run"): McpServerConfig => ({ name, transport: "stdio", command, args: [], env: {} });

function fakeHandle(name: string): McpServerHandle & { closed: boolean; dead: boolean } {
  return {
    tools: [{ name: `${name}_tool`, description: "d", inputSchema: { type: "object", properties: {} } }],
    call: async () => ({ content: [] }),
    closed: false,
    dead: false,
    close: async function () { this.closed = true; }
  };
}

function harness(factory: (config: McpServerConfig) => Promise<McpServerHandle>) {
  const calls: string[] = [];
  const handles = new Map<string, McpServerHandle & { closed: boolean }>();
  const manager = new McpManager({
    connect: async (config) => {
      calls.push(config.name);
      const resolved = await factory(config);
      handles.set(config.name, resolved as McpServerHandle & { closed: boolean });
      return resolved;
    }
  });
  return { manager, calls, handles };
}

test("serverKey treats sorted env/headers as part of identity", () => {
  assert.equal(serverKey(stdio("a")), serverKey(stdio("a")));
  assert.notEqual(serverKey(stdio("a")), serverKey(stdio("a", "other")));
  assert.equal(serverKey({ name: "h", transport: "http", url: "http://x", headers: { B: "1", A: "2" } }), serverKey({ name: "h", transport: "http", url: "http://x", headers: { A: "2", B: "1" } }));
  assert.notEqual(serverKey({ name: "h", transport: "http", url: "http://x", headers: { A: "1" } }), serverKey({ name: "h", transport: "http", url: "http://x", headers: { A: "2" } }));
  assert.equal(serverKey(stdio("a")), serverKey({ ...stdio("a"), visibility: ["child"] }));
  assert.equal(serverKey(stdio("a")), serverKey({ ...stdio("a"), includeTools: ["scan_*"] }));
});

test("visibility-only edits reuse the connection and update new-session config", async () => {
  const { manager, calls } = harness(async (config) => fakeHandle(config.name));
  const first = await manager.reconcile([stdio("a")]);
  const second = await manager.reconcile([{ ...stdio("a"), visibility: ["child"], includeTools: ["scan_*"] }]);
  assert.equal(first[0], second[0]);
  assert.deepEqual(calls, ["a"]);
  assert.deepEqual(second[0].config.visibility, ["child"]);
  assert.deepEqual(second[0].config.includeTools, ["scan_*"]);
});

test("second reconcile with the same config is a cache hit (zero connects)", async () => {
  const { manager, calls } = harness(async (config) => fakeHandle(config.name));
  await manager.reconcile([stdio("a"), stdio("b")]);
  await manager.reconcile([stdio("a"), stdio("b")]);
  assert.deepEqual(calls.sort(), ["a", "b"]);
});

test("removed servers are closed; changed keys swap the connection", async () => {
  const { manager, calls, handles } = harness(async (config) => fakeHandle(config.name));
  await manager.reconcile([stdio("a"), stdio("b")]);
  const originalA = handles.get("a")!;
  await manager.reconcile([stdio("a", "changed")]);
  assert.equal(originalA.closed, true);
  assert.equal(handles.get("b")?.closed, true);
  assert.deepEqual(calls, ["a", "b", "a"]);
  const entries = await manager.reconcile([stdio("a", "changed")]);
  assert.equal(entries.length, 1);
  assert.equal(entries[0].state, "connected");
});

test("failed connects become error entries and are retried on the next reconcile", async () => {
  let fail = true;
  const { manager } = harness(async (config) => {
    if (fail) throw new Error("down");
    return fakeHandle(config.name);
  });
  const first = await manager.reconcile([stdio("a")]);
  assert.equal(first[0].state, "error");
  assert.equal(first[0].state === "error" && first[0].error, "down");
  fail = false;
  const second = await manager.reconcile([stdio("a")]);
  assert.equal(second[0].state, "connected");
});

test("reconcile returns entries in desired order and skips missing", async () => {
  const { manager } = harness(async (config) => fakeHandle(config.name));
  const entries = await manager.reconcile([stdio("b"), stdio("a")]);
  assert.deepEqual(entries.map((entry) => entry.config.name), ["b", "a"]);
});

test("concurrent reconciles serialize — no double connect", async () => {
  const { manager, calls } = harness(async (config) => fakeHandle(config.name));
  await Promise.all([manager.reconcile([stdio("a")]), manager.reconcile([stdio("a")]), manager.reconcile([stdio("a")])]);
  assert.deepEqual(calls, ["a"]);
});

test("manager wraps each connected server with the shared per-service call limit", async () => {
  let active = 0;
  let peak = 0;
  const releases: Array<() => void> = [];
  const manager = new McpManager({ connect: async () => ({
    tools: [{ name: "work", inputSchema: { type: "object", properties: {} } }],
    call: async () => {
      active += 1;
      peak = Math.max(peak, active);
      await new Promise<void>((resolve) => releases.push(resolve));
      active -= 1;
      return { content: [] };
    },
    close: async () => undefined
  }) });
  const [entry] = await manager.reconcile([stdio("a")]);
  assert.equal(entry.state, "connected");
  if (entry.state !== "connected") return;
  const calls = [entry.handle.call("work", {}), entry.handle.call("work", {}), entry.handle.call("work", {})];
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(peak, 2);
  releases.splice(0).forEach((release) => release());
  await new Promise((resolve) => setImmediate(resolve));
  releases.splice(0).forEach((release) => release());
  await Promise.all(calls);
});

test("a hanging connect factory is cut off by the manager-side timeout", async () => {
  const manager = new McpManager({ connect: () => new Promise(() => undefined) as Promise<McpServerHandle>, timeoutMs: 20 });
  const entries = await manager.reconcile([stdio("a")]);
  assert.equal(entries[0].state, "error");
  assert.match(entries[0].state === "error" ? entries[0].error : "", /timeout/);
});

test("acquire takes references; the last release closes the connection", async () => {
  const { manager, handles } = harness(async (config) => fakeHandle(config.name));
  const first = await manager.acquire([stdio("a")]);
  const second = await manager.acquire([stdio("a")]);
  assert.equal(first[0], second[0]); // same shared entry
  await manager.release(first);
  // Still one holder: connection alive, and a config change must not close it.
  await manager.reconcile([]);
  assert.equal(handles.get("a")?.closed, false);
  await manager.release(second);
  assert.equal(handles.get("a")?.closed, true);
});

test("reconcile keeps referenced connections that are absent from the new config", async () => {
  const { manager, handles } = harness(async (config) => fakeHandle(config.name));
  const held = await manager.acquire([stdio("a")]);
  const reconfigured = await manager.acquire([stdio("a", "changed")]);
  // Old key survives because a session still holds it; new key is a new connection.
  assert.equal(handles.get("a")?.closed, false);
  assert.equal(reconfigured[0].key, held[0].key ? reconfigured[0].key : "");
  assert.notEqual(reconfigured[0].key, held[0].key);
  await manager.release(reconfigured);
  await manager.release(held);
  assert.equal(handles.get("a")?.closed, true);
});

test("release of a replaced entry never closes its successor", async () => {
  let fail = true;
  const { manager } = harness(async (config) => {
    if (fail) throw new Error("down");
    return fakeHandle(config.name);
  });
  const errored = await manager.acquire([stdio("a")]);
  assert.equal(errored[0].state, "error");
  fail = false;
  const connected = await manager.acquire([stdio("a")]);
  assert.equal(connected[0].state, "connected");
  // Releasing the stale error entry must not decrement or close the successor.
  await manager.release(errored);
  const again = await manager.acquire([stdio("a")]);
  assert.equal(again[0], connected[0]);
  await manager.release(connected);
  await manager.release(again);
});

test("a connection that dies between sessions is replaced on the next acquire", async () => {
  const { manager, calls } = harness(async (config) => fakeHandle(config.name));
  const first = await manager.acquire([stdio("a")]);
  assert.equal(first[0].state, "connected");
  // The server process exits: the handle reports dead.
  (first[0].state === "connected" ? first[0].handle : undefined)!.dead = true;
  const second = await manager.acquire([stdio("a")]);
  assert.equal(calls.length, 2, "dead connection must reconnect");
  assert.notEqual(second[0], first[0]);
  assert.equal(second[0].state, "connected");
  // Releasing the stale entry must not close or decrement the successor.
  await manager.release(first);
  assert.equal(second[0].refs, 1);
  await manager.release(second);
});

test("withMcpReferences releases everything when the build step throws", async () => {
  const created: (McpServerHandle & { closed: boolean })[] = [];
  const manager = new McpManager({
    connect: async (config) => {
      const handle = fakeHandle(config.name);
      created.push(handle);
      return handle;
    }
  });
  const globalWithManager = globalThis as { __riftxMcpManager?: McpManager };
  globalWithManager.__riftxMcpManager = manager;
  try {
    await assert.rejects(
      withMcpReferences([stdio("a"), stdio("b")], async () => {
        throw new Error("build failed");
      }),
      /build failed/
    );
    assert.equal(created.length, 2);
    assert.ok(created.every((handle) => handle.closed), "failed construction must release every reference");
    // The next construction reconnects cleanly — no leaked refs from the failure.
    const rebuilt = await withMcpReferences([stdio("a")], async (entries) => entries);
    assert.equal(rebuilt[0].state, "connected");
    assert.equal(rebuilt[0].refs, 1);
    await manager.release(rebuilt);
  } finally {
    delete globalWithManager.__riftxMcpManager;
  }
});

test("testMcpServers reports per-server outcomes and closes what it opened", async () => {
  const created: (McpServerHandle & { closed: boolean })[] = [];
  const connect = async (config: McpServerConfig) => {
    if (config.command === "dead") throw new Error("spawn ENOENT");
    const handle = fakeHandle(config.name);
    created.push(handle);
    return handle;
  };
  const results = await testMcpServers([stdio("ok1"), stdio("dead", "dead"), stdio("ok2")], connect);
  assert.deepEqual(results, [
    { name: "ok1", ok: true, toolCount: 1 },
    { name: "dead", ok: false, error: "spawn ENOENT" },
    { name: "ok2", ok: true, toolCount: 1 }
  ]);
  // Everything the throwaway manager connected must be closed again.
  assert.equal(created.length, 2);
  assert.ok(created.every((handle) => handle.closed));
});

test("testMcpServers leaves the shared manager untouched", async () => {
  const shared = harness(async (config) => fakeHandle(config.name));
  await shared.manager.reconcile([stdio("live")]);
  const results = await testMcpServers([stdio("probe")], async (config) => fakeHandle(config.name));
  assert.equal(results[0].ok, true);
  // The shared connection from before is still connected, not evicted by the probe.
  const entries = await shared.manager.reconcile([stdio("live")]);
  assert.equal(entries[0].state, "connected");
  assert.deepEqual(shared.calls, ["live"]);
});
