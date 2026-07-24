import {
  AlertTriangle,
  CheckCircle2,
  FileKey2,
  Search,
  TerminalSquare,
} from "lucide-react";
import type { Engagement, EngagementReport } from "../models";

interface ActivityTimelineProps {
  engagement: Engagement;
  report: EngagementReport | null;
  loading: boolean;
}

export function ActivityTimeline({
  engagement,
  report,
  loading,
}: ActivityTimelineProps) {
  const hasActivity =
    report &&
    (report.tasks.length > 0 ||
      report.executions.length > 0 ||
      report.findings.length > 0 ||
      report.evidence.length > 0);

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
