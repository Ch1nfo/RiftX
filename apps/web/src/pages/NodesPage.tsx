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
import { useI18n } from "../i18n";

export function NodesPage() {
  const { language, t } = useI18n();
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
          <span className="section-kicker">{t("Outbound runner registry")}</span>
          <h2>{t("Host-native execution fleet")}</h2>
          <p>
            {t("Durable runner identities, health signals, platforms, and advertised capabilities.")}
          </p>
        </div>
        <div className="mono-chip">
          <Radio size={14} /> {t("heartbeat / 10s refresh")}
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
            <span className="panel-kicker">{t("node inventory")}</span>
            <h3>{t("Registered runners")}</h3>
          </div>
          <span className="mono-chip">{t("{count} durable identities", { count: nodes.data.items.length })}</span>
        </div>

        {nodes.data.items.length ? (
          <div className="tool-table-wrap">
            <table className="tool-table">
              <thead>
                <tr>
                  <th>{t("Node")}</th>
                  <th>{t("Status")}</th>
                  <th>{t("OS / architecture")}</th>
                  <th>{t("Shell / working directory")}</th>
                  <th>{t("Tools")}</th>
                  <th>{t("Active tasks")}</th>
                  <th>Runner</th>
                  <th>{t("Capabilities")}</th>
                  <th>{t("Last heartbeat")}</th>
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
                    <td>
                      <strong>{node.shell ?? t("unknown")}</strong>
                      <small className="tool-reason" title={node.working_directory ?? ""}>
                        {node.working_directory ?? t("not reported")}
                      </small>
                    </td>
                    <td>{node.tool_count ?? t("unknown")}</td>
                    <td>
                      <strong>{node.active_execution_ids.length}</strong>
                      <small className="tool-reason">
                        {node.current_run_ids.length
                          ? node.current_run_ids.join(", ")
                          : t("idle")}
                      </small>
                    </td>
                    <td>{node.runner_version}</td>
                    <td>
                      <div className="capability-list">
                        {node.capabilities.length ? node.capabilities.map((capability) => (
                          <span key={capability}>{capability.replaceAll("_", " ")}</span>
                        )) : <span>runner</span>}
                      </div>
                    </td>
                    <td>{relativeTime(node.last_seen_at, language)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <EmptyState icon={Server} title="No runners registered">
            {t("Start a local or remote RiftX Runner to register its execution capabilities.")}
          </EmptyState>
        )}
      </section>
    </div>
  );
}

function relativeTime(value: string | null, language: "en" | "zh-CN"): string {
  if (!value) return language === "zh-CN" ? "从未" : "never";
  const elapsed = Date.now() - new Date(value).getTime();
  const seconds = Math.max(0, Math.round(elapsed / 1000));
  const formatter = new Intl.RelativeTimeFormat(language, { numeric: "auto" });
  if (seconds < 60) return formatter.format(-seconds, "second");
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return formatter.format(-minutes, "minute");
  return formatter.format(-Math.round(minutes / 60), "hour");
}
