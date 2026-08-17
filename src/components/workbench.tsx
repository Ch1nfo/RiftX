"use client";

import Link from "next/link";
import { useEffect, useMemo, useRef, useState } from "react";
import { Archive, ArrowDown, ArrowUp, Brain, Command, Gear, List, Plus, ShieldWarning, Stop, TerminalWindow, X } from "@phosphor-icons/react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { ApprovalModeMenu, ContextRing, ErrorNotice, ModelMenu, RiftxLogo, ThemeToggle, Tip } from "./ui";
import type { ApprovalMode, ApprovalRequest, ContextUsage, ModelProfile, RiftxEvent, SessionSummary } from "@/lib/types";

type Message = { id: string; role: "user" | "assistant" | "thinking" | "tool"; content: string; toolName?: string; toolCallId?: string; status?: string; isError?: boolean };

const emptyUsage: ContextUsage = { tokens: 0, contextWindow: 128000, percent: 0, input: 0, output: 0, cacheRead: 0, cacheWrite: 0, remaining: 128000 };

function ToolCard({ message }: { message: Message }) {
  const [open, setOpen] = useState(message.status === "running");
  useEffect(() => { setOpen(message.status === "running"); }, [message.status]);
  return <details className={`tool-card ${message.isError ? "error" : ""}`} open={open} onToggle={(event) => setOpen(event.currentTarget.open)}>
    <summary className="tool-card-head"><span><Command size={14} />{message.toolName}</span><span className={`tool-status ${message.status}`}>{message.status === "running" ? "运行中" : message.status === "error" ? "失败" : "完成"}</span></summary>
    <pre>{message.content}</pre>
  </details>;
}

