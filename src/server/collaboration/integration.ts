import { projectSubagents } from "./projection";
import { randomUUID } from "node:crypto";
import { join } from "node:path";
import { BOARD_CONTEXT_TYPE, BOARD_MESSAGE_TYPE, type BoardAgent } from "@/lib/collaboration";
import type { SessionRecord } from "@/server/pi/session-registry";
import { enqueueSessionAction } from "@/server/pi/session-join";
import { extractLastAssistantResult } from "@/server/pi/subagent-result";
import { waitForAgentEvents } from "@/server/pi/pi-internals";
import { shutdownSessionRecord } from "@/server/pi/session-shutdown";
import { BoardError, BoardStore } from "./store";
import { BoardRuntime, collaborationContext, type CollaborationActor, type CollaborationPacket } from "./runtime";

export function boardPath(root: string, sessionId: string) {
  if (!/^[A-Za-z0-9_-]+$/.test(sessionId)) throw new BoardError("OUT_OF_SCOPE", "Invalid session ID", 400);
  return join(root, "collaboration", sessionId, "board.sqlite");
}

export function createSessionBoard(record: SessionRecord, store: BoardStore, createChild: (agent: BoardAgent) => Promise<SessionRecord>) {
  const records = new Map<string, SessionRecord>([["main", record]]);
  record.collaborationChildren = records;
  // eslint-disable-next-line prefer-const
  let runtime!: BoardRuntime;
  const adapter = (id: string, target: SessionRecord): CollaborationActor => {
    let lastTask: string | undefined;
    const message = (packet: CollaborationPacket) => ({ customType: BOARD_MESSAGE_TYPE, content: packet.content, display: false,
      details: { board: store.sessionId, revision: packet.revision, messageIds: packet.messageIds, recipient: id } });
    return {
      busy: () => target.session.isStreaming || Boolean(target.compacting) || Boolean(target.pendingActions),
      run: (packet) => enqueueSessionAction(target, async () => {
        if (store.read().status !== "running" || target.aborting || target.shutdownPromise) throw new BoardError("SESSION_PAUSED", "Agent was paused before delivery");
        store.assertFence(store.read(), id, runtime.fence(id));
        const currentTask = store.read().agents.find((a) => a.id === id)?.taskId;
        if (currentTask !== lastTask) { target.gate.beginTask(); lastTask = currentTask; }
        await target.session.sendCustomMessage(message(packet), { triggerTurn: true });
        await waitForAgentEvents(target.session);
        const result = extractLastAssistantResult(target.sessionManager.getBranch());
        return { summary: result.summary, error: Boolean(result.error) };
      }),
      steer: async (packet) => {
        if (store.read().status !== "running") throw new BoardError("SESSION_PAUSED", "Agent was paused before delivery");
        await target.session.sendCustomMessage(message(packet), { deliverAs: "steer" });
      },
      stop: async () => {
        target.abortEpoch = (target.abortEpoch ?? 0) + 1;
        target.gate.rejectAll(); target.session.abortCompaction(); target.session.abortBash();
        let timer: ReturnType<typeof setTimeout> | undefined;
        try {
          return await Promise.race([
            Promise.all([target.session.abort(), target.browser?.close()]).then(() => true, () => false),
            new Promise<boolean>((resolve) => { timer = setTimeout(() => resolve(false), 15_000); })
          ]);
        } finally { clearTimeout(timer); }
      }
    };
  };
  runtime = new BoardRuntime(store, {
    actor: async (id) => {
      let target = records.get(id);
      if (!target) {
        const identity = store.read().agents.find((a) => a.id === id);
        if (!identity) throw new BoardError("OUT_OF_SCOPE", "Unknown agent", 404);
        target = await createChild(identity); records.set(id, target);
        target.emitter.on("event", (event: import("@/lib/types").RiftxEvent) => {
          if (event.type.startsWith("approval_")) {
            const approval = event.approval ? { ...event.approval, subagentId: id, threadId: target!.id, agentName: identity.name,
              taskSummary: store.read().tasks.find((t) => t.owner === id && t.status === "running")?.objective } : undefined;
            store.apply("system", randomUUID(), "agent_meta", { agentId: id, pendingApprovalCount: target!.gate.pendingRequests().length });
            record.emitter.emit("event", { ...event, approval, subagentId: id });
          }
          if (["tool_end", "done", "error"].includes(event.type)) record.emitter.emit("event", { type: "collaboration_activity", agentId: id, activity: event });
        });
        store.apply("system", randomUUID(), "agent_meta", { agentId: id, transcript: target.session.sessionFile, model: `${target.profile.provider}/${target.profile.model}`, recreated: Boolean(identity.transcript) });
      }
      return adapter(id, target);
    },
    release: async (id) => {
      const target = records.get(id); if (!target || id === "main") return;
      await shutdownSessionRecord(target); records.delete(id);
    },
    emit: (event) => {
      record.emitter.emit("event", { type: "collaboration", collaboration: event });
      for (const task of projectSubagents(store.read())) record.emitter.emit("event", { type: "subagent_snapshot", task });
    }
  });
  // Link before the first user turn. Existing records are paused; merely opening
  // the page never schedules a model call.
  runtime.registerActor("main", adapter("main", record));
  return runtime;
}

export function installBoardContext(record: SessionRecord, runtime: BoardRuntime, actorId: string) {
  const previous = record.session.agent.transformContext;
  let sampledIds: string[] = [];
  const previousUnsubscribe = record.collaborationUnsubscribe;
  const unsubscribe = record.session.subscribe((event) => {
    if (event.type === "message_end" && event.message.role === "assistant" && !["error", "aborted"].includes(event.message.stopReason)) {
      runtime.included(actorId, sampledIds); sampledIds = [];
    }
  });
  record.collaborationUnsubscribe = () => { unsubscribe(); previousUnsubscribe?.(); };
  record.session.agent.transformContext = async (messages, signal) => {
    if (signal?.aborted) return messages;
    const refresh = (input: typeof messages) => {
      const state = runtime.store.read();
      runtime.store.assertFence(state.status === "completed" ? { ...state, status: "running" } : state, actorId, runtime.fence(actorId));
      const packet = collaborationContext(state, actorId);
      const retained = input.filter((m) => !(m.role === "custom" && [BOARD_CONTEXT_TYPE, BOARD_MESSAGE_TYPE].includes(m.customType)));
      retained.push({ role: "custom", customType: BOARD_CONTEXT_TYPE, content: packet.content, display: false, timestamp: Date.now() });
      return { messages: retained, ids: packet.ids };
    };
    const prepared = refresh(messages);
    const transformed = previous ? await previous(prepared.messages, signal) : prepared.messages;
    if (signal?.aborted) return transformed;
    const latest = refresh(transformed);
    sampledIds = latest.ids;
    return latest.messages;
  };
}
