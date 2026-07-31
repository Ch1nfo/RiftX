import {
  AlertTriangle,
  CheckCircle2,
  CirclePlus,
  KeyRound,
  Loader2,
  Pencil,
  Save,
  ServerCog,
  Star,
  Trash2,
  X,
} from "lucide-react";
import { FormEvent, useState } from "react";

import type {
  ModelProfile,
  ModelProfileSummary,
  ModelProviderKind,
  ModelRequestMode,
  UpdateModelProfilePayload,
} from "../api/types";
import { api } from "../api/client";
import { EmptyState } from "../components/EmptyState";
import { ErrorState } from "../components/ErrorState";
import { LoadingState } from "../components/LoadingState";
import { useModelProfileControl, useModelProfiles } from "../hooks/queries";
import { useI18n } from "../i18n";

interface ModelEditorState {
  originalName: string | null;
  name: string;
  provider: ModelProviderKind;
  model: string;
  requestMode: ModelRequestMode;
  baseUrl: string;
  apiKeyEnv: string;
  requiresApiKey: boolean;
  timeoutSeconds: string;
  maxRetries: string;
  apiKey: string;
  clearStoredApiKey: boolean;
  hasStoredApiKey: boolean;
  apiKeyConfigured: boolean;
}

const MAX_MODEL_TIMEOUT_SECONDS = 600;

const newProfile = (): ModelEditorState => ({
  originalName: null,
  name: "",
  provider: "openai_compatible",
  model: "",
  requestMode: "chat_completions",
  baseUrl: "",
  apiKeyEnv: "RIFTX_MODEL_API_KEY",
  requiresApiKey: true,
  timeoutSeconds: "120",
  maxRetries: "2",
  apiKey: "",
  clearStoredApiKey: false,
  hasStoredApiKey: false,
  apiKeyConfigured: false,
});

