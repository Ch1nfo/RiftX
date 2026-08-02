import { ArrowLeft, ArrowRight, Loader2, Plus } from "lucide-react";
import { FormEvent, useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";

import type { ApprovalMode, CreateRunPayload, EntryPoint } from "../api/types";
import { ErrorState } from "../components/ErrorState";
import { PixelIcon } from "../components/PixelIcon";
import { useCreateRun, useModelProfiles, useNodes } from "../hooks/queries";
import { useI18n } from "../i18n";

export function NewRunPage() {
  const { t } = useI18n();
  const navigate = useNavigate();
  const createRun = useCreateRun();
  const nodes = useNodes();
  const modelProfiles = useModelProfiles();
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
  const [modelProfile, setModelProfile] = useState("");
  const [submissionError, setSubmissionError] = useState<Error | null>(null);

  useEffect(() => {
    if (modelProfile || !modelProfiles.data) return;
    const effective = modelProfiles.data.profiles.find(
      (profile) =>
        profile.name === modelProfiles.data?.effective_default_profile &&
        profile.api_key_configured,
    );
    const firstAvailable = modelProfiles.data.profiles.find(
      (profile) => profile.api_key_configured,
    );
    setModelProfile(effective?.name ?? firstAvailable?.name ?? "");
  }, [modelProfile, modelProfiles.data]);

  const selectedModelProfile = modelProfiles.data?.profiles.find(
    (profile) => profile.name === modelProfile,
  );
  const modelProfileReady =
    !modelProfiles.error && selectedModelProfile?.api_key_configured === true;

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setSubmissionError(null);
    try {
      const parsedEntries = parseEntryPoints(entryPoints);
      const scopeValues = splitLines(scope);
      const payload: CreateRunPayload = {
        objective: objective.trim(),
        node_id: nodeId.trim() || "local",
        approval_mode: approvalMode,
        ...(modelProfile.trim() ? { model_profile: modelProfile.trim() } : {}),
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
    } catch (error) {
      setSubmissionError(
        error instanceof Error ? error : new Error(t("Could not create the Run")),
      );
    }
  }

  return (
    <div className="form-page">
      <div className="form-intro">
        <Link className="back-link" to="/">
          <ArrowLeft size={15} /> {t("Dashboard")}
        </Link>
        <div className="mission-path">
          <PixelIcon name="run" />
          <span>RIFTX / {t("New guided operation")}</span>
        </div>
        <h2>{t("Define the objective and boundary, then continue in conversation.")}</h2>
        <p>
          {t("RiftX stores this context and opens a durable conversation. No model or tool action starts until you send the first specific instruction.")}
        </p>
        <div className="form-principles">
          <div>
            <PixelIcon name="target" />
            <span>
              <strong>{t("Explicit scope")}</strong>
              {t("Define entry points and exclusions before execution.")}
            </span>
          </div>
          <div>
            <PixelIcon name="shield" />
            <span>
              <strong>{t("Approval-aware")}</strong>
              {t("Choose how sensitive actions cross the human boundary.")}
            </span>
          </div>
        </div>
      </div>

      <form className="run-form panel" onSubmit={(event) => void submit(event)}>
        <div className="form-section">
          <div className="section-number">01</div>
          <div className="section-copy">
            <h3>{t("Mission")}</h3>
            <p>{t("Describe the desired outcome, not a sequence of commands.")}</p>
          </div>
          <label className="field field-wide">
            <span>{t("Objective")}</span>
            <textarea
              required
              rows={4}
              value={objective}
              onChange={(event) => setObjective(event.target.value)}
              placeholder={t("Validate the authorized staging service and document confirmed exposure paths.")}
            />
          </label>
          <label className="field">
            <span>{t("Engagement name")}</span>
            <input
              value={engagementName}
              onChange={(event) => setEngagementName(event.target.value)}
              placeholder={t("Q3 staging validation")}
            />
          </label>
          <label className="field">
            <span>{t("Authorization reference")}</span>
            <input
              value={authorization}
              onChange={(event) => setAuthorization(event.target.value)}
              placeholder={t("Ticket, SOW, or approval ID")}
            />
          </label>
          <label className="field field-wide">
            <span>{t("Success criteria")}</span>
            <textarea
              rows={3}
              value={successCriteria}
              onChange={(event) => setSuccessCriteria(event.target.value)}
              placeholder={t("One criterion per line\nIdentify exposed services\nProduce evidence-backed findings")}
            />
          </label>
        </div>

        <div className="form-section">
          <div className="section-number">02</div>
          <div className="section-copy">
            <h3>{t("Boundary")}</h3>
            <p>{t("Give the Agent concrete starting points and hard exclusions.")}</p>
          </div>
          <label className="field field-wide">
            <span>{t("Entry points")}</span>
            <textarea
              rows={4}
              value={entryPoints}
              onChange={(event) => setEntryPoints(event.target.value)}
              placeholder={t("One KIND=VALUE per line\nurl=https://staging.example.test\nip=10.10.10.20")}
            />
            <small>{t("Supported kinds: cidr, ip, domain, url, file, text")}</small>
          </label>
          <label className="field">
            <span>{t("Scope assets")}</span>
            <textarea
              rows={4}
              value={scope}
              onChange={(event) => setScope(event.target.value)}
              placeholder={"10.10.10.0/24\napi.example.test"}
            />
          </label>
          <label className="field">
            <span>{t("Exclusions")}</span>
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
            <h3>{t("Runtime")}</h3>
            <p>{t("Select the host boundary and human-control posture.")}</p>
          </div>
          <label className="field">
            <span>{t("Execution node")}</span>
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
            <span>{t("Workspace")}</span>
            <input
              value={workspace}
              onChange={(event) => setWorkspace(event.target.value)}
              placeholder={t("Auto-generated when blank")}
            />
          </label>
          <label className="field field-wide">
            <span>{t("Model profile")}</span>
            <select
              aria-label={t("Model profile")}
              value={modelProfile}
              onChange={(event) => setModelProfile(event.target.value)}
              disabled={modelProfiles.isLoading}
            >
              {!modelProfile ? (
                <option value="">{t("Use server default")}</option>
              ) : null}
              {modelProfiles.data?.profiles.map((profile) => (
                <option
                  key={profile.name}
                  value={profile.name}
                  disabled={!profile.api_key_configured}
                >
                  {profile.name} · {profile.model} · {profile.request_mode}
                  {profile.is_effective_default ? ` · ${t("effective default")}` : ""}
                </option>
              ))}
            </select>
            <small>
              {modelProfiles.data
                ? modelProfileReady
                  ? t("The selected profile is validated before the Run is created.")
                  : t("Configure a credential for this profile before creating the Run.")
                : t("Loading model profiles")}
            </small>
          </label>
          <fieldset className="mode-field field-wide">
            <legend>{t("Approval mode")}</legend>
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
                  <strong>{t(mode)}</strong>
                  <span>{t(approvalDescription(mode))}</span>
                </label>
              ))}
            </div>
          </fieldset>
        </div>

        {modelProfiles.error ? <ErrorState error={modelProfiles.error} /> : null}
        {submissionError ?? createRun.error ? (
          <ErrorState error={submissionError ?? createRun.error ?? new Error()} />
        ) : null}
        <div className="form-actions">
          <Link className="secondary-button" to="/">
            {t("Cancel")}
          </Link>
          <button
            className="primary-button"
            type="submit"
            disabled={createRun.isPending || modelProfiles.isLoading || !modelProfileReady}
          >
            {createRun.isPending ? <Loader2 className="spin" size={17} /> : <Plus size={17} />}
            {t("Create and continue to chat")}
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
