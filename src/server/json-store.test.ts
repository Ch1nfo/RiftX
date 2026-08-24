import assert from "node:assert/strict";
import { mkdir, mkdtemp, readdir, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { readJsonStore, writeJsonStoreAtomic } from "./json-store";

test("json store round-trips data atomically", async () => {
  const root = await mkdtemp(join(tmpdir(), "riftx-json-"));
  const file = join(root, "state.json");
  try {
    assert.equal(await readJsonStore(file), undefined);
    await writeJsonStoreAtomic(file, { value: 42, nested: { list: [1, 2] } });
    assert.deepEqual(await readJsonStore<{ value: number }>(file), { value: 42, nested: { list: [1, 2] } });
    // No temporary files are left behind.
    assert.deepEqual((await readdir(root)).filter((name) => name.includes(".tmp-")), []);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("a corrupt store is backed up and reads as empty", async () => {
  const root = await mkdtemp(join(tmpdir(), "riftx-json-"));
  const file = join(root, "state.json");
  try {
    await writeFile(file, "{ not valid json", "utf8");
    assert.equal(await readJsonStore(file), undefined);
    const leftovers = (await readdir(root)).filter((name) => name.startsWith("state.json.corrupt-"));
    assert.equal(leftovers.length, 1);
    assert.equal(await readFile(join(root, leftovers[0]), "utf8"), "{ not valid json");
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("write failures surface instead of being swallowed", async () => {
  const root = await mkdtemp(join(tmpdir(), "riftx-json-"));
  // A file where a directory is expected makes every write path fail with
  // ENOTDIR regardless of platform permissions.
  await writeFile(join(root, "blocker"), "not a directory", "utf8");
  try {
    await assert.rejects(() => writeJsonStoreAtomic(join(root, "blocker", "state.json"), { value: 1 }), /ENOTDIR|not a directory/i);
    await assert.rejects(() => readJsonStore(join(root, "blocker", "state.json")), /ENOTDIR|not a directory/i);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("a rename failure after a successful write leaves no temporary file", async () => {
  const root = await mkdtemp(join(tmpdir(), "riftx-json-"));
  // The temporary file is created next to the target; making the target an
  // existing directory lets writeFile succeed but rename fail.
  await mkdir(join(root, "state.json"), { recursive: true });
  try {
    await assert.rejects(() => writeJsonStoreAtomic(join(root, "state.json"), { value: 1 }), /EISDIR|ENOTDIR|directory/i);
    const leftovers = (await readdir(root)).filter((name) => name.includes(".tmp-"));
    assert.deepEqual(leftovers, []);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});
