"use client";

import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { useLanguage } from "@/lib/i18n";
import type { BoardSnapshot, BoardWork } from "@/lib/collaboration";
import type { RiftxEvent, SubagentLogEntry } from "@/lib/types";

const labels = {
  zh: { title: "共享协作", tasks: "任务板", agents: "Agent", messages: "通信记录", pause: "暂停", resume: "恢复", cancel: "取消", retry: "重试", send: "发送", to: "接收 Agent", body: "消息内容", limits: "调整预算", save: "保存", wakes: "自主唤醒", work: "接纳工作", notes: "消息与便笺", retries: "额外重试", budgetHint: "协作次数预算，不是 Token 费用上限", accepted: "已接受", queued: "待投递", in_context: "已进入上下文", replied: "已回复", legacy: "此会话使用旧协作流程", loading: "正在读取协作状态…", empty: "暂无记录", dependencies: "依赖", acceptance: "验收条件", priority: "优先级", owner: "执行者", reason: "阻塞原因", result: "结果", pending: "待投递", confirm: "上一次执行是否已确认停止？仅在确认相关进程和工具已停止后继续重试。", acknowledge: "确认执行已停止", uncertain: "执行状态待确认", stopping: "正在停止…", refresh: "重新读取", log: "工具记录", facts: "公共调查更新", reply: "回复", resources: "临时浏览器等资源在休眠后需要重新建立" },
  en: { title: "Shared collaboration", tasks: "Task board", agents: "Agents", messages: "Messages", pause: "Pause", resume: "Resume", cancel: "Cancel", retry: "Retry", send: "Send", to: "Recipient", body: "Message", limits: "Adjust budgets", save: "Save", wakes: "Autonomous wakes", work: "Admitted work", notes: "Messages and notes", retries: "Extra retries", budgetHint: "Collaboration counts, not a token cost cap", accepted: "Accepted", queued: "Pending delivery", in_context: "Entered context", replied: "Replied", legacy: "This session uses legacy collaboration", loading: "Loading collaboration…", empty: "No records yet", dependencies: "Dependencies", acceptance: "Acceptance criteria", priority: "Priority", owner: "Owner", reason: "Blocked reason", result: "Result", pending: "Pending", confirm: "Has the previous execution definitely stopped? Retry only after verifying its processes and tools have stopped.", acknowledge: "Confirm execution stopped", uncertain: "Execution needs confirmation", stopping: "Stopping…", refresh: "Refresh", log: "Tool activity", facts: "Shared investigation updates", reply: "Reply", resources: "Temporary browser resources must be recreated after sleep" }
};
const states: Record<string, [string, string]> = {
  retry_budget: ["工作重试预算已用尽", "Work retry budget exhausted"], stopping: ["正在停止…", "Stopping…"], proposed: ["待批准", "Proposed"], ready: ["可领取", "Ready"], running: ["运行中", "Running"], awaiting_review: ["待验收", "Awaiting review"], done: ["已验收", "Accepted"], blocked: ["已阻塞", "Blocked"], failed: ["失败", "Failed"], cancelled: ["已取消", "Cancelled"], rejected: ["已拒绝", "Rejected"], idle: ["空闲", "Idle"], sleeping: ["休眠", "Sleeping"], paused: ["已暂停", "Paused"], waiting_user: ["等待用户", "Waiting for user"], completed: ["已完成", "Completed"], dependencies: ["等待依赖完成", "Waiting for dependencies"], restart: ["重启后等待恢复", "Resume required after restart"], user: ["用户暂停", "Paused by user"], wake_budget: ["自主唤醒预算已用尽", "Autonomous wake budget exhausted"], execution_uncertain: ["执行状态待确认", "Execution needs confirmation"], execution_failed: ["执行失败", "Execution failed"], agent_start_failed: ["Agent 启动失败", "Agent could not start"], no_progress: ["需要用户继续处理", "User input required"]
};

