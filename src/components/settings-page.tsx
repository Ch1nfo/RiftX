"use client";

import Link from "next/link";
import { Archive, ArrowLeft, Check, FloppyDisk, Plus, Trash, Warning } from "@phosphor-icons/react";
import { useEffect, useState } from "react";
import { API_TYPES, SUBAGENT_AGGRESSIVENESS, TRANSPORTS, clampConcurrency, type AppConfig, type ModelProfile, type SessionSummary, type SubagentAggressiveness } from "@/lib/types";
import { Field, LanguageToggle, RiftxLogo, SelectField, ThemeToggle } from "./ui";
import { useLanguage } from "@/lib/i18n";

const labels: Record<string, string> = {
  "openai-completions": "OpenAI Chat Completions",
  "openai-responses": "OpenAI Responses",
  "anthropic-messages": "Anthropic Messages",
  "google-generative-ai": "Google Generative AI"
};

export function SettingsPage() {
  const { t } = useLanguage();
  const [config, setConfig] = useState<AppConfig | null>(null);
  const [selected, setSelected] = useState("");
  const [maxConcurrentDraft, setMaxConcurrentDraft] = useState("1");
  const [browserScopeDraft, setBrowserScopeDraft] = useState("");
  const [savingSection, setSavingSection] = useState<string | null>(null);
  const [savedSection, setSavedSection] = useState<string | null>(null);
  const [error, setError] = useState("");
  const [archivedSessions, setArchivedSessions] = useState<SessionSummary[]>([]);
  const [archivedLoading, setArchivedLoading] = useState(false);
  const [archivedLoaded, setArchivedLoaded] = useState(false);
  const [activeSection, setActiveSection] = useState<"model-agent" | "archived">("model-agent");

  useEffect(() => {
    fetch("/api/settings/model-profiles").then(async (response) => {
      const data = await response.json();
      if (!response.ok || !Array.isArray(data.profiles)) {
        setError((data as { error?: string }).error ?? t("settingsLoadFailed"));
        return;
      }
      setConfig(data);
      setSelected(data.profiles?.[0]?.id ?? "");
      setMaxConcurrentDraft(String(clampConcurrency(Number(data.maxConcurrentSubagents) || 1)));
      setBrowserScopeDraft(Array.isArray(data.browserScope) ? data.browserScope.join("\n") : "");
    }).catch(() => setError(t("settingsLoadFailed")));
    return undefined;
  }, []);

  useEffect(() => {
    if (activeSection !== "archived" || archivedLoaded) return;
    const controller = new AbortController();
    setArchivedLoading(true);
    fetch("/api/sessions?archived=true", { signal: controller.signal }).then(async (response) => {
      const data = await response.json() as SessionSummary[] | { error?: string };
      if (!response.ok) throw new Error(!Array.isArray(data) ? data.error : undefined);
      setArchivedSessions(Array.isArray(data) ? data : []);
      setArchivedLoaded(true);
    }).catch((reason: unknown) => {
      if (!controller.signal.aborted) setError(reason instanceof Error && reason.message ? reason.message : t("settingsLoadFailed"));
    }).finally(() => {
      if (!controller.signal.aborted) setArchivedLoading(false);
    });
    return () => controller.abort();
  }, [activeSection, archivedLoaded, t]);

  if (!config) return <div className="settings-loading">{t("loadingSettings")}</div>;

  const profile = config.profiles.find((item) => item.id === selected) ?? config.profiles[0];
  const updateProfile = (patch: Partial<ModelProfile>) => setConfig({ ...config, profiles: config.profiles.map((item) => item.id === profile.id ? { ...item, ...patch } : item) });
  const updateMaxConcurrentDraft = (value: string) => {
    const digits = value.replace(/\D/g, "").slice(0, 2);
    setMaxConcurrentDraft(digits);
    if (digits) setConfig({ ...config, maxConcurrentSubagents: clampConcurrency(Number(digits)) });
  };
  const normalizeMaxConcurrentDraft = () => {
    const normalized = clampConcurrency(Number(maxConcurrentDraft) || 1);
    setMaxConcurrentDraft(String(normalized));
    setConfig({ ...config, maxConcurrentSubagents: normalized });
  };
  const saveSection = async (section: string, patch: Record<string, unknown>, applyResponse?: (data: Partial<AppConfig>) => Partial<AppConfig>) => {
    setSavingSection(section);
    setSavedSection(null);
    setError("");
    try {
      const response = await fetch("/api/settings/model-profiles", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(patch) });
      const data = await response.json() as Partial<AppConfig> & { error?: string };
      if (!response.ok) throw new Error(data.error ?? t("saveFailed"));
      setConfig((current) => current ? { ...current, ...(applyResponse ? applyResponse(data) : patch) } : current);
      setSavedSection(section);
      window.setTimeout(() => setSavedSection((current) => current === section ? null : current), 1800);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : t("saveFailed"));
    } finally {
      setSavingSection(null);
    }
  };
  const sectionSaveButton = (section: string, onSave: () => void) => <button className="button secondary settings-section-save" disabled={savingSection !== null} onClick={onSave}>{savingSection === section ? t("saving") : savedSection === section ? <><Check size={16} />{t("saved")}</> : <><FloppyDisk size={16} />{t("saveSettings")}</>}</button>;
  const addProfile = () => {
    const id = `profile-${Date.now()}`;
    const next = { ...profile, id, name: t("newModelProfile"), apiKey: "" };
    setConfig({ ...config, profiles: [...config.profiles, next] });
    setSelected(id);
  };
  const removeProfile = () => {
    if (config.profiles.length < 2) return;
    const next = config.profiles.filter((item) => item.id !== profile.id);
    setConfig({ ...config, profiles: next, activeProfileId: config.activeProfileId === profile.id ? next[0].id : config.activeProfileId });
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
      <a href="#archived" className={`settings-nav-item ${activeSection === "archived" ? "active" : ""}`} onClick={() => setActiveSection("archived")}>{t("archived")}</a>
    </aside>
    <main className="settings-main">
      <header className="settings-header"><div><span className="eyebrow">{t("workspaceSettings")}</span><h1>{activeSection === "archived" ? t("archived") : t("modelAgent")}</h1><p>{activeSection === "archived" ? t("archivedDesc") : t("modelSettingsDesc")}</p></div><div className="settings-header-actions"><LanguageToggle /><ThemeToggle /></div></header>
      <div className="settings-grid">
        {activeSection === "model-agent" ? <>
        <section id="model-agent" className="settings-card">
          <div className="card-heading"><div><h2>{t("modelProfiles")}</h2><p>{t("modelProfilesDesc")}</p></div><div className="inline-actions"><button className="icon-button" onClick={addProfile} aria-label={t("addProfile")}><Plus size={17} /></button><button className="icon-button danger-icon" onClick={removeProfile} aria-label={t("removeProfile")}><Trash size={16} /></button>{sectionSaveButton("profiles", () => void saveSection("profiles", { profiles: config.profiles, activeProfileId: config.activeProfileId }, (data) => ({ profiles: data.profiles ?? config.profiles, activeProfileId: data.activeProfileId ?? config.activeProfileId })))}</div></div>
          <div className="profile-tabs">{config.profiles.map((item) => <button key={item.id} className={item.id === profile.id ? "active" : ""} onClick={() => setSelected(item.id)}>{item.name || item.model}</button>)}</div>
          <div className="form-grid">
            <Field label={t("profileName")}><input value={profile.name} onChange={(event) => updateProfile({ name: event.target.value })} /></Field>
            <Field label="Provider"><input value={profile.provider} onChange={(event) => updateProfile({ provider: event.target.value })} /></Field>
            <Field label="Model ID"><input value={profile.model} onChange={(event) => updateProfile({ model: event.target.value })} /></Field>
            <Field label="API Key"><input type="password" value={profile.apiKey ?? ""} onChange={(event) => updateProfile({ apiKey: event.target.value })} placeholder={t("apiKeyHint")} /></Field>
            <Field label="Base URL"><input value={profile.baseUrl} onChange={(event) => updateProfile({ baseUrl: event.target.value })} /></Field>
            <Field label={t("apiProtocol")}><SelectField value={profile.api} onValueChange={(value) => updateProfile({ api: value as ModelProfile["api"] })} options={API_TYPES.map((value) => ({ value, label: labels[value] }))} /></Field>
            <Field label={t("transportLabel")}><SelectField value={profile.transport} onValueChange={(value) => updateProfile({ transport: value as ModelProfile["transport"] })} options={TRANSPORTS.map((value) => ({ value, label: value.toUpperCase() }))} /></Field>
            <Field label="Thinking level"><SelectField value={profile.thinkingLevel} onValueChange={(value) => updateProfile({ thinkingLevel: value as ModelProfile["thinkingLevel"] })} options={["off", "minimal", "low", "medium", "high", "xhigh"].map((value) => ({ value, label: value }))} /></Field>
            <Field label="Context window"><input type="number" min={1024} step={1024} value={profile.contextWindow} onChange={(event) => updateProfile({ contextWindow: Number(event.target.value) })} /></Field>
            <Field label="Max output tokens"><input type="number" min={256} step={256} value={profile.maxTokens} onChange={(event) => updateProfile({ maxTokens: Number(event.target.value) })} /></Field>
            <div className="form-grid-full"><label className="toggle-row no-divider"><span><strong>{t("supportsImages")}</strong><small>{t("supportsImagesHint")}</small></span><input type="checkbox" checked={profile.supportsImages === true} onChange={(event) => updateProfile({ supportsImages: event.target.checked })} /></label></div>
          </div>
        </section>
        <section className="settings-card system-prompt-card"><div className="card-heading"><div><h2>{t("systemPrompt")}</h2><p>{t("systemPromptDesc")}</p></div>{sectionSaveButton("systemPrompt", () => void saveSection("systemPrompt", { systemPromptEnabled: config.systemPromptEnabled, systemPrompt: config.systemPrompt }))}</div><label className="toggle-row"><span><strong>{t("customSystemPromptEnabled")}</strong><small>{t("customSystemPromptEnabledDesc")}</small></span><input type="checkbox" checked={config.systemPromptEnabled} onChange={(event) => setConfig({ ...config, systemPromptEnabled: event.target.checked })} /></label>{config.systemPromptEnabled ? <Field label={t("customSystemPrompt")}><textarea className="settings-prompt-input" value={config.systemPrompt} onChange={(event) => setConfig({ ...config, systemPrompt: event.target.value })} placeholder={t("systemPromptPlaceholder")} rows={12} /></Field> : null}</section>
        <section className="settings-card"><div className="card-heading"><div><h2>{t("childAgent")}</h2><p>{t("childAgentDesc")}</p></div>{sectionSaveButton("childAgent", () => void saveSection("childAgent", { childProfileId: config.childProfileId, childInherit: config.childInherit, maxConcurrentSubagents: config.maxConcurrentSubagents, subagentAggressiveness: config.subagentAggressiveness }))}</div><label className="toggle-row"><span><strong>{t("inheritMain")}</strong><small>{t("inheritMainDesc")}</small></span><input type="checkbox" checked={config.childInherit} onChange={(event) => setConfig({ ...config, childInherit: event.target.checked })} /></label>{config.childInherit ? null : <Field label={t("independentProfile")}><SelectField value={config.childProfileId ?? profile.id} onValueChange={(value) => setConfig({ ...config, childProfileId: value })} options={config.profiles.map((item) => ({ value: item.id, label: item.name || item.model }))} /></Field>}<Field label={t("maxConcurrentSubagents")}><input type="text" inputMode="numeric" maxLength={2} value={maxConcurrentDraft} onChange={(event) => updateMaxConcurrentDraft(event.target.value)} onFocus={(event) => { const input = event.currentTarget; const end = input.value.length; window.requestAnimationFrame(() => { if (input.isConnected) input.setSelectionRange(end, end); }); }} onBlur={normalizeMaxConcurrentDraft} placeholder={t("maxConcurrentSubagentsHint")} /></Field><Field label={t("subagentAggressiveness")}><SelectField value={config.subagentAggressiveness} onValueChange={(value) => setConfig({ ...config, subagentAggressiveness: value as SubagentAggressiveness })} options={SUBAGENT_AGGRESSIVENESS.map((value) => ({ value, label: value === "low" ? t("subagentLow") : value === "high" ? t("subagentHigh") : t("subagentDefault") }))} /></Field>{config.subagentAggressiveness === "high" ? <div className="safety-note"><Warning size={18} weight="regular" /><span>{t("subagentHighWarning")}</span></div> : null}</section>
        <section className="settings-card"><div className="card-heading"><div><h2>{t("browserScope")}</h2><p>{t("browserScopeDesc")}</p></div>{sectionSaveButton("browserScope", () => void saveSection("browserScope", { browserScope: browserScopeDraft.split(/\r?\n/).map((line) => line.trim()).filter(Boolean), browserIgnoreTlsErrors: config.browserIgnoreTlsErrors }))}</div><Field label={t("browserScope")}><textarea className="settings-prompt-input" value={browserScopeDraft} onChange={(event) => setBrowserScopeDraft(event.target.value)} placeholder={"10.0.0.0/8\n10.0.181.248:8000\n*.target.com"} rows={5} spellCheck={false} /></Field><label className="toggle-row no-divider"><span><strong>{t("ignoreTlsErrors")}</strong><small>{t("ignoreTlsErrorsDesc")}</small></span><input type="checkbox" checked={config.browserIgnoreTlsErrors !== false} onChange={(event) => setConfig({ ...config, browserIgnoreTlsErrors: event.target.checked })} /></label></section><section className="settings-card"><div className="card-heading"><div><h2>{t("webResearch")}</h2><p>{t("webResearchDesc")}</p></div>{sectionSaveButton("webResearch", () => void saveSection("webResearch", { webSearch: { tavilyApiKey: config.webSearch?.tavilyApiKey ?? "" } }, () => ({})))}</div><Field label={t("webSearchTavilyKey")}><input type="password" value={config.webSearch?.tavilyApiKey ?? ""} onChange={(event) => setConfig({ ...config, webSearch: { tavilyApiKey: event.target.value } })} placeholder={t("webSearchTavilyKeyPlaceholder")} /></Field></section>
        </> : <section id="archived" className="settings-card"><div className="card-heading"><div><h2>{t("archived")}</h2><p>{t("archivedDesc")}</p></div><Archive size={18} color="var(--muted)" /></div>{archivedLoading ? <div className="archived-empty">{t("loading")}</div> : archivedSessions.length ? <div className="archived-session-list">{archivedSessions.map((session) => <div className="archived-session-row" key={session.id}><div className="session-copy"><strong>{session.name || t("newSessionEnglish")}</strong><small>{new Date(session.updatedAt).toLocaleString()}</small></div><button className="icon-button danger-icon" aria-label={`${t("deleteArchived")} ${session.name || t("archived")}`} title={t("deleteArchived")} onClick={() => void deleteArchived(session)}><Trash size={16} /></button></div>)}</div> : <div className="archived-empty">{t("noArchived")}</div>}</section>}
      </div>
      {error ? <div className="inline-error">{error}</div> : null}
    </main>
  </div>;
}
