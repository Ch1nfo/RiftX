import assert from "node:assert/strict";
import test from "node:test";
import { getScreenshotPath, isScreenshotId } from "./evidence-path";

test("accepts generated screenshot ids and keeps them under the shots directory", () => {
  const id = "s-123e4567-e89b-12d3-a456-426614174000";
  assert.equal(isScreenshotId(id), true);
  assert.equal(getScreenshotPath("/tmp/evidence", "session", id), "/tmp/evidence/session/shots/s-123e4567-e89b-12d3-a456-426614174000.png");
});

test("rejects path traversal and arbitrary screenshot ids", () => {
  assert.equal(isScreenshotId("../../../secret"), false);
  assert.throws(() => getScreenshotPath("/tmp/evidence", "session", "../../../secret"));
  assert.throws(() => getScreenshotPath("/tmp/evidence", "session", "latest"));
});
