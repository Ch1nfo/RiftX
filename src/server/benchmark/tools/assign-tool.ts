import { benchmarkMemoryLocator } from "../memory";
import { selectBlackboard, blackboardLabel, handoffAttempt, handoffCandidate, evidenceBackedRuleOuts } from "../blackboard";
import { Type } from "@sinclair/typebox";
import type { ToolDefinition } from "@mariozechner/pi-coding-agent";
import { BenchmarkError, type BenchmarkController } from "../controller";
import { hasReusableBenchmarkContainer, type BenchmarkLedger, type BenchmarkPhase, type ChallengeState } from "../ledger";
import { BENCHMARK_HANDOFF_GUIDANCE, BENCHMARK_ENDGAME_GUIDANCE } from "../continuity";

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
    async execute(_toolCallId: string, params: { uniqueCode: string }, signal?: AbortSignal) {
      const uniqueCode = params.uniqueCode;
      return ledger.runChallengeAction(uniqueCode, async () => {
        const reservationId: `subagent:${string}` = `subagent:res-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
        let platformStarted = false;
        let rollback: "none" | "released" | "closed" | "close_failed" = "none";
        try {
          signal?.throwIfAborted();
          const reusable = ledger.getChallenge(uniqueCode);
          const reuseLiveContainer = hasReusableBenchmarkContainer(reusable);
          await ledger.reserve(uniqueCode, reservationId, { isSubagent: true });
          signal?.throwIfAborted();
          let startResult;
          try {
            startResult = reuseLiveContainer
              ? { unique_code: uniqueCode, container_addr: [...reusable!.containerAddrs] }
              : await controller.startChallenge(uniqueCode);
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

          signal?.throwIfAborted();
          const challenge = await ledger.confirmStarted(uniqueCode, startResult.container_addr, reservationId);

          signal?.throwIfAborted();
          const brief = buildBrief(challenge, startResult.container_addr, ledger.getState().phase, ledger.isEndgame(),
            []);
          const result = await spawnSubagent(brief, uniqueCode, startResult.container_addr, reservationId);
          if (result.duplicate) {
            if (ledger.getChallenge(uniqueCode)?.owner === reservationId) {
              await ledger.releaseOnSubagentExit(uniqueCode, "duplicate subagent task", reservationId);
              try {
                if (ledger.getChallenge(uniqueCode)?.status === "closing") {
                  await controller.closeChallenge(uniqueCode);
                  await ledger.confirmClosed(uniqueCode);
                  rollback = "closed";
                } else rollback = "released";
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
                if (ledger.getChallenge(uniqueCode)?.status === "closing") {
                  await controller.closeChallenge(uniqueCode);
                  await ledger.confirmClosed(uniqueCode);
                  rollback = "closed";
                } else rollback = "released";
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
          const rollbackNote = rollback === "released" ? "The reservation was released; any preserved environment remains available for reassignment."
            : rollback === "closed" ? "The container was closed and the challenge released."
              : rollback === "close_failed" ? "The challenge was released but its container may still occupy a platform slot; sync will reconcile."
                : "Another worker may own this challenge; no rollback was performed.";
          return { content: [{ type: "text" as const, text: `Failed to assign ${uniqueCode}: ${message}. ${rollbackNote}` }], details: { assigned: false, error: message, rollbackOutcome: rollback } };
        }
      });
    }
  } as ToolDefinition;
}

function buildBrief(challenge: ChallengeState, containerAddrs: string[], phase: BenchmarkPhase, endgame: boolean, sharedIntel: string[]): string {
  const supportedRuleOuts = evidenceBackedRuleOuts(challenge);
  const previousApproaches = challenge.approachHistory.slice(-6).map((attempt) => JSON.stringify(handoffAttempt(attempt, supportedRuleOuts)));
  const clipped = (value: string, limit: number) => value.length <= limit ? value : `${value.slice(0, limit - 14)}...[truncated]`;
  const blackboard = selectBlackboard(challenge, 10).map((entry) => `- ${blackboardLabel(entry)}: ${clipped(entry.summary, 800)}${entry.evidenceRef ? ` [${entry.evidenceRef}]` : ""}`);
  const previousCandidate = handoffCandidate(challenge.currentApproach, challenge.nextProbe);
  return [
    JSON.stringify(benchmarkMemoryLocator(challenge.uniqueCode)),
    `Solve this TSec benchmark challenge and find ALL remaining flag(s).`, ``,
    `## Challenge: ${challenge.uniqueCode}`,
    `Schedule: ${phase} | Attempt: ${challenge.attemptCount} | Score: ${challenge.totalScore}pts | Progress: ${challenge.correctFlagCount}/${challenge.flagCount}`,
    `Flag format: default flag{...}; any format stated in the description takes precedence.`,
    ``, `## Description`, challenge.description,
    ``, `## Target`, `Container address(es): ${containerAddrs.join(", ")}`,
    ...(challenge.hintUsed ? [``, `## Hint (already purchased, score deducted)`, clipped(challenge.hintContent || "(requested; no content returned)", 1_200)] : []),
    ...(blackboard.length ? [``, `## Challenge blackboard`, ...blackboard] : []),
    ...(challenge.triedFamilies.length ? [`Previously tried: ${challenge.triedFamilies.join(", ")}`] : []),
    ...(supportedRuleOuts.length ? [`Recorded exclusions (check their evidence): ${supportedRuleOuts.join(", ")}`] : []),
    ...(previousCandidate ? [JSON.stringify({ previousCandidate })] : []),
    ...(previousApproaches.length ? [``, `## Previous approaches`, ...previousApproaches] : []),
    ...(challenge.attemptCount > 1 ? [
      ``, `## Recovery instruction`,
      `This attempt runs under the same 30-minute deadline: one notice at 25, hard stop at 30 (checkpoint and submit stay available there). Only verified progress in the final five minutes — a newly accepted flag or a stage_transition checkpoint with previously unseen evidence — can earn one 10-minute extension; plan to checkpoint before the deadline. ${endgame ? BENCHMARK_ENDGAME_GUIDANCE : BENCHMARK_HANDOFF_GUIDANCE} If a handoff is justified, save a checkpoint with evidence and unresolved work.`
    ] : [
      ``, `## First-attempt timing`,
      `The runtime silently limits this first attempt to 30 minutes. It warns once at 25 minutes. At 30 minutes, immediately save concise blackboard notes and defer; checkpoints and flags do not extend this timer.`
    ]),
    ...(sharedIntel.length ? [``, `## Relevant shared intelligence`, ...sharedIntel.map((item) => `- ${item}`)] : []),
    ``, `## Standing orders`,
    `- Work only on this challenge. Cheap probes first, then systematic depth.`,
    `- Your working directory is a per-challenge local workspace that persists across attempts; keep replayable scripts (e.g. stage-01-*.sh) and artifacts there so a later attempt can resume from them instead of re-deriving everything.`,
    `- Submit every observed flag immediately through benchmark_control; continue until all flags are submitted or the attempt ends.`,
    `- Keep the blackboard useful with concise observed facts, evidence, tested approaches, supported exclusions and unresolved questions. Do not prescribe the current route or next steps to your successor.`,
    `- Only report flags observed verbatim in tool output.`,
    ``, `## Return format`,
    `FLAG: exact captured flag string(s), else NONE`,
    `FINDINGS: creds, access gained, key observations, useful artifact paths`,
    `RULED_OUT: approaches tried and why they failed`,
    `UNCERTAINTIES: unresolved questions and limits of the evidence; no proposed next steps`
  ].join("\n");
}
