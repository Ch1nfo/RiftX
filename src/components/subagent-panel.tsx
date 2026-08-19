"use client";

import { ArrowsClockwise, CaretDown, CircleNotch, Stop, UsersThree, X } from "@phosphor-icons/react";
import { useLanguage } from "@/lib/i18n";
import type { SubagentStatus, SubagentTask } from "@/lib/types";
import { useState } from "react";

const statusLabels: Record<SubagentStatus, { zh: string; en: string }> = {
  queued: { zh: "排队", en: "Queued" },
  running: { zh: "运行中", en: "Running" },
  completed: { zh: "完成", en: "Completed" },
  failed: { zh: "失败", en: "Failed" },
  cancelled: { zh: "已取消", en: "Cancelled" },
  interrupted: { zh: "已中断", en: "Interrupted" }
};

function statusLabel(status: SubagentStatus, language: "zh" | "en") {
  return statusLabels[status][language];
}

export function SubagentPanel({ tasks, running, maxConcurrent, onCancel, onRetry }: { tasks: SubagentTask[]; running: number; maxConcurrent: number; onCancel: (taskId: string) => void; onRetry: (taskId: string) => void }) {
  const { language, t } = useLanguage();
  const [open, setOpen] = useState(true);
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});
  if (!tasks.length) return null;
  return <aside className={`subagent-panel ${open ? "open" : "collapsed"}`} aria-label={t("subagents")}>
    <button className="subagent-panel-head" onClick={() => setOpen((value) => !value)}>
      <span><UsersThree size={15} />{t("subagents")}</span>
      <span className="subagent-count">{running} / {maxConcurrent || "—"}<CaretDown size={13} className={open ? "rotated" : ""} /></span>
    </button>
    {open ? <div className="subagent-list">{tasks.slice().reverse().map((task) => {
      const isExpanded = Boolean(expanded[task.id]);
      const active = task.status === "queued" || task.status === "running";
      return <article className={`subagent-item ${task.status}`} key={task.id}>
        <button className="subagent-item-head" onClick={() => setExpanded((current) => ({ ...current, [task.id]: !isExpanded }))}>
          <span className="subagent-item-title"><span className={`subagent-status-dot ${task.status}`}>{task.status === "running" ? <CircleNotch size={12} className="spin" /> : null}</span><strong>{task.name}</strong></span>
          <span className="subagent-status">{statusLabel(task.status, language)}<CaretDown size={12} className={isExpanded ? "rotated" : ""} /></span>
        </button>
        <p className="subagent-task-summary">{task.task}</p>
        <div className="subagent-meta"><span>{task.model || t("loadingSubagentModel")}</span>{task.pendingApprovalCount ? <span className="subagent-approval-count">{t("approvalCount", { count: String(task.pendingApprovalCount) })}</span> : null}</div>
        {isExpanded ? <div className="subagent-details">
          {task.logs.length ? <div className="subagent-logs">{task.logs.map((log) => <details className={`subagent-log-card ${log.type}`} key={log.id}>
            <summary className="subagent-log-head"><span className="subagent-log-head-main"><span className="subagent-log-type">{log.type === "tool" ? log.toolName : log.type}</span>{log.status ? <span className={`subagent-log-status ${log.status}`}>{log.status === "running" ? t("running") : log.status === "error" ? t("failed") : t("complete")}</span> : null}</span><CaretDown size={12} className="subagent-log-caret" /></summary>
            <pre>{log.content}</pre>
          </details>)}</div> : null}
          {task.summary ? <details className="subagent-summary subagent-fold">
            <summary><strong>{t("finalSummary")}</strong><CaretDown size={12} className="subagent-log-caret" /></summary>
            <p>{task.summary}</p>
          </details> : null}
          {task.error ? <details className="subagent-error subagent-fold">
            <summary><strong>{t("errorLabel")}</strong><CaretDown size={12} className="subagent-log-caret" /></summary>
            <p>{task.error}</p>
          </details> : null}
          <div className="subagent-actions">{active ? <button className="subagent-action danger" onClick={() => onCancel(task.id)} title={t("stop")}><Stop size={14} />{t("stop")}</button> : null}{!active && task.status !== "completed" ? <button className="subagent-action" onClick={() => onRetry(task.id)} title={t("retry")}><ArrowsClockwise size={14} />{t("retry")}</button> : null}</div>
        </div> : null}
      </article>;
    })}</div> : null}
    {!open ? <X size={13} className="subagent-panel-collapsed-icon" /> : null}
  </aside>;
}
