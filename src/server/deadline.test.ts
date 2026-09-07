import assert from "node:assert/strict";
import test from "node:test";
import { runWithDeadline } from "./deadline";

test("runWithDeadline rejects work that ignores its AbortSignal", async () => {
  await assert.rejects(
    runWithDeadline(() => new Promise<void>(() => undefined), {
      timeoutMs: 10,
      timeoutMessage: "deadline reached"
    }),
    /deadline reached/
  );
});

test("runWithDeadline forwards caller cancellation", async () => {
  const controller = new AbortController();
  const running = runWithDeadline(() => new Promise<void>(() => undefined), {
    signal: controller.signal,
    timeoutMs: 1_000,
    timeoutMessage: "deadline reached"
  });
  controller.abort(new Error("user stopped"));
  await assert.rejects(running, /user stopped/);
});
