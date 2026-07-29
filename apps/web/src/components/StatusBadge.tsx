import type { RunStatus, ToolAvailability } from "../api/types";

const labels: Record<string, string> = {
  waiting_approval: "Waiting approval",
  registered_only: "Registered only",
  misconfigured: "Misconfigured",
};

export function StatusBadge({ status }: { status: RunStatus | ToolAvailability | string }) {
  return (
    <span className={`status-badge status-${status}`}>
      <span className="status-dot" aria-hidden="true" />
      {labels[status] ?? status.replaceAll("_", " ")}
    </span>
  );
}
