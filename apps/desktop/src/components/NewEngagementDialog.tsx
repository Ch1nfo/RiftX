import { AlertTriangle, ShieldAlert, X } from "lucide-react";
import { FormEvent, useEffect, useMemo, useState } from "react";
import { bridgeError, llmSettings } from "../bridge";
import { AUTO_MODE_CONFIRMATION } from "../constants";
import type {
  CreateEngagementInput,
  DesktopBridgeError,
  EnvironmentClass,
  ExecutionMode,
  LlmProfileSettings,
} from "../models";

interface NewEngagementDialogProps {
  open: boolean;
  busy: boolean;
  onClose: () => void;
  onCreate: (input: CreateEngagementInput) => Promise<void>;
  onError: (error: DesktopBridgeError) => void;
}

const AUTO_DEFAULT_TTL_SECONDS = 8 * 60 * 60;

const splitLines = (value: string) =>
  value
    .split(/\r?\n|,/)
    .map((item) => item.trim())
    .filter(Boolean);

export function NewEngagementDialog({
  open,
  busy,
  onClose,
  onCreate,
  onError,
}: NewEngagementDialogProps) {
  const [name, setName] = useState("");
  const [objective, setObjective] = useState("");
  const [successCriteria, setSuccessCriteria] = useState("");
  const [entryPoints, setEntryPoints] = useState("");
  const [cidrs, setCidrs] = useState("");
  const [domains, setDomains] = useState("");
  const [capabilities, setCapabilities] = useState("network.discovery");
  const [mode, setMode] = useState<ExecutionMode>("pentest");
  const [environment, setEnvironment] = useState<EnvironmentClass>("lab");
  const [profiles, setProfiles] = useState<LlmProfileSettings[]>([]);
  const [llmProfile, setLlmProfile] = useState("");
  const [profilesLoading, setProfilesLoading] = useState(false);
  const [confirmation, setConfirmation] = useState("");
  const [autoExpiresAt, setAutoExpiresAt] = useState<number | null>(null);

  useEffect(() => {
    if (!open) {
      return;
    }
    setProfilesLoading(true);
    void llmSettings()
      .then((settings) => {
        setProfiles(settings.profiles);
        setLlmProfile(settings.defaultProfile);
      })
      .catch((cause) => onError(bridgeError(cause)))
      .finally(() => setProfilesLoading(false));
  }, [onError, open]);

  const selectMode = (next: ExecutionMode) => {
    setMode(next);
    setConfirmation("");
    if (next === "auto") {
      setEnvironment("lab");
      setAutoExpiresAt(Math.floor(Date.now() / 1000) + AUTO_DEFAULT_TTL_SECONDS);
    } else {
      setAutoExpiresAt(null);
    }
  };

  const autoConfirmed =
    mode !== "auto" || confirmation === AUTO_MODE_CONFIRMATION;
  const expiresLabel = useMemo(() => {
    if (autoExpiresAt === null) {
      return null;
    }
    return new Date(autoExpiresAt * 1000).toLocaleString();
  }, [autoExpiresAt]);

  if (!open) {
    return null;
  }

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (!autoConfirmed) {
      return;
    }
    await onCreate({
      name: name.trim(),
      objective: objective.trim(),
      successCriteria: splitLines(successCriteria),
      entryPoints: splitLines(entryPoints),
      cidrs: splitLines(cidrs),
      domains: splitLines(domains),
      ports: [],
      mode,
      llmProfile,
      environment: mode === "auto" ? "lab" : environment,
      capabilities: splitLines(capabilities),
      identities: [],
      startsAt: null,
      expiresAt: mode === "auto" ? autoExpiresAt : null,
      confirmation: mode === "auto" ? confirmation : null,
    });
  };

  return (
    <div className="dialog-backdrop" role="presentation">
      <form className="new-engagement-dialog" onSubmit={submit}>
        <header>
          <div>
            <span>New task</span>
            <h2>Engagement definition</h2>
          </div>
          <button
            className="icon-button"
            type="button"
            aria-label="Close"
            title="Close"
            onClick={onClose}
            disabled={busy}
          >
            <X size={18} />
          </button>
        </header>

        <div className="dialog-fields">
          <label>
            <span>Name</span>
            <input
              required
              maxLength={160}
              value={name}
              onChange={(event) => setName(event.target.value)}
              placeholder="Authorized assessment"
              autoFocus
            />
          </label>
          <label className="wide-field">
            <span>Objective</span>
            <textarea
              required
              maxLength={4096}
              value={objective}
              onChange={(event) => setObjective(event.target.value)}
              placeholder="Validate whether an attack path reaches the domain controller."
            />
          </label>
          <label>
            <span>Entry points</span>
            <textarea
              required
              value={entryPoints}
              onChange={(event) => setEntryPoints(event.target.value)}
              placeholder="10.10.20.15"
            />
          </label>
          <label>
            <span>Authorized CIDRs</span>
            <textarea
              required
              value={cidrs}
              onChange={(event) => setCidrs(event.target.value)}
              placeholder="10.10.20.0/24"
            />
          </label>
          <label>
            <span>Domains</span>
            <textarea
              value={domains}
              onChange={(event) => setDomains(event.target.value)}
              placeholder="corp.test"
            />
          </label>
          <label>
            <span>Success criteria</span>
            <textarea
              value={successCriteria}
              onChange={(event) => setSuccessCriteria(event.target.value)}
              placeholder="Record a validated attack path"
            />
          </label>
          <label className="wide-field">
            <span>Capabilities</span>
            <input
              required
              value={capabilities}
              onChange={(event) => setCapabilities(event.target.value)}
            />
          </label>
        </div>

        <div className="mode-fields">
          <fieldset>
            <legend>Mode</legend>
            <div className="segmented-control">
              {(
                [
                  { id: "redTeam", label: "RedTeam" },
                  { id: "pentest", label: "Pentest" },
                  { id: "auto", label: "Auto" },
                ] as const
              ).map((option) => (
                <button
                  key={option.id}
                  type="button"
                  className={mode === option.id ? "active" : undefined}
                  onClick={() => selectMode(option.id)}
                >
                  {option.label}
                </button>
              ))}
            </div>
          </fieldset>
          <label>
            <span>Environment</span>
            <select
              value={mode === "auto" ? "lab" : environment}
              onChange={(event) =>
                setEnvironment(event.target.value as EnvironmentClass)
              }
              disabled={mode === "auto"}
            >
              <option value="lab">Lab</option>
              <option value="staging">Staging</option>
              <option value="production">Production</option>
            </select>
          </label>
          <label>
            <span>LLM profile</span>
            <select
              required
              value={llmProfile}
              onChange={(event) => setLlmProfile(event.target.value)}
              disabled={profilesLoading}
            >
              {profiles.map((profile) => (
                <option key={profile.profileName} value={profile.profileName}>
                  {profile.profileName} · {profile.model}
                </option>
              ))}
            </select>
          </label>
        </div>

        {mode === "auto" && (
          <div className="auto-mode-confirmation create-auto-confirmation">
            <div className="mode-warning critical">
              <ShieldAlert size={16} />
              <span>
                Auto is lab / range only. RiftX will ask for fewer approvals
                while running. Pause and Kill Switch stay available. Type the
                confirmation phrase to create this task.
              </span>
            </div>
            <dl>
              <div>
                <dt>Environment</dt>
                <dd>lab</dd>
              </div>
              <div>
                <dt>Authorization expiry</dt>
                <dd>{expiresLabel ?? "Not set"}</dd>
              </div>
            </dl>
            <label>
              <span>Type the confirmation phrase</span>
              <code>{AUTO_MODE_CONFIRMATION}</code>
              <input
                value={confirmation}
                disabled={busy}
                autoComplete="off"
                spellCheck={false}
                onChange={(event) => setConfirmation(event.target.value)}
              />
            </label>
            <div className="auto-create-hint">
              <AlertTriangle size={14} />
              <span>Default authorization window is 8 hours from create time.</span>
            </div>
          </div>
        )}

        <footer>
          <button type="button" className="secondary-button" onClick={onClose}>
            Cancel
          </button>
          <button
            type="submit"
            className="primary-button"
            disabled={
              busy || profilesLoading || !llmProfile || !autoConfirmed
            }
          >
            {busy ? "Creating..." : "Create task"}
          </button>
        </footer>
      </form>
    </div>
  );
}
