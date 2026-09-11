import { createHash } from "node:crypto";
import { mkdir } from "node:fs/promises";
import { join } from "node:path";
import type { BashToolOptions, ToolDefinition } from "@mariozechner/pi-coding-agent";
import { MutationLock } from "@/server/pi/mutation-lock";
import { createTimedLocalTools } from "@/server/pi/local-tool-timeout";
import { createTimedBashTool } from "@/server/pi/bash-timeout";
import { Type, type TObject } from "@sinclair/typebox";

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
  constructor(readonly root: string, private current: string | undefined, private readonly resetBrowser: () => Promise<void>) {}

  get cwd() {
    return this.current ? join(challengeDirectory(this.root, this.current), "work") : join(this.root, "coordinator");
  }

  async activate(uniqueCode?: string) {
    if (this.ready && this.current === uniqueCode) return;
    // Invalidate queued calls before any asynchronous teardown can fail.
    this.epoch++;
    this.ready = false;
    await this.resetBrowser();
    const cwd = uniqueCode ? join(challengeDirectory(this.root, uniqueCode), "work") : join(this.root, "coordinator");
    await mkdir(cwd, { recursive: true });
    this.current = uniqueCode;
    this.ready = true;
  }

  install(tool: { name: string; execute?: (id: string, params: unknown, signal?: AbortSignal, ...rest: unknown[]) => Promise<unknown> }) {
    if (!tool.execute || tool.name === "assign_benchmark_challenge") return;
    const execute = tool.execute.bind(tool);
    tool.execute = async (id, params, signal, ...rest) => {
      const epoch = this.epoch;
      const action = (params as { action?: string } | undefined)?.action;
      const transition = tool.name === "benchmark_control" && ["sync", "acquire", "submit", "defer", "abandon"].includes(action ?? "");
      const release = await (transition ? this.lock.acquire(signal) : this.lock.acquireShared(signal));
      try {
        if (!transition && (!this.ready || epoch !== this.epoch)) {
          return { content: [{ type: "text", text: "Challenge changed while this tool was queued. The tool was not executed. Reissue it for the current challenge." }], details: { challengeChanged: true } };
        }
        return await execute(id, params, signal, ...rest);
      } finally { release(); }
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
    ...(tool.name === "bash" ? {
      description: `${tool.description} For online password guessing (including custom scripts), set passwordEnumeration=true. The benchmark shares a 120-second total per challenge and caps each guessing call at 30 seconds; offline computation is excluded.`,
      parameters: { ...tool.parameters, properties: { ...(tool.parameters as TObject).properties, passwordEnumeration: Type.Boolean({ description: "This command attempts multiple candidate passwords against a login service, including through a script." }) } }
    } : {}),
    execute: async (...args) => {
      const cwd = getCwd();
      await mkdir(join(cwd, ".tmp"), { recursive: true });
      if (cache?.cwd !== cwd) cache = { cwd, tools: create(cwd) };
      return cache.tools.find((candidate) => candidate.name === tool.name)!.execute(...args);
    }
  }));
}
