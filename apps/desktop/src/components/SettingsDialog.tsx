import { X } from "lucide-react";
import { useEffect, useState } from "react";
import type { DesktopBridgeError } from "../models";
import { SkillsSettingsView, ToolsSettingsView } from "./ExtensionDiagnostics";
import { ModelSettingsView } from "./ModelSettingsView";

interface SettingsDialogProps {
  open: boolean;
  onClose: () => void;
  onError: (error: DesktopBridgeError) => void;
  onRuntimeChanged: (available: boolean) => void;
  setupRequired?: boolean;
  settingsLocked?: boolean;
}

type SettingsTab = "model" | "tools" | "skills";

const TAB_TITLES: Record<SettingsTab, string> = {
  model: "Model",
  tools: "Tools",
  skills: "Skills",
};

const TAB_LABELS: Record<SettingsTab, string> = {
  model: "Model",
  tools: "Tools",
  skills: "Skills",
};

export function SettingsDialog({
  open,
  onClose,
  onError,
  onRuntimeChanged,
  setupRequired = false,
  settingsLocked = false,
}: SettingsDialogProps) {
  const [tab, setTab] = useState<SettingsTab>(
    setupRequired ? "tools" : "model",
  );
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!open) {
      setTab(setupRequired ? "tools" : "model");
      setBusy(false);
    }
  }, [open, setupRequired]);

  if (!open) {
    return null;
  }

  return (
    <div
      className="dialog-backdrop"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget && !busy) {
          onClose();
        }
      }}
    >
      <section
        className="settings-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="settings-title"
      >
        <header className="dialog-heading">
          <div>
            <span>{setupRequired ? "First-time setup" : "Settings"}</span>
            <h2 id="settings-title">{TAB_TITLES[tab]}</h2>
          </div>
          <button
            type="button"
            className="icon-button"
            aria-label="Close settings"
            title="Close"
            onClick={onClose}
            disabled={busy}
          >
            <X size={17} />
          </button>
        </header>

        {setupRequired && (
          <div className="settings-onboarding" role="status">
            <strong>Complete setup before creating a task</strong>
            <span>
              1. Confirm Tools directories and run Doctor. 2. Configure the
              model Profile and API key. 3. Pass the automatic text-stream and
              function-tool connection checks.
            </span>
          </div>
        )}

        {settingsLocked && (
          <div className="settings-lock-warning" role="alert">
            <strong>Settings changes are locked during active execution</strong>
            <span>
              Pause or interrupt the active turn before changing model, Tools,
              or Skills settings. No configuration has been written.
            </span>
          </div>
        )}

        <div className="settings-tabs" role="tablist" aria-label="Settings">
          {(["model", "tools", "skills"] as SettingsTab[]).map((value) => (
            <button
              key={value}
              type="button"
              role="tab"
              aria-selected={tab === value}
              className={tab === value ? "active" : undefined}
              onClick={() => setTab(value)}
              disabled={busy}
            >
              {TAB_LABELS[value]}
            </button>
          ))}
        </div>

        <fieldset className="settings-content" disabled={settingsLocked}>
          {tab === "model" && (
            <ModelSettingsView
              onBusyChange={setBusy}
              onError={onError}
              onRuntimeChanged={onRuntimeChanged}
            />
          )}
          {tab === "tools" && <ToolsSettingsView onError={onError} />}
          {tab === "skills" && <SkillsSettingsView onError={onError} />}
        </fieldset>
      </section>
    </div>
  );
}
