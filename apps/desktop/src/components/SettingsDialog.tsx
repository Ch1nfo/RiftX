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
}: SettingsDialogProps) {
  const [tab, setTab] = useState<SettingsTab>("model");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!open) {
      setTab("model");
      setBusy(false);
    }
  }, [open]);

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
            <span>Settings</span>
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

        {tab === "model" && (
          <ModelSettingsView
            onBusyChange={setBusy}
            onError={onError}
            onRuntimeChanged={onRuntimeChanged}
          />
        )}
        {tab === "tools" && <ToolsSettingsView onError={onError} />}
        {tab === "skills" && <SkillsSettingsView onError={onError} />}
      </section>
    </div>
  );
}
