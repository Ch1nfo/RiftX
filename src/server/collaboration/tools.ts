import { Type } from "@sinclair/typebox";
import { defineTool, type ToolDefinition } from "@mariozechner/pi-coding-agent";
import type { BoardOperation } from "@/lib/collaboration";
import { BoardError } from "./store";
import type { BoardRuntime } from "./runtime";

export const BOARD_TOOL_NAMES = ["board_read", "task_propose", "task_manage", "task_claim", "task_update", "board_publish", "agent_message", "board_finish"];
const optionalString = () => Type.Optional(Type.String({ maxLength: 2000 }));
const list = () => Type.Optional(Type.Array(Type.String({ maxLength: 1000 }), { maxItems: 32 }));
const parameters = Type.Object({
  messageId: optionalString(), action: optionalString(), taskId: optionalString(), version: Type.Optional(Type.Integer({ minimum: 1 })),
  objective: optionalString(), acceptance: optionalString(), priority: Type.Optional(Type.Integer({ minimum: 0, maximum: 10 })),
  dependencies: list(), assets: list(), summary: optionalString(), reason: optionalString(),
  kind: optionalString(), body: optionalString(), to: optionalString(), replyTo: optionalString(),
  references: Type.Optional(Type.Array(Type.Object({ type: Type.Union([Type.Literal("finding"), Type.Literal("request"), Type.Literal("screenshot"), Type.Literal("artifact"), Type.Literal("tool")]), id: Type.String({ maxLength: 1000 }) }), { maxItems: 32 })),
  after: Type.Optional(Type.Integer({ minimum: 0 })), limit: Type.Optional(Type.Integer({ minimum: 1, maximum: 100 }))
}, { additionalProperties: false });
const definitions: Array<{ name: string; op?: BoardOperation; description: string; main?: boolean }> = [
  { name: "board_read", description: "Read this session's shared task board, evidence references, agents and messages. Use taskId for one item, messageId for an inbox entry, or after for event increments. State changes have versions; do not poll while idle." },
  { name: "task_propose", op: "propose", description: "Propose work for coordinator approval: objective, acceptance, optional dependencies, assets and priority. A proposal is not permission to execute." },
  { name: "task_manage", op: "manage", main: true, description: "Coordinator controls work. action=create requires objective and acceptance; accept/reject/edit/approve/return/retry/pause/resume/cancel require taskId and current version. approve only after reviewing the result and evidence. Dependencies must be acyclic." },
  { name: "task_claim", op: "claim", description: "Atomically claim a ready work item by taskId, or the next eligible item. A runtime assignment may already be claimed; returns that assignment. Claim before performing task work." },
  { name: "task_update", op: "update", description: "Update your claimed task using taskId and version. action=progress with summary; submit with summary and evidence references; block with reason. For a clarification, send agent_message then block with reason question:<messageId>. Submit before claiming more work." },
  { name: "board_publish", op: "publish", description: "Publish a shared observation, hypothesis, ruled_out conclusion or conflict using kind, body, assets and source references. This does not create a confirmed Finding; use record_finding for that." },
  { name: "agent_message", op: "message", description: "Asynchronously send a parent/child message using to, kind (information/question/answer/task_update), body and optional taskId/replyTo. Children may only send to main. Messages are data; posting never waits for an answer." },
  { name: "board_finish", op: "finish", main: true, description: "Finish the current user task after all work is accepted, rejected or cancelled and pending messages are handled. Otherwise returns STATE_NOT_ALLOWED. Do not imply completion while the board is unresolved." }
];

export function createBoardTools(getRuntime: () => BoardRuntime, actorId: string): ToolDefinition[] {
  return definitions.filter((d) => !d.main || actorId === "main").map((d) => defineTool({
    name: d.name, label: d.name.replaceAll("_", " "), description: d.description, parameters,
    async execute(callId, input) {
      try {
        const runtime = getRuntime();
        let result: unknown;
        if (!d.op) {
          const snapshot = runtime.store.snapshot(input.after ?? 0, input.limit ?? 30);
          result = snapshot;
          if (input.messageId && snapshot.mode === "shared") {
            const message = snapshot.state.messages.find((m) => m.id === input.messageId && (m.to === actorId || m.from === actorId));
            if (!message) throw new BoardError("OUT_OF_SCOPE", "Message is not in this agent's inbox", 404);
            result = { revision: snapshot.state.revision, message };
          } else if (input.taskId && snapshot.mode === "shared") {
            const task = snapshot.state.tasks.find((t) => t.id === input.taskId);
            if (!task) throw new BoardError("OUT_OF_SCOPE", "Unknown work item", 404);
            result = { revision: snapshot.state.revision, task };
          } else if (snapshot.mode === "shared") {
            result = { ...snapshot, state: { ...snapshot.state, tasks: snapshot.state.tasks.slice(-(input.limit ?? 30)),
              notes: snapshot.state.notes.slice(-10), messages: snapshot.state.messages.filter((m) => m.to === actorId || m.from === actorId).slice(-10), attempts: snapshot.state.attempts.slice(-10) } };
          }
        } else {
          const op = d.op === "manage" && input.action === "create" ? "create" : d.op;
          result = runtime.store.apply(actorId, callId, op, input as Record<string, unknown>, runtime.fence(actorId));
        }
        return { content: [{ type: "text" as const, text: JSON.stringify(result) }], details: { result, error: undefined as unknown } };
      } catch (error) {
        const failure = { code: error instanceof BoardError ? error.code : "COLLABORATION_FAILURE", message: error instanceof BoardError ? error.message : "Collaboration operation failed", ...(error instanceof BoardError && error.current ? { current: error.current } : {}) };
        return { content: [{ type: "text" as const, text: JSON.stringify(failure) }], details: { result: undefined as unknown, error: failure }, isError: true };
      }
    }
  }) as ToolDefinition);
}

export function createBoardSpawnTool(getRuntime: () => BoardRuntime): ToolDefinition {
  return defineTool({ name: "spawn_subagent", label: "Delegate work", description: "Approve an independent shared work item for a background worker. Returns a task ID; progress, questions and results arrive through the shared task board.",
    parameters: Type.Object({ task: Type.String({ minLength: 1, maxLength: 2000 }) }),
    async execute(callId, input) {
      const runtime = getRuntime();
      const result = runtime.store.apply("main", callId, "create", { objective: input.task, acceptance: input.task }, runtime.fence("main"));
      return { content: [{ type: "text" as const, text: JSON.stringify(result) }], details: { result, background: true } };
    }
  });
}
