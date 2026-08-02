import {
  AlertTriangle,
  Archive,
  CheckCircle2,
  ChevronRight,
  Download,
  FileSearch,
  GitBranch,
  Loader2,
  Server,
  ShieldCheck,
  Wrench,
  X,
} from "lucide-react";
import {
  type KeyboardEvent,
  type ReactNode,
  useEffect,
  useRef,
  useState,
} from "react";

import { api } from "../api/client";
import type {
  RunAction,
  RunActionListAttempt,
  RunActionListItem,
} from "../api/types";
import { EmptyState } from "../components/EmptyState";
import { ErrorState } from "../components/ErrorState";
import { LoadingState } from "../components/LoadingState";
import { StatusBadge } from "../components/StatusBadge";
import { useI18n } from "../i18n";

const GRAPH_NODE_ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._:@+~-]{0,511}$/;
const GRAPH_ACTION_COMPONENT_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._@+~-]{0,127}$/;

interface ActionTimelineProps {
  items: RunActionListItem[];
  loading: boolean;
  error: Error | null;
  paginationError?: Error | null;
  selectedActionId: string | null;
  hasMore: boolean;
  loadingMore: boolean;
  onLoadMore: () => void;
  onSelect: (actionId: string, trigger: HTMLButtonElement) => void;
}

