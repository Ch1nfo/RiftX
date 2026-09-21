import assert from "node:assert/strict";
import test from "node:test";
import { mkdtempSync, rmSync, writeFileSync, unlinkSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { SessionManager } from "@mariozechner/pi-coding-agent";
import { sessionProtocol } from "./protocol";
import { boardPath } from "./integration";
import { BoardStore } from "./store";

test("explicit protocol markers distinguish old sessions and missing/corrupt databases", async () => {
  const root = mkdtempSync(join(tmpdir(), "riftx-protocol-"));
  try {
    const legacy = SessionManager.inMemory();
    assert.equal(await sessionProtocol(root, legacy, false), false);
    const fresh = SessionManager.inMemory();
    assert.equal(await sessionProtocol(root, fresh, true), true);
    assert.equal(await sessionProtocol(root, fresh, false), true);
    const path = boardPath(root, fresh.getSessionId());
    assert.throws(() => new BoardStore(path, fresh.getSessionId()), /missing/);
    writeFileSync(path, "corrupt database");
    assert.throws(() => new BoardStore(path, fresh.getSessionId()), /cannot be read|could not be opened/);
    unlinkSync(join(dirname(path), "protocol.json"));
    assert.equal(await sessionProtocol(root, fresh, false), true, "the transcript marker independently preserves the protocol");
    writeFileSync(join(dirname(path), "protocol.json"), '{"version":999}');
    await assert.rejects(sessionProtocol(root, fresh, false), /Unsupported/);
    assert.throws(() => boardPath(root, "../../other"), /Invalid session/);
  } finally { rmSync(root, { recursive: true, force: true }); }
});
