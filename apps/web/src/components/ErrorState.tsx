import { AlertTriangle } from "lucide-react";

import { RiftXAPIError } from "../api/client";
import { useI18n } from "../i18n";

export function ErrorState({ error }: { error: Error }) {
  const { t } = useI18n();
  const code = error instanceof RiftXAPIError ? error.code : "client_error";
  const stopDetails =
    error instanceof RiftXAPIError
      ? safetyStopDetails(error.code, error.details)
      : null;
  return (
    <div className="error-state" role="alert">
      <AlertTriangle size={22} />
      <div className="error-state-body">
        <strong>{error.message}</strong>
        <span className="error-code">{code}</span>
        {stopDetails ? (
          <section
            className="stop-failure-details"
            aria-label={t("Safety stop disposition")}
          >
            <h4>{t("Safety stop disposition")}</h4>
            {stopDetails.runId ? (
              <p>{t("Run {run}", { run: stopDetails.runId })}</p>
            ) : null}
            <div className="stop-failure-table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>{t("Resource type")}</th>
                    <th>{t("Resource ID")}</th>
                    <th>{t("Node")}</th>
                    <th>{t("Stop result")}</th>
                    <th>{t("Reason")}</th>
                  </tr>
                </thead>
                <tbody>
                  {stopDetails.rows.map((row) => (
                    <tr key={`${row.resourceType}:${row.resourceId}`}>
                      <td>{t(row.resourceLabel)}</td>
                      <td>
                        <code>{row.resourceId}</code>
                      </td>
                      <td>
                        <code>{row.nodeId ?? t("Unknown node")}</code>
                      </td>
                      <td>
                        {row.confirmed
                          ? row.confirmedStatus
                            ? t("Stopped ({status})", { status: row.confirmedStatus })
                            : t("Stop confirmed")
                          : `${t("Stop unconfirmed")}${
                              row.observedStatus ? ` (${row.observedStatus})` : ""
                            }`}
                      </td>
                      <td>
                        {row.confirmed
                          ? t("Not applicable")
                          : (row.failure ?? t("Reason unavailable"))}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </section>
        ) : null}
      </div>
    </div>
  );
}

type SafetyStopDetails = {
  runId: string | null;
  rows: StopResourceRow[];
};

type StopResourceRow = {
  resourceType: StopResourceType;
  resourceLabel: StopResourceLabel;
  resourceId: string;
  nodeId: string | null;
  observedStatus: string | null;
  confirmedStatus: string | null;
  failure: string | null;
  confirmed: boolean;
};

type StopResourceType = (typeof STOP_RESOURCE_TYPES)[number][0];
type StopResourceLabel = (typeof STOP_RESOURCE_TYPES)[number][1];

const STOP_RESOURCE_TYPES = [
  ["executions", "Execution"],
  ["browser_sessions", "Browser session"],
  ["target_http_requests", "Target HTTP request"],
] as const;

function safetyStopDetails(
  code: string,
  value: Record<string, unknown> | unknown[],
): SafetyStopDetails | null {
  if (code !== "execution_cancel_failed" && code !== "safety_stop_failed") {
    return null;
  }
  if (!isRecord(value)) return null;
  const nestedRows = stopResourceRows(value.stop_resources);
  const rows = nestedRows.some((row) => row.resourceType === "executions")
    ? nestedRows
    : [...legacyExecutionRows(value), ...nestedRows];
  if (!rows.length) return null;
  return {
    runId: typeof value.run_id === "string" ? value.run_id : null,
    rows,
  };
}

function stopResourceRows(value: unknown): StopResourceRow[] {
  if (!isRecord(value)) return [];
  return STOP_RESOURCE_TYPES.flatMap(([resourceType, resourceLabel]) =>
    resourceRows(resourceType, resourceLabel, value[resourceType]),
  );
}

function legacyExecutionRows(value: Record<string, unknown>): StopResourceRow[] {
  return resourceRows("executions", "Execution", {
    attempted_ids: value.execution_ids,
    node_ids: value.execution_nodes,
    observed_statuses: value.execution_statuses,
    confirmed_ids: value.confirmed_execution_ids,
    confirmed_statuses: value.confirmed_statuses,
    failures: value.failed_executions,
  });
}

function resourceRows(
  resourceType: StopResourceType,
  resourceLabel: StopResourceLabel,
  value: unknown,
): StopResourceRow[] {
  if (!isRecord(value)) return [];
  const attemptedIds = uniqueStrings(value.attempted_ids);
  const nodeIds = stringRecord(value.node_ids);
  const observedStatuses = stringRecord(value.observed_statuses);
  const confirmedStatuses = stringRecord(value.confirmed_statuses);
  const failures = stringRecord(value.failures);
  const confirmedIds = new Set(uniqueStrings(value.confirmed_ids));
  const succeeded = value.succeeded === true;
  return attemptedIds.map((resourceId) => {
    const confirmedStatus = confirmedStatuses[resourceId] ?? null;
    const confirmed = succeeded || confirmedIds.has(resourceId) || confirmedStatus !== null;
    return {
      resourceType,
      resourceLabel,
      resourceId,
      nodeId: nodeIds[resourceId] ?? null,
      observedStatus: observedStatuses[resourceId] ?? null,
      confirmedStatus,
      failure: confirmed ? null : (failures[resourceId] ?? null),
      confirmed,
    };
  });
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function uniqueStrings(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return [...new Set(value.filter((item): item is string => typeof item === "string"))];
}

function stringRecord(value: unknown): Record<string, string> {
  if (!isRecord(value)) return {};
  return Object.fromEntries(
    Object.entries(value).filter(
      (entry): entry is [string, string] => typeof entry[1] === "string",
    ),
  );
}
