import type { BoardRuntime } from "./runtime";
const getCollaboration = async (id: string) => (await import("@/server/pi/session-manager")).getCollaboration(id);
import { errorResponse } from "@/server/errors";
import { BoardError } from "./store";

export type CollaborationRequest = "read" | "messages" | "control" | "task" | "agent";
export async function collaborationResponse(request: Request, sessionId: string, kind: CollaborationRequest, entityId?: string, resolveBoard: (id: string) => Promise<BoardRuntime | undefined> = getCollaboration) {
  try {
    const runtime = await resolveBoard(sessionId);
    if (!runtime) {
      if (kind === "read") return Response.json({ mode: "legacy" });
      throw new BoardError("STATE_NOT_ALLOWED", "This session uses the legacy collaboration workflow");
    }
    if (kind === "read") {
      const url = new URL(request.url);
      const after = Number(url.searchParams.get("after") ?? 0);
      const limit = Number(url.searchParams.get("limit") ?? 100);
      if (!Number.isSafeInteger(after) || after < 0 || !Number.isSafeInteger(limit) || limit < 1 || limit > 500) throw new BoardError("INVALID_INPUT", "Invalid event cursor or page size", 400);
      return Response.json(runtime.store.snapshot(after, limit), { headers: { "Cache-Control": "no-store" } });
    }
    const raw = await request.text();
    if (raw.length > 16000) throw new BoardError("INVALID_INPUT", "Request too large", 413);
    let input: Record<string, unknown>;
    try { input = JSON.parse(raw); } catch { throw new BoardError("INVALID_INPUT", "Invalid JSON", 400); }
    if (!input || typeof input !== "object" || Array.isArray(input)) throw new BoardError("INVALID_INPUT", "Expected a command object", 400);
    if (typeof input.commandId !== "string" || !input.commandId.trim()) throw new BoardError("INVALID_INPUT", "commandId is required", 400);
    const { commandId, ...payload } = input;
    const allowed = kind === "messages" ? ["to", "kind", "body", "taskId", "replyTo"]
      : kind === "control" ? ["action", "tasks", "wakes", "messages", "retries"]
      : kind === "task" ? ["action", "version", "confirmStopped"] : ["action"];
    if (Object.keys(payload).some((key) => !allowed.includes(key))) throw new BoardError("OUT_OF_SCOPE", "Unsupported command fields", 400);
    if (kind === "control" && !["pause", "resume", "limits"].includes(String(payload.action))) throw new BoardError("INVALID_INPUT", "Invalid board action", 400);
    if (kind === "task" && !["pause", "resume", "cancel", "retry"].includes(String(payload.action))) throw new BoardError("INVALID_INPUT", "Invalid work action", 400);
    if (kind === "agent" && !["pause", "resume"].includes(String(payload.action))) throw new BoardError("INVALID_INPUT", "Invalid agent action", 400);
    if (payload.confirmStopped === true && entityId) {
      const owner = runtime.store.read().tasks.find((t) => t.id === entityId)?.owner;
      if (owner && runtime.isExecuting(owner)) throw new BoardError("STATE_NOT_ALLOWED", "The previous execution is still active; wait for its tools to stop");
    }
    const op = kind === "messages" ? "message" : kind === "task" ? "manage" : kind === "agent" ? "agent_control" : "control";
    const args = { ...payload, ...(kind === "task" ? { taskId: entityId } : kind === "agent" ? { agentId: entityId } : {}) };
    const result = runtime.store.apply("user", commandId, op, args);
    // The mutation is durable before cancellation/notification can yield.
    if (payload.action === "pause" || payload.action === "cancel") await runtime.stopInvalidExecutions();
    runtime.kick();
    return Response.json({ result, snapshot: runtime.store.snapshot() });
  } catch (error) {
    if (error instanceof BoardError) return Response.json({ error: error.message, code: error.code, current: error.current }, { status: error.status });
    return errorResponse(error, "Collaboration operation failed");
  }
}