export function ModelsPage() {
  const { t } = useI18n();
  const profiles = useModelProfiles();
  const [adminToken, setAdminToken] = useState("");
  const control = useModelProfileControl(adminToken);
  const [editor, setEditor] = useState<ModelEditorState | null>(null);
  const [editorError, setEditorError] = useState<string | null>(null);
  const [adminError, setAdminError] = useState<Error | null>(null);
  const [detailPending, setDetailPending] = useState<string | null>(null);
  const [savePending, setSavePending] = useState(false);

  if (profiles.isLoading) return <LoadingState label="Loading model profiles" />;
  if (profiles.error) return <ErrorState error={profiles.error} />;
  if (!profiles.data) return null;

  const mutationError = control.setDefault.error ?? control.remove.error;
  const hasAdminToken = Boolean(adminToken.trim());

  const saveProfile = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!editor) return;
    setEditorError(null);
    const token = adminToken.trim();
    if (!token) {
      setEditorError(t("RIFTX_ADMIN_TOKEN is required"));
      return;
    }
    let profileName: string;
    let payload: UpdateModelProfilePayload;
    try {
      ({ profileName, payload } = editorPayload(editor));
    } catch (error) {
      setEditorError(error instanceof Error ? t(error.message) : t("Invalid model profile"));
      return;
    }

    setSavePending(true);
    setEditor((current) => (current ? { ...current, apiKey: "" } : current));
    try {
      const profile = await api.updateModelProfile(profileName, payload, token);
      setEditor(profileToEditor(profile));
      await profiles.refetch();
    } catch (error) {
      setEditorError(error instanceof Error ? error.message : t("Invalid model profile"));
    } finally {
      setSavePending(false);
    }
  };

  const editProfile = async (profile: ModelProfileSummary) => {
    const token = adminToken.trim();
    if (!token) return;
    setAdminError(null);
    setEditorError(null);
    setDetailPending(profile.name);
    try {
      setEditor(profileToEditor(await api.getModelProfile(profile.name, token)));
    } catch (error) {
      setAdminError(error instanceof Error ? error : new Error(t("Invalid model profile")));
    } finally {
      setDetailPending(null);
    }
  };

  const removeProfile = (profile: ModelProfileSummary) => {
    if (!window.confirm(t("Delete model profile {name}?", { name: profile.name }))) return;
    control.remove.mutate(profile.name, {
      onSuccess: () => {
        if (editor?.originalName === profile.name) setEditor(null);
      },
    });
  };

  return (
    <div className="page-stack">
      <section className="hero-strip compact-hero model-hero">
        <div>
          <span className="kicker">{t("Agent model routing")}</span>
          <h2>{t("Manage model endpoints without exposing credentials.")}</h2>
          <p>
            {t("Profiles define the request protocol, endpoint, model name, timeout, retries, and credential source used by both WebUI and CLI runs.")}
          </p>
        </div>
        <button
          className="primary-button"
          type="button"
          disabled={!hasAdminToken}
          onClick={() => {
            setEditor(newProfile());
            setEditorError(null);
          }}
        >
          <CirclePlus size={17} />
          {t("New profile")}
        </button>
      </section>

      <section className="panel model-admin-auth">
        <div>
          <KeyRound size={18} />
          <span>
            <strong>{t("Remote configuration authorization")}</strong>
            <small>
              {t("Set RIFTX_ADMIN_TOKEN before changing model configuration. Enter it here for this page only; it is held in memory and never written to browser storage.")}
            </small>
          </span>
        </div>
        <label className="field">
          <span>{t("Admin token (session only)")}</span>
          <input
            aria-label={t("Admin token (session only)")}
            type="password"
            value={adminToken}
            autoComplete="new-password"
            spellCheck={false}
            placeholder={t("RIFTX_ADMIN_TOKEN is required")}
            onChange={(event) => setAdminToken(event.target.value)}
          />
        </label>
      </section>

      {adminError ? <ErrorState error={adminError} /> : null}
      {mutationError ? <ErrorState error={mutationError} /> : null}

      {editor ? (
        <ModelEditor
          value={editor}
          error={editorError}
          pending={savePending}
          onChange={setEditor}
          onSubmit={(event) => void saveProfile(event)}
          onClose={() => {
            setEditor(null);
            setEditorError(null);
          }}
        />
      ) : null}

      <section className="panel model-registry-panel">
        <div className="panel-header">
          <div>
            <span className="panel-kicker">models.yaml / secure local secret store</span>
            <h3>{t("Configured model profiles")}</h3>
          </div>
          <div className="model-registry-meta">
            <span className="mono-chip">
              {t("default")} / {profiles.data.default_profile}
            </span>
          </div>
        </div>

        {profiles.data.profiles.length ? (
          <div className="model-profile-grid">
            {profiles.data.profiles.map((profile) => (
              <article className="model-profile-card" key={profile.name}>
                <div className="model-profile-head">
                  <div className="model-profile-icon">
                    <ServerCog size={19} />
                  </div>
                  <div>
                    <div className="model-profile-title">
                      <h4>{profile.name}</h4>
                      {profile.is_default ? <span>{t("default")}</span> : null}
                      {profile.is_effective_default && !profile.is_default ? (
                        <span>{t("effective default")}</span>
                      ) : null}
                    </div>
                    <p>{profile.model}</p>
                  </div>
                </div>

                <dl className="model-profile-details">
                  <div>
                    <dt>{t("Request mode")}</dt>
                    <dd>{profile.request_mode}</dd>
                  </div>
                </dl>

                <div
                  className={`credential-state ${profile.api_key_configured ? "configured" : "missing"}`}
                >
                  {profile.api_key_configured ? (
                    <CheckCircle2 size={15} />
                  ) : (
                    <AlertTriangle size={15} />
                  )}
                  <span>
                    {profile.api_key_configured
                      ? t("Credential configured")
                      : t("API key required")}
                  </span>
                </div>

                <div className="model-profile-actions">
                  <button
                    className="secondary-button compact-button"
                    type="button"
                    disabled={!hasAdminToken || detailPending !== null}
                    onClick={() => void editProfile(profile)}
                  >
                    {detailPending === profile.name ? (
                      <Loader2 className="spin" size={14} />
                    ) : (
                      <Pencil size={14} />
                    )}{" "}
                    {t("Edit")}
                  </button>
                  <button
                    className="secondary-button compact-button"
                    type="button"
                    disabled={
                      profile.is_default ||
                      control.setDefault.isPending ||
                      !hasAdminToken ||
                      !profile.api_key_configured
                    }
                    onClick={() => control.setDefault.mutate(profile.name)}
                  >
                    <Star size={14} /> {t("Set default")}
                  </button>
                  <button
                    className="danger-button compact-button"
                    type="button"
                    disabled={profile.is_default || control.remove.isPending || !hasAdminToken}
                    onClick={() => removeProfile(profile)}
                  >
                    <Trash2 size={14} /> {t("Delete")}
                  </button>
                </div>
              </article>
            ))}
          </div>
        ) : (
          <EmptyState icon={ServerCog} title="No model profiles configured">
            {t("Create a profile before starting a Run.")}
          </EmptyState>
        )}
      </section>
    </div>
  );
}

