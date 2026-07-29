import { ArrowLeft, ArrowRight, Crosshair, Loader2, Plus, ShieldCheck } from "lucide-react";
import { FormEvent, useState } from "react";
import { Link, useNavigate } from "react-router-dom";

import type { ApprovalMode, CreateRunPayload, EntryPoint } from "../api/types";
import { ErrorState } from "../components/ErrorState";
import { useCreateRun, useNodes } from "../hooks/queries";

export function NewRunPage() {
  const navigate = useNavigate();
  const createRun = useCreateRun();
  const nodes = useNodes();
  const [objective, setObjective] = useState("");
  const [engagementName, setEngagementName] = useState("");
  const [authorization, setAuthorization] = useState("");
  const [successCriteria, setSuccessCriteria] = useState("");
  const [entryPoints, setEntryPoints] = useState("");
  const [scope, setScope] = useState("");
  const [exclusions, setExclusions] = useState("");
  const [workspace, setWorkspace] = useState("");
  const [nodeId, setNodeId] = useState("local");
  const [approvalMode, setApprovalMode] = useState<ApprovalMode>("balanced");

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const parsedEntries = parseEntryPoints(entryPoints);
    const scopeValues = splitLines(scope);
    const payload: CreateRunPayload = {
      objective: objective.trim(),
      node_id: nodeId.trim() || "local",
      approval_mode: approvalMode,
      success_criteria: splitLines(successCriteria).map((description) => ({
        description,
        required: true,
      })),
      entry_points: parsedEntries,
      scope: classifyScope(scopeValues, splitLines(exclusions)),
      ...(workspace.trim() ? { workspace_path: workspace.trim() } : {}),
      ...(engagementName.trim()
        ? {
            engagement: {
              name: engagementName.trim(),
              ...(authorization.trim()
                ? { authorization_reference: authorization.trim() }
                : {}),
            },
          }
        : {}),
    };
    const run = await createRun.mutateAsync(payload);
    navigate(`/runs/${run.id}`);
  }

  return (
    <div className="form-page">
      <div className="form-intro">
        <Link className="back-link" to="/">
          <ArrowLeft size={15} /> Dashboard
        </Link>
        <span className="kicker">New durable operation</span>
        <h2>Define the objective and execution boundary.</h2>
        <p>
          RiftX stores this configuration before Temporal starts the workflow. The run
          remains observable even if a client disconnects.
        </p>
        <div className="form-principles">
          <div>
            <Crosshair size={18} />
            <span>
              <strong>Explicit scope</strong>
              Define entry points and exclusions before execution.
            </span>
          </div>
          <div>
            <ShieldCheck size={18} />
            <span>
              <strong>Approval-aware</strong>
              Choose how sensitive actions cross the human boundary.
            </span>
          </div>
        </div>
      </div>

      <form className="run-form panel" onSubmit={(event) => void submit(event)}>
        <div className="form-section">
          <div className="section-number">01</div>
          <div className="section-copy">
            <h3>Mission</h3>
            <p>Describe the desired outcome, not a sequence of commands.</p>
          </div>
          <label className="field field-wide">
            <span>Objective</span>
            <textarea
              required
              rows={4}
              value={objective}
              onChange={(event) => setObjective(event.target.value)}
              placeholder="Validate the authorized staging service and document confirmed exposure paths."
            />
          </label>
          <label className="field">
            <span>Engagement name</span>
            <input
              value={engagementName}
              onChange={(event) => setEngagementName(event.target.value)}
              placeholder="Q3 staging validation"
            />
          </label>
          <label className="field">
            <span>Authorization reference</span>
            <input
              value={authorization}
              onChange={(event) => setAuthorization(event.target.value)}
              placeholder="Ticket, SOW, or approval ID"
            />
          </label>
          <label className="field field-wide">
            <span>Success criteria</span>
            <textarea
              rows={3}
              value={successCriteria}
              onChange={(event) => setSuccessCriteria(event.target.value)}
              placeholder={"One criterion per line\nIdentify exposed services\nProduce evidence-backed findings"}
            />
          </label>
        </div>

        <div className="form-section">
          <div className="section-number">02</div>
          <div className="section-copy">
            <h3>Boundary</h3>
            <p>Give the Agent concrete starting points and hard exclusions.</p>
          </div>
          <label className="field field-wide">
            <span>Entry points</span>
            <textarea
              rows={4}
              value={entryPoints}
              onChange={(event) => setEntryPoints(event.target.value)}
              placeholder={"One KIND=VALUE per line\nurl=https://staging.example.test\nip=10.10.10.20"}
            />
            <small>Supported kinds: cidr, ip, domain, url, file, text</small>
          </label>
          <label className="field">
            <span>Scope assets</span>
            <textarea
              rows={4}
              value={scope}
              onChange={(event) => setScope(event.target.value)}
              placeholder={"10.10.10.0/24\napi.example.test"}
            />
          </label>
          <label className="field">
            <span>Exclusions</span>
            <textarea
              rows={4}
              value={exclusions}
              onChange={(event) => setExclusions(event.target.value)}
              placeholder={"10.10.10.1\n/production"}
            />
          </label>
        </div>

        <div className="form-section">
          <div className="section-number">03</div>
          <div className="section-copy">
            <h3>Runtime</h3>
            <p>Select the host boundary and human-control posture.</p>
          </div>
          <label className="field">
            <span>Execution node</span>
            <select value={nodeId} onChange={(event) => setNodeId(event.target.value)}>
              {!nodes.data?.items.some((node) => node.id === nodeId) ? (
                <option value={nodeId}>{nodeId}</option>
              ) : null}
              {nodes.data?.items.map((node) => (
                <option
                  key={node.id}
                  value={node.id}
                  disabled={["offline", "lost"].includes(node.status)}
                >
                  {node.name} · {node.platform}/{node.architecture} · {node.status}
                </option>
              ))}
            </select>
          </label>
          <label className="field">
            <span>Workspace</span>
            <input
              value={workspace}
              onChange={(event) => setWorkspace(event.target.value)}
              placeholder="Auto-generated when blank"
            />
          </label>
          <fieldset className="mode-field field-wide">
            <legend>Approval mode</legend>
            <div className="mode-options">
              {(["auto", "balanced", "manual"] as ApprovalMode[]).map((mode) => (
                <label key={mode} className={approvalMode === mode ? "selected" : ""}>
                  <input
                    type="radio"
                    name="approval_mode"
                    value={mode}
                    checked={approvalMode === mode}
                    onChange={() => setApprovalMode(mode)}
                  />
                  <strong>{mode}</strong>
                  <span>{approvalDescription(mode)}</span>
                </label>
              ))}
            </div>
          </fieldset>
        </div>

        {createRun.error ? <ErrorState error={createRun.error} /> : null}
        <div className="form-actions">
          <Link className="secondary-button" to="/">
            Cancel
          </Link>
          <button className="primary-button" type="submit" disabled={createRun.isPending}>
            {createRun.isPending ? <Loader2 className="spin" size={17} /> : <Plus size={17} />}
            Create durable run
            {!createRun.isPending ? <ArrowRight size={16} /> : null}
          </button>
        </div>
      </form>
    </div>
  );
}

