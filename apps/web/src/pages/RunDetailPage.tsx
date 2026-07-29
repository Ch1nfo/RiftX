import {
  Activity,
  AlertTriangle,
  ArrowLeft,
  Ban,
  Bot,
  CheckCircle2,
  ChevronRight,
  CirclePause,
  Clock3,
  FileWarning,
  Loader2,
  MessageSquareText,
  Play,
  Send,
  ShieldAlert,
  TerminalSquare,
  Wrench,
} from "lucide-react";
import { FormEvent, useState } from "react";
import ReactMarkdown from "react-markdown";
import { Link, useParams } from "react-router-dom";

import type { Approval, Finding, RunEvent } from "../api/types";
import { EmptyState } from "../components/EmptyState";
import { ErrorState } from "../components/ErrorState";
import { LoadingState } from "../components/LoadingState";
import { StatusBadge } from "../components/StatusBadge";
import { TerminalPanel } from "../components/TerminalPanel";
import {
  useFindings,
  useApprovalControl,
  useApprovals,
  useRun,
  useRunControl,
  useRunEvents,
} from "../hooks/queries";
import { useEventStream } from "../hooks/useEventStream";

type DetailTab =
  | "overview"
  | "timeline"
  | "approvals"
  | "terminal"
  | "findings"
  | "report";

export function RunDetailPage() {
  const { runId = "" } = useParams();
  const run = useRun(runId);
  const events = useRunEvents(runId);
  const findings = useFindings(runId);
  const approvals = useApprovals(runId);
  const approvalControls = useApprovalControl(runId);
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
                ["timeline", `Timeline ${eventItems.length}`],
                ["approvals", `Approvals ${pendingApprovals.length}`],
                ["terminal", "Terminal"],
                ["findings", `Findings ${findings.data?.items.length ?? 0}`],
                ["report", "Report"],
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
            {tab === "findings" ? (
              <Findings findings={findings.data?.items ?? []} loading={findings.isLoading} />
            ) : null}
            {tab === "report" ? <ReportPlaceholder completed={run.data.status === "completed"} /> : null}
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

function Findings({ findings, loading }: { findings: Finding[]; loading: boolean }) {
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
      {findings.map((finding) => (
        <article className="finding-card" key={finding.id}>
          <div className={`severity-marker severity-${finding.severity}`} />
          <div>
            <div className="finding-head">
              <span className={`severity-label severity-${finding.severity}`}>
                {finding.severity}
              </span>
              <span>{finding.status.replaceAll("_", " ")}</span>
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
          </div>
        </article>
      ))}
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

function ReportPlaceholder({ completed }: { completed: boolean }) {
  return (
    <EmptyState icon={completed ? CheckCircle2 : FileWarning} title="Report generation is queued for M8">
      {completed
        ? "The run is complete. The structured report pipeline will attach Markdown and HTML outputs in V2-M8."
        : "RiftX generates a report after the Agent completes the run and enters the reporting phase."}
    </EmptyState>
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
