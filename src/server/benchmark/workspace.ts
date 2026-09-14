import { createHash } from "node:crypto";
import { AsyncLocalStorage } from "node:async_hooks";
import { mkdir } from "node:fs/promises";
import { join } from "node:path";
import type { BashToolOptions, ToolDefinition } from "@mariozechner/pi-coding-agent";
import { MutationLock } from "@/server/pi/mutation-lock";
import { createTimedLocalTools } from "@/server/pi/local-tool-timeout";
import { createTimedBashTool } from "@/server/pi/bash-timeout";

import { attemptContext, type BenchmarkExecutionBinding } from "./fencing";

const fileLocks = new WeakMap<object, Map<string, MutationLock>>();

/** Shared only by workers touching the same challenge directory in this run. */
export function benchmarkMutationLock(run: object, directory: string): MutationLock {
  let locks = fileLocks.get(run);
  if (!locks) fileLocks.set(run, locks = new Map());
  let lock = locks.get(directory);
  if (!lock) locks.set(directory, lock = new MutationLock());
  return lock;
}

const key = (value: string) => createHash("sha256").update(value).digest("hex");

export function benchmarkWorkspaceRoot(cwd: string, platformUrl: string) {
  return join(cwd, ".riftx", "benchmark", key(platformUrl.replace(/\/$/, "")));
}

export function challengeDirectory(root: string, uniqueCode: string) {
  return join(root, key(uniqueCode));
}

/** Per-worker switching barrier. Never changes the process-wide cwd. */
export class BenchmarkWorkspace {
  private readonly lock = new MutationLock();
  private epoch = 0;
  private ready = true;
  private readonly submission = new AsyncLocalStorage<{ activation?: { code?: string } }>();
  private generation?: string;
  constructor(readonly root: string, private current: string | undefined, private readonly resetBrowser: () => Promise<void>, private readonly binding?: BenchmarkExecutionBinding) {
    this.generation = this.generationKey();
  }
  private generationKey() { return JSON.stringify(this.binding?.snapshot()); }

  get cwd() {
    return this.current ? join(challengeDirectory(this.root, this.current), "work") : join(this.root, "coordinator");
  }

  /** Background submission confirmation may release ownership between turns. */
  async reconcile(uniqueCode?: string): Promise<boolean> {
    if (this.ready && this.current === uniqueCode && this.generation === this.generationKey()) return false;
    const release = await this.lock.acquire();
    try { await this.activate(uniqueCode); return true; }
    finally { release(); }
  }

  async activate(uniqueCode?: string) {
    const submission = this.submission.getStore();
    if (submission) { submission.activation = { code: uniqueCode }; return; }
    if (this.ready && this.current === uniqueCode && this.generation === this.generationKey()) return;
    if (uniqueCode) this.binding?.validate();
    // Invalidate queued calls before any asynchronous teardown can fail.
    this.epoch++;
    this.ready = false;
    await this.resetBrowser();
    const cwd = uniqueCode ? join(challengeDirectory(this.root, uniqueCode), "work") : join(this.root, "coordinator");
    await mkdir(cwd, { recursive: true });
    this.current = uniqueCode;
    this.generation = this.generationKey();
    this.ready = true;
  }

  install(tool: { name: string; execute?: (id: string, params: unknown, signal?: AbortSignal, ...rest: unknown[]) => Promise<unknown> }) {
    if (!tool.execute || tool.name === "assign_benchmark_challenge") return;
    const execute = tool.execute.bind(tool);
    tool.execute = async (id, params, signal, ...rest) => {
      const epoch = this.epoch;
      const fence = attemptContext.getStore() ?? this.binding?.snapshot();
      const action = (params as { action?: string } | undefined)?.action;
      const submitting = tool.name === "benchmark_control" && action === "submit";
      const transition = tool.name === "benchmark_control" && ["sync", "acquire", "defer", "abandon"].includes(action ?? "");
      const release = await (transition ? this.lock.acquire(signal) : this.lock.acquireShared(signal));
      const submission: { activation?: { code?: string } } = {};
      try {
        if (!transition && (!this.ready || epoch !== this.epoch)) {
          return { content: [{ type: "text", text: "Challenge changed while this tool was queued. The tool was not executed. Reissue it for the current challenge." }], details: { challengeChanged: true } };
        }
        if (fence && !(tool.name === "benchmark_control" && ["sync", "status", "acquire"].includes(action ?? ""))) this.binding?.validate(fence);
        return await (submitting ? this.submission.run(submission, () => execute(id, params, signal, ...rest)) : execute(id, params, signal, ...rest));
      } finally {
        release();
        if (submission.activation) {
          // Platform receipt is already recorded. Directory/browser teardown
          // waits until all old calls finish, without holding the ledger lock.
          const finish = await this.lock.acquire();
          try { if (epoch === this.epoch) await this.activate(submission.activation.code); }
          finally { finish(); }
        }
      }
    };
  }
}

/** Resolve relative file paths and shell cwd against the active challenge. */
export function createWorkspaceLocalTools(getCwd: () => string, bashOptions: BashToolOptions): ToolDefinition[] {
  const create = (cwd: string): ToolDefinition[] => [
    ...createTimedLocalTools(cwd),
    createTimedBashTool(cwd, {
      ...bashOptions,
      spawnHook: (context) => {
        const updated = bashOptions.spawnHook?.(context) ?? context;
        const temp = join(cwd, ".tmp");
        return { ...updated, env: { ...updated.env, TMPDIR: temp, TMP: temp, TEMP: temp } };
      }
    }) as ToolDefinition
  ];
  let cache: { cwd: string; tools: ToolDefinition[] } | undefined;
  return create(getCwd()).map((tool) => ({
    ...tool,
    ...(tool.name === "bash" ? { description: `${tool.description} Record useful evidence and checkpoint durable progress as you work.` } : {}),
    execute: async (...args) => {
      const cwd = getCwd();
      await mkdir(join(cwd, ".tmp"), { recursive: true });
      if (cache?.cwd !== cwd) cache = { cwd, tools: create(cwd) };
      return cache.tools.find((candidate) => candidate.name === tool.name)!.execute(...args);
    }
  }));
}
