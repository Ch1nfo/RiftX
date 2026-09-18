import assert from "node:assert/strict";
import { copyFile, mkdtemp, rm, symlink } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { pathToFileURL } from "node:url";
import test from "node:test";
import type { SessionEntry } from "@mariozechner/pi-coding-agent";
import { prepareCompactionWithBudget } from "./pi-internals";

const settings = { enabled: true, reserveTokens: 128, keepRecentTokens: 1 };

test("loads the installed SDK from the module location when the working directory has no dependencies", async () => {
  const previousCwd = process.cwd();
  const directory = await mkdtemp(join(tmpdir(), "riftx-pi-sdk-working-directory-"));
  const timestamp = new Date().toISOString();
  const entries: SessionEntry[] = [
    { type: "message", id: "older", parentId: null, timestamp, message: { role: "user", content: "Synthetic earlier context.", timestamp: 1 } },
    { type: "message", id: "recent", parentId: "older", timestamp, message: { role: "user", content: "Synthetic recent context.", timestamp: 2 } }
  ];
  try {
    process.chdir(directory);
    const preparation = await prepareCompactionWithBudget(entries, settings);
    assert.ok(preparation);
    assert.equal(preparation.firstKeptEntryId, "recent");
    assert.equal(preparation.messagesToSummarize.length, 1);
  } finally {
    process.chdir(previousCwd);
    await rm(directory, { recursive: true, force: true });
  }
});

test("a missing private SDK module warns once while every attempt still rejects", async (t) => {
  const previousCwd = process.cwd();
  const directory = await mkdtemp(join(tmpdir(), "riftx-pi-sdk-missing-"));
  const copiedModule = join(directory, "pi-internals.mts");
  const warnings: unknown[][] = [];
  t.mock.method(console, "warn", (...args: unknown[]) => { warnings.push(args); });
  try {
    await copyFile(new URL("./pi-internals.ts", import.meta.url), copiedModule);
    await symlink(new URL("./compaction-retry.ts", import.meta.url), join(directory, "compaction-retry.ts"));
    process.chdir(directory);
    const isolatedCwd = process.cwd();
    const isolated = await import(pathToFileURL(copiedModule).href) as typeof import("./pi-internals");
    let firstFailure: Error | undefined;
    await assert.rejects(isolated.prepareCompactionWithBudget([], settings), (error: unknown) => {
      assert.ok(error instanceof Error);
      firstFailure = error;
      return true;
    });
    await assert.rejects(isolated.prepareCompactionWithBudget([], settings), Error);
    assert.equal(warnings.length, 1);
    const warning = JSON.stringify(warnings[0].map((value) => value instanceof Error ? { name: value.name, message: value.message } : value));
    assert.ok(warning.includes("@mariozechner/pi-coding-agent"));
    assert.ok(warning.includes(isolatedCwd));
    assert.ok(firstFailure);
    assert.ok(warning.includes(firstFailure.message));
  } finally {
    process.chdir(previousCwd);
    await rm(directory, { recursive: true, force: true });
  }
});
