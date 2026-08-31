import assert from "node:assert/strict";
import test from "node:test";
import { orderSessionsByActivity, withRunningSessionIds, withSessionActivity } from "./session-activity";
import type { SessionSummary } from "./types";

function session(id: string, updatedAt: string, running = false): SessionSummary {
  return { id, path: "", name: id, firstMessage: "", updatedAt, archived: false, running };
}

test("running sessions lead the list with newest activity first", () => {
  const ordered = orderSessionsByActivity([
    session("new-idle", "2026-08-31T12:00:00.000Z"),
    session("old-running", "2026-08-31T10:00:00.000Z", true),
    session("new-running", "2026-08-31T11:00:00.000Z", true),
    session("old-idle", "2026-08-31T09:00:00.000Z")
  ]);
  assert.deepEqual(ordered.map((item) => item.id), ["new-running", "old-running", "new-idle", "old-idle"]);
});

test("continuing an old session marks it running and refreshes its activity time", () => {
  const before = [session("new", "2026-08-31T12:00:00.000Z"), session("old", "2026-08-30T12:00:00.000Z")];
  const next = orderSessionsByActivity(withSessionActivity(before, "old", true, true));
  assert.equal(next[0].id, "old");
  assert.equal(next[0].running, true);
  assert.ok(Date.parse(next[0].updatedAt) > Date.parse(before[0].updatedAt));
});

test("status reconciliation clears sessions that stopped while not selected", () => {
  const next = withRunningSessionIds([session("a", "2026-08-31T12:00:00.000Z", true), session("b", "2026-08-31T11:00:00.000Z", true)], ["b"]);
  assert.deepEqual(next.map((item) => [item.id, item.running]), [["a", false], ["b", true]]);
});

test("unchanged status reconciliation preserves references", () => {
  const before = [session("a", "2026-08-31T12:00:00.000Z", true), session("b", "2026-08-31T11:00:00.000Z")];
  assert.equal(withRunningSessionIds(before, ["a"]), before);
});
