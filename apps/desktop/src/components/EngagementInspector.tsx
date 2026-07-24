import { ChevronRight, FileText, ShieldCheck } from "lucide-react";
import type {
  Engagement,
  EngagementReport,
  ExecutionMode,
} from "../models";
import { ModeControl } from "./ModeControl";

interface EngagementInspectorProps {
  engagement: Engagement | null;
  report: EngagementReport | null;
  modeBusy: boolean;
  modeBlocked: boolean;
  onModeChange: (mode: ExecutionMode, confirmation: string | null) => void;
  onOpenReport: () => void;
}

export function EngagementInspector({
  engagement,
  report,
  modeBusy,
  modeBlocked,
  onModeChange,
  onOpenReport,
}: EngagementInspectorProps) {
  if (!engagement) {
    return (
      <aside className="inspector empty-inspector">
        <ShieldCheck size={22} />
        <p>No task selected.</p>
      </aside>
    );
  }

  const network = engagement.authorization.network;
  return (
    <aside className="inspector" aria-label="Engagement details">
      <div className="inspector-heading">
        <span>Engagement</span>
        <div className="inspector-heading-actions">
          <strong>{engagement.status}</strong>
          <button
            type="button"
            className="icon-button"
            aria-label="Open report"
            title="Report"
            onClick={onOpenReport}
          >
            <FileText size={15} />
          </button>
        </div>
      </div>

      <section>
        <h3>Objective</h3>
        <p>{engagement.objective.summary}</p>
        {engagement.objective.successCriteria.map((criterion) => (
          <div className="criterion" key={criterion}>
            <ChevronRight size={14} />
            <span>{criterion}</span>
          </div>
        ))}
      </section>

      <ModeControl
        engagement={engagement}
        busy={modeBusy}
        blocked={modeBlocked}
        onChange={onModeChange}
      />

      <section>
        <h3>Scope</h3>
        <dl className="scope-list">
          <div>
            <dt>Entry points</dt>
            <dd>{engagement.entryPoints.length}</dd>
          </div>
          <div>
            <dt>CIDRs</dt>
            <dd>{network.cidrs.length}</dd>
          </div>
          <div>
            <dt>Domains</dt>
            <dd>{network.domains.length}</dd>
          </div>
          <div>
            <dt>Environment</dt>
            <dd>{engagement.authorization.environment}</dd>
          </div>
          <div>
            <dt>LLM profile</dt>
            <dd>{engagement.llmProfile}</dd>
          </div>
        </dl>
        <div className="scope-values">
          {[
            ...engagement.entryPoints,
            ...network.cidrs,
            ...network.domains,
          ].map((value) => (
            <code key={value}>{value}</code>
          ))}
        </div>
      </section>

      <section>
        <h3>Current state</h3>
        <dl className="metric-grid">
          <div>
            <dt>Assets</dt>
            <dd>{report?.assets.length ?? 0}</dd>
          </div>
          <div>
            <dt>Services</dt>
            <dd>{report?.services.length ?? 0}</dd>
          </div>
          <div>
            <dt>Findings</dt>
            <dd>{report?.findings.length ?? 0}</dd>
          </div>
          <div>
            <dt>Evidence</dt>
            <dd>{report?.evidence.length ?? 0}</dd>
          </div>
        </dl>
      </section>

      <section>
        <h3>Capabilities</h3>
        <div className="capability-list">
          {engagement.authorization.capabilities.map((capability) => (
            <span key={capability}>{capability}</span>
          ))}
        </div>
      </section>

      <footer className="policy-revision">
        <span>Policy revision</span>
        <code>{engagement.policyRevision.slice(0, 12)}</code>
      </footer>
    </aside>
  );
}
