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
import { mergeFetchedMessages, type MergeableMessage } from "@/lib/message-merge";
import { applyRiftxEvent, applyMessageDeltas, isSubagentApproval, normalizeMessages, type MessageDelta, type SessionEventContext } from "@/lib/session-events";
import { containsToken, findRequestTarget, type EvidenceTarget } from "@/lib/evidence-navigation";
import { useConversationScroll } from "./use-conversation-scroll";

type Message = MergeableMessage;
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

export function Workbench() {
  const { language, t } = useLanguage();
  const tRef = useRef(t);
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
  const [contextCompacting, setContextCompacting] = useState(false);
  const [bootstrapping, setBootstrapping] = useState(true);
  const [approvalQueue, setApprovalQueue] = useState<ApprovalRequest[]>([]);
  const [subagents, setSubagents] = useState<SubagentTask[]>([]);
  const [findings, setFindings] = useState<Finding[]>([]);
  const [maxConcurrentSubagents, setMaxConcurrentSubagents] = useState(3);
  const [subagentFocus, setSubagentFocus] = useState<{ taskId: string; logId?: string } | null>(null);
  const [error, setError] = useState("");
  const [mobileNav, setMobileNav] = useState(false);
  const [streamGeneration, setStreamGeneration] = useState(0);
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

  // Session switches must update the ref synchronously, ahead of React's
  // commit: a fetch response from the previous session can land between the
  // sidebar click and the effect cleanup, and the ref is the only guard that
  // is already up to date inside that window.
  const selectSession = (id: string) => {
    activeIdRef.current = id;
    setActiveId(id);
  };

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
      selectSession(data.activeSessionId ?? "");
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
    resetConversationView();
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
      if (controller.signal.aborted || activeIdRef.current !== activeId) return;
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
      if (controller.signal.aborted || activeIdRef.current !== activeId) return;
      for (const task of (data.tasks ?? []).map(cloneSubagentTask)) queueSubagentTask(task, "snapshot");
      setMaxConcurrentSubagents(Number(data.maxConcurrent ?? 3));
    }).catch(() => undefined);
    fetch(`/api/sessions/${activeId}/findings`, { signal: controller.signal }).then((response) => response.json()).then((data: { findings?: Finding[] }) => {
      if (controller.signal.aborted || activeIdRef.current !== activeId) return;
      setFindings(data.findings ?? []);
    }).catch(() => undefined);
    fetch(`/api/sessions/${activeId}/messages`, { signal: controller.signal }).then((response) => response.ok ? response.json() as Promise<Message[]> : []).then((items: Message[]) => {
      if (controller.signal.aborted || activeIdRef.current !== activeId) return;
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
          if (disposed || controller.signal.aborted || activeIdRef.current !== activeId) return;
          flushMessageDeltas();
          setMessages((current) => mergeFetchedMessages(current, normalizeMessages(items)));
        })
        .catch(() => undefined);
    };
    source.onmessage = (event) => {
      // Same pre-cleanup window as the fetch guards: events from the previous
      // session's stream must not write into the newly selected one.
      if (disposed || activeIdRef.current !== activeId) return;
      reconnectAttempts = 0;
      let payload: RiftxEvent | null;
      try {
        payload = parseRiftxEvent(JSON.parse(event.data));
      } catch {
        return;
      }
      if (payload) applyRiftxEvent(payload, eventContext);
    };
    source.onerror = () => {
      if (disposed || activeIdRef.current !== activeId) return;
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
  const { conversationRef, conversationInnerRef, showJumpToLatest, visibleMessageCount, handleConversationScroll, revealToolCard, requestToolReveal, expandVisibleMessages, loadEarlierMessages, jumpToLatest, pauseAutoFollow, resumeAutoFollow, resetConversationView } = useConversationScroll(messages, visibleMessages.length, MESSAGE_BATCH_SIZE);
  const eventContext: SessionEventContext = { activeId, t, queueMessageDelta, flushMessageDeltas, setMessages, setFindings, queueSubagentTask, queueSubagentTaskPatch, setApprovalQueue, setUsage, setSessions, setMainAgentRunning, setContextCompacting, setError };
  const displayedMessages = useMemo(() => visibleMessages.slice(-visibleMessageCount), [visibleMessages, visibleMessageCount]);
  const hasEarlierMessages = displayedMessages.length < visibleMessages.length;
  const messageLabels = useMemo<MessageLabels>(() => ({ you: t("you"), thinking: t("thinking"), thinkingNow: t("thinkingNow"), thinkingDone: t("thinkingDone"), queued: t("queued"), running: t("running"), failed: t("failed"), stopped: t("stopped"), complete: t("complete") }), [t]);
  const running = mainAgentRunning || subagentRunning > 0 || approvalQueue.length > 0;
  const composerBusy = mainAgentRunning || approvalQueue.some((item) => !item.subagentId);

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
    resumeAutoFollow();
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
    selectSession("");
    setMessages([]);
    setUsage(makeEmptyUsage());
    setError("");
    try {
      const response = await fetch("/api/sessions", { method: "POST" });
      const data = await response.json() as SessionSummary & { error?: string };
      if (!response.ok) throw new Error(data.error ?? t("sendFailed"));
      setSessions((current) => [data, ...current.filter((session) => session.id !== data.id)]);
      selectSession(data.id);
      setUsage(usageFromSession(data));
    } catch (reason) {
      selectSession(previousActiveId);
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
      selectSession(data.activeSessionId ?? "");
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
    selectSession(next?.id ?? "");
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

  const scrollToTool = (toolCallId: string, toolName?: string, subagentId?: string) => {
    if (revealToolCard(toolCallId)) return;
    const hiddenIndex = visibleMessages.findIndex((message) => message.toolCallId === toolCallId);
    if (hiddenIndex >= 0) {
      pauseAutoFollow();
      requestToolReveal(toolCallId);
      expandVisibleMessages(visibleMessages.length - hiddenIndex + 1);
      return;
    }
    // Exact log-id matches are unambiguous. The tool-name fallback is only
    // valid inside a finding's own subagent: without a scope, a stale main
    // toolCallId (compaction can drop it from the loaded branch) must not
    // silently navigate to an unrelated subagent that happens to share the
    // generic tool name — fall through to the unavailable error instead.
    const scoped = subagentId ? subagents.filter((task) => task.id === subagentId) : subagents;
    const subagent = scoped.find((task) => task.logs.some((log) => log.id === toolCallId))
      ?? (subagentId && toolName ? scoped.find((task) => task.logs.some((log) => log.toolName === toolName)) : undefined);
    if (subagent) {
      const log = subagent.logs.find((entry) => entry.id === toolCallId)
        ?? (toolName ? [...subagent.logs].reverse().find((entry) => entry.toolName === toolName) : undefined);
      if (log) scrollToSubagentLog(subagent.id, log.id);
      return;
    }
    setError(t("evidenceTargetUnavailable"));
  };

  const scrollToRequest = (requestRef: string, finding: Finding) => {
    const target = findRequestTarget(requestRef, finding, messages, subagents);
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
      : message.role === "tool" && (message.status === "running" || message.status === "queued")
        ? { ...message, status: "cancelled", isError: true, content: message.content ? `${message.content}\n\n${t("stopped")}` : t("stopped") }
        : message));
    void fetch(`/api/sessions/${activeId}/abort`, { method: "POST" });
  };

  const decide = async (approved: boolean, scope: "once" | "task" = "once") => {
    if (!approval || !activeId) return;
    const response = await fetch(`/api/sessions/${activeId}/approval`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ approvalId: approval.id, approved, scope }) });
    const result = await response.json() as { ok?: boolean; error?: string };
    if (!response.ok || result.ok !== true) {
      setError(result.error ?? t("approvalExpired"));
      // The request is no longer decidable server-side (expired, or its
      // timeout event was missed while disconnected): keeping the card would
      // wedge the composer in guide/stop mode until a page reload.
      setApprovalQueue((current) => current.filter((item) => item.id !== approval.id));
      return;
    }
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
      <div className="session-list">{bootstrapping ? <span className="session-loading">{t("loading")}</span> : sessions.map((session) => <div key={session.id} className="session-item-row"><button className={`session-item ${activeId === session.id ? "active" : ""}`} onClick={() => { selectSession(session.id); setMessages([]); setMobileNav(false); }}><span className="session-dot" /><span className="session-copy">{session.name === t("summarizeTitle") ? <span className="session-title-loading" role="status" aria-label={t("summarizeTitle")} title={t("summarizeTitle")} /> : <strong>{session.name || t("newSessionEnglish")}</strong>}<small>{new Date(session.updatedAt).toLocaleDateString()}</small></span></button><button className="session-archive" aria-label={`${t("archive")} ${session.name || t("archived")}`} title={t("archive")} onClick={(event) => { event.stopPropagation(); void archiveSession(session.id); }}><Archive size={14} /></button></div>)}</div>
      <div className="sidebar-bottom"><div className="sidebar-settings-row"><Link href="/settings" className="sidebar-link"><Gear size={17} />{t("settings")}</Link></div></div>
    </aside>
    {mobileNav ? <button className="scrim mobile-only" onClick={() => setMobileNav(false)} aria-label={t("closeNav")} /> : null}
    <main className="main-panel">
      <header className="topbar"><button className="icon-button mobile-only" onClick={() => setMobileNav(true)} aria-label={t("settings")}><List size={19} /></button><button className="workspace workspace-button" type="button" disabled={bootstrapping || workspaceChoosing} aria-busy={workspaceChoosing} onClick={() => void chooseWorkingDirectory()} title={t("changeWorkingDirectory")} aria-label={workspaceChoosing ? t("choosingWorkingDirectory") : t("changeWorkingDirectory")}><FolderOpen size={16} /><span>{cwd || t("workingDirectory")}</span></button><div className="topbar-spacer" /><div className="topbar-actions"><LanguageToggle /><ThemeToggle /></div></header>
      <section ref={conversationRef} className="conversation" onScroll={handleConversationScroll}><div ref={conversationInnerRef} className="conversation-inner">{visibleMessages.length === 0 ? <div className="empty-state"><div className="empty-orbit"><RiftxLogo decorative /></div><h1>{bootstrapping ? t("loadingWorkspace") : activeId ? t("ready") : t("noSession")}</h1><p>{bootstrapping ? t("readingWorkspace") : activeId ? t("readOrTest") : t("createSessionFirst")}</p>{activeId && !bootstrapping ? <div className="prompt-suggestions"><button onClick={() => setInput(t("overview"))}>{t("overview")}</button><button onClick={() => setInput(t("checkRisks"))}>{t("checkRisks")}</button></div> : null}</div> : <>{hasEarlierMessages ? <button className="load-earlier" type="button" onClick={loadEarlierMessages}><ArrowUp size={14} />{t("loadEarlierMessages")}</button> : null}{displayedMessages.map((message) => <MessageItem key={message.id} message={message} labels={messageLabels} />)}</>}{contextCompacting ? <article className="message thinking context-compaction-message" role="status" aria-live="polite"><div className="message-body"><div className="thinking-copy"><span className="thinking-title"><Brain size={14} weight="bold" />{t("contextCompacting")}</span></div></div></article> : null}</div>{showJumpToLatest ? <button className="jump-latest" type="button" aria-label={t("jumpLatest")} title={t("jumpLatest")} onClick={jumpToLatest}><ArrowDown size={17} weight="bold" /></button> : null}</section>
      <footer className="composer-wrap">{approval ? <div className="approval-card"><div className="approval-card-main"><div className="approval-icon"><WarningCircle size={18} weight="bold" /></div><div className="approval-card-copy"><div className="approval-card-title"><span className="eyebrow">{approval.subagentId ? t("subagentApproval") : t("needConfirm")}</span><strong>{approval.subagentId ? approval.agentName : approval.toolName}</strong><span className="approval-card-risk">{t("highRisk")}</span></div>{scopeExpansion ? <p>{t("browserScopeApproval")}</p> : approval.subagentId ? <p>{t("subagentRequestsTool", { agent: approval.agentName ?? "", tool: approval.toolName })}</p> : <p>{approval.toolName === "browser" ? t("browserApproval") : t("terminalApproval")}</p>}</div></div><details className="approval-command"><summary><code>{summarizeApprovalInput(approval.input)}</code><span>{t("expandCommand")}</span></summary><pre>{formatApprovalInput(approval.input)}</pre></details><div className="approval-actions"><button className="button reject" onClick={() => void decide(false)}>{t("reject")}</button><button className="button ghost" onClick={() => void decide(true, "task")}>{t(scopeExpansion ? "allowScopeTask" : "allowTask")}</button><button className="button primary" onClick={() => void decide(true)}>{t(scopeExpansion ? "allowScopeOnce" : "allowOnce")}</button></div></div> : null}<div className="composer"><textarea ref={composerInputRef} value={input} disabled={!activeId || bootstrapping} onChange={(event) => setInput(event.target.value)} onCompositionStart={() => { compositionActiveRef.current = true; compositionEndedAtRef.current = 0; }} onCompositionEnd={() => { compositionActiveRef.current = false; compositionEndedAtRef.current = Date.now(); }} onKeyDown={(event) => { if (event.nativeEvent.isComposing || event.keyCode === 229 || compositionActiveRef.current) return; if (event.key === "Enter" && !event.shiftKey) { if (Date.now() - compositionEndedAtRef.current < 150) { compositionEndedAtRef.current = 0; return; } event.preventDefault(); void send(); } }} placeholder={bootstrapping ? t("loadingWorkspace") : composerBusy ? t("guide") : activeId ? t("ask") : t("createSessionFirst")} rows={1} /><div className="composer-bottom"><div className="composer-tools"><ApprovalModeMenu value={approvalMode} onValueChange={(mode) => void changeApprovalMode(mode)} disabled={bootstrapping || mainAgentRunning} /><span className="composer-hint"><span className="keycap">Shift</span> + <span className="keycap">Enter</span> {t("shiftEnter")}</span></div><div className="composer-actions"><ContextRing percent={bootstrapping ? null : usage.percent} label={bootstrapping ? "—" : usage.percent === null ? "—" : `${Math.round(usage.percent)}`} detail={detail} />{bootstrapping ? <span className="model-label">{t("loadingModel")}</span> : modelProfiles.length > 1 ? <ModelMenu value={activeProfileId} onValueChange={(profileId) => void changeModel(profileId)} options={modelProfiles.map((profile) => ({ value: profile.id, label: `${profile.provider}/${profile.model}` }))} disabled={mainAgentRunning || modelSwitching} /> : <span className="model-label">{modelName}</span>}{composerBusy ? (input.trim() ? <button className="send-button" aria-label={t("sendGuide")} title={t("sendGuide")} onClick={() => void send("steer")}><ArrowUp size={18} weight="bold" /></button> : <button className="send-button stop" aria-label={t("stop")} title={t("stop")} onClick={stopAll}><Stop size={17} weight="fill" /></button>) : input.trim() ? <button className="send-button" aria-label={t("send")} title={t("send")} onClick={() => void send("prompt")} disabled={!activeId || bootstrapping}><ArrowUp size={18} weight="bold" /></button> : running ? <button className="send-button stop" aria-label={t("stop")} title={t("stop")} onClick={stopAll}><Stop size={17} weight="fill" /></button> : <button className="send-button" aria-label={t("send")} title={t("send")} onClick={() => void send("prompt")} disabled={!activeId || bootstrapping || !input.trim()}><ArrowUp size={18} weight="bold" /></button>}</div></div></div></footer>
    </main>
    <aside className="right-rail" aria-label={t("subagents")}><SubagentPanel tasks={subagents} running={subagentRunning} maxConcurrent={maxConcurrentSubagents} onCancel={(taskId) => void cancelSubagent(taskId)} onRetry={(taskId) => void retrySubagent(taskId)} focus={subagentFocus} /><FindingsPanel sessionId={activeId || undefined} findings={findings} onPatch={(id, patch) => void patchFindingInSession(id, patch)} onToolClick={scrollToTool} onRequestClick={scrollToRequest} /></aside>
    {error ? <div className="toast-wrap"><ErrorNotice message={error} onDismiss={() => setError("")} /></div> : null}
  </div>;
}
