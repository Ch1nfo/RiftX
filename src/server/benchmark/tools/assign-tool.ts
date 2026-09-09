import { Type } from "@sinclair/typebox";
import type { ToolDefinition } from "@mariozechner/pi-coding-agent";
import type { BenchmarkController } from "../controller";
import type { BenchmarkLedger, ChallengeState } from "../ledger";
import type { BrowserManager } from "@/browser";
import { reconcileExpiredHandoffs, scheduleHandoffCleanup } from "../handoff";

/**
 * Assigns a benchmark challenge to a new SubAgent using a strict
 * reserve → start → confirm → spawn → bind sequence. The SubAgent slot
 * limit is checked inside ledger.reserve()'s serializer critical section,
 * closing the concurrent-assign race. All rollback paths verify reservation
 * identity before touching the ledger or the platform.
 */

export function createAssignBenchmarkChallengeTool(
  controller: BenchmarkController,
  ledger: BenchmarkLedger,
  browser: BrowserManager,
  spawnSubagent: (task: string, uniqueCode: string, containerAddrs: string[], reservationOwner: `subagent:${string}`) => Promise<{ taskId?: string; duplicate?: boolean; cancelled?: boolean; handoffPreserved?: boolean }>
): ToolDefinition {
  const tool: ToolDefinition = {
    name: "assign_benchmark_challenge",
    label: "Assign benchmark challenge",
    description: "Assign a benchmark challenge to a background SubAgent. Atomically reserves the challenge, starts its container, and dispatches a SubAgent with an auto-constructed brief. Max 2 concurrent benchmark SubAgents. The SubAgent works independently and its result arrives automatically.",
    promptSnippet: "assign_benchmark_challenge(uniqueCode)",
    parameters: Type.Object({
      uniqueCode: Type.String({ description: "The challenge unique_code to assign" })
    }),
    executionMode: "parallel",
    async execute(_toolCallId: string, params: { uniqueCode: string }) {
      return ledger.runAction(async () => {
        await reconcileExpiredHandoffs(controller, ledger);
        const uniqueCode = params.uniqueCode;
        const reservationId: `subagent:${string}` = `subagent:res-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
        const rollback: { outcome: "none" | "released" | "handoff_restored" | "closed" | "close_failed" } = { outcome: "none" };
        const beforeReserve = ledger.getChallenge(uniqueCode);
        const reuseLiveContainer = beforeReserve?.status === "handoff_waiting"
          && beforeReserve.containerStatus === "available" && beforeReserve.containerAddrs.length > 0;
        const preservedAddrs = reuseLiveContainer ? [...beforeReserve.containerAddrs] : [];

      try {
        // Phase 1: atomic reserve with SubAgent slot check inside the serializer.
        await ledger.reserve(uniqueCode, reservationId, { isSubagent: true });

        // Phase 2: platform start.
        let startResult;
        if (reuseLiveContainer) {
          startResult = { unique_code: uniqueCode, container_addr: preservedAddrs };
        } else {
          try {
            startResult = await controller.startChallenge(uniqueCode);
          } catch (error) {
            if (ledger.getChallenge(uniqueCode)?.owner === reservationId) {
              await ledger.releaseReservation(uniqueCode, reservationId);
              rollback.outcome = "handoff_restored";
            }
            throw error;
          }
        }

        // Phase 3: confirm started.
        const challenge = await ledger.confirmStarted(uniqueCode, startResult.container_addr, reservationId);

        // Phase 4: grant browser scope on the parent's manager (the child's
        // own BrowserManager gets scope via RuntimeDeps.containerAddrs after creation).
        for (const addr of startResult.container_addr) {
          const normalized = addr.includes("://") ? addr : `http://${addr}/`;
          browser.grantScope(normalized, true);
        }

        // Phase 5: build brief and dispatch SubAgent (passes containerAddrs for child scope).
        const brief = buildBrief(challenge, startResult.container_addr, ledger.getState().phase, ledger.intelForChallenge(challenge, startResult.container_addr).map((entry) => `${entry.target}: ${entry.intel}`));
        const result = await spawnSubagent(brief, uniqueCode, startResult.container_addr, reservationId);
        if (result.duplicate) {
          if (ledger.getChallenge(uniqueCode)?.owner === reservationId) {
            if (reuseLiveContainer) {
              await ledger.restoreWarmHandoffAfterAssignmentFailure(uniqueCode, reservationId);
              scheduleHandoffCleanup(controller, ledger, uniqueCode);
              rollback.outcome = "handoff_restored";
            } else {
              await ledger.releaseOnSubagentExit(uniqueCode, "duplicate subagent task", reservationId);
              try {
                await controller.closeChallenge(uniqueCode);
                await ledger.confirmClosed(uniqueCode);
              } catch {
                await ledger.markCloseFailed(uniqueCode);
              }
              rollback.outcome = ledger.getChallenge(uniqueCode)?.status === "closing" ? "close_failed" : "closed";
            }
          }
          return {
            content: [{ type: "text" as const, text: `A matching SubAgent task already exists for ${uniqueCode}. Its result will arrive when complete.` }],
            details: { assigned: false, duplicate: true }
          };
        }

        if (result.cancelled) {
          // The bridge already released the challenge after binding a task
          // that was cancelled mid-dispatch. Valuable recovery state may have
          // been returned to warm handoff instead of closing the container.
          return {
            content: [{ type: "text" as const, text: result.handoffPreserved
              ? `The SubAgent for ${uniqueCode} was cancelled during dispatch. The live container was returned to warm handoff — assign a fresh worker within 2 minutes.`
              : `The SubAgent for ${uniqueCode} was cancelled during dispatch. The challenge was released and its container closed — re-assign it when ready.` }],
            details: { assigned: false, cancelledDuringDispatch: true, handoffPreserved: result.handoffPreserved === true }
          };
        }
        return {
          content: [{ type: "text" as const, text: `Assigned ${uniqueCode} to a Benchmark SubAgent${reuseLiveContainer ? " through a warm handoff (existing container state preserved)" : ""}. Container: ${startResult.container_addr.join(", ")}. The SubAgent works independently — continue YOUR challenge. Its result arrives automatically.` }],
          details: { assigned: true, uniqueCode, taskId: result.taskId }
        };
      } catch (error) {
        // Rollback only if WE still own it AND we haven't already rolled back.
        let closeSucceeded = false;
        if (rollback.outcome === "none" && ledger.getChallenge(uniqueCode)?.owner === reservationId) {
          if (reuseLiveContainer) {
            await ledger.restoreWarmHandoffAfterAssignmentFailure(uniqueCode, reservationId);
            scheduleHandoffCleanup(controller, ledger, uniqueCode);
            rollback.outcome = "handoff_restored";
          } else {
            await ledger.releaseOnSubagentExit(uniqueCode, `assignment failed: ${error instanceof Error ? error.message : String(error)}`, reservationId);
            try {
              await controller.closeChallenge(uniqueCode);
              await ledger.confirmClosed(uniqueCode);
              closeSucceeded = true;
            } catch {
              await ledger.markCloseFailed(uniqueCode);
              closeSucceeded = false;
            }
            rollback.outcome = closeSucceeded ? "closed" : "close_failed";
          }
        }
        const message = error instanceof Error ? error.message : String(error);
        const rollbackNote = rollback.outcome === "released"
          ? "The reservation was released; no live container was confirmed."
          : rollback.outcome === "handoff_restored"
            ? "The existing live container was returned to warm-handoff state for another worker."
          : rollback.outcome === "closed"
            ? "The container was closed and the challenge released."
            : rollback.outcome === "close_failed"
              ? "The challenge was released but the container close FAILED — it still occupies a platform slot; sync will reconcile."
              : "Another worker may own this challenge — no rollback was performed.";
        return {
          content: [{ type: "text" as const, text: `Failed to assign ${uniqueCode}: ${message}. ${rollbackNote} Try a different challenge or retry later.` }],
          details: { assigned: false, error: message, rollbackOutcome: rollback.outcome, closeSucceeded }
        };
      }
      });
    }
  } as ToolDefinition;
  return tool;
}

