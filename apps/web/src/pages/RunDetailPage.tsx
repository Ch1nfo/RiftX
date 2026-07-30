import {
  Activity,
  Archive,
  AlertTriangle,
  ArrowLeft,
  Ban,
  Bot,
  CheckCircle2,
  ChevronRight,
  CirclePause,
  Clock3,
  Download,
  ExternalLink,
  FileText,
  FileWarning,
  Loader2,
  MessageSquareText,
  Pencil,
  Play,
  Plus,
  Save,
  Send,
  ShieldAlert,
  TerminalSquare,
  Trash2,
  Wrench,
  X,
} from "lucide-react";
import { FormEvent, useState } from "react";
import ReactMarkdown from "react-markdown";
import { Link, useParams } from "react-router-dom";

import { api } from "../api/client";
import type {
  Approval,
  Artifact,
  Execution,
  Finding,
  FindingEvidence,
  Report,
  RunEvent,
} from "../api/types";
import { EmptyState } from "../components/EmptyState";
import { ErrorState } from "../components/ErrorState";
import { LoadingState } from "../components/LoadingState";
import { StatusBadge } from "../components/StatusBadge";
import { TerminalPanel } from "../components/TerminalPanel";
import {
  useApprovalControl,
  useApprovals,
  useArtifactControl,
  useArtifacts,
  useExecutions,
  useFindingControl,
  useFindings,
  useReportControl,
  useReports,
  useRun,
  useRunControl,
  useRunEvents,
} from "../hooks/queries";
import { useEventStream } from "../hooks/useEventStream";

type DetailTab =
  | "overview"
  | "agent"
  | "tool-calls"
  | "timeline"
  | "approvals"
  | "terminal"
  | "artifacts"
  | "findings"
  | "report";

