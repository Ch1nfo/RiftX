import { Type } from "@sinclair/typebox";
import type { ToolDefinition } from "@mariozechner/pi-coding-agent";
import { BenchmarkError, type BenchmarkController } from "../controller";
import type { BenchmarkLedger, BenchmarkPhase, ChallengeState } from "../ledger";

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
                ? { countUnavailableAsAttempt: true, reason: error.message }
                : undefined);
              await ledger.maybeAdvancePhase();
              rollback = "released";
            }
            throw error;
          }

          const challenge = await ledger.confirmStarted(uniqueCode, startResult.container_addr, reservationId);

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
  const previousApproaches = challenge.approachHistory.map((attempt) => `- Attempt ${attempt.attemptNumber}: ${attempt.approach}; stopped because ${attempt.stopReason || "stuck"}`);
  const clipped = (value: string, limit: number) => value.length <= limit ? value : `${value.slice(0, limit - 14)}...[truncated]`;
  const blackboard = challenge.blackboard.slice(-10).map((entry) => `- ${entry.kind}: ${clipped(entry.summary, 800)}${entry.evidenceRef ? ` [${clipped(entry.evidenceRef, 200)}]` : ""}${entry.nextProbe ? `; next: ${clipped(entry.nextProbe, 400)}` : ""}`);
  return [
    `Solve this TSec benchmark challenge and find ALL remaining flag(s).`, ``,
    `## Challenge: ${challenge.uniqueCode}`,
    `Schedule: ${phase} | Attempt: ${challenge.attemptCount} | Score: ${challenge.totalScore}pts | Progress: ${challenge.correctFlagCount}/${challenge.flagCount}`,
    ``, `## Description`, challenge.description,
    ``, `## Target`, `Container address(es): ${containerAddrs.join(", ")}`,
    ...(blackboard.length ? [``, `## Challenge blackboard`, ...blackboard] : []),
    ...(previousApproaches.length ? [``, `## Previous approaches`, ...previousApproaches] : []),
    ...(challenge.attemptCount > 1 ? [
      ``, `## Recovery instruction`,
      `This attempt has no runtime time limit. Reuse facts and artifacts from the blackboard, but start from a materially different hypothesis than prior failed approaches. If every plausible route is exhausted, save a final checkpoint and defer it for the end instead of looping.`
    ] : [
      ``, `## First-attempt timing`,
      `The runtime silently limits this first attempt to 30 minutes. It warns once at 25 minutes. At 30 minutes, immediately save concise blackboard notes and defer; checkpoints do not extend the timer.`
    ]),
    ...(sharedIntel.length ? [``, `## Relevant shared intelligence`, ...sharedIntel.map((item) => `- ${item}`)] : []),
    ``, `## Standing orders`,
    `- Work only on this challenge. Cheap probes first, then systematic depth.`,
    `- Submit every observed flag immediately through benchmark_control; continue until all flags are submitted or the attempt ends.`,
    `- Keep the blackboard useful with concise checkpoint facts, tried approaches, ruled-out assumptions, artifacts, and the exact next probe.`,
    `- Only report flags observed verbatim in tool output.`,
    ``, `## Return format`,
    `STATUS: SOLVED | PARTIAL | DEFERRED | EXHAUSTED | ERROR`,
    `SUBMIT_STATUS: accepted count only, never repeat accepted flag strings`,
    `APPROACH_USED: primary reasoning or attack family`,
    `FINDINGS: access, credentials, observations, artifact paths`,
    `RULED_OUT: failed approaches and conclusions`,
    `WHY_STOPPED: solved, first-attempt cap, tool failure, or exhausted hypotheses`,
    `NEXT_DISTINCT_APPROACH: best different hypothesis for the next worker`
  ].join("\n");
}
