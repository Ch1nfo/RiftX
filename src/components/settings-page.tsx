"use client";

import Link from "next/link";
import { Archive, ArrowLeft, Check, FloppyDisk, Plus, Trash, Warning } from "@phosphor-icons/react";
import { useEffect, useState } from "react";
import { API_TYPES, SUBAGENT_AGGRESSIVENESS, TRANSPORTS, type AppConfig, type ModelProfile, type SessionSummary, type SubagentAggressiveness } from "@/lib/types";
import { Field, LanguageToggle, RiftxLogo, SelectField, ThemeToggle } from "./ui";
import { useLanguage } from "@/lib/i18n";

const labels: Record<string, string> = {
  "openai-completions": "OpenAI Chat Completions",
  "openai-responses": "OpenAI Responses",
  "anthropic-messages": "Anthropic Messages",
  "google-generative-ai": "Google Generative AI"
};

export function SettingsPage() {
  const { t, language } = useLanguage();
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
    }).catch(() => setError(t("settingsLoadFailed")));
    return undefined;
  }, []);

  if (!config) return <div className="settings-loading">{t("loadingSettings")}</div>;

  const profile = config.profiles.find((item) => item.id === selected) ?? config.profiles[0];
  const updateProfile = (patch: Partial<ModelProfile>) => setConfig({ ...config, profiles: config.profiles.map((item) => item.id === profile.id ? { ...item, ...patch } : item) });
  const save = async () => {
    const response = await fetch("/api/settings/model-profiles", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(config) });
    if (!response.ok) { setError(t("saveFailed")); return; }
    setConfig(await response.json());
    setSaved(true);
    setTimeout(() => setSaved(false), 1800);
  };
  const addProfile = () => {
    const id = `profile-${Date.now()}`;
    const next = { ...profile, id, name: t("newModelProfile"), apiKey: "" };
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
    if (!window.confirm(t("confirmDelete", { name: session.name || t("newSessionEnglish") }))) return;
    const response = await fetch(`/api/sessions/${session.id}`, { method: "DELETE" });
    if (!response.ok) { setError((await response.json()).error ?? t("deleteArchivedFailed")); return; }
    setArchivedSessions((current) => current.filter((item) => item.id !== session.id));
  };
  return <div className="settings-shell">
    <aside className="settings-nav">
      <Link href="/" className="back-link"><ArrowLeft size={16} />{t("backToWorkbench")}</Link>
      <div className="settings-brand"><div className="brand-mark"><RiftxLogo /></div><div><strong>RiftX {t("settings")}</strong><small>{t("localAgent")} {t("configuration")}</small></div></div>
      <div className="settings-nav-label">{t("config")}</div>
      <a href="#model-agent" className={`settings-nav-item ${activeSection === "model-agent" ? "active" : ""}`} onClick={() => setActiveSection("model-agent")}>{t("modelAgent")}</a>
      <a href="#tool-security" className={`settings-nav-item ${activeSection === "tool-security" ? "active" : ""}`} onClick={() => setActiveSection("tool-security")}>{t("toolSecurity")}</a>
    </aside>
    <main className="settings-main">
      <header className="settings-header"><div><span className="eyebrow">{t("workspaceSettings")}</span><h1>{t("modelAgent")}</h1><p>{t("modelSettingsDesc")}</p></div><div className="settings-header-actions"><LanguageToggle /><ThemeToggle />{saved ? <span className="settings-save-status" role="status">{t("saved")}</span> : null}<button className="button primary" onClick={() => void save()}>{saved ? <Check size={17} /> : <FloppyDisk size={17} />} {saved ? t("saved") : t("saveSettings")}</button></div></header>
      <div className="settings-grid">
        <section id="model-agent" className="settings-card">
          <div className="card-heading"><div><h2>{t("modelProfiles")}</h2><p>{t("modelProfilesDesc")}</p></div><div className="inline-actions"><button className="icon-button" onClick={addProfile} aria-label={t("addProfile")}><Plus size={17} /></button><button className="icon-button danger-icon" onClick={removeProfile} aria-label={t("removeProfile")}><Trash size={16} /></button></div></div>
          <div className="profile-tabs">{config.profiles.map((item) => <button key={item.id} className={item.id === profile.id ? "active" : ""} onClick={() => setSelected(item.id)}>{item.name || item.model}</button>)}</div>
          <div className="form-grid">
            <Field label={t("profileName")}><input value={profile.name} onChange={(event) => updateProfile({ name: event.target.value })} /></Field>
            <Field label="Provider"><input value={profile.provider} onChange={(event) => updateProfile({ provider: event.target.value })} /></Field>
            <Field label="Model ID"><input value={profile.model} onChange={(event) => updateProfile({ model: event.target.value })} /></Field>
            <Field label="API Key" hint={t("apiKeyHint")}><input type="password" value={profile.apiKey ?? ""} onChange={(event) => updateProfile({ apiKey: event.target.value })} placeholder="••••••••" /></Field>
            <Field label="Base URL"><input value={profile.baseUrl} onChange={(event) => updateProfile({ baseUrl: event.target.value })} /></Field>
            <Field label={t("apiProtocol")}><SelectField value={profile.api} onValueChange={(value) => updateProfile({ api: value as ModelProfile["api"] })} options={API_TYPES.map((value) => ({ value, label: labels[value] }))} /></Field>
            <Field label={t("transportLabel")}><SelectField value={profile.transport} onValueChange={(value) => updateProfile({ transport: value as ModelProfile["transport"] })} options={TRANSPORTS.map((value) => ({ value, label: value.toUpperCase() }))} /></Field>
            <Field label="Thinking level"><SelectField value={profile.thinkingLevel} onValueChange={(value) => updateProfile({ thinkingLevel: value as ModelProfile["thinkingLevel"] })} options={["off", "minimal", "low", "medium", "high", "xhigh"].map((value) => ({ value, label: value }))} /></Field>
            <Field label="Context window"><input type="number" min={1024} step={1024} value={profile.contextWindow} onChange={(event) => updateProfile({ contextWindow: Number(event.target.value) })} /></Field>
            <Field label="Max output tokens"><input type="number" min={256} step={256} value={profile.maxTokens} onChange={(event) => updateProfile({ maxTokens: Number(event.target.value) })} /></Field>
          </div>
        </section>
        <section className="settings-card"><div className="card-heading"><div><h2>{t("childAgent")}</h2><p>{t("childAgentDesc")}</p></div></div><label className="toggle-row"><span><strong>{t("inheritMain")}</strong><small>{t("inheritMainDesc")}</small></span><input type="checkbox" checked={config.childInherit} onChange={(event) => setConfig({ ...config, childInherit: event.target.checked })} /></label><Field label={t("independentProfile")}><SelectField value={config.childProfileId ?? profile.id} onValueChange={(value) => setConfig({ ...config, childProfileId: value })} options={config.profiles.map((item) => ({ value: item.id, label: item.name || item.model }))} /></Field><Field label={t("maxConcurrentSubagents")} hint={t("maxConcurrentSubagentsHint")}><input type="number" min={1} max={8} step={1} value={config.maxConcurrentSubagents} onChange={(event) => setConfig({ ...config, maxConcurrentSubagents: Math.min(8, Math.max(1, Number(event.target.value) || 1)) })} /></Field><Field label={t("subagentAggressiveness")} hint={t("subagentAggressivenessDesc")}><SelectField value={config.subagentAggressiveness} onValueChange={(value) => setConfig({ ...config, subagentAggressiveness: value as SubagentAggressiveness })} options={SUBAGENT_AGGRESSIVENESS.map((value) => ({ value, label: value === "low" ? t("subagentLow") : value === "high" ? t("subagentHigh") : t("subagentDefault") }))} /></Field>{config.subagentAggressiveness === "high" ? <div className="safety-note"><Warning size={18} weight="regular" /><span>{t("subagentHighWarning")}</span></div> : null}<div className="field-hint subagent-new-session-note">{t("subagentNewSessionNote")}</div></section>
        <section className="settings-card"><div className="card-heading"><div><h2>{t("workDir")}</h2><p>{t("workDirDesc")}</p></div></div><Field label={t("currentDir")}><input value={config.cwd} onChange={(event) => setConfig({ ...config, cwd: event.target.value })} /></Field><div className="safety-note"><Warning size={18} weight="regular" /><span>{t("highRiskNote")}</span></div></section>
        <section id="tool-security" className="settings-card"><div className="card-heading"><div><h2>{t("toolSecurity")}</h2><p>{t("securityDesc")}</p></div><Warning size={18} color="var(--muted)" weight="regular" /></div><div className="security-policy-list"><div><strong>{t("allowedByDefault")}</strong><span>{t("readOnlyTools")}</span></div><div><strong>{t("needsApproval")}</strong><span>{t("riskyTools")}</span></div><div><strong>{t("currentMode")}</strong><span>{config.approvalMode === "request" ? t("requestApprovalMode") : config.approvalMode === "auto" ? t("helpApproveMode") : t("fullAccessMode")}{language === "en" ? ". " : "，"}{t("modeInComposer")}</span></div></div></section>
        <section className="settings-card"><div className="card-heading"><div><h2>{t("archived")}</h2><p>{t("archivedDesc")}</p></div><Archive size={18} color="var(--muted)" /></div>{archivedSessions.length ? <div className="archived-session-list">{archivedSessions.map((session) => <div className="archived-session-row" key={session.id}><div className="session-copy"><strong>{session.name || t("newSessionEnglish")}</strong><small>{new Date(session.updatedAt).toLocaleString()}</small></div><button className="icon-button danger-icon" aria-label={`${t("deleteArchived")} ${session.name || t("archived")}`} title={t("deleteArchived")} onClick={() => void deleteArchived(session)}><Trash size={16} /></button></div>)}</div> : <div className="archived-empty">{t("noArchived")}</div>}</section>
      </div>
      {error ? <div className="inline-error">{error}</div> : null}
    </main>
  </div>;
}
