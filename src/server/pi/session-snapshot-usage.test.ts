import assert from "node:assert/strict";
import test from "node:test";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { SessionManager } from "@mariozechner/pi-coding-agent";
import type { ModelProfile } from "@/lib/types";
import { sessionSnapshotFromFile } from "./session-snapshot";
import { estimateMessagesContextUsage } from "./context-usage";

test("inactive session usage includes the retained prefix, fixed context and persisted calibration", async () => {
  const directory = await mkdtemp(join(tmpdir(), "riftx-snapshot-usage-"));
  try {
    const manager = SessionManager.inMemory();
    manager.appendModelChange("fixture", "m1");
    manager.appendMessage({ role: "user", content: "Discarded history ".repeat(1000), timestamp: 1 });
    const kept = manager.appendMessage({ role: "user", content: "Retained detail ".repeat(1000), timestamp: 2 });
    manager.appendCompaction("checkpoint", kept, 100_000, { riftx: { budget: { fixedTokens: 6000, tokenRatio: 2 } } });
    const path = join(directory, "session.jsonl");
    await writeFile(path, manager.getBranch().map((entry) => JSON.stringify(entry)).join("\n"));
    const profile = { id: "fixture", provider: "fixture", model: "m1", contextWindow: 128_000 } as ModelProfile;
    const snapshot = await sessionSnapshotFromFile(path, [profile]);
    assert.ok(snapshot);
    const expected = estimateMessagesContextUsage(manager.buildSessionContext().messages, 0).tokens * 2 + 6000;
    assert.equal(snapshot.usage.tokens, expected);
    assert.equal(snapshot.usage.source, "estimated");
    assert.equal(snapshot.usage.percent, expected / 128_000 * 100);
  } finally { await rm(directory, { recursive: true, force: true }); }
});
