import {
  AlertTriangle,
  CheckCircle2,
  FileCode2,
  Loader2,
  RefreshCw,
  ServerCog,
  ShieldCheck,
  Wrench,
} from "lucide-react";

import { EmptyState } from "../components/EmptyState";
import { ErrorState } from "../components/ErrorState";
import { LoadingState } from "../components/LoadingState";
import { MetricCard } from "../components/MetricCard";
import { StatusBadge } from "../components/StatusBadge";
import { useRefreshTools, useTools } from "../hooks/queries";

export function ToolsPage() {
  const nodeId = "local";
  const tools = useTools(nodeId);
  const refresh = useRefreshTools(nodeId);

  if (tools.isLoading) return <LoadingState label="Probing local tools" />;
  if (tools.error) return <ErrorState error={tools.error} />;
  if (!tools.data) return null;

  const available = tools.data.tools.filter(
    (tool) => tool.state.availability === "available",
  ).length;
  const unhealthy = tools.data.tools.filter((tool) =>
    ["unavailable", "misconfigured"].includes(tool.state.availability),
  ).length;
  const capabilities = new Set(
    tools.data.tools.flatMap((tool) => tool.definition.capabilities),
  ).size;

  return (
    <div className="page-stack">
      <section className="hero-strip compact-hero">
        <div>
          <span className="kicker">Node-local capability map</span>
          <h2>Know what the Agent can actually execute.</h2>
          <p>
            The registry reflects resolved host paths and version probes. Unavailable tools
            are excluded from the Agent-visible snapshot.
          </p>
        </div>
        <button
          className="primary-button"
          onClick={() => refresh.mutate()}
          disabled={refresh.isPending}
        >
          {refresh.isPending ? (
            <Loader2 className="spin" size={17} />
          ) : (
            <RefreshCw size={17} />
          )}
          Refresh registry
        </button>
      </section>

      {refresh.error ? <ErrorState error={refresh.error} /> : null}

      <section className="metrics-grid tool-metrics">
        <MetricCard
          label="Available"
          value={available}
          note="Ready for Agent execution"
          icon={CheckCircle2}
          tone="mint"
        />
        <MetricCard
          label="Needs attention"
          value={unhealthy}
          note="Unavailable or misconfigured"
          icon={AlertTriangle}
          tone="amber"
        />
        <MetricCard
          label="Capabilities"
          value={capabilities}
          note="Distinct Agent capability labels"
          icon={ShieldCheck}
          tone="blue"
        />
        <MetricCard
          label="Generation"
          value={tools.data.generation}
          note={tools.data.execution_policy.replaceAll("_", " ")}
          icon={ServerCog}
        />
      </section>

      <section className="panel registry-panel">
        <div className="panel-header">
          <div>
            <span className="panel-kicker">tools.yaml snapshot</span>
            <h3>Configured tools</h3>
          </div>
          <span className="mono-chip">digest / {tools.data.source_digest.slice(0, 10)}</span>
        </div>

        {tools.data.tools.length ? (
          <div className="tool-table-wrap">
            <table className="tool-table">
              <thead>
                <tr>
                  <th>Tool</th>
                  <th>Status</th>
                  <th>Resolved command</th>
                  <th>Version</th>
                  <th>Capabilities</th>
                  <th>Approval</th>
                </tr>
              </thead>
              <tbody>
                {tools.data.tools.map((tool) => (
                  <tr key={tool.definition.id}>
                    <td>
                      <div className="tool-name-cell">
                        <span>
                          {tool.definition.executor === "pty" ? (
                            <FileCode2 size={17} />
                          ) : (
                            <Wrench size={17} />
                          )}
                        </span>
                        <div>
                          <strong>{tool.definition.id}</strong>
                          <small>{tool.definition.executor}</small>
                        </div>
                      </div>
                    </td>
                    <td>
                      <StatusBadge status={tool.state.availability} />
                      {tool.state.reason ? (
                        <small className="tool-reason">{tool.state.reason}</small>
                      ) : null}
                    </td>
                    <td>
                      <code>{tool.state.resolved_command ?? tool.definition.command[0]}</code>
                    </td>
                    <td>{tool.state.version ?? "—"}</td>
                    <td>
                      <div className="capability-list">
                        {tool.definition.capabilities.map((capability) => (
                          <span key={capability}>{capability.replaceAll("_", " ")}</span>
                        ))}
                      </div>
                    </td>
                    <td>{tool.definition.approval_level}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <EmptyState icon={Wrench} title="No tools configured">
            Add definitions to tools.yaml and refresh the registry.
          </EmptyState>
        )}
      </section>
    </div>
  );
}