export function ActionTimeline({
  items,
  loading,
  error,
  paginationError = null,
  selectedActionId,
  hasMore,
  loadingMore,
  onLoadMore,
  onSelect,
}: ActionTimelineProps) {
  const { language, t } = useI18n();
  if (loading && !items.length) return <LoadingState label="Loading Actions" />;
  if (error) return <ErrorState error={error} />;
  if (!items.length) {
    return (
      <EmptyState icon={Wrench} title="No Actions yet">
        {t("Run Actions appear after the Agent proposes a durable tool intent.")}
      </EmptyState>
    );
  }

  function moveFocus(
    event: KeyboardEvent<HTMLButtonElement>,
    index: number,
  ) {
    const triggers = event.currentTarget
      .closest("ol")
      ?.querySelectorAll<HTMLButtonElement>("[data-action-trigger]");
    if (!triggers?.length) return;
    let nextIndex: number | null = null;
    if (event.key === "ArrowDown" || event.key === "ArrowRight") {
      nextIndex = (index + 1) % triggers.length;
    } else if (event.key === "ArrowUp" || event.key === "ArrowLeft") {
      nextIndex = (index - 1 + triggers.length) % triggers.length;
    } else if (event.key === "Home") {
      nextIndex = 0;
    } else if (event.key === "End") {
      nextIndex = triggers.length - 1;
    }
    if (nextIndex === null) return;
    event.preventDefault();
    triggers[nextIndex]?.focus();
  }

  return (
    <section className="action-timeline" aria-labelledby="run-actions-heading">
      <div className="action-timeline-heading">
        <div>
          <span className="panel-kicker">{t("Auditable work")}</span>
          <h3 id="run-actions-heading">{t("Run Actions")}</h3>
        </div>
        <span className="action-loaded-count">
          {hasMore
            ? t("{count} actions loaded; more are available.", { count: items.length })
            : t("{count} actions loaded.", { count: items.length })}
        </span>
      </div>

      <ol className="action-list" aria-label={t("Run Actions")}>
        {items.map((action, index) => {
          const selected = selectedActionId === action.action_id;
          const attempt = displayedAttempt(action);
          const stopConfirmation = action.current_execution_id
            ? action.current_stop_confirmation
            : action.latest_stop_confirmation;
          const truncated = actionTruncationLabels(action, t);
          const partial = action.correlation_quality !== "exact" ||
            action.lifecycle === "partial" ||
            action.partial_reasons.length > 0;
          const tool = action.tool_id ?? action.skill_id ?? t("Unknown tool");
          return (
            <li key={`${action.run_id}:${action.action_id}`}>
              <article
                className={`action-card${selected ? " selected" : ""}`}
                aria-labelledby={`action-title-${safeDomId(action.action_id)}`}
              >
                <button
                  className="action-card-trigger"
                  type="button"
                  data-action-trigger
                  aria-expanded={selected}
                  aria-controls="action-context-inspector"
                  aria-label={t("Inspect action {tool}", { tool })}
                  onClick={(event) => onSelect(action.action_id, event.currentTarget)}
                  onKeyDown={(event) => moveFocus(event, index)}
                >
                  <span className="action-tool-icon" aria-hidden="true">
                    <Wrench size={17} />
                  </span>
                  <span className="action-title-copy">
                    <span className="action-identity">
                      {t("Session {session} · Cycle {cycle} · Step {step}", {
                        session: action.session_id,
                        cycle: action.cycle_id,
                        step: action.step_id,
                      })}
                    </span>
                    <strong id={`action-title-${safeDomId(action.action_id)}`}>{tool}</strong>
                    <small>{action.target_summary ?? t("No target summary")}</small>
                  </span>
                  <StatusBadge status={action.lifecycle} />
                  <ChevronRight size={17} aria-hidden="true" />
                </button>

                <div className="action-card-body">
                  <div className="action-answer action-why">
                    <span>{t("Why")}</span>
                    <p>{action.reason || t("No public reason supplied.")}</p>
                  </div>
                  <dl className="action-card-facts">
                    <div>
                      <dt>{t("Approval")}</dt>
                      <dd>{action.approval_status ? t(action.approval_status) : t("No approval record")}</dd>
                    </div>
                    <div>
                      <dt>{t("Result")}</dt>
                      <dd>{t(action.lifecycle)}</dd>
                    </div>
                    <div>
                      <dt>{t("Stop confirmation")}</dt>
                      <dd className={stopConfirmation === "unconfirmed" ? "stop-unconfirmed" : ""}>
                        {t(stopConfirmationLabel(stopConfirmation))}
                      </dd>
                    </div>
                    <div>
                      <dt>{t("Runner")}</dt>
                      <dd>
                        {attempt?.node_id ? (
                          <><Server size={13} /> {attempt.node_id}</>
                        ) : (
                          <><AlertTriangle size={13} /> {t("Runner unknown")}</>
                        )}
                      </dd>
                    </div>
                    <div>
                      <dt>{t("Started")}</dt>
                      <dd>
                        {attempt?.started_at
                          ? formatActionTime(attempt.started_at, language, t)
                          : t("Not started")}
                      </dd>
                    </div>
                    <div>
                      <dt>{t("Duration")}</dt>
                      <dd>{formatActionDuration(attempt, t)}</dd>
                    </div>
                    <div>
                      <dt>{t("Evidence")}</dt>
                      <dd>
                        {t("{count} findings", { count: action.finding_count })} · {" "}
                        {t("{count} artifacts", { count: action.artifact_count })}
                      </dd>
                    </div>
                  </dl>

                  {attempt?.exit_code !== null && attempt?.exit_code !== undefined ? (
                    <span className="action-exit-code">
                      {t("Exit code {code}", { code: attempt.exit_code })}
                    </span>
                  ) : null}
                  <div className="action-flags" aria-label={t("Action data quality")}>
                    {partial ? (
                      <span className="action-flag partial">
                        <AlertTriangle size={12} /> {t("Partial action")}
                      </span>
                    ) : null}
                    {truncated.map((label) => (
                      <span className="action-flag truncated" key={label}>
                        <Archive size={12} /> {label}
                      </span>
                    ))}
                    {action.output_available ? (
                      <span className="action-flag">
                        <Archive size={12} /> {t("Output available through Artifact")}
                      </span>
                    ) : null}
                    {stopConfirmation === "unconfirmed" ? (
                      <span className="action-flag partial">
                        <AlertTriangle size={12} /> {t("Stop unconfirmed")}
                      </span>
                    ) : null}
                  </div>
                </div>
              </article>
            </li>
          );
        })}
      </ol>

      {paginationError ? (
        <div className="action-pagination-error">
          <ErrorState error={paginationError} />
        </div>
      ) : null}

      {hasMore ? (
        <button
          className="secondary-button action-load-more"
          type="button"
          disabled={loadingMore}
          onClick={onLoadMore}
        >
          {loadingMore ? <Loader2 className="spin" size={15} /> : <Archive size={15} />}
          {t("Load more actions")}
        </button>
      ) : null}
    </section>
  );
}

