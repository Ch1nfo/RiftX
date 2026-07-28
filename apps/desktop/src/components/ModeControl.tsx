import { LoaderCircle, ShieldAlert } from "lucide-react";
import { useEffect, useState } from "react";
import { AUTO_MODE_CONFIRMATION } from "../constants";
import type { CredentialGrant, Engagement, ExecutionMode } from "../models";

const MODES: { id: ExecutionMode; label: string }[] = [
  { id: "redTeam", label: "RedTeam" },
  { id: "pentest", label: "Pentest" },
  { id: "auto", label: "Auto" },
];

interface ModeControlProps {
  engagement: Engagement;
  busy: boolean;
  blocked: boolean;
  credentialGrants: CredentialGrant[];
  onChange: (mode: ExecutionMode, confirmation: string | null) => void;
}

export function ModeControl({
  engagement,
  busy,
  blocked,
  credentialGrants,
  onChange,
}: ModeControlProps) {
  const [target, setTarget] = useState<ExecutionMode>(engagement.mode);
  const [confirmation, setConfirmation] = useState("");

  useEffect(() => {
    setTarget(engagement.mode);
    setConfirmation("");
  }, [engagement.id, engagement.mode]);

  const changing = target !== engagement.mode;
  const completed =
    engagement.status === "completed" || engagement.status === "expired";
  const controlsDisabled = busy || blocked || completed;
  const autoConfirmed =
    target !== "auto" || confirmation === AUTO_MODE_CONFIRMATION;
  const activeCredentialGrants = credentialGrants.filter(
    (grant) =>
      grant.revokedAt === null &&
      grant.expiresAt > Math.floor(Date.now() / 1000),
  );

  return (
    <section className="mode-control">
      <h3>Execution mode</h3>
      <div className="segmented-control" aria-label="Execution mode">
        {MODES.map((mode) => (
          <button
            type="button"
            className={target === mode.id ? "active" : ""}
            disabled={controlsDisabled}
            aria-pressed={target === mode.id}
            onClick={() => {
              setTarget(mode.id);
              setConfirmation("");
            }}
            key={mode.id}
          >
            {mode.label}
          </button>
        ))}
      </div>

      {blocked && (
        <p className="mode-status">
          Finish or interrupt active work and resolve pending approvals before
          changing mode.
        </p>
      )}

      {changing && target === "redTeam" && (
        <div className="mode-warning">
          <ShieldAlert size={16} />
          <span>
            RedTeam is for exercise-style attack work. Dangerous commands and
            high-risk tools require human approval.
          </span>
        </div>
      )}

      {changing && target === "pentest" && (
        <div className="mode-warning">
          <ShieldAlert size={16} />
          <span>
            Pentest is for operator-led inspections. Most actions can run;
            dangerous commands still require approval.
          </span>
        </div>
      )}

      {changing && target === "auto" && (
        <div className="auto-mode-confirmation">
          <div className="mode-warning critical">
            <ShieldAlert size={16} />
            <span>
              Switch to Auto only for lab / range targets with a valid
              authorization expiry. RiftX will interrupt less often; use Pause
              or Kill Switch if you need to stop.
            </span>
          </div>
          <dl>
            <div>
              <dt>Objective</dt>
              <dd>{engagement.objective.summary}</dd>
            </div>
            <div>
              <dt>Environment</dt>
              <dd>{engagement.authorization.environment}</dd>
            </div>
            <div>
              <dt>Declared scope entries</dt>
              <dd>
                {engagement.entryPoints.length +
                  engagement.authorization.network.cidrs.length +
                  engagement.authorization.network.domains.length}
              </dd>
            </div>
            <div>
              <dt>Credential grants</dt>
              <dd>{activeCredentialGrants.length}</dd>
            </div>
            <div>
              <dt>Authorization expiry</dt>
              <dd>
                {engagement.authorization.window.expiresAt
                  ? new Date(
                      engagement.authorization.window.expiresAt * 1000,
                    ).toLocaleString()
                  : "Not set — Auto requires an expiry"}
              </dd>
            </div>
          </dl>
          {activeCredentialGrants.length > 0 && (
            <div className="auto-grant-list">
              {activeCredentialGrants.map((grant) => (
                <code key={grant.id}>
                  credential://{grant.credentialId} ·{" "}
                  {grant.allowedCapabilities.join(", ")} · max {grant.maxUses}
                </code>
              ))}
            </div>
          )}
          <label>
            <span>Type the confirmation phrase</span>
            <code>{AUTO_MODE_CONFIRMATION}</code>
            <input
              value={confirmation}
              disabled={controlsDisabled}
              autoComplete="off"
              spellCheck={false}
              onChange={(event) => setConfirmation(event.target.value)}
            />
          </label>
        </div>
      )}

      {changing && (
        <button
          type="button"
          className="mode-apply-button"
          disabled={controlsDisabled || !autoConfirmed}
          onClick={() =>
            onChange(target, target === "auto" ? confirmation : null)
          }
        >
          {busy && <LoaderCircle className="spin" size={14} />}
          Apply {MODES.find((mode) => mode.id === target)?.label ?? target} mode
        </button>
      )}
    </section>
  );
}
