import assert from "node:assert/strict";
import test from "node:test";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import type { AgentSessionEvent } from "@mariozechner/pi-coding-agent";
import type { SessionRecord } from "@/server/pi/session-registry";
import { BoardStore } from "./store";
import { BoardRuntime } from "./runtime";
import { installBoardContext } from "./integration";

test("preparing context is not a delivery acknowledgment; failed model requests preserve inbox", async () => {
  const dir = mkdtempSync(join(tmpdir(), "riftx-delivery-"));
  const store = new BoardStore(join(dir, "board.sqlite"), "test", { create: true });
  const runtime = new BoardRuntime(store, { actor: async () => { throw new Error("No model in this fixture"); }, release: async () => {}, emit: () => {} }, 60000);
  let listener!: (event: AgentSessionEvent) => void;
  const record = { session: { agent: {}, subscribe: (callback: typeof listener) => { listener = callback; return () => {}; } } } as unknown as SessionRecord;
  try {
    await runtime.beginUserTurn();
    store.apply("user", "message", "message", { to: "main", kind: "information", body: "Durable inbox data" });
    installBoardContext(record, runtime, "main");
    const context = await record.session.agent.transformContext!([], new AbortController().signal);
    assert.match(JSON.stringify(context), /Durable inbox data/);
    assert.equal(store.read().messages[0].status, "queued", "a crash before sampling must retry delivery");
    listener({ type: "message_end", message: { role: "assistant", stopReason: "error" } } as AgentSessionEvent);
    assert.equal(store.read().messages[0].status, "queued");
    listener({ type: "message_end", message: { role: "assistant", stopReason: "stop" } } as AgentSessionEvent);
    assert.equal(store.read().messages[0].status, "in_context");
    const ids = store.read().messages.map((m) => m.id);
    runtime.included("main", ids); runtime.included("main", ids);
    assert.equal(store.read().messages.length, 1, "duplicate acknowledgments never duplicate the message");
  } finally { runtime.userTurnEnded(); await runtime.close(); rmSync(dir, { recursive: true, force: true }); }
});