interface ActionInspectorProps {
  actionId: string;
  action: RunAction | undefined;
  loading: boolean;
  error: Error | null;
  onClose: () => void;
  onOpenGraph: (nodeId: string) => void;
  focusOnOpen?: boolean;
}

export function ActionInspector({
  actionId,
  action,
  loading,
  error,
  onClose,
  onOpenGraph,
  focusOnOpen = true,
}: ActionInspectorProps) {
  const { language, t } = useI18n();
  const closeRef = useRef<HTMLButtonElement>(null);
  const activeActionIdRef = useRef(actionId);
  const downloadOperationRef = useRef(0);
  const [downloadState, setDownloadState] = useState<{
    actionId: string;
    error: Error | null;
    status: string;
  }>({ actionId, error: null, status: "" });
  activeActionIdRef.current = actionId;
  const candidateAction = action?.action_id === actionId ? action : undefined;
  const displayedAction = !loading && !error ? candidateAction : undefined;
  const graphNodeId = displayedAction
    ? explicitActionGraphNodeId(displayedAction)
    : null;

  useEffect(() => {
    downloadOperationRef.current += 1;
    setDownloadState({ actionId, error: null, status: "" });
    if (focusOnOpen) closeRef.current?.focus();
    return () => {
      downloadOperationRef.current += 1;
    };
  }, [actionId, focusOnOpen]);

  async function downloadArtifact(artifactId: string) {
    const operation = downloadOperationRef.current + 1;
    downloadOperationRef.current = operation;
    const selection = actionId;
    setDownloadState({
      actionId: selection,
      error: null,
      status: t("Downloading Artifact {id}", { id: artifactId }),
    });
    try {
      await api.downloadAuthenticatedUrl(
        `/api/v1/artifacts/${encodeURIComponent(artifactId)}/content`,
        `artifact-${artifactId}`,
      );
      if (
        activeActionIdRef.current === selection &&
        downloadOperationRef.current === operation
      ) {
        setDownloadState({
          actionId: selection,
          error: null,
          status: t("Artifact {id} download started", { id: artifactId }),
        });
      }
    } catch (caught) {
      if (
        activeActionIdRef.current === selection &&
        downloadOperationRef.current === operation
      ) {
        setDownloadState({
          actionId: selection,
          error: caught instanceof Error ? caught : new Error(t("Download failed")),
          status: "",
        });
      }
    }
  }

  return (
    <section
      id="action-context-inspector"
      className="panel action-inspector"
      role="region"
      aria-label={t("Context Inspector")}
      onKeyDown={(event) => {
        if (event.key === "Escape") {
          event.preventDefault();
          onClose();
        }
      }}
    >
      <div className="action-inspector-header">
        <div>
          <span className="panel-kicker">{t("Context Inspector")}</span>
          <h3>{displayedAction?.tool_id ?? displayedAction?.skill_id ?? t("Run Action")}</h3>
          <code>{actionId}</code>
        </div>
        <button
          ref={closeRef}
          className="icon-button"
          type="button"
          aria-label={t("Close Context Inspector")}
          onClick={onClose}
        >
          <X size={17} />
        </button>
      </div>

      {loading ? <LoadingState label="Loading Action details" /> : null}
      {error ? <ErrorState error={error} /> : null}
      {!loading && !error && !displayedAction ? (
        <EmptyState icon={FileSearch} title="Action detail unavailable">
          {t("The selected Action could not be resolved in this Run.")}
        </EmptyState>
      ) : null}

      {displayedAction ? (
        <div className="action-inspector-body">
          <InspectorSection title={t("Why")} icon={<FileSearch size={15} />}>
            <p>{displayedAction.reason || t("No public reason supplied.")}</p>
            <IdentityPath action={displayedAction} />
          </InspectorSection>

          <InspectorSection title={t("What happened")} icon={<Wrench size={15} />}>
            <dl className="inspector-facts">
              <div><dt>{t("Tool")}</dt><dd>{displayedAction.tool_id ?? displayedAction.skill_id ?? t("Unknown tool")}</dd></div>
              <div><dt>{t("Target")}</dt><dd>{displayedAction.target_summary ?? t("No target summary")}</dd></div>
              <div><dt>{t("Created")}</dt><dd>{formatActionTime(displayedAction.created_at, language, t)}</dd></div>
              <div><dt>{t("Updated")}</dt><dd>{formatActionTime(displayedAction.updated_at, language, t)}</dd></div>
            </dl>
            <div className="inspector-subsection">
              <strong>{t("Redacted arguments")}</strong>
              <pre>{JSON.stringify(displayedAction.arguments_summary, null, 2)}</pre>
            </div>
          </InspectorSection>

          <InspectorSection title={t("Approval")} icon={<ShieldCheck size={15} />}>
            {displayedAction.approval ? (
              <dl className="inspector-facts">
                <div><dt>{t("Status")}</dt><dd>{displayedAction.approval.status ? t(displayedAction.approval.status) : t("Unknown")}</dd></div>
                <div><dt>{t("Decided by")}</dt><dd>{displayedAction.approval.actor ?? t("Unknown")}</dd></div>
                <div><dt>{t("Decision time")}</dt><dd>{formatActionTime(displayedAction.approval.decided_at, language, t)}</dd></div>
                <div><dt>{t("Feedback")}</dt><dd>{displayedAction.approval.feedback_summary ?? t("None")}</dd></div>
              </dl>
            ) : <p>{t("No approval record")}</p>}
          </InspectorSection>

          <InspectorSection title={t("Execution attempts")} icon={<Server size={15} />}>
            {displayedAction.executions.length ? (
              <ol className="inspector-attempts">
                {displayedAction.executions.map((execution) => (
                  <li key={execution.execution_id}>
                    <div>
                      <strong>{execution.node_id || t("Runner unknown")}</strong>
                      <StatusBadge status={execution.status ?? "unknown"} />
                    </div>
                    <code>{execution.execution_id}</code>
                    <span>{formatActionDuration(execution, t)}</span>
                    {execution.exit_code !== null ? <span>{t("Exit code {code}", { code: execution.exit_code })}</span> : null}
                    <span className={execution.stop_confirmation === "unconfirmed" ? "stop-unconfirmed" : ""}>
                      {t(stopConfirmationLabel(execution.stop_confirmation))}
                    </span>
                    {execution.error_summary ? <p className="inspector-error-summary">{execution.error_summary}</p> : null}
                  </li>
                ))}
              </ol>
            ) : <p>{t("No execution attempts")}</p>}
            <CoverageNotice label={t("Execution attempts")} coverage={displayedAction.attempt_coverage} />
          </InspectorSection>

          <InspectorSection title={t("Result and evidence")} icon={<CheckCircle2 size={15} />}>
            <dl className="inspector-facts">
              <div><dt>{t("Result")}</dt><dd>{t(displayedAction.lifecycle)}</dd></div>
              <div><dt>{t("Current stop")}</dt><dd className={displayedAction.current_stop_confirmation === "unconfirmed" ? "stop-unconfirmed" : ""}>{t(stopConfirmationLabel(displayedAction.current_stop_confirmation))}</dd></div>
              <div><dt>{t("Output")}</dt><dd>{displayedAction.result.output_available ? t("Available through authorized Artifact") : t("No bounded output available")}</dd></div>
              <div><dt>{t("Findings")}</dt><dd>{displayedAction.evidence.finding_count}</dd></div>
              <div><dt>{t("Artifacts")}</dt><dd>{displayedAction.evidence.artifact_ids.length} / {displayedAction.result.artifact_count}</dd></div>
            </dl>
            {displayedAction.evidence.artifact_ids.length ? (
              <div className="inspector-link-list" aria-label={t("Artifact evidence links")}>
                {displayedAction.evidence.artifact_ids.map((artifactId) => (
                  <button
                    className="secondary-button"
                    type="button"
                    key={artifactId}
                    aria-label={t("Download Artifact {id}", { id: artifactId })}
                    onClick={() => void downloadArtifact(artifactId)}
                  >
                    <Download size={13} /> {t("Artifact")} {artifactId}
                  </button>
                ))}
              </div>
            ) : null}
            {displayedAction.evidence.finding_ids.length ? (
              <div className="inspector-reference-list">
                <strong>{t("Finding references")}</strong>
                {displayedAction.evidence.finding_ids.map((findingId) => <code key={findingId}>{findingId}</code>)}
              </div>
            ) : null}
            <CoverageNotice label={t("Findings")} coverage={displayedAction.evidence.finding_coverage} />
            <CoverageNotice label={t("Audit events")} coverage={displayedAction.evidence.event_coverage} />
            {displayedAction.result.truncated ? <p className="coverage-warning"><AlertTriangle size={13} /> {t("Artifact references are truncated")}</p> : null}
          </InspectorSection>

          <InspectorSection title={t("Graph lineage")} icon={<GitBranch size={15} />}>
            {graphNodeId ? (
              <button
                className="secondary-button"
                type="button"
                onClick={() => onOpenGraph(graphNodeId)}
              >
                <GitBranch size={13} /> {t("Open in Graph")}
              </button>
            ) : (
              <p className="coverage-warning">
                <AlertTriangle size={13} />
                {t("Action-to-Graph link unsupported: the server Graph reference is missing, partial, malformed, or outside this Run; RiftX will not infer one.")}
              </p>
            )}
          </InspectorSection>

          {displayedAction.correlation_quality !== "exact" || displayedAction.partial_reasons.length ? (
            <InspectorSection title={t("Data quality")} icon={<AlertTriangle size={15} />}>
              <p>{t("This Action is partial or ambiguous; RiftX will not infer missing lineage.")}</p>
              {displayedAction.partial_reasons.length ? (
                <ul className="partial-reason-list">
                  {displayedAction.partial_reasons.map((reason) => <li key={reason}><code>{reason}</code></li>)}
                </ul>
              ) : null}
            </InspectorSection>
          ) : null}
        </div>
      ) : null}

      {downloadState.actionId === actionId && downloadState.error ? (
        <ErrorState error={downloadState.error} />
      ) : null}
      <span className="sr-only" role="status" aria-live="polite">
        {downloadState.actionId === actionId ? downloadState.status : ""}
      </span>
    </section>
  );
}

