import assert from "node:assert/strict";
import test from "node:test";
import { hasNode24CleanupRegression, reportUnsupportedNode } from "../bin/node-runtime.mjs";

test("recognizes the Node 24.19+ native addon cleanup regression", () => {
  assert.equal(hasNode24CleanupRegression("24.18.1"), false);
  assert.equal(hasNode24CleanupRegression("24.19.0"), true);
  assert.equal(hasNode24CleanupRegression("24.21.0"), true);
  assert.equal(hasNode24CleanupRegression("22.19.0"), false);
  assert.equal(hasNode24CleanupRegression("25.0.0"), false);
  assert.equal(reportUnsupportedNode("24.18.1"), false);
});
