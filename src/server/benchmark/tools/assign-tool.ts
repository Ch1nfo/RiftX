import { Type } from "@sinclair/typebox";
import type { ToolDefinition } from "@mariozechner/pi-coding-agent";
import type { BenchmarkController } from "../controller";
import type { BenchmarkLedger, ChallengeState } from "../ledger";
import type { BrowserManager } from "@/browser";

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
  spawnSubagent: (task: string, uniqueCode: string, containerAddrs: string[], reservationOwner: `subagent:${string}`) => Promise<{ taskId?: string; duplicate?: boolean; cancelled?: boolean }>
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
        const uniqueCode = params.uniqueCode;
        const reservationId: `subagent:${string}` = `subagent:res-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
        const rollback: { outcome: "none" | "released" | "closed" | "close_failed" } = { outcome: "none" };

      try {
        // Phase 1: atomic reserve with SubAgent slot check inside the serializer.
        await ledger.reserve(uniqueCode, reservationId, { isSubagent: true });

        // Phase 2: platform start.
        let startResult;
        try {
          startResult = await controller.startChallenge(uniqueCode);
        } catch (error) {
          if (ledger.getChallenge(uniqueCode)?.owner === reservationId) {
            await ledger.releaseReservation(uniqueCode, reservationId);
            rollback.outcome = "released";
          }
          throw error;
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
            await ledger.releaseOnSubagentExit(uniqueCode, "duplicate subagent task", reservationId);
            try {
              await controller.closeChallenge(uniqueCode);
              await ledger.confirmClosed(uniqueCode);
            } catch {
              await ledger.markCloseFailed(uniqueCode);
            }
            rollback.outcome = ledger.getChallenge(uniqueCode)?.status === "closing" ? "close_failed" : "closed";
          }
          return {
            content: [{ type: "text" as const, text: `A matching SubAgent task already exists for ${uniqueCode}. Its result will arrive when complete.` }],
            details: { assigned: false, duplicate: true }
          };
        }

        if (result.cancelled) {
          // The bridge already released the challenge and closed the container
          // after binding a task that was cancelled mid-dispatch.
          return {
            content: [{ type: "text" as const, text: `The SubAgent for ${uniqueCode} was cancelled during dispatch. The challenge was released and its container closed — re-assign it when ready.` }],
            details: { assigned: false, cancelledDuringDispatch: true }
          };
        }
        return {
          content: [{ type: "text" as const, text: `Assigned ${uniqueCode} to a Benchmark SubAgent. Container: ${startResult.container_addr.join(", ")}. The SubAgent works independently — continue YOUR challenge. Its result arrives automatically.` }],
          details: { assigned: true, uniqueCode, taskId: result.taskId }
        };
      } catch (error) {
        // Rollback only if WE still own it AND we haven't already rolled back.
        let closeSucceeded = false;
        if (rollback.outcome === "none" && ledger.getChallenge(uniqueCode)?.owner === reservationId) {
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
        const message = error instanceof Error ? error.message : String(error);
        const rollbackNote = rollback.outcome === "released"
          ? "The reservation was released; no live container was confirmed."
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

function buildBrief(challenge: ChallengeState, containerAddrs: string[], phase: "first_pass" | "second_pass" | "completed", sharedIntel: string[]): string {
  const recovery = [
    challenge.lastSignalContent ? `Last meaningful signal: ${challenge.lastSignalContent}` : "",
    challenge.triedFamilies.length ? `Tried families: ${challenge.triedFamilies.join(", ")}` : "",
    challenge.nextProbe ? `Saved next probe: ${challenge.nextProbe}` : "",
    challenge.deferredReason ? `Why the previous worker stopped: ${challenge.deferredReason}` : "",
    challenge.hintContent ? `Purchased hint: ${challenge.hintContent}` : ""
  ].filter(Boolean);
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
    ...(recovery.length ? [
      ``,
      `## Recovery notes from the previous worker`,
      ...recovery,
      `Use these facts, but take a DIFFERENT approach from the ruled-out families.`
    ] : []),
    ...(sharedIntel.length ? [
      ``,
      `## Relevant shared intelligence`,
      ...sharedIntel.map((item) => `- ${item}`)
    ] : []),
    ``,
    `## Standing orders`,
    `- Cheap probes first, then systematic depth.`,
    `- 8 minutes without meaningful progress → checkpoint final notes, call ${phase === "second_pass" ? "abandon" : "defer"}, then return.`,
    `- Browser-first for web targets; bash for tooling.`,
    `- Flag format: check the description above; default is flag{...}.`,
    `- Only report flags observed verbatim in tool output.`,
    ``,
    `## Return format (mandatory)`,
    `SUBMIT_STATUS: flags submitted via benchmark_control (count only, no strings), or NONE`,
    `FINDINGS: creds, access, key observations`,
    `RULED_OUT: approaches tried and why they failed`,
    `NEXT: best remaining hypotheses for a fresh agent`
  ].join("\n");
}
