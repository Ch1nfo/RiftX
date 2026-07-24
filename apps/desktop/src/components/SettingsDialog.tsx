import { X } from "lucide-react";
import { useState } from "react";
import type { DesktopBridgeError } from "../models";
import { ModelSettingsView } from "./ModelSettingsView";

interface SettingsDialogProps {
  open: boolean;
  onClose: () => void;
  onError: (error: DesktopBridgeError) => void;
  onRuntimeChanged: (available: boolean) => void;
}

export function SettingsDialog({
  open,
  onClose,
  onError,
  onRuntimeChanged,
}: SettingsDialogProps) {
  const [busy, setBusy] = useState(false);

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
            <h2 id="settings-title">Model access</h2>
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

        <ModelSettingsView
          onBusyChange={setBusy}
          onError={onError}
          onRuntimeChanged={onRuntimeChanged}
        />
      </section>
    </div>
  );
}