export function CollaborationPanel({ sessionId, legacy, focus, onReference, onRunningChange, onModeChange }: { sessionId: string; legacy: ReactNode; focus?: { taskId: string; logId?: string } | null; onModeChange?: (shared: boolean) => void; onRunningChange?: (value: boolean) => void; onReference?: (type: string, id: string, agent?: string) => void }) {
  const { language } = useLanguage();
  const zh = language.startsWith("zh"); const t = labels[zh ? "zh" : "en"];
  const label = (value: string) => states[value]?.[zh ? 0 : 1] ?? value;
  const [snapshot, setSnapshot] = useState<BoardSnapshot>();
  const [tab, setTab] = useState<"tasks" | "agents" | "messages">("tasks");
  const [error, setError] = useState(""); const [busy, setBusy] = useState(false);
  const [recipient, setRecipient] = useState("main"); const [body, setBody] = useState("");
  const [replyTo, setReplyTo] = useState<string>(); const [limitsOpen, setLimitsOpen] = useState(false);
  const [logs, setLogs] = useState<Record<string, SubagentLogEntry[]>>({});
  const [expanded, setExpanded] = useState<string>();
  const current = useRef(sessionId); current.current = sessionId;
  const revision = useRef(-1); const requestedSeq = useRef(0); const loading = useRef(false); const again = useRef(false);
  const endpoint = `/api/sessions/${encodeURIComponent(sessionId)}/collaboration`;
  const apply = useCallback((next: BoardSnapshot) => {
    if (next.mode === "shared") {
      if (next.state.revision < revision.current) return;
      revision.current = next.state.revision;
    }
    setSnapshot(next);
  }, []);
  const refresh = useCallback(async () => {
    if (!sessionId) return;
    if (loading.current) { again.current = true; return; }
    loading.current = true;
    try {
      let attempts = 0;
      do {
        attempts++;
        again.current = false;
        const response = await fetch(`${endpoint}?after=${Math.max(0, revision.current)}`, { cache: "no-store", signal: AbortSignal.timeout(15000) });
        const data = await response.json();
        if (current.current !== sessionId) return;
        if (!response.ok) throw new Error(data.error ?? "Collaboration unavailable");
        apply(data); setError("");
        if (data.mode === "shared" && data.state.revision < requestedSeq.current) again.current = true;
      } while (again.current && attempts < 3 && current.current === sessionId);
    } catch (e) { if (current.current === sessionId) setError(e instanceof Error ? e.message : "Collaboration unavailable"); }
    finally { loading.current = false; }
  }, [sessionId, endpoint, apply]);
  useEffect(() => {
    revision.current = -1; requestedSeq.current = 0; loading.current = false; again.current = false;
    setSnapshot(undefined); setError(""); setLogs({}); setExpanded(undefined); setRecipient("main"); setReplyTo(undefined); setBody("");
    void refresh();
    let timer: ReturnType<typeof setTimeout> | undefined;
    const event = (raw: Event) => {
      const message = (raw as CustomEvent<RiftxEvent>).detail;
      if (message.sessionId !== sessionId) return;
      if (message.type === "collaboration" && message.collaboration) {
        if (message.collaboration.seq <= revision.current) return;
        requestedSeq.current = Math.max(requestedSeq.current, message.collaboration.seq);
      } else if (message.type !== "connected") return;
      clearTimeout(timer); timer = setTimeout(() => { void refresh(); }, 100);
    };
    window.addEventListener("riftx:collaboration", event);
    return () => { clearTimeout(timer); window.removeEventListener("riftx:collaboration", event); };
  }, [sessionId, refresh]);
  const loadLogs = useCallback(async (id: string) => {
    try {
      const response = await fetch(`${endpoint}/agents/${encodeURIComponent(id)}/activity`, { cache: "no-store" });
      const data = await response.json();
      if (response.ok && current.current === sessionId) setLogs((v) => ({ ...v, [id]: data.logs }));
    } catch { /* can retry without changing board state */ }
  }, [endpoint, sessionId]);
  useEffect(() => { if (focus) { setTab("agents"); setExpanded(focus.taskId); void loadLogs(focus.taskId); } }, [focus, loadLogs]);
  useEffect(() => {
    if (!expanded) return;
    const event = (raw: Event) => {
      const e = (raw as CustomEvent<RiftxEvent>).detail;
      if (e.sessionId === sessionId && e.type === "collaboration_activity" && e.agentId === expanded) {
        const activity = e.activity as RiftxEvent;
        if (activity.type === "tool_end" || activity.type === "done") void loadLogs(expanded);
      }
    };
    window.addEventListener("riftx:collaboration", event);
    return () => window.removeEventListener("riftx:collaboration", event);
  }, [expanded, loadLogs, sessionId]);
  async function command(path: string, payload: Record<string, unknown>) {
    if (busy) return false;
    setBusy(true); setError("");
    try {
      const response = await fetch(endpoint + path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ commandId: crypto.randomUUID(), ...payload }) });
      const data = await response.json();
      if (current.current !== sessionId) return false;
      if (!response.ok) throw new Error(data.error);
      apply(data.snapshot); return true;
    } catch (e) { if (current.current === sessionId) { setError(e instanceof Error ? e.message : "Command failed"); void refresh(); } return false; }
    finally { if (current.current === sessionId) setBusy(false); }
  }
  useEffect(() => {
    const s = snapshot?.mode === "shared" ? snapshot.state : undefined;
    onModeChange?.(Boolean(s));
    onRunningChange?.(Boolean(s && (s.agents.some((a) => a.status === "running") || (s.status === "running" && s.tasks.some((task) => !["done", "cancelled", "rejected"].includes(task.status))))));
  }, [snapshot, onRunningChange, onModeChange]);
  useEffect(() => () => onRunningChange?.(false), [onRunningChange]);
  if (!sessionId) return null;
  if (snapshot?.mode === "legacy") return <><p className="collaboration-legacy">{t.legacy}</p>{legacy}</>;
  const state = snapshot?.mode === "shared" ? snapshot.state : undefined;
  const name = (id?: string) => state?.agents.find((a) => a.id === id)?.name ?? (id === "user" ? (zh ? "用户" : "User") : id ?? "—");
  const references = (refs: BoardWork["references"], owner?: string) => refs.map((ref) => <button className="collaboration-reference" key={`${ref.type}:${ref.id}`} onClick={() => onReference?.(ref.type, ref.id, owner)}>{ref.type}: {ref.id}</button>);
  async function taskAction(task: BoardWork, action: string) {
    const uncertain = (action === "retry" || (action === "cancel" && task.status === "cancelled")) && state?.attempts.some((a) => a.taskId === task.id && a.status === "uncertain");
    if (uncertain && !window.confirm(t.confirm)) return;
    await command(`/tasks/${task.id}/actions`, { action, version: task.version, ...(uncertain ? { confirmStopped: true } : {}) });
  }
  return <section className="collaboration-panel" aria-label={t.title}>
    <header><strong>{t.title}</strong><span>{state ? label(state.status) : t.loading}</span></header>
    {error ? <div role="alert" className="collaboration-error">{error}<button onClick={() => void refresh()}>{t.refresh}</button></div> : null}
    {state ? <>
      {state.reason ? <p role="status">{label(state.reason)}</p> : null}
      <div className="collaboration-controls">
        {state.status !== "completed" ? <button disabled={busy} onClick={() => void command("/control", { action: state.status === "running" ? "pause" : "resume" })}>{busy ? t.stopping : state.status === "running" ? t.pause : t.resume}</button> : null}
        <button onClick={() => setLimitsOpen((v) => !v)}>{t.limits}</button>
      </div>
      <p className="collaboration-budget">{t.work} {state.used.tasks}/{state.limits.tasks} · {t.wakes} {state.used.wakes}/{state.limits.wakes}<br />{t.notes} {state.used.messages}/{state.limits.messages}<br /><small>{t.budgetHint}</small></p>
      {limitsOpen ? <form key={`${state.runId}-${JSON.stringify(state.limits)}`} className="collaboration-limits" onSubmit={(e) => { e.preventDefault(); const form = new FormData(e.currentTarget); void command("/control", { action: "limits", ...Object.fromEntries(["tasks", "wakes", "messages", "retries"].map((key) => [key, Number(form.get(key))])) }).then((ok) => { if (ok) setLimitsOpen(false); }); }}>
        {(["tasks", "wakes", "messages", "retries"] as const).map((key) => <label key={key}>{key === "tasks" ? t.work : key === "wakes" ? t.wakes : key === "messages" ? t.notes : t.retries}<input aria-label={key} name={key} type="number" min={key === "retries" ? 0 : 1} max={10000} defaultValue={state.limits[key]} required /></label>)}<button disabled={busy}>{t.save}</button>
      </form> : null}
      <nav aria-label={t.title}>{(["tasks", "agents", "messages"] as const).map((key) => <button key={key} aria-pressed={tab === key} onClick={() => setTab(key)}>{t[key]}</button>)}</nav>
      <div className="collaboration-list">
        {tab === "tasks" ? <>{state.tasks.length ? state.tasks.map((task) => <article key={task.id} id={`board-task-${task.id}`}>
          <strong>{task.objective}</strong><span className="collaboration-status">{label(task.status)}</span>
          <p>{t.priority}: {task.priority} · {t.owner}: {name(task.owner)}</p>
          <details><summary>{t.acceptance}</summary><p>{task.acceptance}</p></details>
          {task.dependencies.length ? <p>{t.dependencies}: {task.dependencies.map((id) => <a key={id} href={`#board-task-${id}`}>{state.tasks.find((v) => v.id === id)?.objective} </a>)}</p> : null}
          {task.blockedReason ? <p>{t.reason}: {label(task.blockedReason)}</p> : null}
          {task.summary ? <details><summary>{t.result}</summary><p>{task.summary}</p>{references(task.references, task.owner)}</details> : null}
          <div className="collaboration-controls">{!["done", "cancelled", "rejected"].includes(task.status) ? <>
            <button disabled={busy} onClick={() => void taskAction(task, task.paused ? "resume" : "pause")}>{task.paused ? t.resume : t.pause}</button>
            <button disabled={busy} onClick={() => void taskAction(task, "cancel")}>{t.cancel}</button>
            {["failed", "blocked", "awaiting_review"].includes(task.status) ? <button disabled={busy} onClick={() => void taskAction(task, "retry")}>{t.retry}</button> : null}
          </> : task.status === "cancelled" && state.attempts.some((a) => a.taskId === task.id && a.status === "uncertain") ? <button disabled={busy} onClick={() => void taskAction(task, "cancel")}>{t.acknowledge}</button> : null}</div>
        </article>) : <p>{t.empty}</p>}
        {state.notes.length ? <details><summary>{t.facts}</summary>{state.notes.slice(-32).map((n) => <article key={n.id}><p>{name(n.author)} · {n.body}</p>{references(n.references, n.author)}</article>)}</details> : null}</> : null}
        {tab === "agents" ? state.agents.map((agent) => <article key={agent.id}>
          <strong>{agent.name}</strong><span className="collaboration-status">{label(agent.status)}</span>
          <p>{state.tasks.find((task) => task.id === agent.taskId)?.objective ?? "—"}</p>
          <p>{t.pending}: {state.messages.filter((m) => m.to === agent.id && m.status === "queued").length}{agent.pendingApprovalCount ? ` · ${zh ? "待审批" : "Approvals"}: ${agent.pendingApprovalCount}` : ""}</p>
          {agent.status === "sleeping" ? <p>{t.resources}</p> : null}
          <div className="collaboration-controls"><button onClick={() => { setRecipient(agent.id); setTab("messages"); }}>{t.send}</button>
            {agent.role === "child" ? <button disabled={busy} onClick={() => void command(`/agents/${agent.id}/actions`, { action: agent.status === "paused" ? "resume" : "pause" })}>{agent.status === "paused" ? t.resume : t.pause}</button> : null}
            <button onClick={() => { setExpanded(expanded === agent.id ? undefined : agent.id); void loadLogs(agent.id); }}>{t.log}</button>
          </div>
          {expanded === agent.id ? <div>{(logs[agent.id] ?? []).map((log) => <details key={log.id} id={`subagent-log-${agent.id}-${log.id}`} open={focus?.logId === log.id}><summary>{log.toolName ?? log.type} · {log.status ? label(log.status) : ""}</summary><pre>{log.content}</pre></details>)}</div> : null}
        </article>) : null}
        {tab === "messages" ? <>
          <form onSubmit={(e) => { e.preventDefault(); void command("/messages", { to: recipient, kind: replyTo ? "answer" : "information", body, ...(replyTo ? { replyTo } : {}) }).then((ok) => { if (ok) { setBody(""); setReplyTo(undefined); } }); }}>
            <label>{t.to}<select value={recipient} onChange={(e) => { setRecipient(e.target.value); setReplyTo(undefined); }}>{state.agents.map((a) => <option key={a.id} value={a.id}>{a.name}</option>)}</select></label>
            {replyTo ? <p>{t.reply}: {replyTo}<button type="button" onClick={() => setReplyTo(undefined)}>×</button></p> : null}
            <label>{t.body}<textarea value={body} onChange={(e) => setBody(e.target.value)} maxLength={2000} required /></label>
            <button disabled={busy || !body.trim()}>{t.send}</button>
          </form>
          {state.messages.slice().reverse().map((message) => <article key={message.id} id={`board-message-${message.id}`}><strong>{name(message.from)} → {name(message.to)}</strong><p>{message.body}</p><small>{t.accepted} · {t[message.status]} · {new Date(message.createdAt).toLocaleString()}</small>{message.taskId ? <a href={`#board-task-${message.taskId}`} onClick={() => setTab("tasks")}>{state.tasks.find((task) => task.id === message.taskId)?.objective}</a> : null}{message.replyTo ? <a href={`#board-message-${message.replyTo}`}>{t.reply}</a> : null}</article>)}
        </> : null}
      </div>
    </> : null}
  </section>;
}
