import { FileSearch, FileWarning, Loader2, MapPin, ShieldX } from "lucide-react";
import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";

import type {
  LocalAuditFinding,
  LocalAuditFindingSeverity,
  LocalAuditJob,
} from "../api/types";
import { EmptyState } from "../components/EmptyState";
import { ErrorState } from "../components/ErrorState";
import { LoadingState } from "../components/LoadingState";
import { PixelIcon } from "../components/PixelIcon";
import { StatusBadge } from "../components/StatusBadge";
import {
  useLocalAudit,
  useLocalAuditControl,
  useLocalAuditFinding,
  useLocalAuditFindings,
  useLocalAuditSeveritySummary,
} from "../hooks/queries";
import { useI18n } from "../i18n";

const TERMINAL_STATUSES = new Set(["completed", "failed", "cancelled"]);

export function LocalAuditDetailPage() {
  const { auditId = "" } = useParams();
  const { t } = useI18n();
  const audit = useLocalAudit(auditId);
  const controls = useLocalAuditControl(auditId);
  const completed = audit.data?.status === "completed";
  const findings = useLocalAuditFindings(auditId, completed);
  const severitySummary = useLocalAuditSeveritySummary(auditId, completed);
  const findingItems = findings.data?.pages.flatMap((page) => page.items) ?? [];
  const findingTotal = findings.data?.pages[0]?.total ?? 0;
  const [selectedFindingId, setSelectedFindingId] = useState("");
  const selectedFinding = useLocalAuditFinding(auditId, selectedFindingId);

  useEffect(() => {
    const first = findingItems[0]?.finding_id ?? "";
    if (!selectedFindingId && first) setSelectedFindingId(first);
  }, [findingItems, selectedFindingId]);

  if (audit.isLoading) return <LoadingState label="Loading local audit" />;
  if (audit.error) return <ErrorState error={audit.error} />;
  if (!audit.data) return null;

  const job = audit.data;
  const terminal = TERMINAL_STATUSES.has(job.status);

  return (
    <div className="page-stack">
      <header className="detail-heading">
        <div>
          <Link className="back-link" to="/audits/new">
            ← {t("New local audit")}
          </Link>
          <div className="detail-title-row">
            <h2>{t("Local code audit")}</h2>
            <StatusBadge status={job.status} />
          </div>
          <div className="run-identity">
            <span>{job.audit_id}</span>
            <span>{t("Same-machine static scan")}</span>
            <span>{t("{count} findings", { count: job.finding_count })}</span>
          </div>
        </div>
        {!terminal ? (
          <div className="control-cluster">
            <button
              type="button"
              className="danger-button"
              disabled={controls.cancel.isPending}
              onClick={() => controls.cancel.mutate()}
            >
              {controls.cancel.isPending ? (
                <Loader2 className="spin" size={16} />
              ) : (
                <ShieldX size={16} />
              )}
              {t("Cancel audit")}
            </button>
          </div>
        ) : null}
      </header>

      {controls.cancel.error ? <ErrorState error={controls.cancel.error} /> : null}

      <div className="detail-layout local-audit-detail-layout">
        <main className="detail-main panel">
          <div className="panel-header local-audit-findings-head">
            <div>
              <h3>{t("Security findings")}</h3>
              <span className="panel-coordinate">
                {t("Built-in deterministic rules")}
              </span>
            </div>
            <span className="mono-chip">
              {completed
                ? t("{count} findings", { count: findingTotal })
                : t(job.status.replaceAll("_", " "))}
            </span>
          </div>
          <div className="local-audit-findings-body">
            {completed ? (
              <SeveritySummary
                counts={severitySummary.data}
                loading={severitySummary.isLoading}
                error={severitySummary.error}
              />
            ) : null}
            <FindingContent
              job={job}
              findings={findingItems}
              total={findingTotal}
              loading={findings.isLoading}
              loadingMore={findings.isFetchingNextPage}
              error={findings.error}
              hasMore={findings.hasNextPage}
              selectedFindingId={selectedFindingId}
              onSelect={setSelectedFindingId}
              onLoadMore={() => void findings.fetchNextPage()}
            />
          </div>
        </main>

        <aside className="detail-sidebar">
          <section className="panel compact-panel">
            <div className="panel-header">
              <h3>{t("Audit status")}</h3>
            </div>
            <AuditFacts job={job} />
          </section>

          <section className="panel compact-panel local-finding-inspector">
            <div className="panel-header">
              <h3>{t("Finding detail")}</h3>
            </div>
            <FindingInspector
              finding={selectedFinding.data}
              loading={selectedFinding.isLoading}
              error={selectedFinding.error}
            />
          </section>
        </aside>
      </div>
    </div>
  );
}

