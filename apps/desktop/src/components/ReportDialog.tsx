import { FileText, X } from "lucide-react";
import { useEffect, useState } from "react";
import type { EngagementReport } from "../models";

type ReportTab = "overview" | "findings" | "evidence" | "markdown";

interface ReportDialogProps {
  open: boolean;
  report: EngagementReport | null;
  markdown: string | null;
  loading: boolean;
  onClose: () => void;
}

export function ReportDialog({
  open,
  report,
  markdown,
  loading,
  onClose,
}: ReportDialogProps) {
  const [tab, setTab] = useState<ReportTab>("overview");

  useEffect(() => {
    if (open) {
      setTab("overview");
    }
  }, [open, report?.engagement.id]);

  if (!open) {
    return null;
  }

  return (
    <div className="dialog-backdrop" role="presentation">
      <section
        className="report-dialog"
        role="dialog"
        aria-modal="true"
        aria-label="Engagement report"
      >
        <header className="dialog-heading">
          <div>
            <span>REPORT</span>
            <h2>{report?.engagement.name ?? "Loading report"}</h2>
          </div>
          <button
            type="button"
            className="icon-button"
            aria-label="Close report"
            title="Close"
            onClick={onClose}
          >
            <X size={16} />
          </button>
        </header>

        <div className="report-tabs" role="tablist" aria-label="Report view">
          {(["overview", "findings", "evidence", "markdown"] as ReportTab[]).map(
            (item) => (
              <button
                type="button"
                role="tab"
                aria-selected={tab === item}
                className={tab === item ? "active" : ""}
                onClick={() => setTab(item)}
                key={item}
              >
                {item}
              </button>
            ),
          )}
        </div>

        <div className="report-content">
          {loading && !report ? (
            <div className="report-empty">
              <FileText size={20} />
              <span>Loading report...</span>
            </div>
          ) : report ? (
            <>
              {tab === "overview" && <ReportOverview report={report} />}
              {tab === "findings" && <ReportFindings report={report} />}
              {tab === "evidence" && <ReportEvidence report={report} />}
              {tab === "markdown" && (
                <pre className="markdown-report">
                  {markdown ?? "Loading Markdown report..."}
                </pre>
              )}
            </>
          ) : (
            <div className="report-empty">
              <FileText size={20} />
              <span>Report unavailable.</span>
            </div>
          )}
        </div>
      </section>
    </div>
  );
}

function ReportOverview({ report }: { report: EngagementReport }) {
  const engagement = report.engagement;
  return (
    <div className="report-overview">
      <div className="report-summary">
        <div>
          <span>Mode</span>
          <strong>{engagement.mode}</strong>
        </div>
        <div>
          <span>Status</span>
          <strong>{engagement.status}</strong>
        </div>
        <div>
          <span>Assets</span>
          <strong>{report.assets.length}</strong>
        </div>
        <div>
          <span>Findings</span>
          <strong>{report.findings.length}</strong>
        </div>
        <div>
          <span>Evidence</span>
          <strong>{report.evidence.length}</strong>
        </div>
      </div>

      <section>
        <h3>Objective</h3>
        <p>{engagement.objective.summary}</p>
        <ul>
          {engagement.objective.successCriteria.map((criterion) => (
            <li key={criterion}>{criterion}</li>
          ))}
        </ul>
      </section>

      <section className="report-columns">
        <div>
          <h3>Authorization</h3>
          <dl>
            <div>
              <dt>Environment</dt>
              <dd>{engagement.authorization.environment}</dd>
            </div>
            <div>
              <dt>CIDRs</dt>
              <dd>{engagement.authorization.network.cidrs.length}</dd>
            </div>
            <div>
              <dt>Domains</dt>
              <dd>{engagement.authorization.network.domains.length}</dd>
            </div>
            <div>
              <dt>Capabilities</dt>
              <dd>{engagement.authorization.capabilities.length}</dd>
            </div>
          </dl>
        </div>
        <div>
          <h3>Execution record</h3>
          <dl>
            <div>
              <dt>Executions</dt>
              <dd>{report.executions.length}</dd>
            </div>
            <div>
              <dt>Tasks</dt>
              <dd>{report.tasks.length}</dd>
            </div>
            <div>
              <dt>Artifacts</dt>
              <dd>{report.artifacts.length}</dd>
            </div>
            <div>
              <dt>Attack paths</dt>
              <dd>{report.attackPaths.length}</dd>
            </div>
          </dl>
        </div>
      </section>

      <section>
        <h3>Immutable extension snapshots</h3>
        <div className="snapshot-manifest">
          <div>
            <span>Tools · {report.toolSnapshot.tools.length}</span>
            <code>{report.toolSnapshot.snapshotSha256}</code>
          </div>
          <div>
            <span>Skills · {report.skillSnapshot.skills.length}</span>
            <code>{report.skillSnapshot.snapshotSha256}</code>
          </div>
          <div>
            <span>Policy</span>
            <code>{engagement.policyRevision}</code>
          </div>
        </div>
      </section>
    </div>
  );
}

function ReportFindings({ report }: { report: EngagementReport }) {
  if (report.findings.length === 0) {
    return <div className="report-empty">No validated findings recorded.</div>;
  }
  return (
    <div className="report-list">
      {report.findings.map((finding) => (
        <article key={finding.id}>
          <header>
            <strong>{finding.title}</strong>
            <span className={`severity ${finding.severity}`}>
              {finding.severity}
            </span>
          </header>
          <p>{finding.description}</p>
        </article>
      ))}
    </div>
  );
}

function ReportEvidence({ report }: { report: EngagementReport }) {
  return (
    <div className="report-evidence">
      <section>
        <h3>Evidence</h3>
        {report.evidence.length === 0 ? (
          <p>No evidence recorded.</p>
        ) : (
          report.evidence.map((evidence) => (
            <div className="manifest-row" key={evidence.id}>
              <span>{evidence.summary}</span>
              <code>{evidence.id}</code>
            </div>
          ))
        )}
      </section>
      <section>
        <h3>Artifact hash manifest</h3>
        {report.artifacts.length === 0 ? (
          <p>No artifacts recorded.</p>
        ) : (
          report.artifacts.map((artifact) => (
            <div className="manifest-row" key={artifact.id}>
              <span>{artifact.path}</span>
              <code>{artifact.sha256}</code>
            </div>
          ))
        )}
      </section>
    </div>
  );
}
