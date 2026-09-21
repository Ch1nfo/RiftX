import assert from "node:assert/strict";
import test from "node:test";
import { mkdtempSync, rmSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { collaborationResponse, type CollaborationRequest } from "./http";
import { BoardStore } from "./store";
import { BoardRuntime } from "./runtime";
import type { BoardWork } from "@/lib/collaboration";

test("HTTP commands bind user identity, enforce action scope, replay safely and reject conflicts", async () => {
  const dir = mkdtempSync(join(tmpdir(), "riftx-http-board-"));
  const store = new BoardStore(join(dir, "board.sqlite"), "session", { create: true });
  const runtime = new BoardRuntime(store, { actor: async () => { throw new Error("Do not start a model in this fixture"); }, release: async () => {}, emit: () => {} }, 10000);
  let command = 0;
  const request = (kind: CollaborationRequest, body: Record<string, unknown>, id?: string) => collaborationResponse(new Request("http://localhost/collaboration", { method: "POST", body: JSON.stringify({ commandId: `command-${++command}`, ...body }) }), "session", kind, id, async (session) => { assert.equal(session, "session"); return runtime; });
  try {
    assert.equal((await request("control", { action: "recover" })).status, 400);
    assert.equal((await request("messages", { to: "main", kind: "information", body: "spoof", actor: "system" })).status, 400);
    assert.equal((await request("messages", { to: "foreign", kind: "information", body: "out of scope" })).status, 404);
    const body = { commandId: "stable-key", to: "main", kind: "information", body: "Queued" };
    assert.equal((await request("messages", body)).status, 200);
    assert.equal((await request("messages", body)).status, 200); assert.equal(store.read().messages.length, 1);
    assert.equal((await request("messages", { ...body, body: "Different" })).status, 409);
    assert.equal((await request("control", { action: "pause" })).status, 200);
    assert.equal(store.read().status, "paused");
    assert.equal((await request("messages", { to: "main", kind: "information", body: "While paused" })).status, 200);
    assert.equal((await request("control", { action: "resume" })).status, 200);
    const task = store.apply("main", "work", "create", { objective: "Test" }, { epoch: store.read().epoch }) as BoardWork;
    assert.equal((await request("task", { action: "approve", version: task.version }, task.id)).status, 400);
    assert.equal((await request("task", { action: "cancel", version: task.version + 1 }, task.id)).status, 409);
    assert.equal((await request("task", { action: "cancel", version: task.version }, task.id)).status, 200);
    const badCursor = await collaborationResponse(new Request("http://localhost/collaboration?after=-1"), "session", "read", undefined, async () => runtime);
    assert.equal(badCursor.status, 400);
    const snapshot = await collaborationResponse(new Request("http://localhost/collaboration?after=0&limit=2"), "session", "read", undefined, async () => runtime);
    const value = await snapshot.json(); assert.equal(value.events.length, 2); assert.ok(value.hasMore);
    const legacy = await collaborationResponse(new Request("http://localhost/collaboration"), "legacy", "read", undefined, async () => undefined);
    assert.deepEqual(await legacy.json(), { mode: "legacy" });
  } finally { await runtime.close(); rmSync(dir, { recursive: true, force: true }); }
});
