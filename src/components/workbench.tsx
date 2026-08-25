"use client";

import Link from "next/link";
import { memo, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { Archive, ArrowDown, ArrowUp, Brain, Command, FolderOpen, Gear, List, Plus, Stop, WarningCircle, X } from "@phosphor-icons/react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { ApprovalModeMenu, ContextRing, ErrorNotice, LanguageToggle, ModelMenu, RiftxLogo, ThemeToggle } from "./ui";
import { SubagentPanel } from "./subagent-panel";
import { FindingsPanel } from "./findings-panel";
import { parseRiftxEvent, type ApprovalMode, type ApprovalRequest, type ContextUsage, type Finding, type FindingPatch, type ModelProfile, type RiftxEvent, type SessionSummary, type SubagentTask, type SubagentTaskPatch } from "@/lib/types";
import { cloneSubagentTask, mergeSubagentTaskPatch, mergeSubagentTasks } from "@/lib/subagent-merge";
import { withSessionProfile } from "@/lib/session-profile-sync";
import { useLanguage } from "@/lib/i18n";
import { isAlreadyProcessingError } from "@/lib/prompt-mode";
import { summarizeToolResult } from "@/lib/tool-result";
import { resolveConversationScroll } from "@/lib/conversation-scroll";
import { mergeFetchedMessages, type MergeableMessage } from "@/lib/message-merge";

type Message = MergeableMessage;
type MessageDelta = { role: "assistant" | "thinking"; content: string };
type MessageLabels = { you: string; thinking: string; thinkingNow: string; thinkingDone: string; queued: string; running: string; failed: string; stopped: string; complete: string };

const MESSAGE_BATCH_SIZE = 200;
const MARKDOWN_PLUGINS = [remarkGfm];

function makeEmptyUsage(contextWindow = 0): ContextUsage {
  const safeWindow = Number.isFinite(contextWindow) && contextWindow > 0 ? contextWindow : 0;
  return { tokens: 0, contextWindow: safeWindow, percent: safeWindow > 0 ? 0 : null, input: null, output: null, cacheRead: null, cacheWrite: null, remaining: safeWindow };
}

function usageFromSession(session?: SessionSummary | null): ContextUsage {
  if (session?.usage) return { ...session.usage };
  return makeEmptyUsage(session?.contextWindow ?? 0);
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

function containsToken(text: string, token: string) {
  const escaped = token.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  return new RegExp(`(?:^|[^\\w])${escaped}(?:[^\\w]|$)`).test(text);
}

type EvidenceTarget = { kind: "tool"; toolCallId: string } | { kind: "subagent"; taskId: string; logId: string };

const ToolCard = memo(function ToolCard({ message, labels }: { message: Message; labels: MessageLabels }) {
  const [open, setOpen] = useState(message.status === "running");
  useEffect(() => { setOpen(message.status === "running"); }, [message.status]);
  return <details id={message.toolCallId ? `tool-${encodeURIComponent(message.toolCallId)}` : undefined} className={`tool-card ${message.isError ? "error" : ""}`} open={open} onToggle={(event) => setOpen(event.currentTarget.open)}>
    <summary className="tool-card-head"><span><Command size={14} />{message.toolName}</span><span className={`tool-status ${message.status}`}>{message.status === "queued" ? labels.queued : message.status === "running" ? labels.running : message.status === "error" ? labels.failed : message.status === "cancelled" ? labels.stopped : labels.complete}</span></summary>
    <pre>{message.content}</pre>
  </details>;
});

const MessageItem = memo(function MessageItem({ message, labels }: { message: Message; labels: MessageLabels }) {
  return <article className={`message ${message.role}${message.status === "error" ? " error" : ""}`}>
    {message.role === "user" ? <div className="avatar user-avatar">{labels.you}</div> : message.role === "assistant" ? <div className="avatar assistant-avatar"><RiftxLogo decorative /></div> : null}
    <div className="message-body">{message.role === "thinking" ? <details className="thinking-block" open={message.status === "streaming"}><summary><span className="thinking-title"><Brain size={14} weight="bold" />{labels.thinking}</span><span className="thinking-state">{message.status === "streaming" ? labels.thinkingNow : labels.thinkingDone}</span></summary><div className="thinking-copy">{message.content}</div></details> : message.role === "tool" ? <ToolCard message={message} labels={labels} /> : <div className="markdown"><ReactMarkdown remarkPlugins={MARKDOWN_PLUGINS}>{message.content}</ReactMarkdown></div>}</div>
  </article>;
});

function applyMessageDeltas(current: Message[], deltas: MessageDelta[]) {
  return deltas.reduce((messages, delta) => {
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

function normalizeMessages(items: Message[]) {
  return items.filter((item) => ["user", "assistant", "thinking", "tool"].includes(item.role)).map((item) => ({ ...item, role: item.role as Message["role"] }));
}

export function Workbench() {
  const { language, t } = useLanguage();
  const tRef = useRef(t);
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [activeId, setActiveId] = useState("");
  const [messages, setMessages] = useState<Message[]>([]);
  const [visibleMessageCount, setVisibleMessageCount] = useState(MESSAGE_BATCH_SIZE);
  const [usage, setUsage] = useState<ContextUsage>(() => makeEmptyUsage());
  const [modelName, setModelName] = useState("No model configured");
  const [modelProfiles, setModelProfiles] = useState<ModelProfile[]>([]);
  const [activeProfileId, setActiveProfileId] = useState("");
  const [approvalMode, setApprovalMode] = useState<ApprovalMode>("request");
  const [cwd, setCwd] = useState("");
  const [workspaceChoosing, setWorkspaceChoosing] = useState(false);
  const [input, setInput] = useState("");
  const [mainAgentRunning, setMainAgentRunning] = useState(false);
  const [contextCompacting, setContextCompacting] = useState(false);
  const [bootstrapping, setBootstrapping] = useState(true);
  const [approvalQueue, setApprovalQueue] = useState<ApprovalRequest[]>([]);
  const [subagents, setSubagents] = useState<SubagentTask[]>([]);
  const [findings, setFindings] = useState<Finding[]>([]);
  const [maxConcurrentSubagents, setMaxConcurrentSubagents] = useState(3);
  const [subagentFocus, setSubagentFocus] = useState<{ taskId: string; logId?: string } | null>(null);
  const [error, setError] = useState("");
  const [mobileNav, setMobileNav] = useState(false);
  const [showJumpToLatest, setShowJumpToLatest] = useState(false);
  const [streamGeneration, setStreamGeneration] = useState(0);
  const conversationRef = useRef<HTMLElement>(null);
  const conversationInnerRef = useRef<HTMLDivElement>(null);
  const shouldAutoScrollRef = useRef(true);
  const endRef = useRef<HTMLDivElement>(null);
  const composerInputRef = useRef<HTMLTextAreaElement>(null);
  const compositionActiveRef = useRef(false);
  const compositionEndedAtRef = useRef(0);
  const subagentsRef = useRef<SubagentTask[]>([]);
  const subagentQueueRef = useRef(new Map<string, SubagentTask>());
  const subagentPatchQueueRef = useRef(new Map<string, SubagentTaskPatch[]>());
  const subagentFlushFrameRef = useRef<number | undefined>(undefined);
  const titleQueueRef = useRef<Promise<void>>(Promise.resolve());
  const titleRequestRef = useRef(0);
  const activeIdRef = useRef("");
  const modelRequestRef = useRef(0);
  const [modelSwitching, setModelSwitching] = useState(false);
  const messageDeltaQueueRef = useRef<MessageDelta[]>([]);
  const messageDeltaFrameRef = useRef<number | undefined>(undefined);
  const scrollFrameRef = useRef<number | undefined>(undefined);
  const lastScrollTopRef = useRef(0);
  const historyScrollRef = useRef<{ height: number; top: number } | null>(null);
  const pendingToolScrollRef = useRef<string | null>(null);

  const flushMessageDeltas = () => {
    if (messageDeltaFrameRef.current !== undefined) cancelAnimationFrame(messageDeltaFrameRef.current);
    messageDeltaFrameRef.current = undefined;
    const pending = messageDeltaQueueRef.current.splice(0);
    if (pending.length) setMessages((current) => applyMessageDeltas(current, pending));
  };

  const queueMessageDelta = (delta: MessageDelta) => {
    const last = messageDeltaQueueRef.current[messageDeltaQueueRef.current.length - 1];
    if (last?.role === delta.role) last.content += delta.content;
    else messageDeltaQueueRef.current.push(delta);
    if (messageDeltaFrameRef.current === undefined) messageDeltaFrameRef.current = requestAnimationFrame(flushMessageDeltas);
  };

  useEffect(() => {
    tRef.current = t;
  }, [t]);

  useEffect(() => {
    activeIdRef.current = activeId;
  }, [activeId]);

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
    const missing = items.filter((session) => (session.name === tRef.current("unnamed") || session.name === "Untitled task" || session.name === "未命名任务") && session.firstMessage).slice(0, 8);
    void missing.reduce<Promise<void>>((chain, session) => chain.then(async () => {
      try {
        const response = await fetch(`/api/sessions/${session.id}/title`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ text: session.firstMessage }) });
        if (!response.ok) return;
        const data = await response.json() as { sessions?: SessionSummary[] };
        if (!data.sessions) return;
        setSessions((current) => current.map((currentSession) => currentSession.name === tRef.current("summarizeTitle") ? currentSession : data.sessions?.find((next) => next.id === currentSession.id) ?? currentSession));
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
    }).catch(() => setError(tRef.current("cannotConnect"))).finally(() => setBootstrapping(false));
    return undefined;
  }, []);

  useEffect(() => {
    subagentsRef.current = subagents;
  }, [subagents]);

  useEffect(() => {
    shouldAutoScrollRef.current = true;
    lastScrollTopRef.current = 0;
    setVisibleMessageCount(MESSAGE_BATCH_SIZE);
    setShowJumpToLatest(false);
    subagentsRef.current = [];
    setSubagents([]);
    setFindings([]);
    setApprovalQueue([]);
    setMainAgentRunning(false);
    setContextCompacting(false);
    const sessionMeta = sessions.find((session) => session.id === activeId);
    setUsage(usageFromSession(sessionMeta));
    if (sessionMeta?.provider && sessionMeta?.model) setModelName(`${sessionMeta.provider}/${sessionMeta.model}`);
    // The selector reflects the session's own profile, not the global default,
    // so switching sessions never shows a model the session is not using.
    setActiveProfileId(sessionMeta?.profileId ?? "");
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
    fetch(`/api/sessions/${activeId}/findings`, { signal: controller.signal }).then((response) => response.json()).then((data: { findings?: Finding[] }) => {
      if (controller.signal.aborted) return;
      setFindings(data.findings ?? []);
    }).catch(() => undefined);
    fetch(`/api/sessions/${activeId}/messages`, { signal: controller.signal }).then((response) => response.json()).then((items: Message[]) => {
      if (controller.signal.aborted) return;
      flushMessageDeltas();
      setMessages((current) => mergeFetchedMessages(current, normalizeMessages(items)));
    }).catch(() => undefined);
    let disposed = false;
    let reconnectAttempts = 0;
    let hasOpened = false;
    const source = new EventSource(`/api/sessions/${activeId}/stream`);
    source.onopen = () => {
      reconnectAttempts = 0;
      if (!hasOpened) {
        hasOpened = true;
        return;
      }
      void fetch(`/api/sessions/${activeId}/messages`, { signal: controller.signal })
        .then((response) => response.ok ? response.json() as Promise<Message[]> : [])
        .then((items) => {
          if (disposed || controller.signal.aborted) return;
          flushMessageDeltas();
          setMessages((current) => mergeFetchedMessages(current, normalizeMessages(items)));
        })
        .catch(() => undefined);
    };
    source.onmessage = (event) => {
      if (disposed) return;
      reconnectAttempts = 0;
      let payload: RiftxEvent | null;
      try {
        payload = parseRiftxEvent(JSON.parse(event.data));
      } catch {
        return;
      }
      if (!payload) return;
      if (payload.type === "connected") return;
      if (payload.type === "text_delta" || payload.type === "thinking_delta") {
        queueMessageDelta({ role: payload.type === "text_delta" ? "assistant" : "thinking", content: String(payload.delta ?? "") });
        return;
      }
      flushMessageDeltas();
      if (payload.type === "finding" && payload.finding) {
        const finding = payload.finding as Finding;
        setFindings((current) => current.some((item) => item.id === finding.id) ? current.map((item) => item.id === finding.id ? finding : item) : [...current, finding]);
        return;
      }
      if (payload.type === "findingPatch" && payload.findingPatch) {
        const findingPatch = payload.findingPatch as FindingPatch;
        setFindings((current) => current.map((item) => item.id === findingPatch.id ? { ...item, ...findingPatch } : item));
        return;
      }
      if ((payload.type.startsWith("subagent_") || payload.type === "approval_decided") && payload.task) {
        const task = payload.task as SubagentTask;
        queueSubagentTask(task);
      }
      if ((payload.type.startsWith("subagent_") || payload.type === "approval_decided") && payload.taskPatch) {
        queueSubagentTaskPatch(payload.taskPatch as SubagentTaskPatch);
      }
      if (payload.type === "approval_decided" && typeof payload.approvalId === "string") {
        setApprovalQueue((current) => current.filter((item) => item.id !== payload.approvalId));
      }
      if (payload.type === "usage") {
        const next = payload.usage as Partial<ContextUsage>;
        setUsage((current) => ({
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
        setSessions((current) => current.map((session) => session.id === activeId ? {
          ...session,
          contextWindow: Number(next.contextWindow ?? session.contextWindow ?? 0),
          usage: {
            ...(session.usage ?? makeEmptyUsage(Number(next.contextWindow ?? session.contextWindow ?? 0))),
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
        if (payload.taskPatch) queueSubagentTaskPatch(payload.taskPatch as SubagentTaskPatch);
        setApprovalQueue((current) => current.some((item) => item.id === request.id) ? current : [...current, request]);
        if (!request.subagentId) setMainAgentRunning(true);
        return;
      }
      if (payload.type.startsWith("subagent_") || payload.type === "approval_decided") return;
      if (["text_delta", "tool_start", "tool_status", "tool_update", "tool_end", "message", "done", "error"].includes(payload.type)) {
        setMessages((current) => current.map((message) => message.role === "thinking" && message.status === "streaming" ? { ...message, status: "done" } : message));
      }
      if (payload.type === "session_state") { setMainAgentRunning(payload.state !== "idle"); setContextCompacting(payload.state === "compacting"); return; }
      if (payload.type === "done") { setMainAgentRunning(false); setContextCompacting(false); setApprovalQueue((current) => current.filter(isSubagentApproval)); setMessages((current) => current.map((message) => message.role === "thinking" ? { ...message, status: "done" } : message.role === "tool" && (message.status === "running" || message.status === "queued") ? { ...message, status: "cancelled", isError: true, content: message.content ? `${message.content}\n\n${t("stopped")}` : t("stopped") } : message)); return; }
      if (payload.type === "tool_status") {
        const toolCallId = String(payload.toolCallId ?? "");
        if (payload.toolStatus === "queued" || payload.toolStatus === "running") setMessages((current) => current.map((message) => message.id === toolCallId ? { ...message, status: payload.toolStatus } : message));
        return;
      }
      if (payload.type === "tool_start") {
        const toolCallId = String(payload.toolCallId ?? crypto.randomUUID());
        setMessages((current) => {
          const existingIndex = current.findIndex((message) => message.toolCallId === toolCallId || message.id === toolCallId);
          const nextMessage = { id: toolCallId, role: "tool" as const, toolCallId, toolName: String(payload.toolName ?? "tool"), content: JSON.stringify(payload.args ?? {}, null, 2), status: payload.toolStatus === "queued" ? "queued" : "running" };
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
        setMessages((current) => current.map((message) => message.id === toolCallId ? { ...message, status: payload.isError ? "error" : "done", isError: Boolean(payload.isError), content: summarizeToolResult(payload.result) } : message)); return;
      }
      if (payload.type === "error") {
        const message = String(payload.error ?? "Agent error");
        if (isAlreadyProcessingError(message)) return;
        setMainAgentRunning(false);
        setContextCompacting(false);
        setApprovalQueue((current) => current.filter(isSubagentApproval));
        setError(message);
      }
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
      if (messageDeltaFrameRef.current !== undefined) cancelAnimationFrame(messageDeltaFrameRef.current);
      messageDeltaFrameRef.current = undefined;
      messageDeltaQueueRef.current = [];
      subagentQueueRef.current.clear();
      subagentPatchQueueRef.current.clear();
      if (subagentFlushFrameRef.current !== undefined) {
        cancelAnimationFrame(subagentFlushFrameRef.current);
        subagentFlushFrameRef.current = undefined;
      }
    };
  }, [activeId, streamGeneration]);

  const subagentRunning = useMemo(() => subagents.filter((task) => task.status === "queued" || task.status === "running").length, [subagents]);
  const visibleMessages = useMemo(() => messages.filter((message) => message.toolName !== "spawn_subagent"), [messages]);
  const displayedMessages = useMemo(() => visibleMessages.slice(-visibleMessageCount), [visibleMessages, visibleMessageCount]);
  const hasEarlierMessages = displayedMessages.length < visibleMessages.length;
  const messageLabels = useMemo<MessageLabels>(() => ({ you: t("you"), thinking: t("thinking"), thinkingNow: t("thinkingNow"), thinkingDone: t("thinkingDone"), queued: t("queued"), running: t("running"), failed: t("failed"), stopped: t("stopped"), complete: t("complete") }), [t]);
  const running = mainAgentRunning || subagentRunning > 0 || approvalQueue.length > 0;
  const composerBusy = mainAgentRunning || approvalQueue.some((item) => !item.subagentId);

  useEffect(() => {
    if (!shouldAutoScrollRef.current || scrollFrameRef.current !== undefined) return;
    scrollFrameRef.current = requestAnimationFrame(() => {
      scrollFrameRef.current = undefined;
      const conversation = conversationRef.current;
      if (conversation && shouldAutoScrollRef.current) {
        conversation.scrollTop = conversation.scrollHeight;
        lastScrollTopRef.current = conversation.scrollTop;
      }
    });
  }, [messages]);

  useEffect(() => {
    const content = conversationInnerRef.current;
    if (!content || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(() => {
      if (!shouldAutoScrollRef.current || scrollFrameRef.current !== undefined) return;
      scrollFrameRef.current = requestAnimationFrame(() => {
        scrollFrameRef.current = undefined;
        const conversation = conversationRef.current;
        if (conversation && shouldAutoScrollRef.current) {
          conversation.scrollTop = conversation.scrollHeight;
          lastScrollTopRef.current = conversation.scrollTop;
        }
      });
    });
    observer.observe(content);
    return () => observer.disconnect();
  }, []);

  useLayoutEffect(() => {
    const conversation = conversationRef.current;
    const historyScroll = historyScrollRef.current;
    if (conversation && historyScroll) conversation.scrollTop = historyScroll.top + conversation.scrollHeight - historyScroll.height;
    historyScrollRef.current = null;
    const toolCallId = pendingToolScrollRef.current;
    if (!toolCallId) return;
    pendingToolScrollRef.current = null;
    const target = document.getElementById(`tool-${encodeURIComponent(toolCallId)}`);
    if (target instanceof HTMLDetailsElement) target.open = true;
    target?.scrollIntoView({ behavior: "smooth", block: "center" });
  }, [visibleMessageCount]);

  useEffect(() => () => {
    if (scrollFrameRef.current !== undefined) cancelAnimationFrame(scrollFrameRef.current);
  }, []);

  const loadEarlierMessages = () => {
    const conversation = conversationRef.current;
    if (conversation) historyScrollRef.current = { height: conversation.scrollHeight, top: conversation.scrollTop };
    shouldAutoScrollRef.current = false;
    if (scrollFrameRef.current !== undefined) cancelAnimationFrame(scrollFrameRef.current);
    scrollFrameRef.current = undefined;
    setVisibleMessageCount((current) => Math.min(visibleMessages.length, current + MESSAGE_BATCH_SIZE));
  };

  const handleConversationScroll = () => {
    const conversation = conversationRef.current;
    if (!conversation) return;
    const { shouldFollow } = resolveConversationScroll({
      wasFollowing: shouldAutoScrollRef.current,
      previousScrollTop: lastScrollTopRef.current,
      scrollTop: conversation.scrollTop,
      distanceFromBottom: conversation.scrollHeight - conversation.clientHeight - conversation.scrollTop
    });
    lastScrollTopRef.current = conversation.scrollTop;
    shouldAutoScrollRef.current = shouldFollow;
    setShowJumpToLatest((current) => {
      const next = !shouldFollow && messages.length > 0;
      return current === next ? current : next;
    });
  };

  const jumpToLatest = () => {
    shouldAutoScrollRef.current = true;
    setShowJumpToLatest(false);
    const conversation = conversationRef.current;
    if (!conversation) return;
    conversation.scrollTop = conversation.scrollHeight;
    lastScrollTopRef.current = conversation.scrollTop;
    requestAnimationFrame(() => {
      conversation.scrollTop = conversation.scrollHeight;
      lastScrollTopRef.current = conversation.scrollTop;
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
    const wasRunning = composerBusy;
    const mode = requestedMode ?? (wasRunning ? "steer" : "prompt");
    const messageId = crypto.randomUUID();
    const markMessageFailed = () => setMessages((current) => current.map((message) => message.id === messageId ? { ...message, status: "error", isError: true } : message));
    setInput("");
    shouldAutoScrollRef.current = true;
    setShowJumpToLatest(false);
    setMessages((current) => [...current, { id: messageId, role: "user", content: text }]);
    const currentSession = sessions.find((session) => session.id === activeId);
    const hasExistingUserMessage = Boolean(currentSession?.firstMessage?.trim()) || messages.some((message) => message.role === "user");
    if (!hasExistingUserMessage) queueSessionTitle(text);
    setMainAgentRunning(true);
    try {
      const response = await fetch(`/api/sessions/${activeId}/prompt`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ text, mode }) });
      if (!response.ok) {
        const data = await response.json().catch(() => ({})) as { error?: string };
        if (!wasRunning) setMainAgentRunning(false);
        markMessageFailed();
        setError(data.error ?? t("sendFailed"));
      }
    } catch (error) {
      if (!wasRunning) setMainAgentRunning(false);
      markMessageFailed();
      setError(error instanceof Error ? error.message : t("sendFailed"));
    }
  };

  const newSession = async () => {
    const previousActiveId = activeId;
    setActiveId("");
    setMessages([]);
    setUsage(makeEmptyUsage());
    setError("");
    try {
      const response = await fetch("/api/sessions", { method: "POST" });
      const data = await response.json() as SessionSummary & { error?: string };
      if (!response.ok) throw new Error(data.error ?? t("sendFailed"));
      setSessions((current) => [data, ...current.filter((session) => session.id !== data.id)]);
      setActiveId(data.id);
      setUsage(usageFromSession(data));
    } catch (reason) {
      setActiveId(previousActiveId);
      setError(reason instanceof Error ? reason.message : t("sendFailed"));
    }
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

  const patchFindingInSession = async (id: string, patch: FindingPatch) => {
    if (!activeId) return;
    const body = { confidence: patch.confidence, dismissed: patch.status === "dismissed" ? true : patch.status === "open" ? false : undefined };
    const response = await fetch(`/api/sessions/${activeId}/findings/${id}`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    if (!response.ok) { setError((await response.json().catch(() => ({})) as { error?: string }).error ?? t("sendFailed")); return; }
    const data = await response.json() as { finding?: Finding };
    if (data.finding) setFindings((current) => current.map((item) => item.id === id ? data.finding! : item));
  };

  const scrollToSubagentLog = (taskId: string, logId: string) => {
    setSubagentFocus({ taskId, logId });
  };

  const findRequestTarget = (requestRef: string, finding: Finding): EvidenceTarget | null => {
    const searchSubagent = (taskId?: string) => {
      const tasks = taskId ? subagents.filter((task) => task.id === taskId) : subagents;
      for (const task of tasks) {
        const log = task.logs.find((entry) => containsToken(entry.content, requestRef));
        if (log) return { kind: "subagent", taskId: task.id, logId: log.id } as const;
      }
      return null;
    };
    const searchMain = () => {
      const tool = messages.find((message) => message.role === "tool" && containsToken(message.content, requestRef));
      return tool?.toolCallId ? { kind: "tool", toolCallId: tool.toolCallId } as const : null;
    };
    if (finding.source === "subagent") return finding.subagentId ? searchSubagent(finding.subagentId) : null;
    if (finding.subagentId) return searchSubagent(finding.subagentId) ?? searchMain();
    return searchMain();
  };

  const scrollToTool = (toolCallId: string, toolName?: string, subagentId?: string) => {
    const target = document.getElementById(`tool-${encodeURIComponent(toolCallId)}`);
    if (target instanceof HTMLDetailsElement) {
      target.open = true;
      target.scrollIntoView({ behavior: "smooth", block: "center" });
      return;
    }
    const hiddenIndex = visibleMessages.findIndex((message) => message.toolCallId === toolCallId);
    if (hiddenIndex >= 0) {
      pendingToolScrollRef.current = toolCallId;
      setVisibleMessageCount(Math.max(visibleMessageCount, visibleMessages.length - hiddenIndex));
      return;
    }
    const scoped = subagentId ? subagents.filter((task) => task.id === subagentId) : subagents;
    const subagent = scoped.find((task) => task.logs.some((log) => log.id === toolCallId))
      ?? (toolName ? scoped.find((task) => task.logs.some((log) => log.toolName === toolName)) : undefined);
    if (subagent) {
      const log = subagent.logs.find((entry) => entry.id === toolCallId)
        ?? (toolName ? [...subagent.logs].reverse().find((entry) => entry.toolName === toolName) : undefined);
      if (log) scrollToSubagentLog(subagent.id, log.id);
      return;
    }
    setError(t("evidenceTargetUnavailable"));
  };

  const scrollToRequest = (requestRef: string, finding: Finding) => {
    const target = findRequestTarget(requestRef, finding);
    if (!target) {
      setError(t("evidenceTargetUnavailable"));
      return;
    }
    if (target.kind === "tool") scrollToTool(target.toolCallId);
    else scrollToSubagentLog(target.taskId, target.logId);
  };

  useEffect(() => {
    if (!subagentFocus?.taskId || !subagentFocus.logId) return;
    let cancelled = false;
    let attempts = 0;
    const seek = () => {
      if (cancelled) return;
      const target = document.getElementById(`subagent-log-${subagentFocus.taskId}-${subagentFocus.logId}`);
      if (target) {
        target.scrollIntoView({ behavior: "smooth", block: "center" });
        return;
      }
      if (attempts++ < 8) {
        requestAnimationFrame(seek);
        return;
      }
      setError(t("evidenceTargetUnavailable"));
    };
    const frame = requestAnimationFrame(seek);
    return () => {
      cancelled = true;
      cancelAnimationFrame(frame);
    };
  }, [subagentFocus, t]);

  const approval = approvalQueue[0] ?? null;

  const stopAll = () => {
    if (!activeId) return;
    setMainAgentRunning(false);
    setContextCompacting(false);
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
    if (running || modelSwitching || profileId === activeProfileId || !activeId) return;
    const targetSessionId = activeId;
    // A token plus the target session guard stale responses: after the user
    // switches to another session (or fires a newer request), a late failure
    // for an old target must not touch the currently displayed selector.
    const request = ++modelRequestRef.current;
    const stillCurrent = () => request === modelRequestRef.current && targetSessionId === activeIdRef.current;
    setError("");
    const previousId = activeProfileId;
    const previousProfile = modelProfiles.find((item) => item.id === previousId);
    const nextProfile = modelProfiles.find((item) => item.id === profileId);
    if (!nextProfile) return;
    setModelSwitching(true);
    setActiveProfileId(profileId);
    setModelName(`${nextProfile.provider}/${nextProfile.model}`);
    try {
      const response = await fetch("/api/settings/model-profiles", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ activeProfileId: profileId, sessionId: targetSessionId }) });
      if (!response.ok) {
        const data = await response.json().catch(() => ({})) as { error?: string };
        throw new Error(data.error ?? t("switchModelFailed"));
      }
      // The session list is always updated for the target: it is pure data,
      // even when the user is already viewing another session.
      setSessions((current) => withSessionProfile(current, targetSessionId, nextProfile));
      if (!stillCurrent()) return;
      setUsage(makeEmptyUsage(nextProfile.contextWindow));
      // Reconnect only for the session the switch belongs to; the reconnect
      // effect restores activeProfileId from the (now updated) sessionMeta.
      setStreamGeneration((current) => current + 1);
    } catch (error) {
      if (!stillCurrent()) return;
      setActiveProfileId(previousId);
      setModelName(previousProfile ? `${previousProfile.provider}/${previousProfile.model}` : "No model configured");
      setError(error instanceof Error ? error.message : t("switchModelFailed"));
    } finally {
      if (request === modelRequestRef.current) setModelSwitching(false);
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
  const scopeExpansion = Boolean(approval?.input && typeof approval.input === "object" && (approval.input as { scopeExpansion?: unknown }).scopeExpansion === true);

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
      <section ref={conversationRef} className="conversation" onScroll={handleConversationScroll}><div ref={conversationInnerRef} className="conversation-inner">{visibleMessages.length === 0 ? <div className="empty-state"><div className="empty-orbit"><RiftxLogo decorative /></div><h1>{bootstrapping ? t("loadingWorkspace") : activeId ? t("ready") : t("noSession")}</h1><p>{bootstrapping ? t("readingWorkspace") : activeId ? t("readOrTest") : t("createSessionFirst")}</p>{activeId && !bootstrapping ? <div className="prompt-suggestions"><button onClick={() => setInput(t("overview"))}>{t("overview")}</button><button onClick={() => setInput(t("checkRisks"))}>{t("checkRisks")}</button></div> : null}</div> : <>{hasEarlierMessages ? <button className="load-earlier" type="button" onClick={loadEarlierMessages}><ArrowUp size={14} />{t("loadEarlierMessages")}</button> : null}{displayedMessages.map((message) => <MessageItem key={message.id} message={message} labels={messageLabels} />)}</>}{contextCompacting ? <article className="message thinking context-compaction-message" role="status" aria-live="polite"><div className="message-body"><div className="thinking-copy"><span className="thinking-title"><Brain size={14} weight="bold" />{t("contextCompacting")}</span></div></div></article> : null}<div ref={endRef} /></div>{showJumpToLatest ? <button className="jump-latest" type="button" aria-label={t("jumpLatest")} title={t("jumpLatest")} onClick={jumpToLatest}><ArrowDown size={17} weight="bold" /></button> : null}</section>
      <footer className="composer-wrap">{approval ? <div className="approval-card"><div className="approval-card-main"><div className="approval-icon"><WarningCircle size={18} weight="bold" /></div><div className="approval-card-copy"><div className="approval-card-title"><span className="eyebrow">{approval.subagentId ? t("subagentApproval") : t("needConfirm")}</span><strong>{approval.subagentId ? approval.agentName : approval.toolName}</strong><span className="approval-card-risk">{t("highRisk")}</span></div>{scopeExpansion ? <p>{t("browserScopeApproval")}</p> : approval.subagentId ? <p>{t("subagentRequestsTool", { agent: approval.agentName ?? "", tool: approval.toolName })}</p> : <p>{approval.toolName === "browser" ? t("browserApproval") : t("terminalApproval")}</p>}</div></div><details className="approval-command"><summary><code>{summarizeApprovalInput(approval.input)}</code><span>{t("expandCommand")}</span></summary><pre>{formatApprovalInput(approval.input)}</pre></details><div className="approval-actions"><button className="button reject" onClick={() => void decide(false)}>{t("reject")}</button><button className="button ghost" onClick={() => void decide(true, "task")}>{t(scopeExpansion ? "allowScopeTask" : "allowTask")}</button><button className="button primary" onClick={() => void decide(true)}>{t(scopeExpansion ? "allowScopeOnce" : "allowOnce")}</button></div></div> : null}<div className="composer"><textarea ref={composerInputRef} value={input} disabled={!activeId || bootstrapping} onChange={(event) => setInput(event.target.value)} onCompositionStart={() => { compositionActiveRef.current = true; compositionEndedAtRef.current = 0; }} onCompositionEnd={() => { compositionActiveRef.current = false; compositionEndedAtRef.current = Date.now(); }} onKeyDown={(event) => { if (event.nativeEvent.isComposing || event.keyCode === 229 || compositionActiveRef.current) return; if (event.key === "Enter" && !event.shiftKey) { if (Date.now() - compositionEndedAtRef.current < 150) { compositionEndedAtRef.current = 0; return; } event.preventDefault(); void send(); } }} placeholder={bootstrapping ? t("loadingWorkspace") : composerBusy ? t("guide") : activeId ? t("ask") : t("createSessionFirst")} rows={1} /><div className="composer-bottom"><div className="composer-tools"><ApprovalModeMenu value={approvalMode} onValueChange={(mode) => void changeApprovalMode(mode)} disabled={bootstrapping || mainAgentRunning} /><span className="composer-hint"><span className="keycap">Shift</span> + <span className="keycap">Enter</span> {t("shiftEnter")}</span></div><div className="composer-actions"><ContextRing percent={bootstrapping ? null : usage.percent} label={bootstrapping ? "—" : usage.percent === null ? "—" : `${Math.round(usage.percent)}`} detail={detail} />{bootstrapping ? <span className="model-label">{t("loadingModel")}</span> : modelProfiles.length > 1 ? <ModelMenu value={activeProfileId} onValueChange={(profileId) => void changeModel(profileId)} options={modelProfiles.map((profile) => ({ value: profile.id, label: `${profile.provider}/${profile.model}` }))} disabled={mainAgentRunning || modelSwitching} /> : <span className="model-label">{modelName}</span>}{composerBusy ? (input.trim() ? <button className="send-button" aria-label={t("sendGuide")} title={t("sendGuide")} onClick={() => void send("steer")}><ArrowUp size={18} weight="bold" /></button> : <button className="send-button stop" aria-label={t("stop")} title={t("stop")} onClick={stopAll}><Stop size={17} weight="fill" /></button>) : input.trim() ? <button className="send-button" aria-label={t("send")} title={t("send")} onClick={() => void send("prompt")} disabled={!activeId || bootstrapping}><ArrowUp size={18} weight="bold" /></button> : running ? <button className="send-button stop" aria-label={t("stop")} title={t("stop")} onClick={stopAll}><Stop size={17} weight="fill" /></button> : <button className="send-button" aria-label={t("send")} title={t("send")} onClick={() => void send("prompt")} disabled={!activeId || bootstrapping || !input.trim()}><ArrowUp size={18} weight="bold" /></button>}</div></div></div></footer>
    </main>
    <aside className="right-rail" aria-label={t("subagents")}><SubagentPanel tasks={subagents} running={subagentRunning} maxConcurrent={maxConcurrentSubagents} onCancel={(taskId) => void cancelSubagent(taskId)} onRetry={(taskId) => void retrySubagent(taskId)} focus={subagentFocus} /><FindingsPanel sessionId={activeId || undefined} findings={findings} onPatch={(id, patch) => void patchFindingInSession(id, patch)} onToolClick={scrollToTool} onRequestClick={scrollToRequest} /></aside>
    {error ? <div className="toast-wrap"><ErrorNotice message={error} onDismiss={() => setError("")} /></div> : null}
  </div>;
}
