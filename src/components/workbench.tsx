"use client";

import Link from "next/link";
import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { Archive, ArrowDown, ArrowUp, Brain, Command, FolderOpen, Gear, List, Plus, Stop, WarningCircle, X } from "@phosphor-icons/react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { ApprovalModeMenu, ContextRing, ErrorNotice, LanguageToggle, ModelMenu, RiftxLogo, ThemeToggle } from "./ui";
import { SubagentPanel } from "./subagent-panel";
import { SUBAGENT_LOG_LIMITS, type ApprovalMode, type ApprovalRequest, type ContextUsage, type ModelProfile, type RiftxEvent, type SessionSummary, type SubagentTask, type SubagentTaskPatch } from "@/lib/types";
import { useLanguage } from "@/lib/i18n";

type Message = { id: string; role: "user" | "assistant" | "thinking" | "tool"; content: string; toolName?: string; toolCallId?: string; status?: string; isError?: boolean };

function makeEmptyUsage(contextWindow = 0): ContextUsage {
  const safeWindow = Number.isFinite(contextWindow) && contextWindow > 0 ? contextWindow : 0;
  return { tokens: 0, contextWindow: safeWindow, percent: safeWindow > 0 ? 0 : null, input: null, output: null, cacheRead: null, cacheWrite: null, remaining: safeWindow };
}

function usageFromSession(session?: SessionSummary | null): ContextUsage {
  if (session?.usage) return { ...session.usage };
  return makeEmptyUsage(session?.contextWindow ?? 0);
}

function trimSubagentLogContent(content: string) {
  if (content.length <= SUBAGENT_LOG_LIMITS.content) return content;
  return content.slice(-SUBAGENT_LOG_LIMITS.content);
}

function sameSubagentTask(left: SubagentTask, right: SubagentTask) {
  const leftLog = left.logs.at(-1);
  const rightLog = right.logs.at(-1);
  return left.status === right.status
    && left.threadId === right.threadId
    && left.model === right.model
    && left.summary === right.summary
    && left.error === right.error
    && left.pendingApprovalCount === right.pendingApprovalCount
    && left.logs.length === right.logs.length
    && leftLog?.content === rightLog?.content
    && leftLog?.status === rightLog?.status;
}

function mergeSubagentTasks(current: SubagentTask[], incoming: SubagentTask[]) {
  const next = current.slice();
  let changed = false;
  for (const task of incoming) {
    const index = next.findIndex((item) => item.id === task.id);
    if (index < 0) {
      next.push(task);
      changed = true;
      continue;
    }
    const previous = next[index];
    // An initial REST snapshot can arrive after SSE has already delivered newer
    // logs/status. Never let that stale snapshot roll a task backwards.
    const stale = previous.logs.length > task.logs.length
      || (previous.status === "running" && task.status === "queued")
      || (previous.finishedAt && !task.finishedAt);
    if (!stale && !sameSubagentTask(previous, task)) {
      next[index] = task;
      changed = true;
    }
  }
  return changed ? next.sort((left, right) => left.createdAt.localeCompare(right.createdAt)) : current;
}

function cloneSubagentTask(task: SubagentTask): SubagentTask {
  return {
    ...task,
    logs: task.logs.map((log) => ({ ...log }))
  };
}

function applySubagentTaskPatch(task: SubagentTask, patch: SubagentTaskPatch) {
  const next = cloneSubagentTask(task);
  if (patch.threadId !== undefined) next.threadId = patch.threadId;
  if (patch.model !== undefined) next.model = patch.model;
  if (patch.pendingApprovalCount !== undefined) next.pendingApprovalCount = patch.pendingApprovalCount;
  if (patch.appendLog) {
    const existingLogIndex = next.logs.findIndex((log) => log.id === patch.appendLog?.id);
    const normalizedLog = { ...patch.appendLog, content: trimSubagentLogContent(patch.appendLog.content) };
    if (existingLogIndex >= 0) next.logs[existingLogIndex] = { ...next.logs[existingLogIndex], ...normalizedLog };
    else next.logs.push(normalizedLog);
  }
  if (patch.patchLog) {
    const logIndex = next.logs.findIndex((log) => log.id === patch.patchLog?.id);
    if (logIndex >= 0) {
      const target = { ...next.logs[logIndex] };
      if (patch.patchLog.content !== undefined) target.content = trimSubagentLogContent(patch.patchLog.content);
      if (patch.patchLog.appendContent) target.content = trimSubagentLogContent(target.content + patch.patchLog.appendContent);
      if (patch.patchLog.status !== undefined) target.status = patch.patchLog.status;
      next.logs[logIndex] = target;
    }
  }
  if (next.logs.length > SUBAGENT_LOG_LIMITS.entries) next.logs = next.logs.slice(-SUBAGENT_LOG_LIMITS.entries);
  return next;
}

