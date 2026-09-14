import { selectBlackboard, blackboardLabel } from "../blackboard";
import { Type } from "@sinclair/typebox";
import type { ToolDefinition } from "@mariozechner/pi-coding-agent";
import { BenchmarkError, type BenchmarkController } from "../controller";
import type { BenchmarkLedger, BenchmarkPhase, ChallengeState } from "../ledger";
import { BENCHMARK_HANDOFF_GUIDANCE } from "../continuity";
import { captureFence } from "../fencing";

/** Reserve, start, dispatch, and bind one challenge. Network mutations are
 * serialized per challenge, not globally across the three workers. */
export function createAssignBenchmarkChallengeTool(
  controller: BenchmarkController,
  ledger: BenchmarkLedger,
  spawnSubagent: (task: string, uniqueCode: string, containerAddrs: string[], reservationOwner: `subagent:${string}`) => Promise<{ taskId?: string; duplicate?: boolean; cancelled?: boolean }>
): ToolDefinition {
  return {
    name: "assign_benchmark_challenge",
    label: "Assign benchmark challenge",
    description: "Assign one eligible benchmark challenge to a background SubAgent. During coverage, challenges must be selected from lowest score to highest and no challenge may be revisited until all have completed one attempt. Max 2 concurrent benchmark SubAgents.",
    promptSnippet: "assign_benchmark_challenge(uniqueCode)",
    parameters: Type.Object({ uniqueCode: Type.String({ description: "An eligible unique_code shown by benchmark_control status" }) }),
    executionMode: "parallel",
    async execute(_toolCallId: string, params: { uniqueCode: string }) {
      const uniqueCode = params.uniqueCode;
      return ledger.runChallengeAction(uniqueCode, async () => {
        const reservationId: `subagent:${string}` = `subagent:res-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
        let platformStarted = false;
        let rollback: "none" | "released" | "closed" | "close_failed" = "none";
        try {
          await ledger.reserve(uniqueCode, reservationId, { isSubagent: true });
          let startResult;
          try {
            startResult = await controller.startChallenge(uniqueCode);
            platformStarted = true;
          } catch (error) {
            if (ledger.getChallenge(uniqueCode)?.owner === reservationId) {
              await ledger.releaseReservation(uniqueCode, reservationId, error instanceof BenchmarkError && error.kind === "resource_unavailable"
                ? { resourceUnavailable: true, reason: error.message }
                : undefined);
              await ledger.maybeAdvancePhase();
              rollback = "released";
            }
            throw error;
          }

          const challenge = await ledger.confirmStarted(uniqueCode, startResult.container_addr, reservationId, true);

          const brief = buildBrief(challenge, startResult.container_addr, ledger.getState().phase,
            ledger.intelForChallenge(challenge, startResult.container_addr).map((entry) => `${entry.target}: ${entry.intel}`));
          const result = await spawnSubagent(brief, uniqueCode, startResult.container_addr, reservationId);
          if (result.duplicate) {
            if (ledger.getChallenge(uniqueCode)?.owner === reservationId) {
              await ledger.releaseOnSubagentExit(uniqueCode, "duplicate subagent task", reservationId);
              try {
                await controller.closeChallenge(uniqueCode);
                await ledger.confirmClosed(uniqueCode);
                rollback = "closed";
              } catch {
                await ledger.markCloseFailed(uniqueCode);
                rollback = "close_failed";
              }
            }
            return { content: [{ type: "text" as const, text: `A matching SubAgent task already exists for ${uniqueCode}; the duplicate container was released.` }], details: { assigned: false, duplicate: true, rollback } };
          }
          if (result.cancelled) {
            return { content: [{ type: "text" as const, text: `The SubAgent for ${uniqueCode} was cancelled during dispatch. Its notes remain in the challenge blackboard; reassign it only when eligible.` }], details: { assigned: false, cancelledDuringDispatch: true } };
          }
          return { content: [{ type: "text" as const, text: `Assigned ${uniqueCode} to a Benchmark SubAgent. Container: ${startResult.container_addr.join(", ")}. Continue your own challenge; the result arrives automatically.` }], details: { assigned: true, uniqueCode, taskId: result.taskId } };
        } catch (error) {
          const failed = ledger.getChallenge(uniqueCode);
          if (failed?.owner === reservationId) await ledger.recordAttemptIncident(captureFence(failed),
            error instanceof BenchmarkError ? "platform_failure" : "harness_bad_handoff",
            error instanceof Error ? error.message : String(error), "assignment");
          if (rollback === "none" && ledger.getChallenge(uniqueCode)?.owner === reservationId) {
            if (platformStarted) {
              await ledger.releaseOnSubagentExit(uniqueCode, `assignment failed: ${error instanceof Error ? error.message : String(error)}`, reservationId);
              try {
                await controller.closeChallenge(uniqueCode);
                await ledger.confirmClosed(uniqueCode);
                rollback = "closed";
              } catch {
                await ledger.markCloseFailed(uniqueCode);
                rollback = "close_failed";
              }
            } else {
              await ledger.releaseReservation(uniqueCode, reservationId);
              rollback = "released";
            }
          }
          const message = error instanceof Error ? error.message : String(error);
          const rollbackNote = rollback === "released" ? "The reservation was released; no live container was confirmed."
            : rollback === "closed" ? "The container was closed and the challenge released."
              : rollback === "close_failed" ? "The challenge was released but its container may still occupy a platform slot; sync will reconcile."
                : "Another worker may own this challenge; no rollback was performed.";
          return { content: [{ type: "text" as const, text: `Failed to assign ${uniqueCode}: ${message}. ${rollbackNote}` }], details: { assigned: false, error: message, rollbackOutcome: rollback } };
        }
      });
    }
  } as ToolDefinition;
}

function buildBrief(challenge: ChallengeState, containerAddrs: string[], phase: BenchmarkPhase, sharedIntel: string[]): string {
  const last = challenge.approachHistory.at(-1);
  const previousApproaches = challenge.approachHistory.map((attempt) => `- Attempt ${attempt.attemptNumber}: tried=${attempt.triedFamilies.join(", ") || "(not recorded)"}; stopped because ${attempt.stopReason || "stuck"}`);
  const clipped = (value: string, limit: number) => value.length <= limit ? value : `${value.slice(0, limit - 14)}...[truncated]`;
  const blackboard = selectBlackboard(challenge, 10).map((entry) => `- ${blackboardLabel(entry)}: ${clipped(entry.summary, 800)}${entry.evidenceRef ? ` [${clipped(entry.evidenceRef, 200)}]` : ""}`);
  return [
    `Identity: attemptId=${challenge.attemptId}; containerEpoch=${challenge.containerEpoch}`,
    ...(last ? [`Last termination: ${last.terminationSource ?? "unknown"}; ${last.terminationReason ?? last.stopReason}; gate=${last.activeGate ?? "none"}; handoff=${last.handoffStatus ?? "unknown"}`] : []),
    `Solve this TSec benchmark challenge and find ALL remaining flag(s).`, ``,
    `## Challenge: ${challenge.uniqueCode}`,
    `Schedule: ${phase} | Attempt: ${challenge.attemptCount} | Score: ${challenge.totalScore}pts | Progress: ${challenge.correctFlagCount}/${challenge.flagCount}`,
    ``, `## Description`, challenge.description,
    ``, `## Target`, `Container address(es): ${containerAddrs.join(", ")}`,
    ...(blackboard.length ? [``, `## Challenge blackboard`, ...blackboard] : []),
    ...(challenge.triedFamilies.length ? [`Previously tried: ${challenge.triedFamilies.join(", ")}`] : []),
    ...(challenge.ruledOutFamilies.length ? [`Recorded exclusions (check their evidence): ${challenge.ruledOutFamilies.join(", ")}`] : []),
    ...(previousApproaches.length ? [``, `## Previous approaches`, ...previousApproaches] : []),
    ...(challenge.attemptCount > 1 ? [
      ``, `## Recovery instruction`,
      `This attempt has no runtime time limit. ${BENCHMARK_HANDOFF_GUIDANCE} If every plausible route is exhausted, save a final checkpoint and defer it for the end instead of looping.`
    ] : [
      ``, `## First-attempt timing`,
      `The runtime silently limits this first attempt to 30 minutes. It warns once at 25 minutes. At 30 minutes, immediately save concise blackboard notes and defer; checkpoints do not extend the timer.`
    ]),
    ...(sharedIntel.length ? [``, `## Relevant shared intelligence`, ...sharedIntel.map((item) => `- ${item}`)] : []),
    ``, `## Standing orders`,
    `- Work only on this challenge. Cheap probes first, then systematic depth.`,
    `- Your working directory is a per-challenge local workspace that persists across attempts; keep replayable scripts (e.g. stage-01-*.sh) and artifacts there so a later attempt can resume from them instead of re-deriving everything.`,
    `- Submit every observed flag immediately through benchmark_control: checkpoint the observed output with its evidenceRef first, then submit the flag referencing that exact evidenceRef; continue until all flags are submitted or the attempt ends.`,
    `- Keep the blackboard useful with concise observed facts, evidence, tested approaches, supported exclusions and unresolved questions. Do not prescribe the current route or next steps to your successor.`,
    `- Only report flags observed verbatim in tool output.`,
    ``, `## Return format`,
    `FLAG: exact captured flag string(s), else NONE`,
    `FINDINGS: creds, access gained, key observations, useful artifact paths`,
    `RULED_OUT: approaches tried and why they failed`,
    `UNCERTAINTIES: unresolved questions and limits of the evidence; no proposed next steps`
  ].join("\n");
}
