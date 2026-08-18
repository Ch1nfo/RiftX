"use client";

import Link from "next/link";
import { useEffect, useMemo, useRef, useState } from "react";
import { Archive, ArrowDown, ArrowUp, Brain, Command, Gear, List, Plus, Stop, TerminalWindow, WarningCircle, X } from "@phosphor-icons/react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { ApprovalModeMenu, ContextRing, ErrorNotice, LanguageToggle, ModelMenu, RiftxLogo, ThemeToggle, Tip } from "./ui";
import type { ApprovalMode, ApprovalRequest, ContextUsage, ModelProfile, RiftxEvent, SessionSummary } from "@/lib/types";
import { useLanguage } from "@/lib/i18n";

type Message = { id: string; role: "user" | "assistant" | "thinking" | "tool"; content: string; toolName?: string; toolCallId?: string; status?: string; isError?: boolean };

const emptyUsage: ContextUsage = { tokens: 0, contextWindow: 128000, percent: 0, input: 0, output: 0, cacheRead: 0, cacheWrite: 0, remaining: 128000 };

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
  const { t } = useLanguage();
  const [open, setOpen] = useState(message.status === "running");
  useEffect(() => { setOpen(message.status === "running"); }, [message.status]);
  return <details className={`tool-card ${message.isError ? "error" : ""}`} open={open} onToggle={(event) => setOpen(event.currentTarget.open)}>
    <summary className="tool-card-head"><span><Command size={14} />{message.toolName}</span><span className={`tool-status ${message.status}`}>{message.status === "running" ? t("running") : message.status === "error" ? t("failed") : t("complete")}</span></summary>
    <pre>{message.content}</pre>
  </details>;
}