function explicitActionGraphNodeId(action: RunAction): string | null {
  const graphRef: unknown = (action as { graph_ref?: unknown }).graph_ref;
  if (
    typeof graphRef !== "object" ||
    graphRef === null ||
    Array.isArray(graphRef)
  ) {
    return null;
  }
  const candidate = graphRef as Record<string, unknown>;
  if (
    candidate.view !== "task" ||
    candidate.projection_quality !== "exact" ||
    typeof candidate.node_id !== "string"
  ) {
    return null;
  }
  const nodeId = candidate.node_id;
  const expectedNodeId = `action:${action.run_id}:${action.action_id}`;
  // The expected value validates the server reference; it is never used as a
  // fallback navigation target when the reference is absent or malformed.
  return GRAPH_ACTION_COMPONENT_PATTERN.test(action.run_id) &&
    GRAPH_ACTION_COMPONENT_PATTERN.test(action.action_id) &&
    GRAPH_NODE_ID_PATTERN.test(nodeId) &&
    nodeId === expectedNodeId
    ? nodeId
    : null;
}

function InspectorSection({
  title,
  icon,
  children,
}: {
  title: string;
  icon: ReactNode;
  children: ReactNode;
}) {
  return (
    <section className="inspector-section">
      <h4>{icon}{title}</h4>
      {children}
    </section>
  );
}

