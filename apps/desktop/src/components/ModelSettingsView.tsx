import {
  AlertCircle,
  Eye,
  EyeOff,
  KeyRound,
  LoaderCircle,
  Trash2,
} from "lucide-react";
import { FormEvent, useCallback, useEffect, useState } from "react";
import {
  bridgeError,
  deleteLlmApiKey,
  llmSettings,
  saveLlmApiKey,
} from "../bridge";
import type { DesktopBridgeError, LlmSettings } from "../models";
import { NotificationControls } from "./NotificationControls";

interface ModelSettingsViewProps {
  onBusyChange: (busy: boolean) => void;
  onError: (error: DesktopBridgeError) => void;
  onRuntimeChanged: (available: boolean) => void;
}

export function ModelSettingsView({
  onBusyChange,
  onError,
  onRuntimeChanged,
}: ModelSettingsViewProps) {
  const [settings, setSettings] = useState<LlmSettings | null>(null);
  const [selectedProfileName, setSelectedProfileName] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [showKey, setShowKey] = useState(false);
  const [confirmRemove, setConfirmRemove] = useState(false);
  const [busy, setBusy] = useState(false);
  const [loadFailed, setLoadFailed] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);

  const updateBusy = useCallback(
    (nextBusy: boolean) => {
      setBusy(nextBusy);
      onBusyChange(nextBusy);
    },
    [onBusyChange],
  );

  useEffect(() => {
    updateBusy(true);
    void llmSettings()
      .then((loaded) => {
        setSettings(loaded);
        setSelectedProfileName(loaded.defaultProfile);
      })
      .catch((cause) => {
        setLoadFailed(true);
        onError(bridgeError(cause));
      })
      .finally(() => updateBusy(false));
  }, [onError, updateBusy]);

  const profile =
    settings?.profiles.find(
      (candidate) => candidate.profileName === selectedProfileName,
    ) ?? null;
  const keyring = profile?.credentialSource === "keyring";

  const save = async (event: FormEvent) => {
    event.preventDefault();
    if (!profile || !apiKey.trim() || busy) {
      return;
    }
    updateBusy(true);
    try {
      const updated = await saveLlmApiKey(profile.profileName, apiKey);
      setSettings(updated);
      setApiKey("");
      setShowKey(false);
      setNotice(
        updated.daemonRestartRequired
          ? "Saved. Restart the externally managed daemon to apply the new key."
          : "Saved. The local runtime is ready.",
      );
      onRuntimeChanged(true);
    } catch (cause) {
      onError(bridgeError(cause));
    } finally {
      updateBusy(false);
    }
  };

  const remove = async () => {
    if (busy || !profile) {
      return;
    }
    updateBusy(true);
    try {
      const updated = await deleteLlmApiKey(profile.profileName);
      setSettings(updated);
      setApiKey("");
      setConfirmRemove(false);
      setNotice(
        updated.daemonRestartRequired
          ? "Removed. Restart the externally managed daemon to clear its active key."
          : "Removed. The local runtime has stopped.",
      );
      onRuntimeChanged(updated.daemonRestartRequired);
    } catch (cause) {
      onError(bridgeError(cause));
    } finally {
      updateBusy(false);
    }
  };

  if (!settings) {
    return (
      <div className="settings-loading">
        {loadFailed ? (
          <AlertCircle size={19} />
        ) : (
          <LoaderCircle className="spin" size={19} />
        )}
        <span>{loadFailed ? "Settings unavailable" : "Loading settings"}</span>
      </div>
    );
  }

  return (
    <form className="settings-body" onSubmit={save}>
      <label className="settings-profile-selector">
        <span>Profile</span>
        <select
          value={selectedProfileName}
          onChange={(event) => {
            setSelectedProfileName(event.target.value);
            setApiKey("");
            setShowKey(false);
            setConfirmRemove(false);
            setNotice(null);
          }}
          disabled={busy}
        >
          {settings.profiles.map((candidate) => (
            <option key={candidate.profileName} value={candidate.profileName}>
              {candidate.profileName}
              {candidate.profileName === settings.defaultProfile
                ? " (default)"
                : ""}
            </option>
          ))}
        </select>
      </label>

      {profile && (
        <>
          <dl className="settings-summary">
            <div>
              <dt>Model</dt>
              <dd>{profile.model}</dd>
            </div>
            <div>
              <dt>Endpoint</dt>
              <dd>{profile.baseUrl}</dd>
            </div>
            <div>
              <dt>Reasoning</dt>
              <dd>{profile.reasoningLevel}</dd>
            </div>
            <div>
              <dt>Context budget</dt>
              <dd>{profile.contextBudget.toLocaleString()}</dd>
            </div>
            <div>
              <dt>Timeout</dt>
              <dd>{profile.timeoutSeconds}s</dd>
            </div>
          </dl>

          <div className="credential-heading">
            <div className="credential-title">
              <KeyRound size={16} />
              <div>
                <strong>API key</strong>
                <span>
                  {profile.credentialSource === "keyring"
                    ? `System keyring · ${profile.credentialName}`
                    : `Environment · ${profile.credentialName}`}
                </span>
              </div>
            </div>
            <span
              className={`credential-status ${
                profile.configured ? "configured" : "missing"
              }`}
            >
              {profile.configured ? "Configured" : "Missing"}
            </span>
          </div>

          {keyring ? (
            <>
              <label className="key-field">
                <span>
                  {profile.configured ? "Replace API key" : "API key"}
                </span>
                <div>
                  <input
                    type={showKey ? "text" : "password"}
                    value={apiKey}
                    onChange={(event) => {
                      setApiKey(event.target.value);
                      setConfirmRemove(false);
                      setNotice(null);
                    }}
                    autoComplete="off"
                    spellCheck={false}
                    disabled={busy}
                  />
                  <button
                    type="button"
                    className="icon-button"
                    aria-label={showKey ? "Hide API key" : "Show API key"}
                    title={showKey ? "Hide API key" : "Show API key"}
                    onClick={() => setShowKey((current) => !current)}
                  >
                    {showKey ? <EyeOff size={16} /> : <Eye size={16} />}
                  </button>
                </div>
              </label>

              <div className="settings-actions">
                {profile.configured &&
                  (confirmRemove ? (
                    <div className="remove-confirmation">
                      <span>Remove stored key?</span>
                      <button
                        type="button"
                        className="secondary-button"
                        onClick={() => setConfirmRemove(false)}
                        disabled={busy}
                      >
                        Cancel
                      </button>
                      <button
                        type="button"
                        className="danger-button"
                        onClick={() => void remove()}
                        disabled={busy}
                      >
                        <Trash2 size={15} />
                        Remove
                      </button>
                    </div>
                  ) : (
                    <button
                      type="button"
                      className="danger-text-button"
                      onClick={() => setConfirmRemove(true)}
                      disabled={busy}
                    >
                      <Trash2 size={15} />
                      Remove
                    </button>
                  ))}
                <button
                  type="submit"
                  className="primary-button"
                  disabled={!apiKey.trim() || busy}
                >
                  {busy && <LoaderCircle className="spin" size={15} />}
                  Save key
                </button>
              </div>
            </>
          ) : (
            <p className="settings-readonly">
              This key is managed outside RiftX Desktop.
            </p>
          )}

          {notice && <p className="settings-notice">{notice}</p>}
        </>
      )}
      <NotificationControls open onError={onError} />
    </form>
  );
}