function mergeSubagentTaskPatch(current: SubagentTask[], patch: SubagentTaskPatch) {
  const index = current.findIndex((task) => task.id === patch.id);
  if (index < 0) return current;
  const updated = applySubagentTaskPatch(current[index], patch);
  if (sameSubagentTask(current[index], updated)) return current;
  const next = current.slice();
  next[index] = updated;
  return next;
}

function isSubagentApproval(request: ApprovalRequest) {
  return Boolean(request.subagentId);
}

function formatApprovalInput(input: unknown) {
  if (input && typeof input === "object" && typeof (input as { command?: unknown }).command === "string") return (input as { command: string }).command;
  try {
    return JSON.stringify(input, null, 2) ?? String(input);
  } catch {
    return String(input);
  }
}

function summarizeApprovalInput(input: unknown) {
  return formatApprovalInput(input).replace(/\s+/g, " ").trim();
}

function ToolCard({ message }: { message: Message }) {
  const { language, t } = useLanguage();
  const [open, setOpen] = useState(message.status === "running");
  useEffect(() => { setOpen(message.status === "running"); }, [message.status]);
  return <details className={`tool-card ${message.isError ? "error" : ""}`} open={open} onToggle={(event) => setOpen(event.currentTarget.open)}>
    <summary className="tool-card-head"><span><Command size={14} />{message.toolName}</span><span className={`tool-status ${message.status}`}>{message.status === "running" ? t("running") : message.status === "error" ? t("failed") : message.status === "cancelled" ? t("stopped") : t("complete")}</span></summary>
    <pre>{message.content}</pre>
  </details>;
}