function buildBrief(challenge: ChallengeState, containerAddrs: string[], phase: "first_pass" | "second_pass" | "endgame" | "completed", sharedIntel: string[]): string {
  const recovery = [
    challenge.lastSignalContent ? `Last meaningful signal: ${challenge.lastSignalContent}` : "",
    challenge.triedFamilies.length ? `Tried families: ${challenge.triedFamilies.join(", ")}` : "",
    challenge.nextProbe ? `Saved next probe: ${challenge.nextProbe}` : "",
    challenge.deferredReason ? `Why the previous worker stopped: ${challenge.deferredReason}` : "",
    challenge.hintContent ? `Purchased hint: ${challenge.hintContent}` : ""
  ].filter(Boolean);
  const previousApproaches = challenge.approachHistory.map((attempt) =>
    `- Attempt ${attempt.attemptNumber}: ${attempt.approach}; stopped because ${attempt.stopReason || "timebox"}`
  );
  const ruledOut = [...new Set(challenge.approachHistory.flatMap((attempt) => attempt.ruledOutFamilies))];
  const recoveryAttempt = phase === "second_pass" || phase === "endgame" || challenge.attemptCount > 1;
  return [
    `Solve this TSec benchmark challenge and find ALL remaining flag(s).`,
    ``,
    `## Challenge: ${challenge.uniqueCode}`,
    `Pass: ${phase} | Difficulty: ${challenge.difficulty} | Score: ${challenge.totalScore}pts | Progress: ${challenge.correctFlagCount}/${challenge.flagCount} flags already submitted`,
    ``,
    `## Description`,
    challenge.description,
    ``,
    `## Target`,
    `Container address(es): ${containerAddrs.join(", ")}`,
    `Connect via browser (navigate to http://<addr>) or bash (curl).`,
    ...(challenge.browserHandoffState ? [`Authenticated browser cookies and origin storage from the previous worker have been restored locally; verify the session before re-authenticating.`] : []),
    ...(recovery.length ? [
      ``,
      `## Recovery notes from the previous worker`,
      ...recovery,
      `Use these facts, but take a DIFFERENT approach from the ruled-out families.`
    ] : []),
    ...(recoveryAttempt ? [
      ``,
      `## Previous approaches`,
      ...(previousApproaches.length ? previousApproaches : [`- Previous attempt timed out without recording its approach.`]),
      ``,
      `## What those attempts ruled out`,
      ...(ruledOut.length ? ruledOut.map((item) => `- ${item}`) : [`- Nothing was recorded conclusively; audit the earlier assumptions before repeating tools.`]),
      ``,
      `## Mandatory strategy reset`,
      `A timed-out approach is a failed hypothesis, not a reason to repeat it longer. Begin with a materially different attack family or reasoning path. Your first three probes must not be mere parameter variations of prior commands. Revisit an old direction only if a new hint, credential, foothold, version fingerprint, source artifact, or platform observation changes its assumptions.`,
      ``,
      `## Suggested unused directions`,
      `Choose from challenge-relevant families not listed above, such as source audit, authorization/IDOR, SSRF, protocol abuse, version-specific CVE, alternate role/state, or direct algorithm recovery.`
    ] : []),
    ...(sharedIntel.length ? [
      ``,
      `## Relevant shared intelligence`,
      ...sharedIntel.map((item) => `- ${item}`)
    ] : []),
    ``,
    `## Standing orders`,
    `- Cheap probes first, then systematic depth.`,
    `- Follow the attempt timebox shown in the live continuity block. When it expires, checkpoint final notes and call defer. Use abandon only when recovery/endgame has no distinct viable hypothesis left.`,
    `- Browser-first for web targets; bash for tooling.`,
    `- Flag format: check the description above; default is flag{...}.`,
    `- Only report flags observed verbatim in tool output.`,
    ``,
    `## Return format (mandatory)`,
    `STATUS: SOLVED | PARTIAL | DEFERRED | EXHAUSTED | ERROR`,
    `SUBMIT_STATUS: flags submitted via benchmark_control (count only, no strings), or NONE`,
    `APPROACH_USED: the primary reasoning/attack family used in this attempt`,
    `FINDINGS: creds, access, key observations`,
    `RULED_OUT: approaches tried and why they failed`,
    `WHY_STOPPED: solved, timebox, tool failure, or exhausted hypotheses`,
    `NEXT_DISTINCT_APPROACH: best materially different hypothesis for a fresh agent`
  ].join("\n");
}
