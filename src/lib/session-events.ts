import type { Dispatch, SetStateAction } from "react";
import type { ApprovalRequest, ContextUsage, Finding, RiftxEvent, SessionSummary, SubagentTask, SubagentTaskPatch } from "@/lib/types";
import type { MergeableMessage } from "@/lib/message-merge";
import { summarizeToolResult } from "@/lib/tool-result";
import { isAlreadyProcessingError } from "@/lib/prompt-mode";

/**
 * SSE event application: the pure-ish dispatch that folds one RiftxEvent into
 * the workbench state. Every dependency (setters, queues, i18n) is injected,
 * so the stream-reconnection semantics are unit-testable without React.
 */

export type MessageDelta = { role: "assistant" | "thinking"; content: string };

export type SessionEventContext = {
  activeId: string;
  t: (key: Parameters<ReturnType<typeof import("@/lib/i18n").useLanguage>["t"]>[0], params?: Record<string, string>) => string;
  queueMessageDelta: (delta: MessageDelta) => void;
  flushMessageDeltas: () => void;
  setMessages: (updater: (current: MergeableMessage[]) => MergeableMessage[]) => void;
  setFindings: Dispatch<SetStateAction<Finding[]>>;
  queueSubagentTask: (task: SubagentTask) => void;
  queueSubagentTaskPatch: (patch: SubagentTaskPatch) => void;
  setApprovalQueue: Dispatch<SetStateAction<ApprovalRequest[]>>;
  setUsage: Dispatch<SetStateAction<ContextUsage>>;
  setSessions: Dispatch<SetStateAction<SessionSummary[]>>;
  setSessionRunning: (sessionId: string, running: boolean) => void;
  setMainAgentRunning: (value: boolean) => void;
  setContextCompacting: (value: boolean) => void;
  setStreamReady: (value: boolean) => void;
  reconcileMessages: () => void;
  setError: (value: string) => void;
};

function isSubagentApproval(request: ApprovalRequest) {
  return Boolean(request.subagentId);
}

export function applyMessageDeltas(current: MergeableMessage[], deltas: MessageDelta[]) {
  return deltas.reduce((messages: MergeableMessage[], delta) => {
    let next = messages;
    let last = next[next.length - 1];
    if (delta.role === "assistant" && last?.role === "thinking" && last.status === "streaming") {
      next = [...next.slice(0, -1), { ...last, status: "done" }];
      last = next[next.length - 1];
    }
    if (last?.role === delta.role) return [...next.slice(0, -1), { ...last, content: last.content + delta.content, status: delta.role === "thinking" ? "streaming" : last.status }];
    return [...next, { id: crypto.randomUUID(), role: delta.role, content: delta.content, status: delta.role === "thinking" ? "streaming" : undefined }];
  }, current);
}

export function normalizeMessages(items: MergeableMessage[]) {
  return items.filter((item) => ["user", "assistant", "thinking", "tool"].includes(item.role)).map((item) => ({ ...item, role: item.role as MergeableMessage["role"] }));
}

