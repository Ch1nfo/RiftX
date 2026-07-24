import { AlertTriangle, X } from "lucide-react";
import { FormEvent, useEffect, useState } from "react";
import { bridgeError, llmSettings } from "../bridge";
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
  const [mode, setMode] = useState<ExecutionMode>("native");
  const [environment, setEnvironment] = useState<EnvironmentClass>("lab");
  const [profiles, setProfiles] = useState<LlmProfileSettings[]>([]);
  const [llmProfile, setLlmProfile] = useState("");
  const [profilesLoading, setProfilesLoading] = useState(false);

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

  if (!open) {
    return null;
  }

  const submit = async (event: FormEvent) => {
    event.preventDefault();
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
      environment,
      capabilities: splitLines(capabilities),
      identities: [],
      startsAt: null,
      expiresAt: null,
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
              {(["native", "hardened", "auto"] as ExecutionMode[]).map(
                (value) => (
                  <button
                    key={value}
                    type="button"
                    className={mode === value ? "active" : undefined}
                    onClick={() => setMode(value)}
                  >
                    {value}
                  </button>
                ),
              )}
            </div>
          </fieldset>
          <label>
            <span>Environment</span>
            <select
              value={environment}
              onChange={(event) =>
                setEnvironment(event.target.value as EnvironmentClass)
              }
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
          <div className="auto-warning">
            <AlertTriangle size={17} />
            <span>Auto Mode is restricted to authorized test labs.</span>
          </div>
        )}

        <footer>
          <button type="button" className="secondary-button" onClick={onClose}>
            Cancel
          </button>
          <button
            type="submit"
            className="primary-button"
            disabled={busy || profilesLoading || !llmProfile}
          >
            {busy ? "Creating..." : "Create task"}
          </button>
        </footer>
      </form>
    </div>
  );
}
