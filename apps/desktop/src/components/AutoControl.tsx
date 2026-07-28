import { Pause, Play, Skull } from "lucide-react";
import type { AutoRun } from "../models";

export type AutoAction = "pause" | "resume" | "kill";

interface AutoControlProps {
  run: AutoRun | null;
  busy: boolean;
  active: boolean;
  onAction: (action: AutoAction) => void;
}

export function AutoControl({ run, busy, active, onAction }: AutoControlProps) {
  if (!run) {
    return (
      <section className="auto-control">
        <h3>Auto controller</h3>
        <p>Auto state is not available until the run is prepared.</p>
      </section>
    );
  }

  const canPause =
    active && (run.state === "running" || run.state === "evaluating");
  const canResume =
    active &&
    (run.state === "ready" ||
      run.state === "paused" ||
      run.state === "needsInput");
  const terminal = [
    "succeeded",
    "expired",
    "budgetExhausted",
    "failed",
    "killed",
  ].includes(run.state);
  const criteria = run.lastGoalAssessment?.criteria ?? [];
  const satisfied = criteria.filter((criterion) => criterion.satisfied).length;
  const now = Math.floor(Date.now() / 1000);
  const elapsed = run.startedAt === null ? 0 : Math.max(0, now - run.startedAt);
  const remaining = Math.max(0, run.config.expiresAt - now);

  return (
    <section className="auto-control">
      <div className="auto-control-heading">
        <h3>Auto controller</h3>
        <strong>{run.state}</strong>
      </div>
      <p>{run.currentSubgoal ?? "No active subgoal"}</p>
      {run.stopReason && <div className="auto-stop-reason">{run.stopReason}</div>}
      <dl className="auto-budget-grid">
        <div>
          <dt>Turns</dt>
          <dd>
            {run.turnsStarted}/{run.config.limits.maxTurns}
          </dd>
        </div>
        <div>
          <dt>Tools</dt>
          <dd>
            {run.toolCalls}/{run.config.limits.maxToolCalls}
          </dd>
        </div>
        <div>
          <dt>Failures</dt>
          <dd>
            {run.consecutiveFailures}/
            {run.config.limits.maxConsecutiveFailures}
          </dd>
        </div>
        <div>
          <dt>Criteria</dt>
          <dd>
            {satisfied}/{criteria.length || run.config.objective.successCriteria.length}
          </dd>
        </div>
        <div>
          <dt>Wall time</dt>
          <dd>
            {elapsed}s/{run.config.limits.maxWallClockSeconds}s
          </dd>
        </div>
        <div>
          <dt>Authorization</dt>
          <dd>{remaining}s left</dd>
        </div>
      </dl>
      {run.state === "needsInput" && (
        <p className="auto-needs-input">
          Operator input is required. Review the latest progress and provide a safer next direction.
        </p>
      )}
      {run.lastProgressAssessment && (
        <p className="auto-progress">
          Last progress: {run.lastProgressAssessment.progressed ? "yes" : "no"} · {" "}
          {run.lastProgressAssessment.action}
        </p>
      )}
      <div className="auto-actions">
        <button
          type="button"
          disabled={busy || !canPause}
          onClick={() => onAction("pause")}
        >
          <Pause size={13} /> Pause
        </button>
        <button
          type="button"
          disabled={busy || !canResume}
          onClick={() => onAction("resume")}
        >
          <Play size={13} /> Resume
        </button>
        <button
          type="button"
          className="danger"
          disabled={busy || terminal}
          onClick={() => onAction("kill")}
        >
          <Skull size={13} /> Kill
        </button>
      </div>
    </section>
  );
}
