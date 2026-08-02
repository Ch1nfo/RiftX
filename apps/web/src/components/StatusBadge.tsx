import type { RunStatus, ToolAvailability } from "../api/types";
import { useI18n } from "../i18n";

const labels: Record<string, string> = {
  waiting_approval: "Waiting approval",
  registered_only: "Registered only",
  misconfigured: "Misconfigured",
  created: "Created",
  preparing: "Preparing",
  running: "Running",
  queued: "Queued",
  starting: "Starting",
  paused: "Paused",
  completed: "Completed",
  exited: "Exited",
  failed: "Failed",
  cancelled: "Cancelled",
  hard_timeout: "Hard timeout",
  available: "Available",
  unavailable: "Unavailable",
  disabled: "Disabled",
  online: "Online",
  offline: "Offline",
  lost: "Lost",
  proposed: "proposed",
  awaiting_approval: "awaiting_approval",
  ready: "ready",
  executing: "executing",
  succeeded: "succeeded",
  partial: "partial",
};

const englishLabels: Record<string, string> = {
  waiting_approval: "Waiting approval",
  registered_only: "Registered only",
  misconfigured: "Misconfigured",
};

export function StatusBadge({ status }: { status: RunStatus | ToolAvailability | string }) {
  const { language, t } = useI18n();
  const label = labels[status] ?? status.replaceAll("_", " ");
  return (
    <span className={`status-badge status-${status}`}>
      <span className="status-dot" aria-hidden="true" />
      {language === "en" ? (englishLabels[status] ?? status.replaceAll("_", " ")) : t(label)}
    </span>
  );
}
