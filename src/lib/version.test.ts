import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { resolve } from "node:path";
import { RIFTX_VERSION } from "./version";

const root = resolve(process.cwd());

test("RiftX release metadata uses one version", async () => {
  const lockText = await readFile(resolve(root, "package-lock.json"), "utf8");
  const lock = JSON.parse(lockText) as { version?: string; packages?: Record<string, { version?: string }> };

  assert.equal(lock.version, RIFTX_VERSION);
  assert.equal(lock.packages?.[""]?.version, RIFTX_VERSION);
});
