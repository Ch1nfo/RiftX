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
          <span className="kicker">Durable execution fabric</span>
          <h2>Keep every agent run observable, recoverable, and under control.</h2>
          <p>
            The browser is a view into persisted state. Closing this page never stops a
            workflow or its host-native execution.
          </p>
        </div>
        <Link className="primary-button" to="/runs/new">
          <Plus size={17} />
          New run
        </Link>
      </section>

      <section className="metrics-grid" aria-label="Run overview">
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
          note={`Local registry generation ${tools.data?.generation ?? "—"}`}
          icon={Wrench}
        />
      </section>

      <section className="dashboard-grid">
        <article className="panel run-list-panel">
          <div className="panel-header">
            <div>
              <span className="panel-kicker">Live operations</span>
              <h3>Active run queue</h3>
            </div>
            <Link className="text-link" to="/runs/new">
              Configure run <ArrowRight size={15} />
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
              Launch a run to populate the durable operation queue.
            </EmptyState>
          )}
        </article>

        <article className="panel status-panel">
          <div className="panel-header">
            <div>
              <span className="panel-kicker">Portfolio signal</span>
              <h3>Run status mix</h3>
            </div>
            <span className="muted-caption">{items.length} total</span>
          </div>
          {chartData.length ? (
            <>
              <div className="status-chart" aria-label="Run status chart">
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
                        background: "#0c1916",
                        border: "1px solid #213c35",
                        borderRadius: "10px",
                      }}
                    />
                  </PieChart>
                </ResponsiveContainer>
                <div className="chart-center">
                  <strong>{items.length}</strong>
                  <span>runs</span>
                </div>
              </div>
              <div className="chart-legend">
                {chartData.map((entry) => (
                  <div key={entry.name}>
                    <span style={{ backgroundColor: statusColors[entry.name] }} />
                    <p>{entry.name.replaceAll("_", " ")}</p>
                    <strong>{entry.value}</strong>
                  </div>
                ))}
              </div>
            </>
          ) : (
            <EmptyState icon={Clock3} title="No status history">
              Status distribution appears after the first run is created.
            </EmptyState>
          )}
        </article>
      </section>

      <section className="panel tool-health-strip">
        <div>
          <span className="panel-kicker">Execution node</span>
          <h3>Local tool health</h3>
          <p>
            {tools.isError
              ? "Registry health could not be loaded."
              : `${availableTools} of ${tools.data?.tools.length ?? 0} configured tools are available.`}
          </p>
        </div>
        <div className="tool-health-actions">
          <span className="mono-chip">node / local</span>
          <Link className="secondary-button" to="/tools">
            Inspect registry <ArrowRight size={15} />
          </Link>
        </div>
      </section>
    </div>
  );
}

function RunRow({ run }: { run: Run }) {
  return (
    <Link className="run-row" to={`/runs/${run.id}`}>
      <div className="run-row-icon">
        <TerminalSquare size={18} />
      </div>
      <div className="run-row-main">
        <strong>{run.objective.description}</strong>
        <span>
          {shortId(run.id)} · {formatRelative(run.created_at)} · {run.node_id}
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

function formatRelative(value: string) {
  const elapsed = Date.now() - new Date(value).getTime();
  const minutes = Math.max(0, Math.round(elapsed / 60_000));
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  return `${Math.round(hours / 24)}d ago`;
}