export function applyRiftxEvent(payload: RiftxEvent, ctx: SessionEventContext) {
  if (payload.type === "connected") {
    // `connected` is emitted only after the server-side Session listener is
    // installed. Reconcile the disk-backed snapshot to close the unavoidable
    // fetch-to-subscribe gap after a WebUI restart or EventSource reconnect.
    ctx.setStreamReady(true);
    ctx.reconcileMessages();
    return;
  }
  if (payload.type === "text_delta" || payload.type === "thinking_delta") {
    ctx.queueMessageDelta({ role: payload.type === "text_delta" ? "assistant" : "thinking", content: String(payload.delta ?? "") });
    return;
  }
  ctx.flushMessageDeltas();
  if (payload.type === "finding" && payload.finding) {
    const finding = payload.finding;
    ctx.setFindings((current) => current.some((item) => item.id === finding.id) ? current.map((item) => item.id === finding.id ? finding : item) : [...current, finding]);
    return;
  }
  if (payload.type === "findingPatch" && payload.findingPatch) {
    const findingPatch = payload.findingPatch;
    ctx.setFindings((current) => current.map((item) => item.id === findingPatch.id ? { ...item, ...findingPatch } : item));
    return;
  }
  if ((payload.type.startsWith("subagent_") || payload.type === "approval_decided") && payload.task) {
    ctx.queueSubagentTask(payload.task as SubagentTask);
  }
  if ((payload.type.startsWith("subagent_") || payload.type === "approval_decided") && payload.taskPatch) {
    ctx.queueSubagentTaskPatch(payload.taskPatch as SubagentTaskPatch);
  }
  if (payload.type === "approval_decided" && typeof payload.approvalId === "string") {
    ctx.setApprovalQueue((current) => current.filter((item) => item.id !== payload.approvalId));
  }
  if (payload.type === "usage") {
    const next = payload.usage as Partial<ContextUsage>;
    ctx.setUsage((current) => ({
      ...current,
      ...next,
      tokens: Number(next.tokens ?? current.tokens),
      contextWindow: Number(next.contextWindow ?? current.contextWindow),
      percent: next.percent === null ? null : Math.min(100, Math.max(0, Number(next.percent ?? current.percent ?? 0))),
      input: next.input === null ? null : next.input === undefined ? current.input : Number(next.input),
      output: next.output === null ? null : next.output === undefined ? current.output : Number(next.output),
      cacheRead: next.cacheRead === null ? null : next.cacheRead === undefined ? current.cacheRead : Number(next.cacheRead),
      cacheWrite: next.cacheWrite === null ? null : next.cacheWrite === undefined ? current.cacheWrite : Number(next.cacheWrite),
      remaining: Number(next.remaining ?? current.remaining)
    }));
    ctx.setSessions((current) => current.map((session) => session.id === ctx.activeId ? {
      ...session,
      contextWindow: Number(next.contextWindow ?? session.contextWindow ?? 0),
      usage: {
        ...(session.usage ?? { tokens: 0, contextWindow: Number(next.contextWindow ?? session.contextWindow ?? 0), percent: 0, input: null, output: null, cacheRead: null, cacheWrite: null, remaining: 0 }),
        ...next,
        tokens: Number(next.tokens ?? session.usage?.tokens ?? 0),
        contextWindow: Number(next.contextWindow ?? session.usage?.contextWindow ?? session.contextWindow ?? 0),
        percent: next.percent === null ? null : Math.min(100, Math.max(0, Number(next.percent ?? session.usage?.percent ?? 0))),
        input: next.input === null ? null : next.input === undefined ? session.usage?.input ?? null : Number(next.input),
        output: next.output === null ? null : next.output === undefined ? session.usage?.output ?? null : Number(next.output),
        cacheRead: next.cacheRead === null ? null : next.cacheRead === undefined ? session.usage?.cacheRead ?? null : Number(next.cacheRead),
        cacheWrite: next.cacheWrite === null ? null : next.cacheWrite === undefined ? session.usage?.cacheWrite ?? null : Number(next.cacheWrite),
        remaining: Number(next.remaining ?? session.usage?.remaining ?? 0)
      }
    } : session));
    return;
  }
  if (payload.type === "approval_required") {
    const request = payload.approval as ApprovalRequest;
    if (payload.taskPatch) ctx.queueSubagentTaskPatch(payload.taskPatch as SubagentTaskPatch);
    ctx.setApprovalQueue((current) => current.some((item) => item.id === request.id) ? current : [...current, request]);
    if (!request.subagentId) {
      ctx.setMainAgentRunning(true);
      ctx.setSessionRunning(ctx.activeId, true);
    }
    return;
  }
  if (payload.type.startsWith("subagent_") || payload.type === "approval_decided") return;
  if (["tool_start", "tool_status", "tool_update", "tool_end", "message", "done", "error"].includes(payload.type)) {
    ctx.setMessages((current) => current.map((message) => message.role === "thinking" && message.status === "streaming" ? { ...message, status: "done" } : message));
  }
  if (payload.type === "message" && payload.turnEnd) {
    // turn_end carries the authoritative completed assistant message. Rather
    // than trusting that every transient text_delta arrived, reload the
    // persisted branch and merge it with the local in-flight tail.
    ctx.reconcileMessages();
    return;
  }
  if (payload.type === "message") return;
  if (payload.type === "session_state") {
    const running = payload.state !== "idle";
    ctx.setMainAgentRunning(running);
    ctx.setSessionRunning(ctx.activeId, running);
    ctx.setContextCompacting(payload.state === "compacting");
    return;
  }
  if (payload.type === "done") {
    // `done` may be the only terminal event received after a brief SSE gap.
    // A final reconciliation makes completed replies visible without forcing
    // the user to switch sessions and back.
    ctx.reconcileMessages();
    ctx.setMainAgentRunning(false);
    ctx.setSessionRunning(ctx.activeId, false);
    ctx.setContextCompacting(false);
    ctx.setApprovalQueue((current) => current.filter(isSubagentApproval));
    ctx.setMessages((current) => current.map((message) => message.role === "thinking" ? { ...message, status: "done" } : message.role === "tool" && (message.status === "running" || message.status === "queued") ? { ...message, status: "cancelled", isError: true, content: message.content ? `${message.content}\n\n${ctx.t("stopped")}` : ctx.t("stopped") } : message));
    return;
  }
  if (payload.type === "tool_status") {
    const toolCallId = String(payload.toolCallId ?? "");
    if (payload.toolStatus === "queued" || payload.toolStatus === "running") ctx.setMessages((current) => current.map((message) => message.id === toolCallId ? { ...message, status: payload.toolStatus } : message));
    return;
  }
  if (payload.type === "tool_start") {
    const toolCallId = String(payload.toolCallId ?? crypto.randomUUID());
    ctx.setMessages((current) => {
      const existingIndex = current.findIndex((message) => message.toolCallId === toolCallId || message.id === toolCallId);
      const nextMessage = { id: toolCallId, role: "tool" as const, toolCallId, toolName: String(payload.toolName ?? "tool"), content: JSON.stringify(payload.args ?? {}, null, 2), status: payload.toolStatus === "queued" ? ("queued" as const) : ("running" as const) };
      if (existingIndex < 0) return [...current, nextMessage];
      return current.map((message, index) => index === existingIndex ? { ...message, ...nextMessage } : message);
    });
    return;
  }
  if (payload.type === "tool_update") {
    // Keep the tool detail stable while a command is running. Streaming
    // partial tool output causes the approval-opened card to flash and
    // reshape continuously; only the final tool result should replace the
    // original argument preview.
    return;
  }
  if (payload.type === "tool_end") {
    const toolCallId = String(payload.toolCallId ?? "");
    ctx.setMessages((current) => current.map((message) => message.id === toolCallId ? { ...message, status: payload.isError ? "error" : "done", isError: Boolean(payload.isError), content: summarizeToolResult(payload.result) } : message));
    return;
  }
  if (payload.type === "error") {
    const message = String(payload.error ?? "Agent error");
    if (isAlreadyProcessingError(message)) return;
    ctx.setMainAgentRunning(false);
    ctx.setSessionRunning(ctx.activeId, false);
    ctx.setContextCompacting(false);
    ctx.setApprovalQueue((current) => current.filter(isSubagentApproval));
    ctx.setError(message);
  }
}