export function parseEntryPoints(raw: string): CreateRunPayload["entry_points"] {
  return splitLines(raw).map((line) => {
    const separator = line.indexOf("=");
    if (separator < 1 || separator === line.length - 1) {
      throw new Error(`Invalid entry point "${line}"; expected KIND=VALUE`);
    }
    const kind = line.slice(0, separator).trim().toLowerCase() as EntryPoint["kind"];
    if (!["cidr", "ip", "domain", "url", "file", "text"].includes(kind)) {
      throw new Error(`Unsupported entry point kind "${kind}"`);
    }
    return { kind, value: line.slice(separator + 1).trim() };
  });
}

function splitLines(value: string) {
  return value
    .split("\n")
    .map((item) => item.trim())
    .filter(Boolean);
}

function classifyScope(values: string[], exclusions: string[]): Partial<CreateRunPayload["scope"]> {
  return {
    cidrs: values.filter((value) => value.includes("/")),
    ips: values.filter((value) => /^\d{1,3}(\.\d{1,3}){3}$/.test(value)),
    domains: values.filter(
      (value) => !value.includes("/") && !/^\d{1,3}(\.\d{1,3}){3}$/.test(value),
    ),
    exclusions,
  };
}

function approvalDescription(mode: ApprovalMode) {
  if (mode === "auto") return "Proceed without routine prompts.";
  if (mode === "manual") return "Require approval for every tool action.";
  return "Pause only for sensitive actions.";
}
