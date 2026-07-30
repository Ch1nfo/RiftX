import { useState } from "react";
import {
  AlertTriangle,
  CheckCircle2,
  FileCode2,
  Loader2,
  Pencil,
  RefreshCw,
  Save,
  ServerCog,
  ShieldCheck,
  Wrench,
  X,
} from "lucide-react";

import type { ToolDefinition, UpdateToolPayload } from "../api/types";
import { EmptyState } from "../components/EmptyState";
import { ErrorState } from "../components/ErrorState";
import { LoadingState } from "../components/LoadingState";
import { MetricCard } from "../components/MetricCard";
import { StatusBadge } from "../components/StatusBadge";
import { useRefreshTools, useTools, useUpdateTool } from "../hooks/queries";

interface ToolEditorState {
  id: string;
  enabled: boolean;
  command: string;
  executor: ToolDefinition["executor"];
  capabilities: string;
  approval: ToolDefinition["approval_level"];
  timeout: string;
  outputPreferred: string;
  versionCommand: string;
  versionTimeout: string;
  environment: string;
}

export function ToolsPage() {
  const nodeId = "local";
  const tools = useTools(nodeId);
  const refresh = useRefreshTools(nodeId);
  const update = useUpdateTool(nodeId);
  const [editor, setEditor] = useState<ToolEditorState | null>(null);
  const [editorError, setEditorError] = useState<string | null>(null);

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

  const saveTool = () => {
    if (!editor) return;
    setEditorError(null);
    try {
      const payload = editorPayload(editor);
      update.mutate(
        { toolId: editor.id, payload },
        { onSuccess: () => setEditor(null) },
      );
    } catch (error) {
      setEditorError(error instanceof Error ? error.message : "Invalid tool definition");
    }
  };

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
      {update.error ? <ErrorState error={update.error} /> : null}

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

      {editor ? (
        <ToolEditor
          value={editor}
          error={editorError}
          pending={update.isPending}
          onChange={setEditor}
          onSave={saveTool}
          onCancel={() => {
            setEditor(null);
            setEditorError(null);
          }}
        />
      ) : null}

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
                  <th>Configure</th>
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
                    <td>
                      <button
                        className="secondary-button compact-button"
                        onClick={() => {
                          setEditor(toolEditorState(tool.definition));
                          setEditorError(null);
                        }}
                      >
                        <Pencil size={14} />
                        Edit
                      </button>
                    </td>
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

function ToolEditor({
  value,
  error,
  pending,
  onChange,
  onSave,
  onCancel,
}: {
  value: ToolEditorState;
  error: string | null;
  pending: boolean;
  onChange: (value: ToolEditorState) => void;
  onSave: () => void;
  onCancel: () => void;
}) {
  const field = <Key extends keyof ToolEditorState>(key: Key, next: ToolEditorState[Key]) =>
    onChange({ ...value, [key]: next });

  return (
    <section className="panel tool-editor" aria-label={`Edit ${value.id}`}>
      <div className="panel-header">
        <div>
          <span className="panel-kicker">Persist and hot reload</span>
          <h3>Edit {value.id}</h3>
        </div>
        <button className="secondary-button compact-button" onClick={onCancel}>
          <X size={14} />
          Close
        </button>
      </div>
      <div className="tool-editor-grid">
        <label className="field tool-enabled-field">
          <span>Enabled</span>
          <input
            type="checkbox"
            checked={value.enabled}
            onChange={(event) => field("enabled", event.target.checked)}
          />
        </label>
        <label className="field">
          <span>Executor</span>
          <select
            value={value.executor}
            onChange={(event) =>
              field("executor", event.target.value as ToolDefinition["executor"])
            }
          >
            <option value="process">process</option>
            <option value="shell">shell</option>
            <option value="pty">pty</option>
          </select>
        </label>
        <label className="field">
          <span>Approval</span>
          <select
            value={value.approval}
            onChange={(event) =>
              field("approval", event.target.value as ToolDefinition["approval_level"])
            }
          >
            <option value="never">never</option>
            <option value="sensitive">sensitive</option>
            <option value="always">always</option>
          </select>
        </label>
        <label className="field">
          <span>Timeout seconds</span>
          <input
            type="number"
            min="0.1"
            step="0.1"
            value={value.timeout}
            onChange={(event) => field("timeout", event.target.value)}
          />
        </label>
        <label className="field field-wide">
          <span>Command argv · one item per line</span>
          <textarea
            rows={4}
            value={value.command}
            onChange={(event) => field("command", event.target.value)}
          />
        </label>
        <label className="field field-wide">
          <span>Capabilities · comma separated</span>
          <input
            value={value.capabilities}
            onChange={(event) => field("capabilities", event.target.value)}
          />
        </label>
        <label className="field">
          <span>Preferred output</span>
          <input
            placeholder="xml, json, jsonl"
            value={value.outputPreferred}
            onChange={(event) => field("outputPreferred", event.target.value)}
          />
        </label>
        <label className="field">
          <span>Version probe timeout</span>
          <input
            type="number"
            min="0.1"
            step="0.1"
            value={value.versionTimeout}
            onChange={(event) => field("versionTimeout", event.target.value)}
          />
        </label>
        <label className="field field-wide">
          <span>Version probe argv · one item per line</span>
          <textarea
            rows={3}
            value={value.versionCommand}
            onChange={(event) => field("versionCommand", event.target.value)}
          />
        </label>
        <label className="field field-wide">
          <span>Environment diff · JSON object</span>
          <textarea
            rows={4}
            value={value.environment}
            onChange={(event) => field("environment", event.target.value)}
          />
        </label>
      </div>
      {error ? <p className="form-error">{error}</p> : null}
      <div className="tool-editor-actions">
        <button className="primary-button" onClick={onSave} disabled={pending}>
          {pending ? <Loader2 className="spin" size={16} /> : <Save size={16} />}
          Save and reload
        </button>
        <button className="secondary-button" onClick={onCancel} disabled={pending}>
          Cancel
        </button>
      </div>
    </section>
  );
}

function toolEditorState(definition: ToolDefinition): ToolEditorState {
  return {
    id: definition.id,
    enabled: definition.enabled,
    command: definition.command.join("\n"),
    executor: definition.executor,
    capabilities: definition.capabilities.join(", "),
    approval: definition.approval_level,
    timeout: String(definition.timeout_seconds),
    outputPreferred: definition.output.preferred ?? "",
    versionCommand: definition.version_probe?.command.join("\n") ?? "",
    versionTimeout: String(definition.version_probe?.timeout_seconds ?? 5),
    environment: JSON.stringify(definition.environment, null, 2),
  };
}

function editorPayload(editor: ToolEditorState): UpdateToolPayload {
  const command = lines(editor.command);
  if (!command.length) throw new Error("Command must contain at least one argv item.");
  const timeout = positiveNumber(editor.timeout, "Timeout");
  const versionCommand = lines(editor.versionCommand);
  const rawEnvironment: unknown = JSON.parse(editor.environment || "{}");
  if (!rawEnvironment || Array.isArray(rawEnvironment) || typeof rawEnvironment !== "object") {
    throw new Error("Environment must be a JSON object.");
  }
  const environment = Object.fromEntries(
    Object.entries(rawEnvironment).map(([key, value]) => {
      if (typeof value !== "string") {
        throw new Error(`Environment value for ${key} must be a string.`);
      }
      return [key, value];
    }),
  );
  return {
    enabled: editor.enabled,
    command,
    executor: editor.executor,
    capabilities: editor.capabilities
      .split(",")
      .map((item) => item.trim())
      .filter(Boolean),
    approval: editor.approval,
    timeout,
    output: { preferred: editor.outputPreferred.trim() || null },
    environment,
    version_probe: versionCommand.length
      ? {
          command: versionCommand,
          timeout_seconds: positiveNumber(editor.versionTimeout, "Version timeout"),
        }
      : null,
  };
}

function lines(value: string): string[] {
  return value
    .split("\n")
    .map((item) => item.trim())
    .filter(Boolean);
}

function positiveNumber(value: string, label: string): number {
  const parsed = Number(value);
  if (!Number.isFinite(parsed) || parsed <= 0) {
    throw new Error(`${label} must be greater than zero.`);
  }
  return parsed;
}