function IdentityPath({ action }: { action: RunAction }) {
  const { t } = useI18n();
  return (
    <dl className="inspector-identity-path">
      <div><dt>{t("Session")}</dt><dd><code>{action.session_id}</code></dd></div>
      <div><dt>{t("Cycle")}</dt><dd><code>{action.cycle_id}</code></dd></div>
      <div><dt>{t("Step")}</dt><dd><code>{action.step_id}</code></dd></div>
    </dl>
  );
}

function CoverageNotice({
  label,
  coverage,
}: {
  label: string;
  coverage: { scanned: number; limit: number; truncated: boolean };
}) {
  const { t } = useI18n();
  if (!coverage.truncated) return null;
  return (
    <p className="coverage-warning">
      <AlertTriangle size={13} />
      {t("{label} truncated at {scanned} of more records.", {
        label,
        scanned: coverage.scanned,
      })}
    </p>
  );
}

function displayedAttempt(action: RunActionListItem): RunActionListAttempt | null {
  if (action.current_execution_id) {
    const current = action.attempts.find(
      (attempt) => attempt.execution_id === action.current_execution_id,
    );
    if (current?.correlation_quality === "exact") return current;
    return null;
  }
  if (action.attempt_order_quality !== "exact" || !action.latest_execution_id) {
    return null;
  }
  const latest = action.attempts.find(
    (attempt) => attempt.execution_id === action.latest_execution_id,
  );
  return latest?.correlation_quality === "exact" ? latest : null;
}

