import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import { readFile } from "node:fs/promises";
import { join } from "node:path";
import test from "node:test";
import { runInNewContext } from "node:vm";
import ts from "typescript";
import { shutdownSessionRecord, type ShutdownTarget } from "./session-shutdown";

// Run the actual child lifecycle with an initialization barrier, without
// constructing providers, loading user skills, or making model requests.
test("cancellation during child initialization disposes the created session", async () => {
  const source = await readFile(new URL("./session-manager.ts", import.meta.url), "utf8");
  const parsed = ts.createSourceFile("session-manager.ts", source, ts.ScriptTarget.Latest, true);
  const declaration = parsed.statements.find((node) => ts.isFunctionDeclaration(node) && node.name?.text === "runChildSession");
  assert.ok(declaration);
  const compiled = ts.transpileModule(declaration.getText(parsed), {
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS }
  }).outputText;
  let releaseInitialization!: () => void;
  let signalInitialization!: () => void;
  const initializing = new Promise<void>((resolve) => { signalInitialization = resolve; });
  const initialized = new Promise<void>((resolve) => { releaseInitialization = resolve; });
  const calls: string[] = [];
  const child = {
    id: "fixture-child",
    gate: { rejectAll() {} },
    session: {
      abortBash() {}, abortCompaction() {},
      abort: async () => { calls.push("abort"); },
      dispose: () => { calls.push("dispose"); },
      prompt: async () => { calls.push("prompt"); }
    },
    browser: { close: async () => {}, shutdown: async () => { calls.push("browser-shutdown"); } },
    emitter: new EventEmitter(),
    unsubscribe: () => { calls.push("unsubscribe"); }
  };
  const run = runInNewContext(`${compiled}\nrunChildSession`, {
    getAppPaths: () => ({ subagents: "/unused" }), join, mkdir: async () => {},
    AgentSessionManager: { create: () => ({}) },
    createRuntimeSession: async () => { signalInitialization(); await initialized; return child; },
    shutdownSessionRecord: async (record: ShutdownTarget) => { calls.push("shutdown"); await shutdownSessionRecord(record); },
    console
  }) as (...args: unknown[]) => Promise<unknown>;
  const controller = new AbortController();
  const running = run({ provider: "fixture", model: "fixture" }, "/unused", {}, {}, {
    task: { parentSessionId: "parent", id: "child", task: "fixture" },
    gate: {}, signal: controller.signal, emit() {}, updateTaskMeta() {}
  }, { benchmark: {} });
  await initializing;
  controller.abort();
  releaseInitialization();
  await assert.rejects(running);
  assert.equal(calls.includes("prompt"), false);
  assert.equal(calls.filter((call) => call === "shutdown").length, 1);
  assert.equal(calls.filter((call) => call === "browser-shutdown").length, 1);
  assert.equal(calls.filter((call) => call === "unsubscribe").length, 1);
  assert.equal(calls.filter((call) => call === "dispose").length, 1);
});
