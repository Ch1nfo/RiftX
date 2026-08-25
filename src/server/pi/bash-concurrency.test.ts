import assert from "node:assert/strict";
import test from "node:test";
import { BashConcurrency } from "./bash-concurrency";

test("limits Bash executions and starts queued work in order", async () => {
  const limiter = new BashConcurrency(2);
  const started: string[] = [];
  const first = await limiter.acquire(undefined, () => started.push("first"));
  const second = await limiter.acquire(undefined, () => started.push("second"));
  let thirdStarted = false;
  const thirdPromise = limiter.acquire(undefined, () => { thirdStarted = true; });

  assert.equal(limiter.running, 2);
  assert.equal(thirdStarted, false);
  first();
  const third = await thirdPromise;
  assert.equal(thirdStarted, true);
  assert.deepEqual(started, ["first", "second"]);
  second();
  third();
  assert.equal(limiter.running, 0);
});

test("aborted queued Bash work does not consume a slot", async () => {
  const limiter = new BashConcurrency(1);
  const release = await limiter.acquire();
  const controller = new AbortController();
  const pending = limiter.acquire(controller.signal);
  controller.abort();
  await assert.rejects(pending, /aborted/);
  release();
  assert.equal(limiter.running, 0);
});

test("raising the limit pumps existing queued work", async () => {
  const limiter = new BashConcurrency(1);
  const release = await limiter.acquire();
  let started = false;
  const pending = limiter.acquire(undefined, () => { started = true; });
  limiter.setLimit(2);
  const queuedRelease = await pending;
  assert.equal(started, true);
  release();
  queuedRelease();
});