function ModelEditor({
  value,
  error,
  pending,
  onChange,
  onSubmit,
  onClose,
}: {
  value: ModelEditorState;
  error: string | null;
  pending: boolean;
  onChange: (value: ModelEditorState) => void;
  onSubmit: (event: FormEvent<HTMLFormElement>) => void;
  onClose: () => void;
}) {
  const { t } = useI18n();
  const field = <Key extends keyof ModelEditorState>(key: Key, next: ModelEditorState[Key]) =>
    onChange({ ...value, [key]: next });

  return (
    <form
      className="panel model-editor"
      aria-label={t(value.originalName ? "Edit model profile {name}" : "Create model profile", {
        name: value.name,
      })}
      onSubmit={onSubmit}
    >
      <div className="panel-header">
        <div>
          <span className="panel-kicker">{t("Persist metadata and hot reload workers")}</span>
          <h3>
            {value.originalName
              ? t("Edit model profile {name}", { name: value.originalName })
              : t("Create model profile")}
          </h3>
        </div>
        <button className="secondary-button compact-button" type="button" onClick={onClose}>
          <X size={14} /> {t("Close")}
        </button>
      </div>

      <div className="model-editor-grid">
        <label className="field">
          <span>{t("Profile name")}</span>
          <input
            required
            value={value.name}
            disabled={value.originalName !== null}
            placeholder="primary"
            pattern="[A-Za-z0-9][A-Za-z0-9._-]{0,63}"
            onChange={(event) => field("name", event.target.value)}
          />
          <small>{t("Used by Runs and CLI commands; existing profile names are immutable.")}</small>
        </label>
        <label className="field">
          <span>{t("Provider")}</span>
          <select
            value={value.provider}
            onChange={(event) => field("provider", event.target.value as ModelProviderKind)}
          >
            <option value="openai">openai</option>
            <option value="openai_compatible">openai_compatible</option>
          </select>
        </label>
        <label className="field">
          <span>{t("Request mode")}</span>
          <select
            value={value.requestMode}
            onChange={(event) => field("requestMode", event.target.value as ModelRequestMode)}
          >
            <option value="chat_completions">chat_completions</option>
            <option value="responses">responses</option>
          </select>
        </label>
        <label className="field">
          <span>{t("Model name")}</span>
          <input
            required
            value={value.model}
            placeholder="gpt-5.6"
            onChange={(event) => field("model", event.target.value)}
          />
        </label>
        <label className="field field-wide">
          <span>{t("Base URL")}</span>
          <input
            aria-label={t("Base URL")}
            type="url"
            required={value.provider === "openai_compatible"}
            value={value.baseUrl}
            placeholder="https://api.openai.com/v1"
            onChange={(event) => field("baseUrl", event.target.value)}
          />
          <small>
            {t(
              value.provider === "openai_compatible"
                ? "Required for openai_compatible providers."
                : "Leave blank to use the provider default endpoint.",
            )}
          </small>
        </label>
        <label className="field">
          <span>{t("Timeout seconds")}</span>
          <input
            aria-label={t("Timeout seconds")}
            type="number"
            min="0.1"
            max={MAX_MODEL_TIMEOUT_SECONDS}
            step="0.1"
            required
            value={value.timeoutSeconds}
            onChange={(event) => field("timeoutSeconds", event.target.value)}
          />
        </label>
        <label className="field">
          <span>{t("Maximum retries")}</span>
          <input
            type="number"
            min="0"
            max="10"
            step="1"
            required
            value={value.maxRetries}
            onChange={(event) => field("maxRetries", event.target.value)}
          />
        </label>
        <label className="field">
          <span>{t("API key environment variable")}</span>
          <input
            value={value.apiKeyEnv}
            placeholder="RIFTX_MODEL_API_KEY"
            spellCheck={false}
            onChange={(event) => field("apiKeyEnv", event.target.value)}
          />
          <small>{t("Remote profiles may reference only RIFTX_MODEL_* variables.")}</small>
        </label>
        <label className="field">
          <span>{t("New stored API key")}</span>
          <input
            aria-label={t("New stored API key")}
            type="password"
            value={value.apiKey}
            autoComplete="new-password"
            spellCheck={false}
            placeholder={value.hasStoredApiKey ? t("Leave blank to keep the stored key") : "sk-..."}
            onChange={(event) => {
              onChange({
                ...value,
                apiKey: event.target.value,
                clearStoredApiKey: event.target.value ? false : value.clearStoredApiKey,
              });
            }}
          />
          <small>{t("Keys are write-only and are never returned to or stored by the browser.")}</small>
        </label>

        <div className="model-security-options field-wide">
          <label>
            <input
              type="checkbox"
              checked={value.requiresApiKey}
              onChange={(event) => field("requiresApiKey", event.target.checked)}
            />
            <span>
              <strong>{t("Require an API key")}</strong>
              <small>{t("Disable only for a trusted local endpoint that does not authenticate.")}</small>
            </span>
          </label>
          {value.originalName && value.hasStoredApiKey ? (
            <label>
              <input
                type="checkbox"
                checked={value.clearStoredApiKey}
                disabled={Boolean(value.apiKey)}
                onChange={(event) => field("clearStoredApiKey", event.target.checked)}
              />
              <span>
                <strong>{t("Remove the stored API key")}</strong>
                <small>{t("The environment credential, if configured, is not changed.")}</small>
              </span>
            </label>
          ) : null}
        </div>
      </div>

      <div className={`credential-editor-note ${value.apiKeyConfigured ? "configured" : ""}`}>
        <KeyRound size={16} />
        <span>
          {value.apiKeyConfigured
            ? t("A credential is currently available. Entering a new key replaces only the secure stored copy.")
            : t("No credential is currently available for this profile.")}
        </span>
      </div>
      {error ? <p className="form-error">{error}</p> : null}
      <div className="model-editor-actions">
        <button className="secondary-button" type="button" onClick={onClose}>
          {t("Cancel")}
        </button>
        <button className="primary-button" type="submit" disabled={pending}>
          {pending ? <Loader2 className="spin" size={16} /> : <Save size={16} />}
          {t("Save profile")}
        </button>
      </div>
    </form>
  );
}