function stopConfirmationLabel(
  value: RunActionListAttempt["stop_confirmation"] | null,
): string {
  if (value === "confirmed") return "Stop confirmed";
  if (value === "unconfirmed") return "Stop unconfirmed";
  if (value === "not_applicable") return "Stop not applicable";
  return "Stop confirmation unknown";
}

function actionTruncationLabels(
  action: RunActionListItem,
  t: ReturnType<typeof useI18n>["t"],
): string[] {
  const labels: string[] = [];
  if (action.attempt_coverage.truncated) labels.push(t("Attempts truncated"));
  if (action.artifacts_truncated) labels.push(t("Artifacts truncated"));
  if (action.finding_coverage.truncated) labels.push(t("Findings truncated"));
  if (action.event_coverage.truncated) labels.push(t("Audit events truncated"));
  return labels;
}

function formatActionDuration(
  attempt: Pick<RunActionListAttempt, "started_at" | "finished_at" | "status"> | null,
  t: ReturnType<typeof useI18n>["t"],
): string {
  if (!attempt?.started_at) return t("Not started");
  if (!attempt.finished_at) {
    const active = ["created", "queued", "starting", "running"].includes(
      attempt.status ?? "",
    );
    return t(active ? "In progress" : "Unknown");
  }
  const started = Date.parse(attempt.started_at);
  const finished = Date.parse(attempt.finished_at);
  if (!Number.isFinite(started) || !Number.isFinite(finished) || finished < started) {
    return t("Unknown");
  }
  const milliseconds = finished - started;
  if (milliseconds < 1_000) return `${milliseconds} ms`;
  return `${(milliseconds / 1_000).toFixed(milliseconds < 10_000 ? 1 : 0)} s`;
}

function formatActionTime(
  value: string | null | undefined,
  language: string,
  t: ReturnType<typeof useI18n>["t"],
): string {
  if (!value) return t("Unavailable");
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return t("Unavailable");
  return new Intl.DateTimeFormat(language === "zh-CN" ? "zh-CN" : "en", {
    dateStyle: "short",
    timeStyle: "medium",
  }).format(date);
}

function safeDomId(value: string): string {
  let result = "";
  for (const character of value) {
    result += /[A-Za-z0-9_-]/.test(character)
      ? character
      : `-${character.codePointAt(0)?.toString(16) ?? "x"}-`;
  }
  return result;
}
