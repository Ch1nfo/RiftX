import {
  AlertTriangle,
  Bot,
  CheckCircle2,
  FileKey2,
  ShieldAlert,
  Search,
  TerminalSquare,
  UserRound,
} from "lucide-react";
import type {
  ApprovalDecision,
  ConversationEntry,
  Engagement,
  EngagementEvent,
  EngagementReport,
  PendingApproval,
} from "../models";

interface ActivityTimelineProps {
  engagement: Engagement;
  report: EngagementReport | null;
  history: ConversationEntry[];
  events: EngagementEvent[];
  approvals: PendingApproval[];
  loading: boolean;
  decidingApprovalId: string | null;
  canLoadOlder: boolean;
  loadingOlder: boolean;
  onApproval: (approvalId: string, decision: ApprovalDecision) => void;
  onLoadOlder: () => void;
}

export function ActivityTimeline({
  engagement,
  report,
  history,
  events,
  approvals,
  loading,
  decidingApprovalId,
  canLoadOlder,
  loadingOlder,
  onApproval,
  onLoadOlder,
}: ActivityTimelineProps) {
  const messages = timelineMessages(history, events);
  const hasActivity =
    messages.length > 0 ||
    approvals.length > 0 ||
    (report &&
      (report.tasks.length > 0 ||
        report.executions.length > 0 ||
        report.findings.length > 0 ||
        report.evidence.length > 0));

  return (
    <div className="timeline" aria-live="polite">
      <article className="timeline-entry objective-entry">
        <span className="timeline-icon">
          <Search size={16} />
        </span>
        <div>
          <header>
            <strong>Objective</strong>
            <span>{engagement.mode} mode</span>
          </header>
          <p>{engagement.objective.summary}</p>
        </div>
      </article>

      {canLoadOlder && (
        <button
          className="history-loader"
          type="button"
          disabled={loadingOlder}
          onClick={onLoadOlder}
        >
          {loadingOlder ? "Loading..." : "Load older messages"}
        </button>
      )}

      {messages.map((message) => (
        <article
          className={`timeline-entry message-entry ${message.role}`}
          key={message.id}
        >
          <span className="timeline-icon">
            {message.role === "operator" ? (
              <UserRound size={16} />
            ) : (
              <Bot size={16} />
            )}
          </span>
          <div>
            <header>
              <strong>
                {message.kind === "plan" ? "agent plan" : message.role}
              </strong>
              <span>{formatTime(message.timestamp)}</span>
            </header>
            <p>{message.text}</p>
          </div>
        </article>
      ))}

      {approvals.map((approval) => (
        <article className="timeline-entry approval-entry" key={approval.id}>
          <span className="timeline-icon">
            <ShieldAlert size={16} />
          </span>
          <div>
            <header>
              <strong>
                {approval.kind === "tool"
                  ? "High-risk tool approval"
                  : "Command approval"}
              </strong>
              <span>{formatTime(approval.requestedAt)}</span>
            </header>
            {approval.reason && <p>{approval.reason}</p>}
            {approval.command && <code>{approval.command}</code>}
            {approval.cwd && <p className="approval-cwd">{approval.cwd}</p>}
            <div className="approval-actions">
              <button
                type="button"
                className="secondary-button"
                disabled={decidingApprovalId === approval.id}
                onClick={() => onApproval(approval.id, "deny")}
              >
                Deny
              </button>
              <button
                type="button"
                className="primary-button"
                disabled={decidingApprovalId === approval.id}
                onClick={() => onApproval(approval.id, "approve")}
              >
                Approve once
              </button>
            </div>
          </div>
        </article>
      ))}

      {report?.tasks.map((task) => (
        <article className="timeline-entry" key={task.id}>
          <span className="timeline-icon">
            <CheckCircle2 size={16} />
          </span>
          <div>
            <header>
              <strong>{task.kind}</strong>
              <span>{task.status}</span>
            </header>
            {task.error && <p className="error-copy">{task.error}</p>}
          </div>
        </article>
      ))}

      {report?.executions.map((execution) => (
        <article className="timeline-entry command-entry" key={execution.id}>
          <span className="timeline-icon">
            <TerminalSquare size={16} />
          </span>
          <div>
            <header>
              <strong>{execution.runner}</strong>
              <span>{execution.status}</span>
            </header>
            {execution.command && <code>{execution.command}</code>}
            {execution.exitCode !== undefined &&
              execution.exitCode !== null && (
                <p>Exit code {execution.exitCode}</p>
              )}
          </div>
        </article>
      ))}

      {report?.findings.map((finding) => (
        <article className="timeline-entry finding-entry" key={finding.id}>
          <span className="timeline-icon">
            <AlertTriangle size={16} />
          </span>
          <div>
            <header>
              <strong>{finding.title}</strong>
              <span>{finding.severity}</span>
            </header>
            <p>{finding.description}</p>
          </div>
        </article>
      ))}

      {report?.evidence.map((evidence) => (
        <article className="timeline-entry evidence-entry" key={evidence.id}>
          <span className="timeline-icon">
            <FileKey2 size={16} />
          </span>
          <div>
            <header>
              <strong>Evidence</strong>
            </header>
            <p>{evidence.summary}</p>
          </div>
        </article>
      ))}

      {!hasActivity && !loading && (
        <div className="timeline-empty">
          <TerminalSquare size={20} />
          <p>Ready for an operator instruction.</p>
        </div>
      )}
      {loading && <div className="timeline-loading">Updating activity...</div>}
    </div>
  );
}

