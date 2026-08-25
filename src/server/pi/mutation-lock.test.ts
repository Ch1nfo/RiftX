import assert from "node:assert/strict";
import test from "node:test";
import { MutationLock } from "./mutation-lock";

test("shared acquisitions can run together while exclusive work waits", async () => {
  const lock = new MutationLock();
  const first = await lock.acquireShared();
  const second = await lock.acquireShared();
  let exclusiveStarted = false;
  const exclusive = lock.acquire().then((release) => {
    exclusiveStarted = true;
    return release;
  });

  await Promise.resolve();
  assert.equal(exclusiveStarted, false);
  first();
  await Promise.resolve();
  assert.equal(exclusiveStarted, false);
  second();
  const releaseExclusive = await exclusive;
  assert.equal(exclusiveStarted, true);
  releaseExclusive();
});

test("a queued shared acquisition does not bypass an exclusive waiter", async () => {
  const lock = new MutationLock();
  const releaseExclusive = await lock.acquire();
  let sharedStarted = false;
  const shared = lock.acquireShared().then((release) => {
    sharedStarted = true;
    return release;
  });

  await Promise.resolve();
  assert.equal(sharedStarted, false);
  releaseExclusive();
  const releaseShared = await shared;
  assert.equal(sharedStarted, true);
  releaseShared();
});

