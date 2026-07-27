import {
  AlertCircle,
  Eye,
  EyeOff,
  KeyRound,
  LoaderCircle,
  Plus,
  Trash2,
} from "lucide-react";
import { FormEvent, useCallback, useEffect, useState } from "react";
import {
  bridgeError,
  deleteLlmApiKey,
  deleteLlmProfile,
  llmProfiles,
  llmSettings,
  saveLlmApiKey,
  setDefaultLlmProfile,
  testLlmProfile,
  upsertLlmProfile,
} from "../bridge";
import type {
  DesktopBridgeError,
  LlmConnectionTestResult,
  LlmProfileList,
  LlmProfileState,
  LlmSettings,
} from "../models";
import { NotificationControls } from "./NotificationControls";

interface ModelSettingsViewProps {
  onBusyChange: (busy: boolean) => void;
  onError: (error: DesktopBridgeError) => void;
  onRuntimeChanged: (available: boolean) => void;
}

const PROFILE_STATE_LABELS: Record<LlmProfileState, string> = {
  unconfigured: "Unconfigured",
  ready: "Ready",
  invalid: "Invalid",
  unreachable: "Unreachable",
  disabled: "Disabled",
  in_use: "In use",
};

export function ModelSettingsView({
  onBusyChange,
  onError,
  onRuntimeChanged,
}: ModelSettingsViewProps) {
  const [settings, setSettings] = useState<LlmSettings | null>(null);
  const [runtimeProfiles, setRuntimeProfiles] =
    useState<LlmProfileList | null>(null);
  const [selectedProfileName, setSelectedProfileName] = useState("");
  const [model, setModel] = useState("");
  const [baseUrl, setBaseUrl] = useState("");
  const [protocol, setProtocol] = useState<"responses" | "chat_completions">(
    "responses",
  );
  const [apiKey, setApiKey] = useState("");
  const [showKey, setShowKey] = useState(false);
  const [confirmRemoveKey, setConfirmRemoveKey] = useState(false);
  const [confirmDeleteProfile, setConfirmDeleteProfile] = useState(false);
  const [showNewProfile, setShowNewProfile] = useState(false);
  const [newProfileName, setNewProfileName] = useState("");
  const [newModel, setNewModel] = useState("");
  const [newBaseUrl, setNewBaseUrl] = useState("");
  const [newProtocol, setNewProtocol] = useState<
    "responses" | "chat_completions"
  >("responses");
  const [busy, setBusy] = useState(false);
  const [loadFailed, setLoadFailed] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [connectionTest, setConnectionTest] =
    useState<LlmConnectionTestResult | null>(null);

  const updateBusy = useCallback(
    (nextBusy: boolean) => {
      setBusy(nextBusy);
      onBusyChange(nextBusy);
    },
    [onBusyChange],
  );

  const applySettings = useCallback(
    (loaded: LlmSettings, preferredProfile?: string) => {
      setSettings(loaded);
      const nextProfile =
        preferredProfile &&
        loaded.profiles.some(
          (candidate) => candidate.profileName === preferredProfile,
        )
          ? preferredProfile
          : loaded.profiles.some(
                (candidate) => candidate.profileName === selectedProfileName,
              )
            ? selectedProfileName
            : loaded.defaultProfile;
      setSelectedProfileName(nextProfile);
      setConnectionTest(null);
      const profile =
        loaded.profiles.find(
          (candidate) => candidate.profileName === nextProfile,
        ) ?? null;
      setModel(profile?.model ?? "");
      setBaseUrl(profile?.baseUrl ?? "");
      setProtocol(profile?.protocol ?? "responses");
    },
    [selectedProfileName],
  );

  const refreshRuntimeProfiles = useCallback(async () => {
    try {
      setRuntimeProfiles(await llmProfiles());
    } catch {
      setRuntimeProfiles(null);
    }
  }, []);

  useEffect(() => {
    updateBusy(true);
    void refreshRuntimeProfiles();
    void llmSettings()
      .then((loaded) => {
        applySettings(loaded);
      })
      .catch((cause) => {
        setLoadFailed(true);
        onError(bridgeError(cause));
      })
      .finally(() => updateBusy(false));
    // eslint-disable-next-line react-hooks/exhaustive-deps -- load once on mount
  }, [onError, refreshRuntimeProfiles, updateBusy]);

  const profile =
    settings?.profiles.find(
      (candidate) => candidate.profileName === selectedProfileName,
    ) ?? null;
  const runtimeProfile =
    runtimeProfiles?.profiles.find(
      (candidate) => candidate.name === selectedProfileName,
    ) ?? null;
  const keyring = profile?.credentialSource === "keyring";
  const isDefault = profile?.profileName === settings?.defaultProfile;
  const canDeleteProfile = (settings?.profiles.length ?? 0) > 1 && !isDefault;

  const restartNotice = (required: boolean, saved: string, restarted: string) =>
    required ? saved : restarted;

  const saveProfile = async (event: FormEvent) => {
    event.preventDefault();
    if (!profile || !model.trim() || !baseUrl.trim() || busy) {
      return;
    }
    updateBusy(true);
    try {
      const updated = await upsertLlmProfile({
        profileName: profile.profileName,
        model: model.trim(),
        baseUrl: baseUrl.trim(),
        protocol,
      });
      applySettings(updated, profile.profileName);
      await refreshRuntimeProfiles();
      setNotice(
        restartNotice(
          updated.daemonRestartRequired,
          "Profile saved. Restart the externally managed daemon to apply endpoint changes.",
          "Profile saved. The local runtime is ready.",
        ),
      );
      onRuntimeChanged(true);
    } catch (cause) {
      onError(bridgeError(cause));
    } finally {
      updateBusy(false);
    }
  };

  const createProfile = async (event: FormEvent) => {
    event.preventDefault();
    if (
      !newProfileName.trim() ||
      !newModel.trim() ||
      !newBaseUrl.trim() ||
      busy
    ) {
      return;
    }
    updateBusy(true);
    try {
      const updated = await upsertLlmProfile({
        profileName: newProfileName.trim(),
        model: newModel.trim(),
        baseUrl: newBaseUrl.trim(),
        protocol: newProtocol,
        makeDefault: settings?.profiles.length === 0,
      });
      applySettings(updated, newProfileName.trim());
      await refreshRuntimeProfiles();
      setShowNewProfile(false);
      setNewProfileName("");
      setNewModel("");
      setNewBaseUrl("");
      setNewProtocol("responses");
      setNotice(
        restartNotice(
          updated.daemonRestartRequired,
          "Profile created. Save an API key, then restart an externally managed daemon if needed.",
          "Profile created. Save an API key to make it ready.",
        ),
      );
      onRuntimeChanged(true);
    } catch (cause) {
      onError(bridgeError(cause));
    } finally {
      updateBusy(false);
    }
  };

  const makeDefault = async () => {
    if (!profile || busy || isDefault) {
      return;
    }
    updateBusy(true);
    try {
      const updated = await setDefaultLlmProfile(profile.profileName);
      applySettings(updated, profile.profileName);
      await refreshRuntimeProfiles();
      setConfirmDeleteProfile(false);
      setNotice(
        restartNotice(
          updated.daemonRestartRequired,
          "Default profile updated. Restart the externally managed daemon to apply.",
          "Default profile updated. The local runtime is ready.",
        ),
      );
      onRuntimeChanged(true);
    } catch (cause) {
      onError(bridgeError(cause));
    } finally {
      updateBusy(false);
    }
  };

  const toggleProfileEnabled = async () => {
    if (!profile || busy) {
      return;
    }
    updateBusy(true);
    try {
      const updated = await upsertLlmProfile({
        profileName: profile.profileName,
        model: profile.model,
        baseUrl: profile.baseUrl,
        protocol: profile.protocol,
        enabled: !profile.enabled,
      });
      applySettings(updated, profile.profileName);
      await refreshRuntimeProfiles();
      setNotice(profile.enabled ? "Profile disabled." : "Profile enabled.");
      onRuntimeChanged(true);
    } catch (cause) {
      onError(bridgeError(cause));
    } finally {
      updateBusy(false);
    }
  };

  const removeProfile = async () => {
    if (!profile || busy || !canDeleteProfile) {
      return;
    }
    updateBusy(true);
    try {
      const updated = await deleteLlmProfile(profile.profileName);
      applySettings(updated);
      await refreshRuntimeProfiles();
      setConfirmDeleteProfile(false);
      setApiKey("");
      setConnectionTest(null);
      setNotice(
        restartNotice(
          updated.daemonRestartRequired,
          "Profile deleted. Restart the externally managed daemon to apply.",
          "Profile deleted. The local runtime is ready.",
        ),
      );
      onRuntimeChanged(true);
    } catch (cause) {
      onError(bridgeError(cause));
    } finally {
      updateBusy(false);
    }
  };

  const runConnectionTest = async () => {
    if (!profile || busy) {
      return;
    }
    updateBusy(true);
    setConnectionTest(null);
    setNotice(null);
    try {
      const result = await testLlmProfile(profile.profileName);
      setConnectionTest(result);
      await refreshRuntimeProfiles();
      setNotice(
        result.ok
          ? "Connection test passed."
          : "Connection test failed. Review the capability matrix.",
      );
    } catch (cause) {
      onError(bridgeError(cause));
    } finally {
      updateBusy(false);
    }
  };

  const save = async (event: FormEvent) => {
    event.preventDefault();
    if (!profile || !apiKey.trim() || busy) {
      return;
    }
    updateBusy(true);
    try {
      const updated = await saveLlmApiKey(profile.profileName, apiKey);
      applySettings(updated, profile.profileName);
      await refreshRuntimeProfiles();
      setApiKey("");
      setShowKey(false);
      setNotice(
        restartNotice(
          updated.daemonRestartRequired,
          "Saved. Restart the externally managed daemon to apply the new key.",
          "Saved. The local runtime is ready.",
        ),
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
      applySettings(updated, profile.profileName);
      await refreshRuntimeProfiles();
      setApiKey("");
      setConfirmRemoveKey(false);
      setNotice(
        restartNotice(
          updated.daemonRestartRequired,
          "Removed. Restart the externally managed daemon to clear its active key.",
          "Removed. The local runtime has stopped.",
        ),
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

  if (settings.profiles.length === 0) {
    return (
      <div className="settings-body">
        <div className="extension-empty">
          <KeyRound size={18} />
          <span>No LLM profiles configured. Create one to connect a model.</span>
        </div>
        <form className="settings-new-profile" onSubmit={createProfile}>
          <label>
            <span>Profile name</span>
            <input
              value={newProfileName}
              onChange={(event) => setNewProfileName(event.target.value)}
              disabled={busy}
              spellCheck={false}
            />
          </label>
          <label>
            <span>Model</span>
            <input
              value={newModel}
              onChange={(event) => setNewModel(event.target.value)}
              disabled={busy}
              spellCheck={false}
            />
          </label>
          <label>
            <span>Endpoint</span>
            <input
              value={newBaseUrl}
              onChange={(event) => setNewBaseUrl(event.target.value)}
              disabled={busy}
              spellCheck={false}
              placeholder="https://api.example.com/v1"
            />
          </label>
          <label>
            <span>Protocol</span>
            <select
              value={newProtocol}
              onChange={(event) =>
                setNewProtocol(
                  event.target.value as "responses" | "chat_completions",
                )
              }
              disabled={busy}
            >
              <option value="responses">Responses</option>
              <option value="chat_completions">Chat Completions</option>
            </select>
          </label>
          <button
            type="submit"
            className="primary-button"
            disabled={
              busy ||
              !newProfileName.trim() ||
              !newModel.trim() ||
              !newBaseUrl.trim()
            }
          >
            {busy && <LoaderCircle className="spin" size={15} />}
            Create profile
          </button>
        </form>
        <NotificationControls open onError={onError} />
      </div>
    );
  }

  return (
    <div className="settings-body">
      <label className="settings-profile-selector">
        <span>Profile</span>
        <select
          value={selectedProfileName}
          onChange={(event) => {
            const next = event.target.value;
            setSelectedProfileName(next);
            const nextProfile =
              settings.profiles.find(
                (candidate) => candidate.profileName === next,
              ) ?? null;
            setModel(nextProfile?.model ?? "");
            setBaseUrl(nextProfile?.baseUrl ?? "");
            setProtocol(nextProfile?.protocol ?? "responses");
            setApiKey("");
            setShowKey(false);
            setConfirmRemoveKey(false);
            setConfirmDeleteProfile(false);
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
          <form className="settings-profile-fields" onSubmit={saveProfile}>
            <label>
              <span>Model</span>
              <input
                value={model}
                onChange={(event) => {
                  setModel(event.target.value);
                  setNotice(null);
                }}
                disabled={busy}
                spellCheck={false}
              />
            </label>
            <label>
              <span>Endpoint</span>
              <input
                value={baseUrl}
                onChange={(event) => {
                  setBaseUrl(event.target.value);
                  setNotice(null);
                }}
                disabled={busy}
                spellCheck={false}
              />
            </label>
            <label>
              <span>Protocol</span>
              <select
                value={protocol}
                onChange={(event) => {
                  setProtocol(
                    event.target.value as "responses" | "chat_completions",
                  );
                  setNotice(null);
                }}
                disabled={busy}
              >
                <option value="responses">Responses</option>
                <option value="chat_completions">Chat Completions</option>
              </select>
            </label>
            <dl className="settings-summary compact">
              <div>
                <dt>State</dt>
                <dd>
                  {runtimeProfile
                    ? PROFILE_STATE_LABELS[runtimeProfile.state]
                    : "Unavailable"}
                </dd>
              </div>
              <div>
                <dt>Runtime</dt>
                <dd>
                  {runtimeProfile?.runtimeReady
                    ? "Initialized"
                    : "Lazy / not initialized"}
                </dd>
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
            {runtimeProfile && (
              <p className="settings-profile-state-detail">
                {runtimeProfile.stateDetail}
              </p>
            )}
            <div className="settings-actions">
              <button
                type="button"
                className="secondary-button"
                onClick={() => void runConnectionTest()}
                disabled={busy || !profile.configured || !profile.enabled}
              >
                Test connection
              </button>
              <button
                type="button"
                className="secondary-button"
                onClick={() => void toggleProfileEnabled()}
                disabled={busy || isDefault}
              >
                {profile.enabled ? "Disable profile" : "Enable profile"}
              </button>
              {!isDefault && profile.enabled && (
                <button
                  type="button"
                  className="secondary-button"
                  onClick={() => void makeDefault()}
                  disabled={busy}
                >
                  Set as default
                </button>
              )}
              {canDeleteProfile &&
                (confirmDeleteProfile ? (
                  <div className="remove-confirmation">
                    <span>Delete profile?</span>
                    <button
                      type="button"
                      className="secondary-button"
                      onClick={() => setConfirmDeleteProfile(false)}
                      disabled={busy}
                    >
                      Cancel
                    </button>
                    <button
                      type="button"
                      className="danger-button"
                      onClick={() => void removeProfile()}
                      disabled={busy}
                    >
                      <Trash2 size={15} />
                      Delete
                    </button>
                  </div>
                ) : (
                  <button
                    type="button"
                    className="danger-text-button"
                    onClick={() => setConfirmDeleteProfile(true)}
                    disabled={busy}
                  >
                    <Trash2 size={15} />
                    Delete profile
                  </button>
                ))}
              <button
                type="submit"
                className="primary-button"
                disabled={
                  busy ||
                  !model.trim() ||
                  !baseUrl.trim() ||
                  (model === profile.model &&
                    baseUrl === profile.baseUrl &&
                    protocol === profile.protocol)
                }
              >
                {busy && <LoaderCircle className="spin" size={15} />}
                Save profile
              </button>
            </div>
          </form>

          {connectionTest && (
            <div
              className={`settings-connection-test ${connectionTest.ok ? "ok" : "failed"}`}
            >
              <strong>
                Connection test{" "}
                {connectionTest.ok ? "passed" : "failed"}
              </strong>
              <ul>
                <li>
                  config: {connectionTest.capabilities.config.status} —{" "}
                  {connectionTest.capabilities.config.detail}
                </li>
                <li>
                  stream text: {connectionTest.capabilities.streamText.status} —{" "}
                  {connectionTest.capabilities.streamText.detail}
                </li>
                <li>
                  function tools:{" "}
                  {connectionTest.capabilities.functionTools.status} —{" "}
                  {connectionTest.capabilities.functionTools.detail}
                </li>
              </ul>
            </div>
          )}

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
            <form onSubmit={save}>
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
                      setConfirmRemoveKey(false);
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
                  (confirmRemoveKey ? (
                    <div className="remove-confirmation">
                      <span>Remove stored key?</span>
                      <button
                        type="button"
                        className="secondary-button"
                        onClick={() => setConfirmRemoveKey(false)}
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
                      onClick={() => setConfirmRemoveKey(true)}
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
            </form>
          ) : (
            <p className="settings-readonly">
              This key is managed outside RiftX Desktop.
            </p>
          )}

          {notice && <p className="settings-notice">{notice}</p>}
        </>
      )}

      <div className="settings-new-profile-toggle">
        {showNewProfile ? (
          <form className="settings-new-profile" onSubmit={createProfile}>
            <strong>New profile</strong>
            <label>
              <span>Name</span>
              <input
                value={newProfileName}
                onChange={(event) => setNewProfileName(event.target.value)}
                disabled={busy}
                spellCheck={false}
              />
            </label>
            <label>
              <span>Model</span>
              <input
                value={newModel}
                onChange={(event) => setNewModel(event.target.value)}
                disabled={busy}
                spellCheck={false}
              />
            </label>
            <label>
              <span>Endpoint</span>
              <input
                value={newBaseUrl}
                onChange={(event) => setNewBaseUrl(event.target.value)}
                disabled={busy}
                spellCheck={false}
                placeholder="https://api.example.com/v1"
              />
            </label>
            <label>
              <span>Protocol</span>
              <select
                value={newProtocol}
                onChange={(event) =>
                  setNewProtocol(
                    event.target.value as "responses" | "chat_completions",
                  )
                }
                disabled={busy}
              >
                <option value="responses">Responses</option>
                <option value="chat_completions">Chat Completions</option>
              </select>
            </label>
            <div className="settings-actions">
              <button
                type="button"
                className="secondary-button"
                onClick={() => setShowNewProfile(false)}
                disabled={busy}
              >
                Cancel
              </button>
              <button
                type="submit"
                className="primary-button"
                disabled={
                  busy ||
                  !newProfileName.trim() ||
                  !newModel.trim() ||
                  !newBaseUrl.trim()
                }
              >
                {busy && <LoaderCircle className="spin" size={15} />}
                Create
              </button>
            </div>
          </form>
        ) : (
          <button
            type="button"
            className="secondary-button"
            onClick={() => setShowNewProfile(true)}
            disabled={busy}
          >
            <Plus size={15} />
            New profile
          </button>
        )}
      </div>

      <NotificationControls open onError={onError} />
    </div>
  );
}