interface TimelineMessage {
  id: string;
  role: "operator" | "agent";
  kind: "message" | "plan";
  text: string;
  timestamp: number;
}

function timelineMessages(
  history: ConversationEntry[],
  events: EngagementEvent[],
): TimelineMessage[] {
  const messages: TimelineMessage[] = history.map((entry) => ({
    id: entry.id,
    role: entry.role,
    kind: entry.kind,
    text: entry.text,
    timestamp: entry.createdAt,
  }));
  const itemIndexes = new Map(
    messages.map((message, index) => [message.id, index]),
  );
  events.forEach((event, index) => {
    const data = record(event.data);
    if (event.kind === "operator/message") {
      const text = stringValue(data?.text);
      if (text) {
        const id =
          stringValue(data?.entryId) ?? `operator-${event.timestamp}-${index}`;
        if (itemIndexes.has(id)) {
          return;
        }
        itemIndexes.set(id, messages.length);
        messages.push({
          id,
          role: "operator",
          kind: "message",
          text,
          timestamp: event.timestamp,
        });
      }
      return;
    }

    const payload = record(data?.payload);
    const itemId = stringValue(payload?.itemId);
    if (event.kind === "item/agentMessage/delta" && itemId) {
      const delta = stringValue(payload?.delta);
      if (!delta) {
        return;
      }
      const existing = itemIndexes.get(itemId);
      if (existing === undefined) {
        itemIndexes.set(itemId, messages.length);
        messages.push({
          id: itemId,
          role: "agent",
          kind: "message",
          text: delta,
          timestamp: event.timestamp,
        });
      } else {
        messages[existing] = {
          ...messages[existing],
          text: `${messages[existing].text}${delta}`,
        };
      }
      return;
    }

    if (event.kind !== "item/completed") {
      return;
    }
    const item = record(payload?.item);
    if (!item || (item.type !== "agentMessage" && item.type !== "plan")) {
      return;
    }
    const text = stringValue(item.text);
    const completedId = stringValue(item.id);
    if (!text || !completedId) {
      return;
    }
    const existing = itemIndexes.get(completedId);
    const completed: TimelineMessage = {
      id: completedId,
      role: "agent",
      kind: item.type === "plan" ? "plan" : "message",
      text,
      timestamp: event.timestamp,
    };
    if (existing === undefined) {
      itemIndexes.set(completedId, messages.length);
      messages.push(completed);
    } else {
      messages[existing] = completed;
    }
  });
  return messages;
}

function record(value: unknown): Record<string, unknown> | null {
  return typeof value === "object" && value !== null
    ? (value as Record<string, unknown>)
    : null;
}

function stringValue(value: unknown): string | null {
  return typeof value === "string" && value.length > 0 ? value : null;
}

function formatTime(timestamp: number): string {
  return new Date(timestamp * 1_000).toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
  });
}
