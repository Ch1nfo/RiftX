import { AlertTriangle, X } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import {
  bridgeError,
  prepareSettingsReload,
  settingsReloadImpact,
} from "../bridge";
import type {
  DesktopBridgeError,
  SettingsReloadImpact,
} from "../models";
import { SkillsSettingsView, ToolsSettingsView } from "./ExtensionDiagnostics";
import { ModelSettingsView } from "./ModelSettingsView";

interface SettingsDialogProps {
  open: boolean;
  onClose: () => void;
  onError: (error: DesktopBridgeError) => void;
  onRuntimeChanged: (available: boolean) => Promise<void> | void;
  setupRequired?: boolean;
  settingsLocked?: boolean;
}

interface PendingCoordination {
  impact: SettingsReloadImpact;
  resolve: (allowed: boolean) => void;
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
  const [coordinationBusy, setCoordinationBusy] = useState(false);
  const [pendingCoordination, setPendingCoordination] =
    useState<PendingCoordination | null>(null);

  useEffect(() => {
    if (!open) {
      pendingCoordination?.resolve(false);
      setPendingCoordination(null);
      setTab(setupRequired ? "tools" : "model");
      setBusy(false);
      setCoordinationBusy(false);
    }
  }, [open, pendingCoordination, setupRequired]);

  const requestMutation = useCallback(async () => {
    if (pendingCoordination || coordinationBusy) {
      return false;
    }
    try {
      const impact = await settingsReloadImpact();
      if (impact.activeTurns.length === 0) {
        return true;
      }
      return await new Promise<boolean>((resolve) => {
        setPendingCoordination({ impact, resolve });
      });
    } catch (cause) {
      onError(bridgeError(cause));
      return false;
    }
  }, [coordinationBusy, onError, pendingCoordination]);

  const cancelCoordination = () => {
    const pending = pendingCoordination;
    setPendingCoordination(null);
    pending?.resolve(false);
  };

  const confirmCoordination = async () => {
    const pending = pendingCoordination;
    if (!pending || coordinationBusy) {
      return;
    }
    setCoordinationBusy(true);
    try {
      await prepareSettingsReload(
        pending.impact.activeTurns.map((turn) => turn.engagementId),
      );
      setPendingCoordination(null);
      pending.resolve(true);
    } catch (cause) {
      setPendingCoordination(null);
      pending.resolve(false);
      onError(bridgeError(cause));
    } finally {
      setCoordinationBusy(false);
    }
  };

  if (!open) {
    return null;
  }

  const interactionBlocked =
    busy || coordinationBusy || pendingCoordination !== null;

  return (
    <div
      className="dialog-backdrop"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget && !interactionBlocked) {
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
            disabled={interactionBlocked}
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

        {settingsLocked && !pendingCoordination && (
          <div className="settings-lock-warning" role="status">
            <strong>Active execution requires confirmation</strong>
            <span>
              When you save a runtime setting, RiftX will show every affected
              task before pausing active work. Canceling leaves configuration
              unchanged.
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
              disabled={interactionBlocked}
            >
              {TAB_LABELS[value]}
            </button>
          ))}
        </div>

        <fieldset className="settings-content" disabled={interactionBlocked}>
          {tab === "model" && (
            <ModelSettingsView
              onBusyChange={setBusy}
              onError={onError}
              onRuntimeChanged={onRuntimeChanged}
              onBeforeMutation={requestMutation}
            />
          )}
          {tab === "tools" && (
            <ToolsSettingsView
              onError={onError}
              onBeforeMutation={requestMutation}
            />
          )}
          {tab === "skills" && <SkillsSettingsView onError={onError} />}
        </fieldset>

        {pendingCoordination && (
          <section
            className="settings-reload-confirmation"
            role="alertdialog"
            aria-modal="true"
            aria-labelledby="settings-reload-title"
          >
            <div className="settings-reload-heading">
              <AlertTriangle size={18} />
              <div>
                <strong id="settings-reload-title">
                  Pause active work and apply this setting?
                </strong>
                <span>
                  RiftX will pause or interrupt the turns below before writing
                  configuration. Tasks remain paused after daemon restart and
                  do not resume automatically.
                </span>
              </div>
            </div>
            <ul>
              {pendingCoordination.impact.activeTurns.map((turn) => (
                <li key={turn.engagementId}>
                  <strong>{turn.engagementName}</strong>
                  <span>
                    {turn.profileName} · {turn.engagementId}
                  </span>
                </li>
              ))}
            </ul>
            <div className="settings-actions">
              <button
                type="button"
                className="secondary-button"
                onClick={cancelCoordination}
                disabled={coordinationBusy}
              >
                Cancel
              </button>
              <button
                type="button"
                className="danger-button"
                onClick={() => void confirmCoordination()}
                disabled={coordinationBusy}
              >
                {coordinationBusy ? "Pausing active work" : "Pause and apply"}
              </button>
            </div>
          </section>
        )}
      </section>
    </div>
  );
}