export function Workbench() {
  const { language, t } = useLanguage();
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [activeId, setActiveId] = useState("");
  const [messages, setMessages] = useState<Message[]>([]);
  const [usage, setUsage] = useState(emptyUsage);
  const [modelName, setModelName] = useState("No model configured");
  const [modelProfiles, setModelProfiles] = useState<ModelProfile[]>([]);
  const [activeProfileId, setActiveProfileId] = useState("");
  const [approvalMode, setApprovalMode] = useState<ApprovalMode>("request");
  const [cwd, setCwd] = useState("");
  const [input, setInput] = useState("");
  const [running, setRunning] = useState(false);
  const [bootstrapping, setBootstrapping] = useState(true);
  const [approvalQueue, setApprovalQueue] = useState<ApprovalRequest[]>([]);
  const [error, setError] = useState("");
  const [mobileNav, setMobileNav] = useState(false);
  const [showJumpToLatest, setShowJumpToLatest] = useState(false);
  const [streamGeneration, setStreamGeneration] = useState(0);
  const conversationRef = useRef<HTMLElement>(null);
  const shouldAutoScrollRef = useRef(true);
  const endRef = useRef<HTMLDivElement>(null);
  const titleQueueRef = useRef<Promise<void>>(Promise.resolve());
  const titleRequestRef = useRef(0);

  const backfillMissingTitles = (items: SessionSummary[]) => {
    const missing = items.filter((session) => session.name === "未命名任务" && session.firstMessage).slice(0, 8);
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
    shouldAutoScrollRef.current = true;
    setShowJumpToLatest(false);
    if (!activeId) return;
    fetch(`/api/sessions/${activeId}/messages`).then((response) => response.json()).then((items: Message[]) => {
      setMessages(items.filter((item) => ["user", "assistant", "thinking", "tool"].includes(item.role)).map((item) => ({ ...item, role: item.role as Message["role"] })));
    }).catch(() => undefined);
    const source = new EventSource(`/api/sessions/${activeId}/stream`);
    source.onmessage = (event) => {
      const payload = JSON.parse(event.data) as RiftxEvent;
      if (payload.type === "connected") return;
      if (payload.type === "usage") {
        const next = payload.usage as Partial<ContextUsage>;
        setUsage((current) => ({ ...current, ...next, tokens: Number(next.tokens ?? current.tokens), contextWindow: Number(next.contextWindow ?? current.contextWindow), percent: next.percent === null ? null : Number(next.percent ?? current.percent ?? 0), input: Number(next.input ?? current.input), output: Number(next.output ?? current.output), cacheRead: Number(next.cacheRead ?? current.cacheRead), cacheWrite: Number(next.cacheWrite ?? current.cacheWrite), remaining: Number(next.remaining ?? current.remaining) }));
        return;
      }
      if (payload.type === "approval_required") {
        const request = payload.approval as ApprovalRequest;
        setApprovalQueue((current) => current.some((item) => item.id === request.id) ? current : [...current, request]);
        setRunning(true);
        return;
      }
      if (["text_delta", "tool_start", "tool_update", "tool_end", "message", "done", "error"].includes(payload.type)) setMessages((current) => current.map((message) => message.role === "thinking" && message.status === "streaming" ? { ...message, status: "done" } : message));
      if (payload.type === "session_state") { setRunning(payload.state !== "idle"); return; }
      if (payload.type === "done") { setRunning(false); setApprovalQueue([]); setMessages((current) => current.map((message) => message.role === "thinking" ? { ...message, status: "done" } : message)); return; }
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
      if (payload.type === "tool_start") { const toolCallId = String(payload.toolCallId ?? crypto.randomUUID()); setMessages((current) => [...current, { id: toolCallId, role: "tool", toolCallId, toolName: String(payload.toolName ?? "tool"), content: JSON.stringify(payload.args ?? {}, null, 2), status: "running" }]); return; }
      if (payload.type === "tool_end") {
        const toolCallId = String(payload.toolCallId ?? "");
        setMessages((current) => current.map((message) => message.id === toolCallId ? { ...message, status: payload.isError ? "error" : "done", isError: Boolean(payload.isError), content: JSON.stringify(payload.result ?? {}, null, 2) } : message)); return;
      }
      if (payload.type === "error") { setRunning(false); setApprovalQueue([]); setError(String(payload.error ?? "Agent error")); }
    };
    source.onerror = () => { setRunning(false); setApprovalQueue([]); setError(language === "en" ? "Live connection lost. Refresh and try again." : "实时连接已断开，请刷新重试"); };
    return () => source.close();
  }, [activeId, streamGeneration]);

  useEffect(() => {
    let frame: number | undefined;
    if (shouldAutoScrollRef.current) {
      frame = requestAnimationFrame(() => {
        const conversation = conversationRef.current;
        if (conversation) conversation.scrollTo({ top: conversation.scrollHeight, behavior: "auto" });
      });
    }
    document.querySelectorAll<HTMLElement>(".thinking-copy").forEach((element) => { element.scrollTop = element.scrollHeight; });
    return () => { if (frame !== undefined) cancelAnimationFrame(frame); };
  }, [messages]);

  const handleConversationScroll = () => {
    const conversation = conversationRef.current;
    if (!conversation) return;
    const atLatest = conversation.scrollHeight - conversation.scrollTop - conversation.clientHeight <= 32;
    shouldAutoScrollRef.current = atLatest;
    setShowJumpToLatest((current) => {
      const next = !atLatest && messages.length > 0;
      return current === next ? current : next;
    });
  };

  const jumpToLatest = () => {
    shouldAutoScrollRef.current = true;
    setShowJumpToLatest(false);
    conversationRef.current?.scrollTo({ top: conversationRef.current.scrollHeight, behavior: "smooth" });
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
        if (!response.ok) throw new Error(data.error ?? (language === "en" ? "Could not generate task title" : "生成任务标题失败"));
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
    const mode = requestedMode ?? (running ? "steer" : "prompt");
    setInput("");
    shouldAutoScrollRef.current = true;
    setShowJumpToLatest(false);
    setMessages((current) => [...current, { id: crypto.randomUUID(), role: "user", content: text }]);
    const currentSession = sessions.find((session) => session.id === activeId);
    const hasExistingUserMessage = Boolean(currentSession?.firstMessage?.trim()) || messages.some((message) => message.role === "user");
    if (!hasExistingUserMessage) queueSessionTitle(text);
    setRunning(true);
    const response = await fetch(`/api/sessions/${activeId}/prompt`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ text, mode }) });
    if (!response.ok) setError((await response.json()).error ?? t("sendFailed"));
  };

  const newSession = async () => {
    const response = await fetch("/api/sessions", { method: "POST" });
    const data = await response.json();
    setActiveId(data.id);
    setMessages([]);
    setUsage(emptyUsage);
    setSessions(await fetch("/api/sessions").then((item) => item.json()));
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
    setUsage(emptyUsage);
  };

  const approval = approvalQueue[0] ?? null;

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
      setError((await response.json()).error ?? (language === "en" ? "Could not change approval mode" : "切换审批模式失败"));
    }
  };

  const changeModel = async (profileId: string) => {
    if (running || profileId === activeProfileId) return;
    const previousId = activeProfileId;
    const previousProfile = modelProfiles.find((item) => item.id === previousId);
    const nextProfile = modelProfiles.find((item) => item.id === profileId);
    if (!nextProfile) return;
    setActiveProfileId(profileId);
    setModelName(`${nextProfile.provider}/${nextProfile.model}`);
    try {
      const response = await fetch("/api/settings/model-profiles", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ activeProfileId: profileId }) });
      if (!response.ok) throw new Error((await response.json()).error ?? (language === "en" ? "Could not switch model" : "切换模型失败"));
      setUsage(emptyUsage);
      setStreamGeneration((current) => current + 1);
    } catch (error) {
      setActiveProfileId(previousId);
      setModelName(previousProfile ? `${previousProfile.provider}/${previousProfile.model}` : "No model configured");
      setError(error instanceof Error ? error.message : (language === "en" ? "Could not switch model" : "切换模型失败"));
    }
  };

  const detail = useMemo(() => {
    const safe = { tokens: Number(usage.tokens) || 0, contextWindow: Number(usage.contextWindow) || 0, input: Number(usage.input) || 0, output: Number(usage.output) || 0, cacheRead: Number(usage.cacheRead) || 0, cacheWrite: Number(usage.cacheWrite) || 0, remaining: Number(usage.remaining) || 0 };
    return <div className="usage-tooltip"><strong>{usage.percent === null ? t("contextUnknown") : `${Math.round(Number(usage.percent) || 0)}%`} {t("context")}</strong><span>{safe.tokens.toLocaleString()} / {safe.contextWindow.toLocaleString()} {t("tokens")}</span><span>{t("input")} {safe.input.toLocaleString()} · {t("output")} {safe.output.toLocaleString()}</span><span>{t("cacheRead")} {safe.cacheRead.toLocaleString()} · {t("cacheWrite")} {safe.cacheWrite.toLocaleString()}</span><span>{t("remaining")} {safe.remaining.toLocaleString()} {t("tokens")}</span></div>;
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
      <header className="topbar"><button className="icon-button mobile-only" onClick={() => setMobileNav(true)} aria-label={t("settings")}><List size={19} /></button><div className="workspace"><TerminalWindow size={16} /><span>{cwd || t("workingDirectory")}</span></div><div className="topbar-spacer" /><div className="topbar-actions"><LanguageToggle /><ThemeToggle /></div></header>
      <section ref={conversationRef} className="conversation" onScroll={handleConversationScroll}><div className="conversation-inner">{messages.length === 0 ? <div className="empty-state"><div className="empty-orbit"><RiftxLogo decorative variant="mark" /></div><h1>{bootstrapping ? t("loadingWorkspace") : activeId ? t("ready") : t("noSession")}</h1><p>{bootstrapping ? t("readingWorkspace") : activeId ? t("readOrTest") : t("createSessionFirst")}</p>{activeId && !bootstrapping ? <div className="prompt-suggestions"><button onClick={() => setInput(t("overview"))}>{t("overview")}</button><button onClick={() => setInput(t("checkRisks"))}>{t("checkRisks")}</button></div> : null}</div> : messages.map((message) => <article key={message.id} className={`message ${message.role}`}>
        {message.role === "user" ? <div className="avatar user-avatar">{language === "en" ? "You" : "你"}</div> : message.role === "assistant" ? <div className="avatar assistant-avatar"><RiftxLogo decorative /></div> : null}
        <div className="message-body">{message.role === "thinking" ? <details className="thinking-block" open={message.status === "streaming"}><summary><span className="thinking-title"><Brain size={14} weight="bold" />{t("thinking")}</span><span className="thinking-state">{message.status === "streaming" ? t("thinkingNow") : t("thinkingDone")}</span></summary><div className="thinking-copy">{message.content}</div></details> : message.role === "tool" ? <ToolCard key={`${message.id}-${message.status}`} message={message} /> : <div className="markdown"><ReactMarkdown remarkPlugins={[remarkGfm]}>{message.content}</ReactMarkdown></div>}</div>
      </article>)}<div ref={endRef} /></div>{showJumpToLatest ? <button className="jump-latest" type="button" aria-label={t("jumpLatest")} title={t("jumpLatest")} onClick={jumpToLatest}><ArrowDown size={17} weight="bold" /></button> : null}</section>
      <footer className="composer-wrap">{approval ? <div className="approval-card"><div className="approval-card-main"><div className="approval-icon"><WarningCircle size={18} weight="bold" /></div><div className="approval-card-copy"><div className="approval-card-title"><span className="eyebrow">{t("needConfirm")}</span><strong>{approval.toolName}</strong><span className="approval-card-risk">{t("highRisk")}</span></div><p>{approval.toolName === "browser" ? t("browserApproval") : t("terminalApproval")}</p></div></div><details className="approval-command"><summary><code>{summarizeApprovalInput(approval.input)}</code><span>{t("expandCommand")}</span></summary><pre>{formatApprovalInput(approval.input)}</pre></details><div className="approval-actions"><button className="button reject" onClick={() => void decide(false)}>{t("reject")}</button><button className="button ghost" onClick={() => void decide(true, "task")}>{t("allowTask")}</button><button className="button primary" onClick={() => void decide(true)}>{t("allowOnce")}</button></div></div> : null}<div className="composer"><textarea value={input} disabled={!activeId || bootstrapping} onChange={(event) => setInput(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); void send(); } }} placeholder={bootstrapping ? t("loadingWorkspace") : running ? t("guide") : activeId ? t("ask") : t("createSessionFirst")} rows={1} /><div className="composer-bottom"><div className="composer-tools"><ApprovalModeMenu value={approvalMode} onValueChange={(mode) => void changeApprovalMode(mode)} disabled={bootstrapping || running} /><span className="composer-hint"><span className="keycap">Shift</span> + <span className="keycap">Enter</span> {t("shiftEnter")}</span></div><div className="composer-actions"><ContextRing percent={bootstrapping ? null : usage.percent} label={bootstrapping ? "—" : usage.percent === null ? "—" : `${Math.round(usage.percent)}`} detail={detail} />{bootstrapping ? <span className="model-label">{t("loadingModel")}</span> : modelProfiles.length > 1 ? <ModelMenu value={activeProfileId} onValueChange={(profileId) => void changeModel(profileId)} options={modelProfiles.map((profile) => ({ value: profile.id, label: `${profile.provider}/${profile.model}` }))} disabled={running} /> : <span className="model-label">{modelName}</span>}{running && input.trim() ? <button className="send-button" aria-label={t("sendGuide")} title={t("sendGuide")} onClick={() => void send("steer")}><ArrowUp size={18} weight="bold" /></button> : running ? <button className="send-button stop" aria-label={t("stop")} title={t("stop")} onClick={() => { setRunning(false); setApprovalQueue([]); void fetch(`/api/sessions/${activeId}/abort`, { method: "POST" }); }}><Stop size={17} weight="fill" /></button> : <button className="send-button" aria-label={t("send")} title={t("send")} onClick={() => void send("prompt")} disabled={!activeId || bootstrapping || !input.trim()}><ArrowUp size={18} weight="bold" /></button>}</div></div></div></footer>
    </main>
    {error ? <div className="toast-wrap"><ErrorNotice message={error} onDismiss={() => setError("")} /></div> : null}
  </div>;
}
