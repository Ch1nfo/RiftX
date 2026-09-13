import assert from "node:assert/strict";
import test from "node:test";
import { BENCHMARK_TOOL_NAMES, WEB_TOOL_NAMES, sessionToolNames } from "./session-tools";

test("runtime inventory is registered for both benchmark workers when provided", () => {
  for (const subagents of [true, false]) {
    assert.equal(sessionToolNames(subagents).includes("tool_inventory"), false);
    assert.equal(sessionToolNames(subagents, true).filter((name) => name === "tool_inventory").length, 1);
  }
});

test("benchmark branch disables public research while retaining target crawling", () => {
  for (const variant of [sessionToolNames(true), sessionToolNames(false)]) {
    assert.equal(variant.includes("crawl"), true, "both variants must whitelist crawl");
    for (const name of WEB_TOOL_NAMES) {
      assert.equal(variant.includes(name), false, `${name} must be disabled`);
    }
  }
});

test("benchmark tools are whitelisted; pentest-only tools are removed", () => {
  for (const variant of [sessionToolNames(true), sessionToolNames(false)]) {
    for (const name of BENCHMARK_TOOL_NAMES) assert.equal(variant.includes(name), true);
    assert.equal(variant.includes("record_finding"), false, "benchmark branch removes record_finding");
    assert.equal(variant.includes("checkpoint_progress"), false, "benchmark branch removes checkpoint_progress");
  }
});

test("spawn_subagent is removed on the benchmark branch", () => {
  assert.equal(sessionToolNames(true).includes("spawn_subagent"), false, "benchmark branch has no spawn_subagent");
  assert.equal(sessionToolNames(false).includes("spawn_subagent"), false);
});

test("the static whitelist reserves the mcp__ namespace for dynamically appended MCP tools", () => {
  for (const variant of [sessionToolNames(true), sessionToolNames(false)]) {
    assert.equal(variant.some((name) => name.startsWith("mcp__")), false);
  }
});
