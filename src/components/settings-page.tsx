"use client";

import Link from "next/link";
import { Archive, ArrowLeft, Check, FloppyDisk, Plus, ShieldWarning, Trash } from "@phosphor-icons/react";
import { useEffect, useState } from "react";
import { API_TYPES, TRANSPORTS, type AppConfig, type ModelProfile, type SessionSummary } from "@/lib/types";
import { Field, RiftxLogo, SelectField, ThemeToggle } from "./ui";

const labels: Record<string, string> = {
  "openai-completions": "OpenAI Chat Completions",
  "openai-responses": "OpenAI Responses",
  "anthropic-messages": "Anthropic Messages",
  "google-generative-ai": "Google Generative AI"
};

export function SettingsPage() {
  const [config, setConfig] = useState<AppConfig | null>(null);
  const [selected, setSelected] = useState("");
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState("");
  const [archivedSessions, setArchivedSessions] = useState<SessionSummary[]>([]);
  const [activeSection, setActiveSection] = useState<"model-agent" | "tool-security">("model-agent");

  useEffect(() => {
    Promise.all([fetch("/api/settings/model-profiles"), fetch("/api/sessions?archived=true")]).then(async ([configResponse, sessionsResponse]) => {
      const data = await configResponse.json();
      setConfig(data);
      setSelected(data.profiles?.[0]?.id ?? "");
      setArchivedSessions(await sessionsResponse.json());
    }).catch(() => setError("无法读取设置"));
    return undefined;
  }, []);

  if (!config) return <div className="settings-loading">加载设置…</div>;

  const profile = config.profiles.find((item) => item.id === selected) ?? config.profiles[0];
  const updateProfile = (patch: Partial<ModelProfile>) => setConfig({ ...config, profiles: config.profiles.map((item) => item.id === profile.id ? { ...item, ...patch } : item) });
  const save = async () => {
    const response = await fetch("/api/settings/model-profiles", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(config) });
    if (!response.ok) { setError("保存失败"); return; }
    setConfig(await response.json());
    setSaved(true);
    setTimeout(() => setSaved(false), 1800);
  };
  const addProfile = () => {
    const id = `profile-${Date.now()}`;
    const next = { ...profile, id, name: "新模型配置", apiKey: "" };
    setConfig({ ...config, profiles: [...config.profiles, next], activeProfileId: id });
    setSelected(id);
  };
  const removeProfile = () => {
    if (config.profiles.length < 2) return;
    const next = config.profiles.filter((item) => item.id !== profile.id);
    setConfig({ ...config, profiles: next, activeProfileId: next[0].id });
    setSelected(next[0].id);
  };
  const deleteArchived = async (session: SessionSummary) => {
    if (!window.confirm(`确定永久删除“${session.name || "New session"}”吗？`)) return;
    const response = await fetch(`/api/sessions/${session.id}`, { method: "DELETE" });
    if (!response.ok) { setError((await response.json()).error ?? "删除归档会话失败"); return; }
    setArchivedSessions((current) => current.filter((item) => item.id !== session.id));
  };

  return <div className="settings-shell">
    <aside className="settings-nav">
      <Link href="/" className="back-link"><ArrowLeft size={16} />返回工作台</Link>
      <div className="settings-brand"><div className="brand-mark"><RiftxLogo /></div><div><strong>RiftX 设置</strong><small>本机 Agent 配置</small></div></div>
      <div className="settings-nav-label">配置</div>
      <a href="#model-agent" className={`settings-nav-item ${activeSection === "model-agent" ? "active" : ""}`} onClick={() => setActiveSection("model-agent")}>模型与 Agent</a>
      <a href="#tool-security" className={`settings-nav-item ${activeSection === "tool-security" ? "active" : ""}`} onClick={() => setActiveSection("tool-security")}>工具安全</a>
    </aside>
    <main className="settings-main">
      <header className="settings-header"><div><span className="eyebrow">WORKSPACE SETTINGS</span><h1>模型与 Agent</h1><p>配置 RiftX 的连接方式、上下文窗口和子 Agent 行为。</p></div><div className="settings-header-actions"><ThemeToggle /><button className="button primary" onClick={() => void save()}>{saved ? <Check size={17} /> : <FloppyDisk size={17} />} {saved ? "已保存" : "保存设置"}</button></div></header>
      <div className="settings-grid">
        <section id="model-agent" className="settings-card">
          <div className="card-heading"><div><h2>模型配置档案</h2><p>主 Agent 与子 Agent 可分别选择模型。</p></div><div className="inline-actions"><button className="icon-button" onClick={addProfile} aria-label="添加配置"><Plus size={17} /></button><button className="icon-button danger-icon" onClick={removeProfile} aria-label="删除配置"><Trash size={16} /></button></div></div>
          <div className="profile-tabs">{config.profiles.map((item) => <button key={item.id} className={item.id === profile.id ? "active" : ""} onClick={() => setSelected(item.id)}>{item.name || item.model}</button>)}</div>
          <div className="form-grid">
            <Field label="配置名称"><input value={profile.name} onChange={(event) => updateProfile({ name: event.target.value })} /></Field>
            <Field label="Provider"><input value={profile.provider} onChange={(event) => updateProfile({ provider: event.target.value })} /></Field>
            <Field label="Model ID"><input value={profile.model} onChange={(event) => updateProfile({ model: event.target.value })} /></Field>
            <Field label="API Key" hint="仅保存在本机配置文件。"><input type="password" value={profile.apiKey ?? ""} onChange={(event) => updateProfile({ apiKey: event.target.value })} placeholder="••••••••" /></Field>
            <Field label="Base URL"><input value={profile.baseUrl} onChange={(event) => updateProfile({ baseUrl: event.target.value })} /></Field>
            <Field label="API 协议"><SelectField value={profile.api} onValueChange={(value) => updateProfile({ api: value as ModelProfile["api"] })} options={API_TYPES.map((value) => ({ value, label: labels[value] }))} /></Field>
            <Field label="传输方式"><SelectField value={profile.transport} onValueChange={(value) => updateProfile({ transport: value as ModelProfile["transport"] })} options={TRANSPORTS.map((value) => ({ value, label: value.toUpperCase() }))} /></Field>
            <Field label="Thinking level"><SelectField value={profile.thinkingLevel} onValueChange={(value) => updateProfile({ thinkingLevel: value as ModelProfile["thinkingLevel"] })} options={["off", "minimal", "low", "medium", "high", "xhigh"].map((value) => ({ value, label: value }))} /></Field>
            <Field label="Context window"><input type="number" min={1024} step={1024} value={profile.contextWindow} onChange={(event) => updateProfile({ contextWindow: Number(event.target.value) })} /></Field>
            <Field label="Max output tokens"><input type="number" min={256} step={256} value={profile.maxTokens} onChange={(event) => updateProfile({ maxTokens: Number(event.target.value) })} /></Field>
          </div>
        </section>
        <section className="settings-card"><div className="card-heading"><div><h2>子 Agent</h2><p>为一次性子任务提供独立模型选择。</p></div></div><label className="toggle-row"><span><strong>继承主 Agent 模型</strong><small>开启后，子 Agent 自动使用当前主模型配置。</small></span><input type="checkbox" checked={config.childInherit} onChange={(event) => setConfig({ ...config, childInherit: event.target.checked })} /></label><Field label="独立配置"><SelectField value={config.childProfileId ?? profile.id} onValueChange={(value) => setConfig({ ...config, childProfileId: value })} options={config.profiles.map((item) => ({ value: item.id, label: item.name || item.model }))} /></Field></section>
        <section className="settings-card"><div className="card-heading"><div><h2>工作目录</h2><p>RiftX 的 read、grep、find、ls 和受控命令都在此目录运行。</p></div></div><Field label="当前目录"><input value={config.cwd} onChange={(event) => setConfig({ ...config, cwd: event.target.value })} /></Field><div className="safety-note"><ShieldWarning size={18} weight="fill" /><span>高风险操作由工作台左下角的审批模式控制。</span></div></section>
        <section id="tool-security" className="settings-card"><div className="card-heading"><div><h2>工具安全</h2><p>查看 RiftX 内置工具的默认权限和当前审批策略。</p></div><ShieldWarning size={18} color="var(--muted)" /></div><div className="security-policy-list"><div><strong>默认允许</strong><span>read、grep、find、ls 只读工具</span></div><div><strong>需要审批</strong><span>bash、write、edit 等可能改变本机状态的工具</span></div><div><strong>当前审批模式</strong><span>{config.approvalMode === "request" ? "请求审批" : config.approvalMode === "auto" ? "帮我审批" : "完全访问"}，可在工作台 composer 左下角切换</span></div></div></section>
        <section className="settings-card"><div className="card-heading"><div><h2>归档会话</h2><p>归档会话不会出现在工作台列表中，可在这里永久删除。</p></div><Archive size={18} color="var(--muted)" /></div>{archivedSessions.length ? <div className="archived-session-list">{archivedSessions.map((session) => <div className="archived-session-row" key={session.id}><div className="session-copy"><strong>{session.name || "New session"}</strong><small>{new Date(session.updatedAt).toLocaleString()}</small></div><button className="icon-button danger-icon" aria-label={`永久删除 ${session.name || "会话"}`} title="永久删除" onClick={() => void deleteArchived(session)}><Trash size={16} /></button></div>)}</div> : <div className="archived-empty">暂无归档会话</div>}</section>
      </div>
      {error ? <div className="inline-error">{error}</div> : null}
    </main>
  </div>;
}
