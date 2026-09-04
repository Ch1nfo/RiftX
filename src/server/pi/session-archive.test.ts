import assert from "node:assert/strict";
import test from "node:test";
import { archivedRestoreBlock, archivedRestoreError, classifyArchivedRestore, restoredArchiveState } from "./session-archive";

test("restoring a session removes only its archived markers", () => {
  const archivedSessionIds = ["restore-me", "keep-me", "restore-me"];
  const archivedSessions = [
    { id: "restore-me", path: "/sessions/restore.jsonl", name: "Restore", firstMessage: "one", updatedAt: "2026-09-04T00:00:00.000Z" },
    { id: "keep-me", path: "/sessions/keep.jsonl", name: "Keep", firstMessage: "two", updatedAt: "2026-09-04T00:00:00.000Z" }
  ];

  const restored = restoredArchiveState({ archivedSessionIds, archivedSessions }, "restore-me");

  assert.deepEqual(restored.archivedSessionIds, ["keep-me"]);
  assert.deepEqual(restored.archivedSessions, [archivedSessions[1]]);
  assert.deepEqual(archivedSessionIds, ["restore-me", "keep-me", "restore-me"], "input remains immutable");
});

test("restoring a current-workspace archived session is allowed", () => {
  assert.deepEqual(classifyArchivedRestore({ archived: true, inCurrentWorkspace: true, sessionFileExists: true }), { ok: true });
  assert.equal(archivedRestoreBlock(true, true), undefined);
});

test("restoring a session from another working directory is rejected", () => {
  assert.deepEqual(classifyArchivedRestore({ archived: true, inCurrentWorkspace: false, sessionFileExists: true }), {
    ok: false,
    code: "SESSION_NOT_IN_WORKSPACE"
  });
  assert.equal(archivedRestoreBlock(false, true), "wrong-workspace");
  assert.equal(archivedRestoreError("SESSION_NOT_IN_WORKSPACE").status, 404);
});

test("restoring a session whose file is gone is rejected as missing", () => {
  assert.deepEqual(classifyArchivedRestore({ archived: true, inCurrentWorkspace: false, sessionFileExists: false }), {
    ok: false,
    code: "SESSION_NOT_FOUND"
  });
  assert.equal(archivedRestoreBlock(false, false), "missing");
  assert.equal(archivedRestoreError("SESSION_NOT_FOUND").status, 404);
});

test("restoring a session that is not archived is rejected", () => {
  assert.deepEqual(classifyArchivedRestore({ archived: false, inCurrentWorkspace: true, sessionFileExists: true }), {
    ok: false,
    code: "SESSION_NOT_ARCHIVED"
  });
  assert.equal(archivedRestoreError("SESSION_NOT_ARCHIVED").status, 400);
});