export function Workbench() {
  const { language, t } = useLanguage();
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [activeId, setActiveId] = useState("");
  const [messages, setMessages] = useState<Message[]>([]);
  const [usage, setUsage] = useState<ContextUsage>(() => makeEmptyUsage());
  const [modelName, setModelName] = useState("No model configured");
  const [modelProfiles, setModelProfiles] = useState<ModelProfile[]>([]);
  const [activeProfileId, setActiveProfileId] = useState("");
  const [approvalMode, setApprovalMode] = useState<ApprovalMode>("request");
  const [cwd, setCwd] = useState("");
  const [workspaceChoosing, setWorkspaceChoosing] = useState(false);
  const [input, setInput] = useState("");
  const [mainAgentRunning, setMainAgentRunning] = useState(false);
  const [bootstrapping, setBootstrapping] = useState(true);
  const [approvalQueue, setApprovalQueue] = useState<ApprovalRequest[]>([]);
  const [subagents, setSubagents] = useState<SubagentTask[]>([]);
  const [maxConcurrentSubagents, setMaxConcurrentSubagents] = useState(3);
  const [error, setError] = useState("");
  const [mobileNav, setMobileNav] = useState(false);
  const [showJumpToLatest, setShowJumpToLatest] = useState(false);
  const [streamGeneration, setStreamGeneration] = useState(0);
  const conversationRef = useRef<HTMLElement>(null);
  const conversationInnerRef = useRef<HTMLDivElement>(null);
  const shouldAutoScrollRef = useRef(true);
  const endRef = useRef<HTMLDivElement>(null);
  const composerInputRef = useRef<HTMLTextAreaElement>(null);
  const subagentsRef = useRef<SubagentTask[]>([]);
  const subagentQueueRef = useRef(new Map<string, SubagentTask>());
  const subagentPatchQueueRef = useRef(new Map<string, SubagentTaskPatch[]>());
  const subagentFlushFrameRef = useRef<number | undefined>(undefined);
  const titleQueueRef = useRef<Promise<void>>(Promise.resolve());
  const titleRequestRef = useRef(0);

  useLayoutEffect(() => {
    const textarea = composerInputRef.current;
    if (!textarea) return;
    const maxHeight = 200;
    textarea.style.height = "auto";
    const nextHeight = Math.min(textarea.scrollHeight, maxHeight);
    textarea.style.height = `${Math.max(48, nextHeight)}px`;
    textarea.style.overflowY = textarea.scrollHeight > maxHeight ? "auto" : "hidden";
  }, [input]);

  const scheduleSubagentFlush = () => {
    if (subagentFlushFrameRef.current !== undefined) return;
    subagentFlushFrameRef.current = requestAnimationFrame(() => {
      subagentFlushFrameRef.current = undefined;
      const pendingTasks = [...subagentQueueRef.current.values()];
      subagentQueueRef.current.clear();
      const pendingPatchEntries = [...subagentPatchQueueRef.current.entries()];
      subagentPatchQueueRef.current.clear();
      let next = mergeSubagentTasks(subagentsRef.current, pendingTasks);
      const carry = new Map<string, SubagentTaskPatch[]>();
      for (const [taskId, patches] of pendingPatchEntries) {
        if (!next.some((task) => task.id === taskId)) {
          carry.set(taskId, patches);
          continue;
        }
        for (const patch of patches) next = mergeSubagentTaskPatch(next, patch);
      }
      subagentsRef.current = next;
      setSubagents(next);
      for (const [taskId, patches] of carry) {
        const existing = subagentPatchQueueRef.current.get(taskId) ?? [];
        subagentPatchQueueRef.current.set(taskId, [...patches, ...existing]);
      }
      if (subagentQueueRef.current.size > 0) scheduleSubagentFlush();
    });
  };

  const queueSubagentTask = (task: SubagentTask, source: "event" | "snapshot" = "event") => {
    subagentQueueRef.current.set(task.id, cloneSubagentTask(task));
    if (source === "event") subagentPatchQueueRef.current.delete(task.id);
    scheduleSubagentFlush();
  };

  const queueSubagentTaskPatch = (patch: SubagentTaskPatch) => {
    const queue = subagentPatchQueueRef.current.get(patch.id) ?? [];
    queue.push({
      ...patch,
      appendLog: patch.appendLog ? { ...patch.appendLog } : undefined,
      patchLog: patch.patchLog ? { ...patch.patchLog } : undefined
    });
    subagentPatchQueueRef.current.set(patch.id, queue);
    scheduleSubagentFlush();
  };

  const backfillMissingTitles = (items: SessionSummary[]) => {
    const missing = items.filter((session) => (session.name === t("unnamed") || session.name === "Untitled task" || session.name === "未命名任务") && session.firstMessage).slice(0, 8);
    void missing.reduce<Promise<void>>((chain, session) => chain.then(async () => {
      try {
        const response = await fetch(`/api/sessions/${session.id}/title`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ text: session.firstMessage }) });
        if (!response.ok) return;
        const data = await response.json() as { sessions?: SessionSummary[] };
        if (!data.sessions) return;
        setSessions((current) => current.map((currentSession) => currentSession.name === t("summarizeTitle") ? currentSession : data.sessions?.find((next) => next.id === currentSession.id) ?? currentSession));
      } catch {
        // Title backfill is best-effort and should not block opening the workbench.
      }
    }), Promise.resolve());
  };

  useEffect(() => {
    fetch("/api/bootstrap").then((response) => response.json()).then((data) => {
      const initialSessions = (data.sessions ?? []) as SessionSummary[];
      setSessions(initialSessions);
      backfillMissingTitles(initialSessions);
      setActiveId(data.activeSessionId ?? "");
      setCwd(data.cwd ?? "");
      const profiles = (data.profiles ?? []) as ModelProfile[];
      setModelProfiles(profiles);
      setActiveProfileId(data.activeProfileId ?? "");
      const profile = profiles.find((item) => item.id === data.activeProfileId);
      if (profile) setModelName(`${profile.provider}/${profile.model}`);
      if (data.approvalMode === "request" || data.approvalMode === "auto" || data.approvalMode === "full") setApprovalMode(data.approvalMode);
    }).catch(() => setError(t("cannotConnect"))).finally(() => setBootstrapping(false));
    return undefined;
  }, []);

  useEffect(() => {
    subagentsRef.current = subagents;
  }, [subagents]);

  useEffect(() => {
    shouldAutoScrollRef.current = true;
    setShowJumpToLatest(false);
    subagentsRef.current = [];
    setSubagents([]);
    setApprovalQueue([]);
    const sessionMeta = sessions.find((session) => session.id === activeId);
    setUsage(usageFromSession(sessionMeta));
    if (sessionMeta?.provider && sessionMeta?.model) setModelName(`${sessionMeta.provider}/${sessionMeta.model}`);
    if (!activeId) return;
    const controller = new AbortController();
    fetch(`/api/sessions/${activeId}`, { signal: controller.signal }).then((response) => response.json()).then((data: Partial<SessionSummary>) => {
      if (controller.signal.aborted) return;
      const nextUsage = data.usage && typeof data.usage === "object"
        ? { ...makeEmptyUsage(Number(data.contextWindow ?? (data.usage as ContextUsage).contextWindow ?? 0)), ...(data.usage as ContextUsage) }
        : makeEmptyUsage(Number(data.contextWindow ?? 0));
      setUsage(nextUsage);
      if (data.provider && data.model) setModelName(`${data.provider}/${data.model}`);
      if (data.id) {
        setSessions((current) => current.map((session) => session.id === data.id ? { ...session, ...data, usage: nextUsage } : session));
      }
    }).catch(() => undefined);
    fetch(`/api/sessions/${activeId}/subagents`, { signal: controller.signal }).then((response) => response.json()).then((data: { tasks?: SubagentTask[]; running?: number; maxConcurrent?: number }) => {
      if (controller.signal.aborted) return;
      for (const task of (data.tasks ?? []).map(cloneSubagentTask)) queueSubagentTask(task, "snapshot");
      setMaxConcurrentSubagents(Number(data.maxConcurrent ?? 3));
    }).catch(() => undefined);
    fetch(`/api/sessions/${activeId}/messages`, { signal: controller.signal }).then((response) => response.json()).then((items: Message[]) => {
      if (controller.signal.aborted) return;
      setMessages(items.filter((item) => ["user", "assistant", "thinking", "tool"].includes(item.role)).map((item) => ({ ...item, role: item.role as Message["role"] })));
    }).catch(() => undefined);
    let disposed = false;
    let reconnectAttempts = 0;
    const source = new EventSource(`/api/sessions/${activeId}/stream`);
    source.onmessage = (event) => {
      if (disposed) return;
      reconnectAttempts = 0;
      let payload: RiftxEvent;
      try {
        payload = JSON.parse(event.data) as RiftxEvent;
      } catch {
        return;
      }
      if (payload.type === "connected") return;
      if ((payload.type.startsWith("subagent_") || payload.type === "approval_decided") && payload.task) {
        const task = payload.task as SubagentTask;
        queueSubagentTask(task);
      }
      if ((payload.type.startsWith("subagent_") || payload.type === "approval_decided") && payload.taskPatch) {
        queueSubagentTaskPatch(payload.taskPatch as SubagentTaskPatch);
      }
      if (payload.type === "usage") {
        const next = payload.usage as Partial<ContextUsage>;
        setUsage((current) => ({
          ...current,
          ...next,
          tokens: Number(next.tokens ?? current.tokens),
          contextWindow: Number(next.contextWindow ?? current.contextWindow),
          percent: next.percent === null ? null : Number(next.percent ?? current.percent ?? 0),
          input: next.input === null ? null : next.input === undefined ? current.input : Number(next.input),
          output: next.output === null ? null : next.output === undefined ? current.output : Number(next.output),
          cacheRead: next.cacheRead === null ? null : next.cacheRead === undefined ? current.cacheRead : Number(next.cacheRead),
          cacheWrite: next.cacheWrite === null ? null : next.cacheWrite === undefined ? current.cacheWrite : Number(next.cacheWrite),
          remaining: Number(next.remaining ?? current.remaining)
        }));
        setSessions((current) => current.map((session) => session.id === activeId ? {
          ...session,
          contextWindow: Number(next.contextWindow ?? session.contextWindow ?? 0),
          usage: {
            ...(session.usage ?? makeEmptyUsage(Number(next.contextWindow ?? session.contextWindow ?? 0))),
            ...next,
            tokens: Number(next.tokens ?? session.usage?.tokens ?? 0),
            contextWindow: Number(next.contextWindow ?? session.usage?.contextWindow ?? session.contextWindow ?? 0),
            percent: next.percent === null ? null : Number(next.percent ?? session.usage?.percent ?? 0),
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
        if (payload.taskPatch) queueSubagentTaskPatch(payload.taskPatch as SubagentTaskPatch);
        setApprovalQueue((current) => current.some((item) => item.id === request.id) ? current : [...current, request]);
        if (!request.subagentId) setMainAgentRunning(true);
        return;
      }
      if (payload.type.startsWith("subagent_") || payload.type === "approval_decided") return;
      if (["text_delta", "tool_start", "tool_update", "tool_end", "message", "done", "error"].includes(payload.type)) setMessages((current) => current.map((message) => message.role === "thinking" && message.status === "streaming" ? { ...message, status: "done" } : message));
      if (payload.type === "session_state") { setMainAgentRunning(payload.state !== "idle"); return; }
      if (payload.type === "done") { setMainAgentRunning(false); setApprovalQueue((current) => current.filter(isSubagentApproval)); setMessages((current) => current.map((message) => message.role === "thinking" ? { ...message, status: "done" } : message.role === "tool" && message.status === "running" ? { ...message, status: "cancelled", isError: true, content: message.content ? `${message.content}\n\n${t("stopped")}` : t("stopped") } : message)); return; }
      if (payload.type === "text_delta") {
        const delta = String(payload.delta ?? "");
        setMessages((current) => { const last = current[current.length - 1]; if (last?.role === "assistant") return [...current.slice(0, -1), { ...last, content: last.content + delta }]; return [...current, { id: crypto.randomUUID(), role: "assistant", content: delta }]; });
        return;
      }
      if (payload.type === "thinking_delta") {
        const delta = String(payload.delta ?? "");
        setMessages((current) => { const last = current[current.length - 1]; if (last?.role === "thinking") return [...current.slice(0, -1), { ...last, content: last.content + delta, status: "streaming" }]; return [...current, { id: crypto.randomUUID(), role: "thinking", content: delta, status: "streaming" }]; });
        return;
      }
      if (payload.type === "tool_start") {
        const toolCallId = String(payload.toolCallId ?? crypto.randomUUID());
        setMessages((current) => {
          const existingIndex = current.findIndex((message) => message.toolCallId === toolCallId || message.id === toolCallId);
          const nextMessage = { id: toolCallId, role: "tool" as const, toolCallId, toolName: String(payload.toolName ?? "tool"), content: JSON.stringify(payload.args ?? {}, null, 2), status: "running" };
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
        setMessages((current) => current.map((message) => message.id === toolCallId ? { ...message, status: payload.isError ? "error" : "done", isError: Boolean(payload.isError), content: JSON.stringify(payload.result ?? {}, null, 2) } : message)); return;
      }
      if (payload.type === "error") { setMainAgentRunning(false); setApprovalQueue((current) => current.filter(isSubagentApproval)); setError(String(payload.error ?? "Agent error")); }
    };
    source.onerror = () => {
      if (disposed) return;
      reconnectAttempts += 1;
      if (source.readyState === EventSource.CLOSED || reconnectAttempts >= 3) {
        setError(t("connectionLostRefresh"));
      }
    };
    return () => {
      disposed = true;
      controller.abort();
      source.close();
      subagentQueueRef.current.clear();
      subagentPatchQueueRef.current.clear();
      if (subagentFlushFrameRef.current !== undefined) {
        cancelAnimationFrame(subagentFlushFrameRef.current);
        subagentFlushFrameRef.current = undefined;
      }
    };
  }, [activeId, streamGeneration]);

  const subagentRunning = useMemo(() => subagents.filter((task) => task.status === "queued" || task.status === "running").length, [subagents]);
  const running = mainAgentRunning || subagentRunning > 0 || approvalQueue.length > 0;
  const composerBusy = mainAgentRunning || approvalQueue.some((item) => !item.subagentId);

  useLayoutEffect(() => {
    const conversation = conversationRef.current;
    if (conversation && shouldAutoScrollRef.current) conversation.scrollTop = conversation.scrollHeight;
  }, [messages]);

  const handleConversationScroll = () => {
    const conversation = conversationRef.current;
    if (!conversation) return;
    const atLatest = conversation.scrollHeight - conversation.clientHeight - conversation.scrollTop <= 24;
    shouldAutoScrollRef.current = atLatest;
    setShowJumpToLatest((current) => {
      const next = !atLatest && messages.length > 0;
      return current === next ? current : next;
    });
  };

  const jumpToLatest = () => {
    shouldAutoScrollRef.current = true;
    setShowJumpToLatest(false);
    const conversation = conversationRef.current;
    if (!conversation) return;
    conversation.scrollTop = conversation.scrollHeight;
    requestAnimationFrame(() => {
      conversation.scrollTop = conversation.scrollHeight;
      handleConversationScroll();
    });
  };

  const queueSessionTitle = (text: string) => {
    const requestId = ++titleRequestRef.current;
    const previousName = sessions.find((session) => session.id === activeId)?.name ?? t("unnamed");
    setSessions((current) => current.map((session) => session.id === activeId ? { ...session, name: t("summarizeTitle"), updatedAt: new Date().toISOString() } : session));
    titleQueueRef.current = titleQueueRef.current
      .catch(() => undefined)
      .then(async () => {
        const response = await fetch(`/api/sessions/${activeId}/title`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ text }) });
        const data = await response.json();
        if (!response.ok) throw new Error(data.error ?? t("generateTitleFailed"));
        if (requestId === titleRequestRef.current) setSessions(data.sessions ?? []);
      })
      .catch((reason: unknown) => {
        if (requestId === titleRequestRef.current) {
          setSessions((current) => current.map((session) => session.id === activeId && session.name === t("summarizeTitle") ? { ...session, name: previousName } : session));
          setError(reason instanceof Error ? reason.message : t("sendFailed"));
        }
      });
  };

  const send = async (requestedMode?: "prompt" | "steer" | "followUp") => {
    const text = input.trim();
    if (!text || !activeId) return;
    const mode = requestedMode ?? (composerBusy ? "steer" : "prompt");
    setInput("");
    shouldAutoScrollRef.current = true;
    setShowJumpToLatest(false);
    setMessages((current) => [...current, { id: crypto.randomUUID(), role: "user", content: text }]);
    const currentSession = sessions.find((session) => session.id === activeId);
    const hasExistingUserMessage = Boolean(currentSession?.firstMessage?.trim()) || messages.some((message) => message.role === "user");
    if (!hasExistingUserMessage) queueSessionTitle(text);
    setMainAgentRunning(true);
    try {
      const response = await fetch(`/api/sessions/${activeId}/prompt`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ text, mode }) });
      if (!response.ok) {
        const data = await response.json().catch(() => ({})) as { error?: string };
        setMainAgentRunning(false);
        setError(data.error ?? t("sendFailed"));
      }
    } catch (error) {
      setMainAgentRunning(false);
      setError(error instanceof Error ? error.message : t("sendFailed"));
    }
  };

  const newSession = async () => {
    const response = await fetch("/api/sessions", { method: "POST" });
    const data = await response.json();
    const nextSessions = await fetch("/api/sessions").then((item) => item.json()) as SessionSummary[];
    const nextSession = nextSessions.find((session) => session.id === data.id);
    setActiveId(data.id);
    setMessages([]);
    setUsage(usageFromSession(nextSession));
    setSessions(nextSessions);
  };

  const chooseWorkingDirectory = async () => {
    setWorkspaceChoosing(true);
    try {
      const response = await fetch("/api/workspace", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ language }) });
      const data = await response.json() as { cwd?: string; sessions?: SessionSummary[]; activeSessionId?: string; cancelled?: boolean; error?: string };
      if (!response.ok) throw new Error(data.error ?? t("changeWorkingDirectoryFailed"));
      if (data.cancelled) return;
      const nextSessions = data.sessions ?? [];
      setCwd(data.cwd ?? cwd);
      setSessions(nextSessions);
      setActiveId(data.activeSessionId ?? "");
      setMessages([]);
      setUsage(makeEmptyUsage());
      setMainAgentRunning(false);
      setApprovalQueue([]);
      subagentsRef.current = [];
      setSubagents([]);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : t("changeWorkingDirectoryFailed"));
    } finally {
      setWorkspaceChoosing(false);
    }
  };

  const archiveSession = async (id: string) => {
    const response = await fetch(`/api/sessions/${id}/archive`, { method: "POST" });
    if (!response.ok) { setError((await response.json()).error ?? t("sendFailed")); return; }
    const data = await response.json();
    const nextSessions = (data.sessions ?? []).filter((session: SessionSummary) => !session.archived);
    setSessions(nextSessions);
    if (id !== activeId) return;
    const next = nextSessions[0];
    setActiveId(next?.id ?? "");
    setMessages([]);
    setUsage(usageFromSession(next));
  };

  const cancelSubagent = async (taskId: string) => {
    if (!activeId) return;
    await fetch(`/api/sessions/${activeId}/subagents/${taskId}/cancel`, { method: "POST" });
  };

  const retrySubagent = async (taskId: string) => {
    if (!activeId) return;
    const response = await fetch(`/api/sessions/${activeId}/subagents/${taskId}/retry`, { method: "POST" });
    if (!response.ok) { setError((await response.json()).error ?? t("sendFailed")); return; }
    const data = await response.json() as { task?: SubagentTask };
    if (data.task) queueSubagentTask(data.task);
  };

  const approval = approvalQueue[0] ?? null;

  const stopAll = () => {
    if (!activeId) return;
    setMainAgentRunning(false);
    setApprovalQueue([]);
    setMessages((current) => current.map((message) => message.role === "thinking"
      ? { ...message, status: "done" }
      : message.role === "tool" && message.status === "running"
        ? { ...message, status: "cancelled", isError: true, content: message.content ? `${message.content}\n\n${t("stopped")}` : t("stopped") }
        : message));
    void fetch(`/api/sessions/${activeId}/abort`, { method: "POST" });
  };

  const decide = async (approved: boolean, scope: "once" | "task" = "once") => {
    if (!approval || !activeId) return;
    const response = await fetch(`/api/sessions/${activeId}/approval`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ approvalId: approval.id, approved, scope }) });
    const result = await response.json() as { ok?: boolean; error?: string };
    if (!response.ok || result.ok !== true) { setError(result.error ?? t("approvalExpired")); return; }
    setApprovalQueue((current) => current.filter((item) => item.id !== approval.id));
  };

  const changeApprovalMode = async (mode: ApprovalMode) => {
    const previous = approvalMode;
    setApprovalMode(mode);
    const response = await fetch("/api/settings/approval-mode", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ approvalMode: mode }) });
    if (!response.ok) {
      setApprovalMode(previous);
      setError((await response.json()).error ?? t("changeApprovalModeFailed"));
    }
  };

  const changeModel = async (profileId: string) => {
    if (running || profileId === activeProfileId) return;
    setError("");
    const previousId = activeProfileId;
    const previousProfile = modelProfiles.find((item) => item.id === previousId);
    const nextProfile = modelProfiles.find((item) => item.id === profileId);
    if (!nextProfile) return;
    setActiveProfileId(profileId);
    setModelName(`${nextProfile.provider}/${nextProfile.model}`);
    try {
      const response = await fetch("/api/settings/model-profiles", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ activeProfileId: profileId }) });
      if (!response.ok) {
        const data = await response.json().catch(() => ({})) as { error?: string };
        throw new Error(data.error ?? t("switchModelFailed"));
      }
      setUsage(makeEmptyUsage(nextProfile.contextWindow));
      setStreamGeneration((current) => current + 1);
    } catch (error) {
      setActiveProfileId(previousId);
      setModelName(previousProfile ? `${previousProfile.provider}/${previousProfile.model}` : "No model configured");
      setError(error instanceof Error ? error.message : t("switchModelFailed"));
    }
  };

  const detail = useMemo(() => {
    const safe = {
      tokens: Number(usage.tokens) || 0,
      contextWindow: Number(usage.contextWindow) || 0,
      input: usage.input,
      output: usage.output,
      cacheRead: usage.cacheRead,
      cacheWrite: usage.cacheWrite,
      remaining: Number(usage.remaining) || 0
    };
    const formatPart = (value: number | null) => value === null ? "—" : value.toLocaleString();
    return <div className="usage-tooltip"><strong>{usage.percent === null ? t("contextUnknown") : `${Math.round(Number(usage.percent) || 0)}%`} {t("context")}</strong><span>{safe.tokens.toLocaleString()} / {safe.contextWindow.toLocaleString()} {t("tokens")}</span><span>{t("input")} {formatPart(safe.input)} · {t("output")} {formatPart(safe.output)}</span><span>{t("cacheRead")} {formatPart(safe.cacheRead)} · {t("cacheWrite")} {formatPart(safe.cacheWrite)}</span><span>{t("remaining")} {safe.remaining.toLocaleString()} {t("tokens")}</span></div>;
  }, [usage]);

  return <div className="app-shell">
    <aside className={`sidebar ${mobileNav ? "open" : ""}`}>
      <div className="brand-row"><div className="brand-mark"><RiftxLogo /></div><span>RiftX</span><button className="icon-button mobile-only" onClick={() => setMobileNav(false)}><X size={17} /></button></div>
      <button className="new-session" onClick={newSession}><Plus size={17} weight="bold" />{t("newSession")}<span className="shortcut">⌘ N</span></button>
      <div className="sidebar-label">{t("recentSessions")}</div>
      <div className="session-list">{bootstrapping ? <span className="session-loading">{t("loading")}</span> : sessions.map((session) => <div key={session.id} className="session-item-row"><button className={`session-item ${activeId === session.id ? "active" : ""}`} onClick={() => { setActiveId(session.id); setMessages([]); setMobileNav(false); }}><span className="session-dot" /><span className="session-copy">{session.name === t("summarizeTitle") ? <span className="session-title-loading" role="status" aria-label={t("summarizeTitle")} title={t("summarizeTitle")} /> : <strong>{session.name || t("newSessionEnglish")}</strong>}<small>{new Date(session.updatedAt).toLocaleDateString()}</small></span></button><button className="session-archive" aria-label={`${t("archive")} ${session.name || t("archived")}`} title={t("archive")} onClick={(event) => { event.stopPropagation(); void archiveSession(session.id); }}><Archive size={14} /></button></div>)}</div>
      <div className="sidebar-bottom"><div className="sidebar-settings-row"><Link href="/settings" className="sidebar-link"><Gear size={17} />{t("settings")}</Link></div></div>
    </aside>
    {mobileNav ? <button className="scrim mobile-only" onClick={() => setMobileNav(false)} aria-label={t("closeNav")} /> : null}
    <main className="main-panel">
      <header className="topbar"><button className="icon-button mobile-only" onClick={() => setMobileNav(true)} aria-label={t("settings")}><List size={19} /></button><button className="workspace workspace-button" type="button" disabled={bootstrapping || workspaceChoosing} aria-busy={workspaceChoosing} onClick={() => void chooseWorkingDirectory()} title={t("changeWorkingDirectory")} aria-label={workspaceChoosing ? t("choosingWorkingDirectory") : t("changeWorkingDirectory")}><FolderOpen size={16} /><span>{cwd || t("workingDirectory")}</span></button><div className="topbar-spacer" /><div className="topbar-actions"><LanguageToggle /><ThemeToggle /></div></header>
      <SubagentPanel tasks={subagents} running={subagentRunning} maxConcurrent={maxConcurrentSubagents} onCancel={(taskId) => void cancelSubagent(taskId)} onRetry={(taskId) => void retrySubagent(taskId)} />
      <section ref={conversationRef} className="conversation" onScroll={handleConversationScroll}><div ref={conversationInnerRef} className="conversation-inner">{messages.length === 0 ? <div className="empty-state"><div className="empty-orbit"><RiftxLogo decorative /></div><h1>{bootstrapping ? t("loadingWorkspace") : activeId ? t("ready") : t("noSession")}</h1><p>{bootstrapping ? t("readingWorkspace") : activeId ? t("readOrTest") : t("createSessionFirst")}</p>{activeId && !bootstrapping ? <div className="prompt-suggestions"><button onClick={() => setInput(t("overview"))}>{t("overview")}</button><button onClick={() => setInput(t("checkRisks"))}>{t("checkRisks")}</button></div> : null}</div> : messages.map((message) => <article key={message.id} className={`message ${message.role}`}>
        {message.role === "user" ? <div className="avatar user-avatar">{t("you")}</div> : message.role === "assistant" ? <div className="avatar assistant-avatar"><RiftxLogo decorative /></div> : null}
        <div className="message-body">{message.role === "thinking" ? <details className="thinking-block" open={message.status === "streaming"}><summary><span className="thinking-title"><Brain size={14} weight="bold" />{t("thinking")}</span><span className="thinking-state">{message.status === "streaming" ? t("thinkingNow") : t("thinkingDone")}</span></summary><div className="thinking-copy">{message.content}</div></details> : message.role === "tool" ? <ToolCard key={`${message.id}-${message.status}`} message={message} /> : <div className="markdown"><ReactMarkdown remarkPlugins={[remarkGfm]}>{message.content}</ReactMarkdown></div>}</div>
      </article>)}<div ref={endRef} /></div>{showJumpToLatest ? <button className="jump-latest" type="button" aria-label={t("jumpLatest")} title={t("jumpLatest")} onClick={jumpToLatest}><ArrowDown size={17} weight="bold" /></button> : null}</section>
      <footer className="composer-wrap">{approval ? <div className="approval-card"><div className="approval-card-main"><div className="approval-icon"><WarningCircle size={18} weight="bold" /></div><div className="approval-card-copy"><div className="approval-card-title"><span className="eyebrow">{approval.subagentId ? t("subagentApproval") : t("needConfirm")}</span><strong>{approval.subagentId ? approval.agentName : approval.toolName}</strong><span className="approval-card-risk">{t("highRisk")}</span></div>{approval.subagentId ? <p>{t("subagentRequestsTool", { agent: approval.agentName ?? "", tool: approval.toolName })}</p> : <p>{approval.toolName === "browser" ? t("browserApproval") : t("terminalApproval")}</p>}</div></div><details className="approval-command"><summary><code>{summarizeApprovalInput(approval.input)}</code><span>{t("expandCommand")}</span></summary><pre>{formatApprovalInput(approval.input)}</pre></details><div className="approval-actions"><button className="button reject" onClick={() => void decide(false)}>{t("reject")}</button><button className="button ghost" onClick={() => void decide(true, "task")}>{t("allowTask")}</button><button className="button primary" onClick={() => void decide(true)}>{t("allowOnce")}</button></div></div> : null}<div className="composer"><textarea ref={composerInputRef} value={input} disabled={!activeId || bootstrapping} onChange={(event) => setInput(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); void send(); } }} placeholder={bootstrapping ? t("loadingWorkspace") : composerBusy ? t("guide") : activeId ? t("ask") : t("createSessionFirst")} rows={1} /><div className="composer-bottom"><div className="composer-tools"><ApprovalModeMenu value={approvalMode} onValueChange={(mode) => void changeApprovalMode(mode)} disabled={bootstrapping || mainAgentRunning} /><span className="composer-hint"><span className="keycap">Shift</span> + <span className="keycap">Enter</span> {t("shiftEnter")}</span></div><div className="composer-actions"><ContextRing percent={bootstrapping ? null : usage.percent} label={bootstrapping ? "—" : usage.percent === null ? "—" : `${Math.round(usage.percent)}`} detail={detail} />{bootstrapping ? <span className="model-label">{t("loadingModel")}</span> : modelProfiles.length > 1 ? <ModelMenu value={activeProfileId} onValueChange={(profileId) => void changeModel(profileId)} options={modelProfiles.map((profile) => ({ value: profile.id, label: `${profile.provider}/${profile.model}` }))} disabled={mainAgentRunning} /> : <span className="model-label">{modelName}</span>}{composerBusy ? (input.trim() ? <button className="send-button" aria-label={t("sendGuide")} title={t("sendGuide")} onClick={() => void send("steer")}><ArrowUp size={18} weight="bold" /></button> : <button className="send-button stop" aria-label={t("stop")} title={t("stop")} onClick={stopAll}><Stop size={17} weight="fill" /></button>) : input.trim() ? <button className="send-button" aria-label={t("send")} title={t("send")} onClick={() => void send("prompt")} disabled={!activeId || bootstrapping}><ArrowUp size={18} weight="bold" /></button> : running ? <button className="send-button stop" aria-label={t("stop")} title={t("stop")} onClick={stopAll}><Stop size={17} weight="fill" /></button> : <button className="send-button" aria-label={t("send")} title={t("send")} onClick={() => void send("prompt")} disabled={!activeId || bootstrapping || !input.trim()}><ArrowUp size={18} weight="bold" /></button>}</div></div></div></footer>
    </main>
    {error ? <div className="toast-wrap"><ErrorNotice message={error} onDismiss={() => setError("")} /></div> : null}
  </div>;
}
