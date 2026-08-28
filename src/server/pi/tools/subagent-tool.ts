import { Type } from "@sinclair/typebox";
import { defineTool, type ToolDefinition } from "@mariozechner/pi-coding-agent";
import type { ModelProfile } from "@/lib/types";
import type { SubagentManager, SubagentRunnerContext, SubagentResult } from "../subagent-manager";
import type { MutationLock } from "../mutation-lock";
import type { BashConcurrency } from "../bash-concurrency";
import type { RuntimeDeps } from "../session-registry";

/** The spawn_subagent tool. `runChild` is injected so this module stays independent of the runtime-creation module. */

export type RunChild = (profile: ModelProfile, cwd: string, mutationLock: MutationLock, bashConcurrency: BashConcurrency, context: SubagentRunnerContext, runtimeDeps: RuntimeDeps) => Promise<SubagentResult>;

export function createSubagentTool(manager: SubagentManager, getChildProfile: () => ModelProfile, cwd: string, mutationLock: MutationLock, bashConcurrency: BashConcurrency, runtimeDeps: RuntimeDeps, runChild: RunChild): ToolDefinition {
  return defineTool({
    name: "spawn_subagent",
    label: "Spawn subagent",
    description: "Start one focused, independent Web penetration testing SubAgent that runs in the background while you continue work; each completed result is delivered to you automatically. Follow the session's subagent delegation policy: use it only for meaningful, non-duplicate, independent work, never poll child logs or task files, and remember every SubAgent is mandatory for the final assessment. A SubAgent cannot create another SubAgent.",
    promptSnippet: "spawn_subagent(task)",
    executionMode: "parallel",
    parameters: Type.Object({
      task: Type.String({ description: "A unique, self-contained task with a clear target surface, evidence goal, and no dependency on another child task." })
    }),
    async execute(_toolCallId, params, signal) {
      const childProfile = getChildProfile();
      const submitted = manager.submitTask(params.task, (context) => runChild(childProfile, cwd, mutationLock, bashConcurrency, context, runtimeDeps));
      void submitted.promise.catch(() => undefined);
      const taskLabel = submitted.task?.name || "subagent task";
      const state = submitted.task?.status || "queued";
      const text = submitted.duplicate
        ? `A matching subagent task is already ${state}. Its existing result will be delivered when complete.`
        : `Subagent task accepted in the background (${state}): ${taskLabel}. Continue independent work. RiftX will return its result when it completes and will wait for it before a final conclusion if needed.`;
      // A duplicate submission shares the original task: aborting THIS call
      // must not cancel the task owned by the first spawn.
      if (signal?.aborted && submitted.task?.id && !submitted.duplicate) manager.cancel(submitted.task.id);
      return { content: [{ type: "text", text }], details: { model: `${childProfile.provider}/${childProfile.model}`, taskId: submitted.task?.id, status: state, background: true } };
    }
  });
}
