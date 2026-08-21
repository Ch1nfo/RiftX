"use client";

import { CaretDown, Eye, EyeSlash, WarningCircle } from "@phosphor-icons/react";
import { useLanguage } from "@/lib/i18n";
import type { Finding, FindingConfidence, FindingEvidence, FindingPatch } from "@/lib/types";
import { useState } from "react";

const confidenceValues: FindingConfidence[] = ["confirmed", "likely", "suspected", "not_reproducible"];

function confidenceLabel(confidence: FindingConfidence, t: ReturnType<typeof useLanguage>["t"]) {
  return confidence === "confirmed" ? t("confidenceConfirmed") : confidence === "likely" ? t("confidenceLikely") : confidence === "suspected" ? t("confidenceSuspected") : t("confidenceNotReproducible");
}

function screenshotUrl(sessionId: string, screenshotId: string) {
  return `/api/sessions/${sessionId}/findings/screenshot/${encodeURIComponent(screenshotId)}`;
}

function evidenceNode(finding: Finding, evidence: FindingEvidence, sessionId: string | undefined, onToolClick: (toolCallId: string, toolName: string, subagentId?: string) => void, onRequestClick: (requestRef: string, finding: Finding) => void, t: ReturnType<typeof useLanguage>["t"]) {
  if (evidence.type === "quote") return <blockquote><small>{t("quoteEvidence")}</small>{evidence.quote}</blockquote>;
  if (evidence.type === "tool") return <div className="finding-tool-evidence"><button className="finding-tool-link" onClick={() => onToolClick(evidence.toolCallId, evidence.toolName, finding.subagentId)}><small>{t("toolEvidence")}</small>{evidence.toolName}<CaretDown size={12} /></button>{evidence.content ? <pre className="finding-tool-snapshot">{evidence.content}</pre> : null}</div>;
  if (evidence.type === "request") return <button className="finding-request-link" onClick={() => onRequestClick(evidence.requestRef, finding)}><small>{t("requestEvidence")}</small><span>{[evidence.method, evidence.url].filter(Boolean).join(" ") || evidence.requestRef}</span>{evidence.status !== undefined ? <em>{evidence.status}</em> : null}</button>;
  return sessionId ? <a className="finding-screenshot-link" href={screenshotUrl(sessionId, evidence.screenshotId)} target="_blank" rel="noreferrer"><small>{t("screenshotEvidence")}</small><img src={screenshotUrl(sessionId, evidence.screenshotId)} alt={evidence.url ? `${evidence.url} screenshot` : "finding screenshot"} loading="lazy" /></a> : <div className="finding-screenshot-missing"><small>{t("screenshotEvidence")}</small>{evidence.screenshotId}</div>;
}

export function FindingsPanel({ sessionId, findings, onPatch, onToolClick, onRequestClick }: { sessionId?: string; findings: Finding[]; onPatch: (id: string, patch: FindingPatch) => void; onToolClick: (toolCallId: string, toolName: string, subagentId?: string) => void; onRequestClick: (requestRef: string, finding: Finding) => void }) {
  const { t } = useLanguage();
  const [open, setOpen] = useState(true);
  const [showDismissed, setShowDismissed] = useState(false);
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});
  const visible = findings.filter((finding) => showDismissed || finding.status !== "dismissed");
  return <aside className={`findings-panel ${open ? "open" : "collapsed"}`} aria-label={t("findings")}>
    <button className="findings-panel-head" onClick={() => setOpen((value) => !value)}>
      <span><WarningCircle size={15} />{t("findings")}</span>
      <span className="findings-count">{visible.length}<CaretDown size={13} className={open ? "rotated" : ""} /></span>
    </button>
    {open ? <div className="findings-content">
      <button className="findings-filter" onClick={() => setShowDismissed((value) => !value)}>{showDismissed ? <EyeSlash size={13} /> : <Eye size={13} />}{showDismissed ? t("hideDismissed") : t("showDismissed")}</button>
      {visible.length ? visible.map((finding) => {
        const isExpanded = Boolean(expanded[finding.id]);
        return <article className={`finding-item ${finding.status}`} key={finding.id}>
          <button className="finding-head" onClick={() => setExpanded((current) => ({ ...current, [finding.id]: !isExpanded }))}>
            <span className={`finding-confidence-dot ${finding.confidence}`} />
            <span className="finding-title">{finding.title}</span>
            <CaretDown size={12} className={isExpanded ? "rotated" : ""} />
          </button>
          <div className="finding-meta"><span>{confidenceLabel(finding.confidence, t)}</span><span>{finding.source === "main" ? t("sourceMain") : t("sourceSubagent")}</span></div>
          <div className="finding-asset">{finding.asset}</div>
          {isExpanded ? <div className="finding-details">
            {finding.impact ? <section><strong>{t("findingImpact")}</strong><p>{finding.impact}</p></section> : null}
            {finding.reproduction ? <section><strong>{t("findingReproduction")}</strong><p>{finding.reproduction}</p></section> : null}
            {finding.evidence.length ? <section><strong>{t("findingEvidence")}</strong><div className="finding-evidence-list">{finding.evidence.map((evidence, index) => <div key={`${finding.id}-${evidence.type}-${index}`}>{evidenceNode(finding, evidence, sessionId, onToolClick, onRequestClick, t)}</div>)}</div></section> : null}
            <div className="finding-actions"><select className="finding-confidence-select" value={finding.confidence} aria-label={t("confidence")} onChange={(event) => onPatch(finding.id, { id: finding.id, confidence: event.target.value as FindingConfidence })}>{confidenceValues.map((value) => <option value={value} key={value}>{confidenceLabel(value, t)}</option>)}</select><button onClick={() => onPatch(finding.id, { id: finding.id, status: finding.status === "dismissed" ? "open" : "dismissed" })}>{finding.status === "dismissed" ? t("restoreFinding") : t("dismissFinding")}</button></div>
          </div> : null}
        </article>;
      }) : <p className="findings-empty">{t("findingsEmpty")}</p>}
    </div> : null}
  </aside>;
}