export function RunDetailPage() {
  const { runId = "" } = useParams();
  const run = useRun(runId);
  const events = useRunEvents(runId);
  const executions = useExecutions(runId);
  const findings = useFindings(runId);
  const artifacts = useArtifacts(runId);
  const approvals = useApprovals(runId);
  const reports = useReports(runId);
  const approvalControls = useApprovalControl(runId);
  const artifactControls = useArtifactControl(runId);
  const findingControls = useFindingControl(runId);
  const reportControls = useReportControl(runId);
  const controls = useRunControl(runId);
  const [tab, setTab] = useState<DetailTab>("timeline");
  const [message, setMessage] = useState("");
  useEventStream(runId, events.isSuccess);

  const eventItems = events.data?.items ?? [];
  const planEvent = [...eventItems]
    .reverse()
    .find((event) => event.event_type === "agent.plan_updated");
  const terminalEvent = [...eventItems]
    .reverse()
    .find(
      (event) =>
        event.event_type === "terminal.opened" &&
        typeof event.payload.session_id === "string",
    );
  const terminalSessionId = terminalEvent?.payload.session_id as string | undefined;
  const pendingApprovals =
    approvals.data?.items.filter((approval) => approval.status === "pending") ?? [];

  async function submitMessage(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const normalized = message.trim();
    if (!normalized) return;
    await controls.message.mutateAsync(normalized);
    setMessage("");
  }

  if (run.isLoading) return <LoadingState label="Loading durable run" />;
  if (run.error) return <ErrorState error={run.error} />;
  if (!run.data) return null;

  const isFinal = ["completed", "failed", "cancelled"].includes(run.data.status);
  const anyControlPending =
    controls.pause.isPending || controls.resume.isPending || controls.cancel.isPending;

  return (
    <div className="page-stack">
      <div className="detail-heading">
        <div>
          <Link className="back-link" to="/">
            <ArrowLeft size={15} /> Dashboard
          </Link>
          <div className="detail-title-row">
            <h2>{run.data.objective.description}</h2>
            <StatusBadge status={run.data.status} />
          </div>
          <p className="run-identity">
            <span>{run.data.id}</span>
            <span>node / {run.data.node_id}</span>
            <span>mode / {run.data.approval_mode}</span>
            <span>model / {run.data.model_profile ?? "default"}</span>
          </p>
        </div>
        <div className="control-cluster">
          <button
            className="secondary-button"
            disabled={isFinal || anyControlPending}
            onClick={() => controls.pause.mutate()}
          >
            <CirclePause size={16} /> Pause
          </button>
          <button
            className="secondary-button"
            disabled={isFinal || anyControlPending}
            onClick={() => controls.resume.mutate()}
          >
            <Play size={16} /> Resume
          </button>
          <button
            className="danger-button"
            disabled={isFinal || anyControlPending}
            onClick={() => controls.cancel.mutate()}
          >
            <Ban size={16} /> Cancel execution
          </button>
        </div>
      </div>

      {controls.pause.error || controls.resume.error || controls.cancel.error ? (
        <ErrorState
          error={
            controls.pause.error ?? controls.resume.error ?? controls.cancel.error ?? new Error()
          }
        />
      ) : null}

      {pendingApprovals.length ? (
        <button className="approval-alert" onClick={() => setTab("approvals")}>
          <ShieldAlert size={19} />
          <span>
            <strong>{pendingApprovals.length} tool call{pendingApprovals.length === 1 ? "" : "s"} awaiting approval</strong>
            Review the exact command, target, and environment before resuming the Agent.
          </span>
          <ChevronRight size={18} />
        </button>
      ) : null}

      <div className="detail-layout">
        <section className="detail-main panel">
          <div className="detail-tabs" role="tablist">
            {(
              [
                ["overview", "Overview"],
                ["agent", "Agent"],
                ["tool-calls", `Tool Calls ${executions.data?.items.length ?? 0}`],
                ["timeline", `Timeline ${eventItems.length}`],
                ["approvals", `Approvals ${pendingApprovals.length}`],
                ["terminal", "Terminal"],
                ["artifacts", `Artifacts ${artifacts.data?.items.length ?? 0}`],
                ["findings", `Findings ${findings.data?.items.length ?? 0}`],
                ["report", `Reports ${reports.data?.items.length ?? 0}`],
              ] as Array<[DetailTab, string]>
            ).map(([value, label]) => (
              <button
                key={value}
                className={tab === value ? "active" : ""}
                onClick={() => setTab(value)}
                role="tab"
                aria-selected={tab === value}
              >
                {label}
              </button>
            ))}
          </div>

          <div className="detail-tab-content">
            {tab === "overview" ? (
              <RunOverview
                successCriteria={run.data.success_criteria}
                planEvent={planEvent}
                eventCount={eventItems.length}
              />
            ) : null}
            {tab === "agent" ? (
              <AgentActivity events={eventItems} loading={events.isLoading} />
            ) : null}
            {tab === "tool-calls" ? (
              <ToolCalls executions={executions.data?.items ?? []} loading={executions.isLoading} />
            ) : null}
            {tab === "timeline" ? <Timeline events={eventItems} loading={events.isLoading} /> : null}
            {tab === "approvals" ? (
              <Approvals
                approvals={approvals.data?.items ?? []}
                loading={approvals.isLoading}
                controls={approvalControls}
              />
            ) : null}
            {tab === "terminal" ? (
              <TerminalPanel runId={runId} initialSessionId={terminalSessionId} />
            ) : null}
            {tab === "artifacts" ? (
              <Artifacts
                artifacts={artifacts.data?.items ?? []}
                loading={artifacts.isLoading}
                controls={artifactControls}
              />
            ) : null}
            {tab === "findings" ? (
              <Findings
                findings={findings.data?.items ?? []}
                loading={findings.isLoading}
                controls={findingControls}
              />
            ) : null}
            {tab === "report" ? (
              <Reports
                reports={reports.data?.items ?? []}
                loading={reports.isLoading}
                reportable={isFinal}
                controls={reportControls}
              />
            ) : null}
          </div>

          {!isFinal ? (
            <form className="message-composer" onSubmit={(event) => void submitMessage(event)}>
              <MessageSquareText size={18} />
              <input
                value={message}
                onChange={(event) => setMessage(event.target.value)}
                placeholder="Send guidance to the durable Agent session…"
                aria-label="Message to Agent"
              />
              <button
                className="composer-send"
                type="submit"
                disabled={!message.trim() || controls.message.isPending}
                aria-label="Send message"
              >
                {controls.message.isPending ? (
                  <Loader2 className="spin" size={17} />
                ) : (
                  <Send size={17} />
                )}
              </button>
            </form>
          ) : null}
        </section>

        <aside className="detail-sidebar">
          <article className="panel compact-panel">
            <div className="panel-header">
              <div>
                <span className="panel-kicker">Lifecycle</span>
                <h3>Run facts</h3>
              </div>
            </div>
            <dl className="fact-list">
              <div>
                <dt>Created</dt>
                <dd>{formatTimestamp(run.data.created_at)}</dd>
              </div>
              <div>
                <dt>Started</dt>
                <dd>{run.data.started_at ? formatTimestamp(run.data.started_at) : "Pending"}</dd>
              </div>
              <div>
                <dt>Workspace</dt>
                <dd title={run.data.workspace_path}>{run.data.workspace_path}</dd>
              </div>
              <div>
                <dt>Workflow</dt>
                <dd title={run.data.temporal_workflow_id ?? ""}>
                  {run.data.temporal_workflow_id ?? "Not started"}
                </dd>
              </div>
            </dl>
          </article>

          <article className="panel compact-panel">
            <div className="panel-header">
              <div>
                <span className="panel-kicker">Boundary</span>
                <h3>Scope</h3>
              </div>
            </div>
            <div className="scope-list">
              {[
                ...run.data.scope.cidrs,
                ...run.data.scope.ips,
                ...run.data.scope.domains,
                ...run.data.scope.url_prefixes,
              ].map((item) => (
                <span className="mono-chip" key={item}>
                  {item}
                </span>
              ))}
              {!run.data.scope.cidrs.length &&
              !run.data.scope.ips.length &&
              !run.data.scope.domains.length &&
              !run.data.scope.url_prefixes.length ? (
                <span className="muted-caption">No explicit scope values</span>
              ) : null}
            </div>
            {run.data.scope.exclusions.length ? (
              <div className="exclusion-list">
                <strong>Exclusions</strong>
                {run.data.scope.exclusions.map((item) => (
                  <span key={item}>{item}</span>
                ))}
              </div>
            ) : null}
          </article>
        </aside>
      </div>
    </div>
  );
}