export function Workbench() {
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

  useEffect(() => {
    fetch("/api/bootstrap").then((response) => response.json()).then((data) => {
      setSessions(data.sessions ?? []);
      setActiveId(data.activeSessionId ?? "");
      setCwd(data.cwd ?? "");
      const profiles = (data.profiles ?? []) as ModelProfile[];
      setModelProfiles(profiles);
      setActiveProfileId(data.activeProfileId ?? "");
      const profile = profiles.find((item) => item.id === data.activeProfileId);
      if (profile) setModelName(`${profile.provider}/${profile.model}`);
      if (data.approvalMode === "request" || data.approvalMode === "auto" || data.approvalMode === "full") setApprovalMode(data.approvalMode);
    }).catch(() => setError("无法连接到 RiftX 后端")).finally(() => setBootstrapping(false));
    return undefined;
  }, []);

  useEffect(() => {
    shouldAutoScrollRef.current = true;
    setShowJumpToLatest(false);
    if (!activeId) return;
    fetch(`/api/sessions/${activeId}/messages`).then((response) => response.json()).then((items: Message[]) => setMessages(items.filter((item) => ["user", "assistant", "thinking", "tool"].includes(item.role)).map((item) => ({ ...item, role: item.role as Message["role"] })))).catch(() => undefined);
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
      if (payload.type === "tool_start") { setMessages((current) => [...current, { id: String(payload.toolCallId ?? crypto.randomUUID()), role: "tool", toolName: String(payload.toolName ?? "tool"), content: JSON.stringify(payload.args ?? {}, null, 2), status: "running" }]); return; }
      if (payload.type === "tool_end") { setMessages((current) => current.map((message) => message.id === String(payload.toolCallId) ? { ...message, status: payload.isError ? "error" : "done", isError: Boolean(payload.isError), content: JSON.stringify(payload.result ?? {}, null, 2) } : message)); return; }
      if (payload.type === "error") { setRunning(false); setApprovalQueue([]); setError(String(payload.error ?? "Agent error")); }
    };
    source.onerror = () => { setRunning(false); setApprovalQueue([]); setError("实时连接已断开，请刷新重试"); };
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

  const send = async (mode: "prompt" | "steer" | "followUp" = "prompt") => {
    const text = input.trim();
    if (!text || !activeId) return;
    setInput("");
    shouldAutoScrollRef.current = true;
    setShowJumpToLatest(false);
    setMessages((current) => [...current, { id: crypto.randomUUID(), role: "user", content: text }]);
    setRunning(true);
    const response = await fetch(`/api/sessions/${activeId}/prompt`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ text, mode }) });
    if (!response.ok) setError((await response.json()).error ?? "发送失败");
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
    if (!response.ok) { setError((await response.json()).error ?? "归档会话失败"); return; }
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
    if (!response.ok) { setError((await response.json()).error ?? "审批操作失败"); return; }
    setApprovalQueue((current) => current.filter((item) => item.id !== approval.id));
  };

  const changeApprovalMode = async (mode: ApprovalMode) => {
    const previous = approvalMode;
    setApprovalMode(mode);
    const response = await fetch("/api/settings/approval-mode", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ approvalMode: mode }) });
    if (!response.ok) {
      setApprovalMode(previous);
      setError((await response.json()).error ?? "切换审批模式失败");
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
      if (!response.ok) throw new Error((await response.json()).error ?? "切换模型失败");
      setUsage(emptyUsage);
      setStreamGeneration((current) => current + 1);
    } catch (error) {
      setActiveProfileId(previousId);
      setModelName(previousProfile ? `${previousProfile.provider}/${previousProfile.model}` : "No model configured");
      setError(error instanceof Error ? error.message : "切换模型失败");
    }
  };

  const detail = useMemo(() => {
    const safe = { tokens: Number(usage.tokens) || 0, contextWindow: Number(usage.contextWindow) || 0, input: Number(usage.input) || 0, output: Number(usage.output) || 0, cacheRead: Number(usage.cacheRead) || 0, cacheWrite: Number(usage.cacheWrite) || 0, remaining: Number(usage.remaining) || 0 };
    return <div className="usage-tooltip"><strong>{usage.percent === null ? "未知" : `${Math.round(Number(usage.percent) || 0)}%`} context</strong><span>{safe.tokens.toLocaleString()} / {safe.contextWindow.toLocaleString()} tokens</span><span>输入 {safe.input.toLocaleString()} · 输出 {safe.output.toLocaleString()}</span><span>缓存读 {safe.cacheRead.toLocaleString()} · 写 {safe.cacheWrite.toLocaleString()}</span><span>剩余 {safe.remaining.toLocaleString()} tokens</span></div>;
  }, [usage]);

  return <div className="app-shell">
    <aside className={`sidebar ${mobileNav ? "open" : ""}`}>
      <div className="brand-row"><div className="brand-mark"><RiftxLogo /></div><span>RiftX</span><button className="icon-button mobile-only" onClick={() => setMobileNav(false)}><X size={17} /></button></div>
      <button className="new-session" onClick={newSession}><Plus size={17} weight="bold" />新建会话<span className="shortcut">⌘ N</span></button>
      <div className="sidebar-label">最近会话</div>
      <div className="session-list">{bootstrapping ? <span className="session-loading">加载中…</span> : sessions.map((session) => <div key={session.id} className="session-item-row"><button className={`session-item ${activeId === session.id ? "active" : ""}`} onClick={() => { setActiveId(session.id); setMessages([]); setMobileNav(false); }}><span className="session-dot" /><span className="session-copy"><strong>{session.name || "New session"}</strong><small>{new Date(session.updatedAt).toLocaleDateString()}</small></span></button><button className="session-archive" aria-label={`归档 ${session.name || "会话"}`} title="归档会话" onClick={(event) => { event.stopPropagation(); void archiveSession(session.id); }}><Archive size={14} /></button></div>)}</div>
      <div className="sidebar-bottom"><div className="sidebar-settings-row"><Link href="/settings" className="sidebar-link"><Gear size={17} />设置</Link><ThemeToggle /></div><div className="runtime-chip"><span className="status-dot" />本机运行</div></div>
    </aside>
    {mobileNav ? <button className="scrim mobile-only" onClick={() => setMobileNav(false)} aria-label="关闭导航" /> : null}
    <main className="main-panel">
      <header className="topbar"><button className="icon-button mobile-only" onClick={() => setMobileNav(true)}><List size={19} /></button><div className="workspace"><TerminalWindow size={16} /><span>{cwd || "当前工作目录"}</span></div><div className="topbar-spacer" /><Tip content="RiftX 基础能力已启用"><span className="capability"><span className="status-dot" />RiftX ready</span></Tip></header>
      <section ref={conversationRef} className="conversation" onScroll={handleConversationScroll}><div className="conversation-inner">{messages.length === 0 ? <div className="empty-state"><div className="empty-orbit"><RiftxLogo decorative variant="mark" /></div><h1>{bootstrapping ? "正在加载工作区" : activeId ? "准备开始" : "暂无会话"}</h1><p>{bootstrapping ? "正在读取会话和模型配置…" : activeId ? "让 RiftX 读取代码、检查配置，或协助你梳理安全问题。" : "新建会话后即可开始使用 RiftX。"}</p>{activeId && !bootstrapping ? <div className="prompt-suggestions"><button onClick={() => setInput("先概览一下当前工作目录")}>概览当前目录</button><button onClick={() => setInput("检查项目里可能存在的安全风险")}>检查安全风险</button></div> : null}</div> : messages.map((message) => <article key={message.id} className={`message ${message.role}`}>
        {message.role === "user" ? <div className="avatar user-avatar">你</div> : message.role === "assistant" ? <div className="avatar assistant-avatar"><RiftxLogo decorative /></div> : null}
        <div className="message-body">{message.role === "thinking" ? <details className="thinking-block" open={message.status === "streaming"}><summary><span className="thinking-title"><Brain size={14} weight="bold" />Thinking</span><span className="thinking-state">{message.status === "streaming" ? "思考中" : "已完成"}</span></summary><div className="thinking-copy">{message.content}</div></details> : message.role === "tool" ? <ToolCard key={`${message.id}-${message.status}`} message={message} /> : <div className="markdown"><ReactMarkdown remarkPlugins={[remarkGfm]}>{message.content}</ReactMarkdown></div>}</div>
      </article>)}<div ref={endRef} /></div>{showJumpToLatest ? <button className="jump-latest" type="button" aria-label="回到最新消息" title="回到最新消息" onClick={jumpToLatest}><ArrowDown size={17} weight="bold" /></button> : null}</section>
      <footer className="composer-wrap"><div className="composer"><textarea value={input} disabled={!activeId || bootstrapping} onChange={(event) => setInput(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); void send(); } }} placeholder={bootstrapping ? "正在加载工作区…" : activeId ? "向 RiftX 描述你要完成的工作…" : "请先新建会话"} rows={1} /><div className="composer-bottom"><div className="composer-tools"><ApprovalModeMenu value={approvalMode} onValueChange={(mode) => void changeApprovalMode(mode)} disabled={bootstrapping || running} /><span className="composer-hint"><span className="keycap">Shift</span> + <span className="keycap">Enter</span> 换行</span></div><div className="composer-actions"><ContextRing percent={bootstrapping ? null : usage.percent} label={bootstrapping ? "—" : usage.percent === null ? "—" : `${Math.round(usage.percent)}`} detail={detail} />{bootstrapping ? <span className="model-label">加载模型…</span> : modelProfiles.length > 1 ? <ModelMenu value={activeProfileId} onValueChange={(profileId) => void changeModel(profileId)} options={modelProfiles.map((profile) => ({ value: profile.id, label: `${profile.provider}/${profile.model}` }))} disabled={running} /> : <span className="model-label">{modelName}</span>}{running ? <button className="send-button stop" onClick={() => { setRunning(false); setApprovalQueue([]); void fetch(`/api/sessions/${activeId}/abort`, { method: "POST" }); }}><Stop size={17} weight="fill" /></button> : <button className="send-button" onClick={() => void send()} disabled={!activeId || bootstrapping || !input.trim()}><ArrowUp size={18} weight="bold" /></button>}</div></div></div></footer>
    </main>
    {approval ? <div className="approval-layer"><div className="approval-card"><div className="approval-icon"><ShieldWarning size={25} weight="fill" /></div><span className="eyebrow">需要确认 {approvalQueue.length > 1 ? `· 待处理 ${approvalQueue.length} 项` : ""}</span><h2>允许 {approval.toolName} 执行吗？</h2><p>RiftX 默认保护本机文件与命令执行。你可以只允许这一次，或允许本次任务后续的同类操作。</p><pre>{JSON.stringify(approval.input, null, 2)}</pre><div className="approval-actions"><button className="button ghost" onClick={() => void decide(false)}>拒绝</button><button className="button ghost" onClick={() => void decide(true)}>允许一次</button><button className="button primary" onClick={() => void decide(true, "task")}>允许本次任务</button></div></div></div> : null}
    {error ? <div className="toast-wrap"><ErrorNotice message={error} onDismiss={() => setError("")} /></div> : null}
  </div>;
}
