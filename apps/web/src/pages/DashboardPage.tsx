import {
  Activity,
  ArrowRight,
  CheckCircle2,
  Clock3,
  Plus,
  ShieldAlert,
  TerminalSquare,
  Wrench,
} from "lucide-react";
import { Link } from "react-router-dom";
import { Cell, Pie, PieChart, ResponsiveContainer, Tooltip } from "recharts";

import type { Run, RunStatus } from "../api/types";
import { EmptyState } from "../components/EmptyState";
import { ErrorState } from "../components/ErrorState";
import { LoadingState } from "../components/LoadingState";
import { MetricCard } from "../components/MetricCard";
import { StatusBadge } from "../components/StatusBadge";
import { useRuns, useTools } from "../hooks/queries";
import { useI18n } from "../i18n";

const statusColors: Record<RunStatus, string> = {
  created: "#6796ff",
  preparing: "#70a8ff",
  running: "#45d6a4",
  waiting_approval: "#f2b95d",
  paused: "#e4a853",
  completed: "#6ee7b7",
  failed: "#ff6f74",
  cancelled: "#8b9290",
};

export function DashboardPage() {
  const { t } = useI18n();
  const runs = useRuns();
  const tools = useTools("local");

  if (runs.isLoading) {
    return <LoadingState />;
  }
  if (runs.error) {
    return <ErrorState error={runs.error} />;
  }

  const items = runs.data?.items ?? [];
  const active = items.filter((run) =>
    ["created", "preparing", "running", "waiting_approval", "paused"].includes(
      run.status,
    ),
  );
  const waiting = items.filter((run) => run.status === "waiting_approval");
  const completed = items.filter((run) => run.status === "completed");
  const availableTools =
    tools.data?.tools.filter((tool) => tool.state.availability === "available").length ?? 0;
  const chartData = Object.entries(
    items.reduce<Partial<Record<RunStatus, number>>>((counts, run) => {
      counts[run.status] = (counts[run.status] ?? 0) + 1;
      return counts;
    }, {}),
  ).map(([name, value]) => ({ name: name as RunStatus, value }));

  return (
    <div className="page-stack">
      <section className="hero-strip">
        <div>
          <span className="kicker">{t("Durable execution fabric")}</span>
          <h2>{t("Keep every agent run observable, recoverable, and under control.")}</h2>
          <p>
            {t("The browser is a view into persisted state. Closing this page never stops a workflow or its host-native execution.")}
          </p>
        </div>
        <Link className="primary-button" to="/runs/new">
          <Plus size={17} />
          {t("New run")}
        </Link>
      </section>

      <section className="metrics-grid" aria-label={t("Run overview")}>
        <MetricCard
          label="Active runs"
          value={active.length}
          note="Created, running, or paused"
          icon={Activity}
          tone="mint"
        />
        <MetricCard
          label="Waiting approval"
          value={waiting.length}
          note="Human action required"
          icon={ShieldAlert}
          tone="amber"
        />
        <MetricCard
          label="Recently completed"
          value={completed.length}
          note="Persisted successful runs"
          icon={CheckCircle2}
          tone="blue"
        />
        <MetricCard
          label="Available tools"
          value={availableTools}
          note={t("Local registry generation {generation}", { generation: tools.data?.generation ?? "—" })}
          icon={Wrench}
        />
      </section>

      <section className="dashboard-grid">
        <article className="panel run-list-panel">
          <div className="panel-header">
            <div>
              <span className="panel-kicker">{t("Live operations")}</span>
              <h3>{t("Active run queue")}</h3>
            </div>
            <Link className="text-link" to="/runs/new">
              {t("Configure run")} <ArrowRight size={15} />
            </Link>
          </div>
          {active.length ? (
            <div className="run-list">
              {active.slice(0, 6).map((run) => (
                <RunRow key={run.id} run={run} />
              ))}
            </div>
          ) : (
            <EmptyState icon={TerminalSquare} title="No active runs">
              {t("Launch a run to populate the durable operation queue.")}
            </EmptyState>
          )}
        </article>

        <article className="panel status-panel">
          <div className="panel-header">
            <div>
              <span className="panel-kicker">{t("Portfolio signal")}</span>
              <h3>{t("Run status mix")}</h3>
            </div>
            <span className="muted-caption">{t("{count} total", { count: items.length })}</span>
          </div>
          {chartData.length ? (
            <>
              <div className="status-chart" aria-label={t("Run status chart")}>
                <ResponsiveContainer width="100%" height="100%">
                  <PieChart>
                    <Pie
                      data={chartData}
                      dataKey="value"
                      nameKey="name"
                      innerRadius={56}
                      outerRadius={79}
                      paddingAngle={3}
                      stroke="transparent"
                    >
                      {chartData.map((entry) => (
                        <Cell key={entry.name} fill={statusColors[entry.name]} />
                      ))}
                    </Pie>
                    <Tooltip
                      contentStyle={{
                        background: "var(--surface-raised)",
                        border: "1px solid var(--line)",
                        borderRadius: "10px",
                        color: "var(--text)",
                      }}
                    />
                  </PieChart>
                </ResponsiveContainer>
                <div className="chart-center">
                  <strong>{items.length}</strong>
                  <span>{t("runs")}</span>
                </div>
              </div>
              <div className="chart-legend">
                {chartData.map((entry) => (
                  <div key={entry.name}>
                    <span style={{ backgroundColor: statusColors[entry.name] }} />
                    <p>{t(entry.name.replaceAll("_", " "))}</p>
                    <strong>{entry.value}</strong>
                  </div>
                ))}
              </div>
            </>
          ) : (
            <EmptyState icon={Clock3} title="No status history">
              {t("Status distribution appears after the first run is created.")}
            </EmptyState>
          )}
        </article>
      </section>

      <section className="panel tool-health-strip">
        <div>
          <span className="panel-kicker">{t("Execution node")}</span>
          <h3>{t("Local tool health")}</h3>
          <p>
            {tools.isError
              ? t("Registry health could not be loaded.")
              : t("{available} of {total} configured tools are available.", {
                  available: availableTools,
                  total: tools.data?.tools.length ?? 0,
                })}
          </p>
        </div>
        <div className="tool-health-actions">
          <span className="mono-chip">node / local</span>
          <Link className="secondary-button" to="/tools">
            {t("Inspect registry")} <ArrowRight size={15} />
          </Link>
        </div>
      </section>
    </div>
  );
}

function RunRow({ run }: { run: Run }) {
  const { language } = useI18n();
  return (
    <Link className="run-row" to={`/runs/${run.id}`}>
      <div className="run-row-icon">
        <TerminalSquare size={18} />
      </div>
      <div className="run-row-main">
        <strong>{run.objective.description}</strong>
        <span>
          {shortId(run.id)} · {formatRelative(run.created_at, language)} · {run.node_id}
        </span>
      </div>
      <StatusBadge status={run.status} />
      <ArrowRight className="row-arrow" size={16} />
    </Link>
  );
}

function shortId(id: string) {
  return id.slice(0, 8);
}

function formatRelative(value: string, language: "en" | "zh-CN") {
  const elapsed = Date.now() - new Date(value).getTime();
  const minutes = Math.max(0, Math.round(elapsed / 60_000));
  const formatter = new Intl.RelativeTimeFormat(language, { numeric: "auto" });
  if (minutes < 1) return formatter.format(0, "minute");
  if (minutes < 60) return formatter.format(-minutes, "minute");
  const hours = Math.round(minutes / 60);
  if (hours < 24) return formatter.format(-hours, "hour");
  return formatter.format(-Math.round(hours / 24), "day");
}