function FindingContent({
  job,
  findings,
  total,
  loading,
  loadingMore,
  error,
  hasMore,
  selectedFindingId,
  onSelect,
  onLoadMore,
}: {
  job: LocalAuditJob;
  findings: LocalAuditFinding[];
  total: number;
  loading: boolean;
  loadingMore: boolean;
  error: Error | null;
  hasMore: boolean;
  selectedFindingId: string;
  onSelect: (findingId: string) => void;
  onLoadMore: () => void;
}) {
  const { t } = useI18n();
  if (error) return <ErrorState error={error} />;
  if (!TERMINAL_STATUSES.has(job.status)) {
    return (
      <div className="local-audit-progress" role="status">
        <span className="loading-spinner" aria-hidden="true" />
        <div>
          <strong>{t(job.status === "scanning" ? "Scanning sealed snapshot" : "Waiting for local scanner")}</strong>
          <p>{t("This page refreshes from durable local Audit state. Closing it does not cancel the scan.")}</p>
        </div>
      </div>
    );
  }
  if (job.status === "failed") {
    return (
      <EmptyState icon={ShieldX} title="Local audit failed">
        {t("The scan stopped with failure code {code}.", {
          code: job.failure_code ?? "local_audit_failed",
        })}
      </EmptyState>
    );
  }
  if (job.status === "cancelled") {
    return (
      <EmptyState icon={ShieldX} title="Local audit cancelled">
        {t("The scan was cancelled before Findings were published.")}
      </EmptyState>
    );
  }
  if (loading) return <LoadingState label="Loading local audit findings" />;
  if (!findings.length) {
    return (
      <EmptyState icon={FileSearch} title="No security findings">
        {t("The built-in rules did not report a matching issue in the scanned files.")}
      </EmptyState>
    );
  }
  return (
    <div className="local-audit-finding-results">
      <div className="local-audit-finding-list">
        {findings.map((finding) => (
          <button
            type="button"
            key={finding.finding_id}
            className={`local-audit-finding-row ${
              finding.finding_id === selectedFindingId ? "selected" : ""
            }`}
            onClick={() => onSelect(finding.finding_id)}
            aria-pressed={finding.finding_id === selectedFindingId}
          >
            <span className={`severity-marker severity-${finding.severity}`} />
            <span className="local-audit-finding-copy">
              <span className="finding-head">
                <strong className={`severity-label severity-${finding.severity}`}>
                  {t(finding.severity)}
                </strong>
                <span>{finding.category}</span>
                <span>{Math.round(finding.confidence * 100)}%</span>
              </span>
              <strong>{finding.title}</strong>
              <small>
                {finding.relative_path}:{finding.line}:{finding.column}
              </small>
            </span>
            <PixelIcon name="chevron" />
          </button>
        ))}
      </div>
      {hasMore ? (
        <button
          type="button"
          className="secondary-button local-audit-load-more"
          disabled={loadingMore}
          onClick={onLoadMore}
        >
          {loadingMore ? <Loader2 className="spin" size={16} /> : null}
          {t("Load more findings")}
          <span>{findings.length} / {total}</span>
        </button>
      ) : null}
    </div>
  );
}

function SeveritySummary({
  counts,
  loading,
  error,
}: {
  counts: Record<LocalAuditFindingSeverity, number> | undefined;
  loading: boolean;
  error: Error | null;
}) {
  const { t } = useI18n();
  if (error) return <ErrorState error={error} />;
  const severities: LocalAuditFindingSeverity[] = [
    "critical",
    "high",
    "medium",
    "low",
    "info",
  ];
  return (
    <section className="local-audit-severity-summary" aria-label={t("Severity summary")}>
      {severities.map((severity) => (
        <div key={severity}>
          <span className={`severity-marker severity-${severity}`} />
          <span>{t(severity)}</span>
          <strong>{loading ? "—" : (counts?.[severity] ?? 0)}</strong>
        </div>
      ))}
    </section>
  );
}

function AuditFacts({ job }: { job: LocalAuditJob }) {
  const { t } = useI18n();
  return (
    <dl className="fact-list">
      <div><dt>{t("Status")}</dt><dd><StatusBadge status={job.status} /></dd></div>
      <div><dt>{t("Files")}</dt><dd>{job.scanned_files} / {job.total_files}</dd></div>
      <div><dt>{t("Findings")}</dt><dd>{job.finding_count}</dd></div>
      <div><dt>{t("Created")}</dt><dd>{formatDate(job.created_at)}</dd></div>
      <div><dt>{t("Started")}</dt><dd>{job.started_at ? formatDate(job.started_at) : "—"}</dd></div>
      <div><dt>{t("Finished")}</dt><dd>{job.finished_at ? formatDate(job.finished_at) : "—"}</dd></div>
      {job.report_digest ? (
        <div><dt>{t("Report")}</dt><dd><code>{shortDigest(job.report_digest)}</code></dd></div>
      ) : null}
    </dl>
  );
}

function FindingInspector({
  finding,
  loading,
  error,
}: {
  finding: LocalAuditFinding | undefined;
  loading: boolean;
  error: Error | null;
}) {
  const { t } = useI18n();
  if (error) return <ErrorState error={error} />;
  if (loading) return <LoadingState label="Loading finding detail" />;
  if (!finding) {
    return (
      <div className="local-finding-empty">
        <FileWarning size={22} />
        <p>{t("Select a Finding to inspect its rule, location, and redacted evidence.")}</p>
      </div>
    );
  }
  return (
    <div className="local-finding-detail">
      <div className="local-finding-detail-title">
        <span className={`severity-label severity-${finding.severity}`}>
          {t(finding.severity)}
        </span>
        <h4>{finding.title}</h4>
      </div>
      <dl>
        <div><dt>{t("Rule")}</dt><dd><code>{finding.rule_id}@{finding.rule_version}</code></dd></div>
        <div><dt>{t("Confidence")}</dt><dd>{Math.round(finding.confidence * 100)}%</dd></div>
        <div><dt>{t("Location")}</dt><dd><MapPin size={13} /> <code>{finding.relative_path}:{finding.line}:{finding.column}</code></dd></div>
      </dl>
      <div className="local-finding-evidence">
        <strong>{t("Redacted evidence")}</strong>
        <pre>{finding.evidence_excerpt}</pre>
      </div>
    </div>
  );
}

function formatDate(value: string) {
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(new Date(value));
}

function shortDigest(value: string) {
  return `${value.slice(0, 10)}…${value.slice(-8)}`;
}