function profileToEditor(profile: ModelProfile): ModelEditorState {
  return {
    originalName: profile.name,
    name: profile.name,
    provider: profile.provider,
    model: profile.model,
    requestMode: profile.request_mode,
    baseUrl: profile.base_url ?? "",
    apiKeyEnv: profile.api_key_env ?? "",
    requiresApiKey: profile.requires_api_key,
    timeoutSeconds: String(profile.timeout_seconds),
    maxRetries: String(profile.max_retries),
    apiKey: "",
    clearStoredApiKey: false,
    hasStoredApiKey: profile.has_stored_api_key,
    apiKeyConfigured: profile.api_key_configured,
  };
}

export function editorPayload(editor: ModelEditorState): {
  profileName: string;
  payload: UpdateModelProfilePayload;
} {
  const profileName = editor.name.trim();
  if (!/^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/.test(profileName)) {
    throw new Error("Invalid model profile name");
  }
  const model = editor.model.trim();
  if (!model) throw new Error("Model name is required");
  const timeoutSeconds = Number(editor.timeoutSeconds);
  if (
    !Number.isFinite(timeoutSeconds) ||
    timeoutSeconds <= 0 ||
    timeoutSeconds > MAX_MODEL_TIMEOUT_SECONDS
  ) {
    throw new Error("Timeout must be a finite number no greater than 600 seconds");
  }
  const maxRetries = Number(editor.maxRetries);
  if (!Number.isInteger(maxRetries) || maxRetries < 0 || maxRetries > 10) {
    throw new Error("Maximum retries must be an integer from 0 to 10");
  }
  const apiKeyEnv = editor.apiKeyEnv.trim();
  if (apiKeyEnv && !/^RIFTX_MODEL_[A-Z0-9_]+$/.test(apiKeyEnv)) {
    throw new Error("API key environment variable must start with RIFTX_MODEL_");
  }
  const baseUrl = editor.baseUrl.trim();
  if (/\$\{[A-Za-z_][A-Za-z0-9_]*\}/.test(baseUrl)) {
    throw new Error("Managed Base URL must not contain environment references");
  }
  if (editor.provider === "openai_compatible" && !baseUrl) {
    throw new Error("Base URL is required for openai_compatible providers");
  }
  if (baseUrl) validateBaseUrl(baseUrl);

  return {
    profileName,
    payload: {
      provider: editor.provider,
      model,
      request_mode: editor.requestMode,
      base_url: baseUrl || null,
      api_key_env: apiKeyEnv || null,
      requires_api_key: editor.requiresApiKey,
      timeout_seconds: timeoutSeconds,
      max_retries: maxRetries,
      ...(editor.apiKey.trim() ? { api_key: editor.apiKey.trim() } : {}),
      ...(editor.clearStoredApiKey ? { clear_stored_api_key: true } : {}),
    },
  };
}

function validateBaseUrl(baseUrl: string) {
  let parsed: URL;
  try {
    parsed = new URL(baseUrl);
  } catch {
    throw new Error("Base URL must be an absolute HTTP or HTTPS URL");
  }
  if (!(["http:", "https:"] as string[]).includes(parsed.protocol) || !parsed.hostname) {
    throw new Error("Base URL must be an absolute HTTP or HTTPS URL");
  }
  if (parsed.username || parsed.password) {
    throw new Error("Base URL must not contain user information");
  }
  if (parsed.search || parsed.hash) {
    throw new Error("Base URL must not contain a query string or fragment");
  }
}
