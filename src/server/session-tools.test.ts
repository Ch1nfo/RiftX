import assert from "node:assert/strict";
import test from "node:test";
import { WEB_TOOL_NAMES, sessionToolNames } from "./session-tools";

test("every web tool name is on the session tool whitelist", () => {
  for (const variant of [sessionToolNames(true), sessionToolNames(false)]) {
    assert.equal(variant.includes("crawl"), true, "both variants must whitelist crawl");
    for (const name of WEB_TOOL_NAMES) {
      assert.equal(variant.includes(name), true, `${name} must be whitelisted`);
    }
  }
});

test("spawn_subagent is whitelisted only for main sessions", () => {
  assert.equal(sessionToolNames(true).includes("spawn_subagent"), true);
  assert.equal(sessionToolNames(false).includes("spawn_subagent"), false);
});
