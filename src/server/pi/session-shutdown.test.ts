import assert from "node:assert/strict";
import test from "node:test";
import { abortSessionRecord, shutdownSessionRecord, type ShutdownTarget } from "./session-shutdown";

type Calls = string[];

function makeFakeRecord(id: string, calls: Calls): ShutdownTarget {
  return {
    id,
    gate: { rejectAll: () => calls.push("rejectAll") },
    session: {
      abortBash: () => calls.push("abortBash"),
      abort: async () => calls.push("abort"),
      dispose: () => calls.push("dispose")
    },
    subagents: { abortAll: async () => calls.push("subagents-abortAll") },
    browser: { close: async () => calls.push("browser-close") },
    unsubscribe: () => calls.push("unsubscribe")
  };
}

test("shutdown aborts the running main agent before detaching, exactly once", async () => {
  const calls: Calls = [];
  const record = makeFakeRecord("s1", calls);
  await shutdownSessionRecord(record);
  // Archiving a running session must actually terminate it: approvals are
  // released first, the main agent (and bash) stops before the browser closes
  // and the SDK session is disposed last.
  assert.equal(calls[0], "rejectAll");
  assert.ok(calls.indexOf("abortBash") < calls.indexOf("abort"), "abortBash precedes abort");
  assert.ok(calls.indexOf("abort") < calls.indexOf("browser-close"), "abort precedes browser close");
  assert.ok(calls.indexOf("browser-close") < calls.indexOf("unsubscribe"), "browser close precedes unsubscribe");
  assert.equal(calls[calls.length - 1], "dispose");
  assert.ok(calls.includes("subagents-abortAll"));
  // Idempotent: a second shutdown does not repeat any side effect.
  const after = calls.length;
  await shutdownSessionRecord(record);
  assert.equal(calls.length, after);
});

test("a failing cleanup step never skips the remaining ones", async () => {
  const calls: Calls = [];
  const record = makeFakeRecord("s2", calls);
  record.session.abort = async () => {
    calls.push("abort");
    throw new Error("agent abort blew up");
  };
  record.browser!.close = async () => {
    calls.push("browser-close");
    throw new Error("browser close blew up");
  };
  record.unsubscribe = () => {
    calls.push("unsubscribe");
    throw new Error("unsubscribe blew up");
  };
  await shutdownSessionRecord(record);
  // Every later step still ran and shutdown resolved instead of rejecting.
  assert.deepEqual(calls, ["rejectAll", "abortBash", "abort", "subagents-abortAll", "browser-close", "unsubscribe", "dispose"]);
});

test("shutdown waits for an in-flight stop before cleaning up", async () => {
  const calls: Calls = [];
  const record = makeFakeRecord("s3", calls);
  let releaseStop: (() => void) | undefined;
  record.abortPromise = new Promise<void>((resolve) => {
    releaseStop = () => resolve();
  });
  const shutdown = shutdownSessionRecord(record);
  await new Promise((resolve) => setTimeout(resolve, 50));
  // Cleanup has not started while the stop is still unwinding.
  assert.equal(calls.length, 0);
  releaseStop!();
  await shutdown;
  assert.equal(calls[calls.length - 1], "dispose");
});

test("a stop arriving during a shutdown waits instead of re-aborting", async () => {
  const calls: string[] = [];
  const record = makeFakeRecord("s4", calls);
  let releaseShutdown: (() => void) | undefined;
  record.session.abort = async () => {
    calls.push("abort");
    await new Promise<void>((resolve) => { releaseShutdown = resolve; });
  };
  const emitted: string[] = [];
  const shutdown = shutdownSessionRecord(record);
  await new Promise((resolve) => setTimeout(resolve, 30));
  // While shutdown is mid-abort, a user Stop arrives: it must not start a
  // second abort/close round on the disposing record. It parks on the
  // shutdown until that finishes.
  const stop = abortSessionRecord(record, (event) => emitted.push(event.type));
  await new Promise((resolve) => setTimeout(resolve, 30));
  assert.equal(calls.filter((call) => call === "abort").length, 1);
  assert.equal(emitted.length, 0, "stop delegated to shutdown emits no idle/done of its own");
  assert.equal(record.aborting, true, "aborting stays raised while shutdown runs");
  releaseShutdown!();
  await shutdown;
  await stop;
  assert.equal(record.aborting, false);
});

test("concurrent stops share a single abort round", async () => {
  const calls: string[] = [];
  const record = makeFakeRecord("s5", calls);
  let release: (() => void) | undefined;
  record.session.abort = async () => {
    calls.push("abort");
    await new Promise<void>((resolve) => { release = resolve; });
  };
  const emitted: string[] = [];
  const first = abortSessionRecord(record, (event) => emitted.push(event.type));
  await new Promise((resolve) => setTimeout(resolve, 20));
  const second = abortSessionRecord(record, (event) => emitted.push(event.type));
  await new Promise((resolve) => setTimeout(resolve, 20));
  assert.equal(calls.filter((call) => call === "abort").length, 1, "both stops share one abort");
  release!();
  await Promise.all([first, second]);
  assert.deepEqual(emitted, ["session_state", "done"], "exactly one idle/done pair");
  assert.equal(record.aborting, false);
});