function AgentActivity({ events, loading }: { events: RunEvent[]; loading: boolean }) {
  const agentEvents = events.filter(
    (event) => event.event_type.startsWith("agent.") || event.event_type === "user.message_queued",
  );
  if (!loading && !agentEvents.length) {
    return (
      <EmptyState icon={Bot} title="No Agent activity yet">
        Plans, messages, tool decisions, and cycle transitions will appear here.
      </EmptyState>
    );
  }
  return <Timeline events={agentEvents} loading={loading} />;
}

function ToolCalls({ executions, loading }: { executions: Execution[]; loading: boolean }) {
  if (loading) return <LoadingState label="Loading tool calls" />;
  if (!executions.length) {
    return (
      <EmptyState icon={Wrench} title="No tool calls yet">
        Host execution records will appear after the Agent invokes a registered tool.
      </EmptyState>
    );
  }
  return (
    <div className="tool-table-wrap">
      <table className="tool-table execution-table">
        <thead>
          <tr>
            <th>Tool / command</th>
            <th>Status</th>
            <th>Node</th>
            <th>Runtime</th>
            <th>Provenance</th>
          </tr>
        </thead>
        <tbody>
          {executions.map((execution) => (
            <tr key={execution.id}>
              <td>
                <div className="tool-name-cell">
                  <span><Wrench size={17} /></span>
                  <div>
                    <strong>{execution.tool_id ?? execution.executor_type}</strong>
                    <small title={execution.command_text ?? execution.argv.join(" ")}>
                      {execution.command_text ?? execution.argv.join(" ")}
                    </small>
                  </div>
                </div>
              </td>
              <td>
                <StatusBadge status={execution.status} />
                {execution.exit_code !== null ? (
                  <small className="tool-reason">exit {execution.exit_code}</small>
                ) : null}
              </td>
              <td>
                <strong>{execution.node_id}</strong>
                <small className="tool-reason">{execution.cwd}</small>
              </td>
              <td>{executionDuration(execution)}</td>
              <td>
                <strong>{execution.tool_version ?? "unversioned"}</strong>
                <small className="tool-reason">
                  {execution.executable_path ?? execution.platform_system}
                </small>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function Timeline({ events, loading }: { events: RunEvent[]; loading: boolean }) {
  if (loading) return <LoadingState label="Loading event timeline" />;
  if (!events.length) {
    return (
      <EmptyState icon={Clock3} title="Timeline is empty">
        Durable events appear here as the workflow progresses.
      </EmptyState>
    );
  }
  return (
    <div className="timeline">
      {events.map((event) => (
        <article className="timeline-event" key={event.id}>
          <div className="timeline-rail">
            <div className={`event-icon event-${eventFamily(event.event_type)}`}>
              <EventIcon eventType={event.event_type} />
            </div>
          </div>
          <div className="event-card">
            <div className="event-header">
              <div>
                <span className="event-sequence">#{event.sequence}</span>
                <strong>{eventTitle(event.event_type)}</strong>
              </div>
              <time>{formatTimestamp(event.created_at)}</time>
            </div>
            <EventPayload event={event} />
          </div>
        </article>
      ))}
    </div>
  );
}

function EventPayload({ event }: { event: RunEvent }) {
  const narrative = [
    event.payload.assistant_message,
    event.payload.message,
    event.payload.summary,
    event.payload.plan_summary,
  ].find((value): value is string => typeof value === "string" && Boolean(value));
  if (narrative) {
    return (
      <div className="event-markdown">
        <ReactMarkdown>{narrative}</ReactMarkdown>
      </div>
    );
  }
  if (!Object.keys(event.payload).length) {
    return <p className="muted-caption">No additional payload.</p>;
  }
  return <pre className="event-json">{JSON.stringify(event.payload, null, 2)}</pre>;
}

function RunOverview({
  successCriteria,
  planEvent,
  eventCount,
}: {
  successCriteria: Array<{ description: string; required: boolean }>;
  planEvent?: RunEvent;
  eventCount: number;
}) {
  return (
    <div className="overview-grid">
      <article className="overview-card">
        <span className="overview-icon">
          <CheckCircle2 size={18} />
        </span>
        <div>
          <span className="panel-kicker">Success criteria</span>
          <h3>{successCriteria.length || "None defined"}</h3>
          {successCriteria.length ? (
            <ul className="criteria-list">
              {successCriteria.map((criterion) => (
                <li key={criterion.description}>
                  <ChevronRight size={14} /> {criterion.description}
                </li>
              ))}
            </ul>
          ) : (
            <p>The Agent will infer completion from the objective.</p>
          )}
        </div>
      </article>
      <article className="overview-card">
        <span className="overview-icon">
          <Activity size={18} />
        </span>
        <div>
          <span className="panel-kicker">Durable activity</span>
          <h3>{eventCount} events</h3>
          <p>Every state transition is replayable from the database timeline.</p>
        </div>
      </article>
      <article className="overview-card overview-wide">
        <span className="overview-icon">
          <Bot size={18} />
        </span>
        <div>
          <span className="panel-kicker">Latest plan</span>
          {planEvent ? (
            <EventPayload event={planEvent} />
          ) : (
            <p>The Agent has not published a plan summary yet.</p>
          )}
        </div>
      </article>
    </div>
  );
}

function Findings({
  findings,
  loading,
  controls,
}: {
  findings: Finding[];
  loading: boolean;
  controls: ReturnType<typeof useFindingControl>;
}) {
  const [editingId, setEditingId] = useState<string | null>(null);
  if (loading) return <LoadingState label="Loading findings" />;
  if (!findings.length) {
    return (
      <EmptyState icon={FileWarning} title="No findings yet">
        Evidence-backed findings created by the Agent will appear here.
      </EmptyState>
    );
  }
  return (
    <div className="finding-list">
      {findings.map((finding) =>
        editingId === finding.id ? (
          <FindingEditor
            key={finding.id}
            finding={finding}
            saving={controls.update.isPending}
            onCancel={() => setEditingId(null)}
            onSave={async (payload) => {
              await controls.update.mutateAsync({ findingId: finding.id, payload });
              setEditingId(null);
            }}
          />
        ) : (
          <article className="finding-card" key={finding.id}>
            <div className={`severity-marker severity-${finding.severity}`} />
            <div>
              <div className="finding-head">
                <span className={`severity-label severity-${finding.severity}`}>
                  {finding.severity}
                </span>
                <span>{finding.status.replaceAll("_", " ")}</span>
                <button
                  className="finding-edit-button"
                  onClick={() => setEditingId(finding.id)}
                  aria-label={`Edit ${finding.title}`}
                >
                  <Pencil size={13} /> Edit
                </button>
              </div>
              <h3>{finding.title}</h3>
              <p>{finding.description || "No description supplied."}</p>
              {finding.affected_assets.length ? (
                <div className="scope-list">
                  {finding.affected_assets.map((asset) => (
                    <span className="mono-chip" key={asset}>
                      {asset}
                    </span>
                  ))}
                </div>
              ) : null}
              <FindingEvidenceList evidence={finding.evidence} />
              {finding.impact || finding.recommendation ? (
                <div className="finding-guidance">
                  {finding.impact ? (
                    <div>
                      <strong>Impact</strong>
                      <p>{finding.impact}</p>
                    </div>
                  ) : null}
                  {finding.recommendation ? (
                    <div>
                      <strong>Recommendation</strong>
                      <p>{finding.recommendation}</p>
                    </div>
                  ) : null}
                </div>
              ) : null}
            </div>
          </article>
        ),
      )}
      {controls.update.error ? <ErrorState error={controls.update.error} /> : null}
    </div>
  );
}

function FindingEvidenceList({ evidence }: { evidence: FindingEvidence[] }) {
  if (!evidence.length) return null;
  return (
    <div className="finding-evidence-list">
      {evidence.map((item, index) => (
        <article
          className="finding-evidence"
          key={`${item.artifact_id ?? "artifact"}-${item.execution_id ?? "execution"}-${index}`}
        >
          <div>
            <strong>Evidence {index + 1}</strong>
            <span>{item.location || "No location marker"}</span>
          </div>
          <p>{item.description || "No evidence description."}</p>
          <div className="finding-evidence-links">
            {item.artifact_id ? (
              <a
                href={api.artifactContentUrlById(item.artifact_id)}
                target="_blank"
                rel="noreferrer"
              >
                <ExternalLink size={12} /> Artifact {item.artifact_id}
              </a>
            ) : null}
            {item.execution_id ? <code>Execution {item.execution_id}</code> : null}
          </div>
        </article>
      ))}
    </div>
  );
}

function FindingEditor({
  finding,
  saving,
  onCancel,
  onSave,
}: {
  finding: Finding;
  saving: boolean;
  onCancel: () => void;
  onSave: (payload: {
    title: string;
    severity: Finding["severity"];
    status: Finding["status"];
    affected_assets: string[];
    description: string;
    evidence: FindingEvidence[];
    reproduction_steps: string[];
    impact: string;
    recommendation: string;
  }) => Promise<void>;
}) {
  const [title, setTitle] = useState(finding.title);
  const [severity, setSeverity] = useState(finding.severity);
  const [status, setStatus] = useState(finding.status);
  const [affectedAssets, setAffectedAssets] = useState(finding.affected_assets.join("\n"));
  const [description, setDescription] = useState(finding.description);
  const [evidence, setEvidence] = useState<FindingEvidence[]>(finding.evidence);
  const [reproductionSteps, setReproductionSteps] = useState(
    finding.reproduction_steps.join("\n"),
  );
  const [impact, setImpact] = useState(finding.impact);
  const [recommendation, setRecommendation] = useState(finding.recommendation);

  function updateEvidence(index: number, patch: Partial<FindingEvidence>) {
    setEvidence((current) =>
      current.map((item, itemIndex) =>
        itemIndex === index ? { ...item, ...patch } : item,
      ),
    );
  }

  return (
    <form
      className="finding-editor"
      onSubmit={(event) => {
        event.preventDefault();
        if (!title.trim()) return;
        void onSave({
          title: title.trim(),
          severity,
          status,
          affected_assets: splitLines(affectedAssets),
          description,
          evidence,
          reproduction_steps: splitLines(reproductionSteps),
          impact,
          recommendation,
        });
      }}
    >
      <div className="finding-editor-grid">
        <label className="finding-editor-title">
          <span>Title</span>
          <input value={title} onChange={(event) => setTitle(event.target.value)} required />
        </label>
        <label>
          <span>Severity</span>
          <select
            value={severity}
            onChange={(event) => setSeverity(event.target.value as Finding["severity"])}
          >
            {(["info", "low", "medium", "high", "critical"] as const).map((value) => (
              <option key={value} value={value}>
                {value}
              </option>
            ))}
          </select>
        </label>
        <label>
          <span>Status</span>
          <select
            value={status}
            onChange={(event) => setStatus(event.target.value as Finding["status"])}
          >
            {(["draft", "confirmed", "resolved", "false_positive"] as const).map(
              (value) => (
                <option key={value} value={value}>
                  {value.replaceAll("_", " ")}
                </option>
              ),
            )}
          </select>
        </label>
        <label className="finding-editor-wide">
          <span>Description</span>
          <textarea
            value={description}
            onChange={(event) => setDescription(event.target.value)}
            rows={4}
          />
        </label>
        <label>
          <span>Affected assets · one per line</span>
          <textarea
            value={affectedAssets}
            onChange={(event) => setAffectedAssets(event.target.value)}
            rows={4}
          />
        </label>
        <label>
          <span>Reproduction steps · one per line</span>
          <textarea
            value={reproductionSteps}
            onChange={(event) => setReproductionSteps(event.target.value)}
            rows={4}
          />
        </label>
        <label>
          <span>Impact</span>
          <textarea value={impact} onChange={(event) => setImpact(event.target.value)} rows={4} />
        </label>
        <label>
          <span>Recommendation</span>
          <textarea
            value={recommendation}
            onChange={(event) => setRecommendation(event.target.value)}
            rows={4}
          />
        </label>
      </div>

      <div className="finding-evidence-editor">
        <div className="finding-editor-section-head">
          <div>
            <span className="panel-kicker">Evidence links</span>
            <h4>Artifacts and executions</h4>
          </div>
          <button
            className="secondary-button"
            type="button"
            onClick={() =>
              setEvidence((current) => [
                ...current,
                {
                  artifact_id: null,
                  execution_id: null,
                  description: "",
                  location: null,
                },
              ])
            }
          >
            <Plus size={14} /> Add evidence
          </button>
        </div>
        {evidence.map((item, index) => (
          <div className="finding-evidence-row" key={index}>
            <label>
              <span>Artifact ID</span>
              <input
                value={item.artifact_id ?? ""}
                onChange={(event) =>
                  updateEvidence(index, { artifact_id: event.target.value || null })
                }
              />
            </label>
            <label>
              <span>Execution ID</span>
              <input
                value={item.execution_id ?? ""}
                onChange={(event) =>
                  updateEvidence(index, { execution_id: event.target.value || null })
                }
              />
            </label>
            <label>
              <span>Location</span>
              <input
                value={item.location ?? ""}
                onChange={(event) =>
                  updateEvidence(index, { location: event.target.value || null })
                }
              />
            </label>
            <label className="finding-evidence-description">
              <span>Description</span>
              <input
                value={item.description}
                onChange={(event) =>
                  updateEvidence(index, { description: event.target.value })
                }
              />
            </label>
            <button
              className="finding-remove-evidence"
              type="button"
              onClick={() =>
                setEvidence((current) => current.filter((_, itemIndex) => itemIndex !== index))
              }
              aria-label={`Remove evidence ${index + 1}`}
            >
              <Trash2 size={14} />
            </button>
          </div>
        ))}
      </div>

      <div className="finding-editor-actions">
        <button className="secondary-button" type="button" onClick={onCancel} disabled={saving}>
          <X size={14} /> Cancel
        </button>
        <button className="primary-button" type="submit" disabled={saving || !title.trim()}>
          {saving ? <Loader2 className="spin" size={14} /> : <Save size={14} />}
          Save finding
        </button>
      </div>
    </form>
  );
}

function Artifacts({
  artifacts,
  loading,
  controls,
}: {
  artifacts: Artifact[];
  loading: boolean;
  controls: ReturnType<typeof useArtifactControl>;
}) {
  const [sourcePath, setSourcePath] = useState("");
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const normalized = sourcePath.trim();
    if (!normalized) return;
    await controls.register.mutateAsync({
      source_path: normalized,
      ...(name.trim() ? { name: name.trim() } : {}),
      ...(description.trim() ? { description: description.trim() } : {}),
    });
    setSourcePath("");
    setName("");
    setDescription("");
  }

  return (
    <div className="artifact-stack">
      <form className="artifact-register" onSubmit={(event) => void submit(event)}>
        <div>
          <span className="panel-kicker">Immutable evidence</span>
          <h3>Register a Run-owned file</h3>
          <p>The path must be inside this Run workspace or its Runner state directory.</p>
        </div>
        <div className="artifact-register-grid">
          <label className="artifact-source-field">
            <span>Source path</span>
            <input
              value={sourcePath}
              onChange={(event) => setSourcePath(event.target.value)}
              placeholder="/path/to/run/workspace/result.xml"
              required
            />
          </label>
          <label>
            <span>Name (optional)</span>
            <input
              value={name}
              onChange={(event) => setName(event.target.value)}
              placeholder="result.xml"
            />
          </label>
          <label className="artifact-description-field">
            <span>Description (optional)</span>
            <input
              value={description}
              onChange={(event) => setDescription(event.target.value)}
              placeholder="What this evidence proves"
            />
          </label>
          <button
            className="primary-button"
            type="submit"
            disabled={!sourcePath.trim() || controls.register.isPending}
          >
            {controls.register.isPending ? (
              <Loader2 className="spin" size={15} />
            ) : (
              <Plus size={15} />
            )}
            Register
          </button>
        </div>
        {controls.register.error ? <ErrorState error={controls.register.error} /> : null}
      </form>

      {loading ? <LoadingState label="Loading artifacts" /> : null}
      {!loading && !artifacts.length ? (
        <EmptyState icon={Archive} title="No artifacts registered">
          Tool outputs, screenshots, logs, and report attachments will appear here.
        </EmptyState>
      ) : null}
      {!loading && artifacts.length ? (
        <div className="artifact-list">
          {artifacts.map((artifact) => (
            <article className="artifact-card" key={artifact.id}>
              <span className="artifact-icon">
                <Archive size={18} />
              </span>
              <div className="artifact-main">
                <div className="artifact-head">
                  <h3>{artifact.name}</h3>
                  <span>{formatBytes(artifact.size)}</span>
                </div>
                <p>{artifact.description || "No description supplied."}</p>
                <div className="artifact-meta">
                  <span>{artifact.mime_type}</span>
                  <code title={artifact.sha256}>sha256:{artifact.sha256}</code>
                  {artifact.execution_id ? <span>exec / {artifact.execution_id}</span> : null}
                </div>
              </div>
              <a
                className="secondary-button artifact-download"
                href={api.artifactContentUrl(artifact)}
                download={artifact.name}
              >
                <Download size={15} /> Download
              </a>
            </article>
          ))}
        </div>
      ) : null}
    </div>
  );
}

function Approvals({
  approvals,
  loading,
  controls,
}: {
  approvals: Approval[];
  loading: boolean;
  controls: ReturnType<typeof useApprovalControl>;
}) {
  const [reasons, setReasons] = useState<Record<string, string>>({});
  if (loading) return <LoadingState label="Loading approvals" />;
  if (!approvals.length) {
    return (
      <EmptyState icon={ShieldAlert} title="No approval requests">
        Sensitive or manually controlled Tool calls will appear here with their exact execution
        snapshot.
      </EmptyState>
    );
  }
  return (
    <div className="approval-list">
      {[...approvals].reverse().map((approval) => {
        const pending = approval.status === "pending";
        const busy = controls.approve.isPending || controls.reject.isPending;
        return (
          <article className={`approval-card approval-${approval.status}`} key={approval.id}>
            <div className="approval-card-head">
              <div>
                <span className="panel-kicker">{approval.status.replaceAll("_", " ")}</span>
                <h3>{approval.tool_name}</h3>
              </div>
              <span className="mono-chip">{approval.id}</span>
            </div>
            <dl className="approval-facts">
              <div>
                <dt>Command</dt>
                <dd><code>{approval.command.join(" ") || "No command snapshot"}</code></dd>
              </div>
              <div>
                <dt>Working directory</dt>
                <dd><code>{approval.cwd || "—"}</code></dd>
              </div>
              <div>
                <dt>Target</dt>
                <dd>{approval.target_summary || "—"}</dd>
              </div>
              <div>
                <dt>Environment changes</dt>
                <dd>
                  {Object.keys(approval.env_diff).length ? (
                    <pre>{JSON.stringify(approval.env_diff, null, 2)}</pre>
                  ) : "None"}
                </dd>
              </div>
              <div>
                <dt>Agent reason</dt>
                <dd>{approval.reason || "No reason supplied."}</dd>
              </div>
            </dl>
            {pending ? (
              <div className="approval-actions">
                <textarea
                  value={reasons[approval.id] ?? ""}
                  onChange={(event) =>
                    setReasons((current) => ({ ...current, [approval.id]: event.target.value }))
                  }
                  placeholder="Optional rejection reason…"
                  aria-label={`Rejection reason for ${approval.tool_name}`}
                  rows={2}
                />
                <div>
                  <button
                    className="secondary-button"
                    disabled={busy}
                    onClick={() =>
                      controls.reject.mutate({
                        approvalId: approval.id,
                        payload: { reason: reasons[approval.id]?.trim() || null },
                      })
                    }
                  >
                    <Ban size={15} /> Reject
                  </button>
                  <button
                    className="secondary-button"
                    disabled={busy}
                    onClick={() =>
                      controls.approve.mutate({ approvalId: approval.id })
                    }
                  >
                    <CheckCircle2 size={15} /> Approve once
                  </button>
                  <button
                    className="primary-button"
                    disabled={busy}
                    onClick={() =>
                      controls.approve.mutate({
                        approvalId: approval.id,
                        payload: { approve_for_run: true },
                      })
                    }
                  >
                    <ShieldAlert size={15} /> Approve for Run
                  </button>
                </div>
              </div>
            ) : (
              <p className="approval-decision">
                Decided by {approval.decided_by ?? "unknown"}
                {approval.decided_at ? ` · ${formatTimestamp(approval.decided_at)}` : ""}
              </p>
            )}
          </article>
        );
      })}
      {controls.approve.error || controls.reject.error ? (
        <ErrorState error={controls.approve.error ?? controls.reject.error ?? new Error()} />
      ) : null}
    </div>
  );
}

function Reports({
  reports,
  loading,
  reportable,
  controls,
}: {
  reports: Report[];
  loading: boolean;
  reportable: boolean;
  controls: ReturnType<typeof useReportControl>;
}) {
  if (loading) return <LoadingState label="Loading generated reports" />;
  const grouped = [...reports].sort(
    (left, right) => Date.parse(right.created_at) - Date.parse(left.created_at),
  );
  return (
    <div className="report-stack">
      <div className="section-toolbar">
        <div>
          <span className="panel-kicker">Restricted source · immutable output</span>
          <h3>Run reports</h3>
          <p>
            {reportable
              ? "Generate Markdown, HTML, and JSON from findings, artifact summaries, and key activity only."
              : "Report generation unlocks after the Run reaches a final status."}
          </p>
        </div>
        <button
          className="primary-button"
          disabled={!reportable || controls.generate.isPending}
          onClick={() => controls.generate.mutate(undefined)}
        >
          {controls.generate.isPending ? (
            <Loader2 className="spin" size={16} />
          ) : (
            <FileText size={16} />
          )}
          Generate reports
        </button>
      </div>
      {controls.generate.error ? <ErrorState error={controls.generate.error} /> : null}
      {!grouped.length ? (
        <EmptyState icon={FileText} title="No reports generated yet">
          Generate a report set now, or let the durable workflow create one after Agent completion.
        </EmptyState>
      ) : (
        <div className="report-grid">
          {grouped.map((report) => (
            <article className="report-card" key={report.id}>
              <div className="report-format">
                <FileText size={19} />
                <strong>{report.format.toUpperCase()}</strong>
              </div>
              <p>{report.finding_ids.length} linked finding{report.finding_ids.length === 1 ? "" : "s"}</p>
              <code>{report.id}</code>
              <span>{formatTimestamp(report.created_at)}</span>
              <a
                className="secondary-button report-open-button"
                href={api.artifactContentUrl({ content_url: report.content_url })}
                target="_blank"
                rel="noreferrer"
              >
                <ExternalLink size={14} /> Open report
              </a>
            </article>
          ))}
        </div>
      )}
    </div>
  );
}

function EventIcon({ eventType }: { eventType: string }) {
  if (eventType.includes("failed")) return <AlertTriangle size={16} />;
  if (eventType.startsWith("agent.")) return <Bot size={16} />;
  if (eventType.includes("tool")) return <Wrench size={16} />;
  if (eventType.includes("approval")) return <ShieldAlert size={16} />;
  if (eventType.includes("execution") || eventType.startsWith("terminal.")) {
    return <TerminalSquare size={16} />;
  }
  if (eventType.startsWith("user.")) return <MessageSquareText size={16} />;
  return <Activity size={16} />;
}

function eventFamily(eventType: string) {
  if (eventType.includes("failed")) return "failed";
  if (eventType.startsWith("agent.")) return "agent";
  if (
    eventType.includes("tool") ||
    eventType.includes("execution") ||
    eventType.startsWith("terminal.")
  ) {
    return "tool";
  }
  if (eventType.startsWith("user.")) return "user";
  return "run";
}

function eventTitle(eventType: string) {
  return eventType
    .split(".")
    .map((part) => part.replaceAll("_", " "))
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" · ");
}

function formatTimestamp(value: string) {
  return new Intl.DateTimeFormat(undefined, {
    month: "short",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  }).format(new Date(value));
}

function executionDuration(execution: Execution) {
  if (!execution.started_at) return "not started";
  const started = new Date(execution.started_at).getTime();
  const finished = execution.finished_at
    ? new Date(execution.finished_at).getTime()
    : Date.now();
  const milliseconds = Math.max(0, finished - started);
  if (milliseconds < 1000) return `${milliseconds}ms`;
  if (milliseconds < 60_000) return `${(milliseconds / 1000).toFixed(1)}s`;
  return `${(milliseconds / 60_000).toFixed(1)}m`;
}

function formatBytes(value: number) {
  if (value < 1024) return `${value} B`;
  const units = ["KiB", "MiB", "GiB", "TiB"];
  let amount = value / 1024;
  let unit = units[0];
  for (let index = 1; index < units.length && amount >= 1024; index += 1) {
    amount /= 1024;
    unit = units[index];
  }
  return `${amount.toFixed(amount >= 10 ? 1 : 2)} ${unit}`;
}

function splitLines(value: string) {
  return value
    .split(/[,\n]/)
    .map((item) => item.trim())
    .filter(Boolean);
}
