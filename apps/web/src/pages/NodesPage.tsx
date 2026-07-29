import {
  AlertTriangle,
  CheckCircle2,
  Cpu,
  Radio,
  Server,
  ServerOff,
} from "lucide-react";

import { EmptyState } from "../components/EmptyState";
import { ErrorState } from "../components/ErrorState";
import { LoadingState } from "../components/LoadingState";
import { MetricCard } from "../components/MetricCard";
import { StatusBadge } from "../components/StatusBadge";
import { useNodes } from "../hooks/queries";

export function NodesPage() {
  const nodes = useNodes();

  if (nodes.isLoading) return <LoadingState label="Loading runner fleet" />;
  if (nodes.error) return <ErrorState error={nodes.error} />;
  if (!nodes.data) return null;

  const online = nodes.data.items.filter((node) => node.status === "online").length;
  const degraded = nodes.data.items.filter((node) => node.status === "degraded").length;
  const unavailable = nodes.data.items.filter((node) =>
    ["offline", "lost"].includes(node.status),
  ).length;
  const capabilities = new Set(nodes.data.items.flatMap((node) => node.capabilities)).size;

  return (
    <div className="page-stack">
      <section className="section-heading split-heading">
        <div>
          <span className="section-kicker">Outbound runner registry</span>
          <h2>Host-native execution fleet</h2>
          <p>
            Durable runner identities, health signals, platforms, and advertised
            capabilities.
          </p>
        </div>
        <div className="mono-chip">
          <Radio size={14} /> heartbeat / 10s refresh
        </div>
      </section>

      <section className="metrics-grid tool-metrics">
        <MetricCard
          label="Online"
          value={online}
          note="Ready for execution"
          icon={CheckCircle2}
          tone="mint"
        />
        <MetricCard
          label="Degraded"
          value={degraded}
          note="Connected with reduced health"
          icon={AlertTriangle}
          tone="amber"
        />
        <MetricCard
          label="Unavailable"
          value={unavailable}
          note="Offline or heartbeat lost"
          icon={ServerOff}
        />
        <MetricCard
          label="Capabilities"
          value={capabilities}
          note="Advertised across the fleet"
          icon={Cpu}
          tone="blue"
        />
      </section>

      <section className="panel registry-panel">
        <div className="panel-header">
          <div>
            <span className="panel-kicker">node inventory</span>
            <h3>Registered runners</h3>
          </div>
          <span className="mono-chip">{nodes.data.items.length} durable identities</span>
        </div>

        {nodes.data.items.length ? (
          <div className="tool-table-wrap">
            <table className="tool-table">
              <thead>
                <tr>
                  <th>Node</th>
                  <th>Status</th>
                  <th>Platform</th>
                  <th>Runner</th>
                  <th>Capabilities</th>
                  <th>Last heartbeat</th>
                </tr>
              </thead>
              <tbody>
                {nodes.data.items.map((node) => (
                  <tr key={node.id}>
                    <td>
                      <div className="tool-name-cell">
                        <span><Server size={17} /></span>
                        <div>
                          <strong>{node.name}</strong>
                          <small>{node.id}</small>
                        </div>
                      </div>
                    </td>
                    <td><StatusBadge status={node.status} /></td>
                    <td>
                      <strong>{node.platform}</strong>
                      <small className="tool-reason">{node.architecture}</small>
                    </td>
                    <td>{node.runner_version}</td>
                    <td>
                      <div className="capability-list">
                        {node.capabilities.length ? node.capabilities.map((capability) => (
                          <span key={capability}>{capability.replaceAll("_", " ")}</span>
                        )) : <span>runner</span>}
                      </div>
                    </td>
                    <td>{relativeTime(node.last_seen_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <EmptyState icon={Server} title="No runners registered">
            Start a local or remote RiftX Runner to register its execution capabilities.
          </EmptyState>
        )}
      </section>
    </div>
  );
}

function relativeTime(value: string | null): string {
  if (!value) return "never";
  const elapsed = Date.now() - new Date(value).getTime();
  const seconds = Math.max(0, Math.round(elapsed / 1000));
  if (seconds < 60) return `${seconds}s ago`;
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  return `${Math.round(minutes / 60)}h ago`;
}
