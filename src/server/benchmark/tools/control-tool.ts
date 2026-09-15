import { clearPendingSubmission, enqueuePendingSubmission, pendingSubmission } from "../pending-submissions";
import { benchmarkMemoryLocator, readBenchmarkMemory, type BenchmarkMemoryKind } from "../memory";
import { selectBlackboard, blackboardLabel, handoffAttempt, handoffCandidate, evidenceBackedRuleOuts } from "../blackboard";
import { Type } from "@sinclair/typebox";
import type { ToolDefinition } from "@mariozechner/pi-coding-agent";
import { BenchmarkError, type BenchmarkController, type VpnCheckResult } from "../controller";
import { hasReusableBenchmarkContainer, type BenchmarkLedger, type ChallengeState, type ProgressSignalKind } from "../ledger";
import type { BrowserManager } from "@/browser";
import { BENCHMARK_HANDOFF_GUIDANCE, BENCHMARK_ENDGAME_GUIDANCE } from "../continuity";

function friendlyError(error: BenchmarkError): string {
  switch (error.kind) {
    case "vpn_check_failed": return `VPN check failed — connect the benchmark VPN, then benchmark_control(action="sync") again. Detail: ${error.message}`;
    case "invalid_state_max_active": return `Container limit reached (3). Continue an active challenge or use benchmark_control(action="defer") for a justified handoff before retrying.`;
    case "invalid_state_task_ended": return `The benchmark run has ended. Stop solving and produce the final score summary.`;
    case "resource_unavailable": return `Platform resource unavailable. Skip to the next challenge; retry this one later.`;
    case "duplicate_submit": return `This flag was already submitted (duplicate — no penalty). Continue finding remaining flags.`;
    case "challenge_not_found": return `Challenge not found on the platform. It may have been removed; sync and move on.`;
    case "not_found": return `Benchmark task not found — BENCHMARK_TOKEN is invalid/missing or the task no longer exists. Report this to the user instead of retrying.`;
    case "timeout": return `${error.message}. Sync first to check state before retrying.`;
    case "connection_error": return `Cannot reach the benchmark platform: ${error.message}. Check VPN/network and retry.`;
    default: return error.message;
  }
}

function isBenchmarkError(error: unknown): error is BenchmarkError {
  return error instanceof BenchmarkError;
}

function isAmbiguousMutationError(error: unknown): error is BenchmarkError {
  return error instanceof BenchmarkError
    && (error.kind === "timeout" || error.kind === "connection_error" || error.kind === "internal_error");
}

function scoreLabel(state: Readonly<ReturnType<BenchmarkLedger["getState"]>>): string {
  return state.scoreExact ? String(state.cumulativeScore) : `${state.cumulativeScore}+ (one or more challenge scores are unavailable from the list endpoint)`;
}

function recoveryText(challenge: ChallengeState, endgame: boolean): string {
  const supportedRuleOuts = evidenceBackedRuleOuts(challenge);
  const previousCandidate = handoffCandidate(challenge.currentApproach, challenge.nextProbe);
  const attempts = challenge.approachHistory.slice(-4);
  const lines = [
    JSON.stringify(benchmarkMemoryLocator(challenge.uniqueCode)),
    challenge.attemptCount > 1
      ? "Work scripts are inherited files only: assume unverified until replayed successfully against this fresh container; record a successful replay as evidence before relying on it."
      : "",
    previousCandidate ? JSON.stringify({ previousCandidate }) : "",
    challenge.lastMeaningfulSignalContent
      ? `Previous meaningful signal: ${challenge.lastMeaningfulSignalContent}`
      : challenge.lastSignalContent ? `Latest checkpoint: ${challenge.lastSignalContent}` : "",
    challenge.triedFamilies.length ? `Already tried: ${challenge.triedFamilies.join(", ")}` : "",
    challenge.deferredReason ? `Previous defer reason: ${challenge.deferredReason}` : "",
    challenge.hintContent ? `Hint already purchased: ${challenge.hintContent}` : "",
    attempts.length ? JSON.stringify({ pastAttempts: attempts.map((attempt) => handoffAttempt(attempt, supportedRuleOuts)) }) : "",
    supportedRuleOuts.length ? `Recorded exclusions (check their evidence): ${supportedRuleOuts.join(", ")}` : "",
    challenge.blackboard.length ? `Blackboard: ${selectBlackboard(challenge, 8).map((entry) => `[${blackboardLabel(entry)}] ${entry.summary}${entry.evidenceRef ? ` [${entry.evidenceRef}]` : ""}`).join(" | ")}` : "",
    challenge.attemptCount > 1 ? `This revisit runs under the same 30-minute deadline (one extension possible only for verified progress in its final ten minutes). ${endgame ? BENCHMARK_ENDGAME_GUIDANCE : BENCHMARK_HANDOFF_GUIDANCE}` : ""
  ].filter(Boolean);
  return lines.length ? `\nRecovery notes (do not repeat these attempts):\n${lines.join("\n")}` : "";
}

function scheduleSnapshot(ledger: BenchmarkLedger, challenge: ChallengeState) {
  const budget = ledger.budgetFor(challenge.uniqueCode);
  return {
    phase: ledger.getState().phase,
    attemptNumber: challenge.attemptCount,
    progress: `${challenge.correctFlagCount}/${challenge.flagCount}`,
    firstAttempt: budget?.firstAttempt ?? false,
    expired: budget?.expired ?? false,
    recommendedAction: budget?.expired ? "write a final checkpoint and defer immediately" : "continue the current challenge"
  };
}

export function createBenchmarkControlTool(
  controller: BenchmarkController,
  ledger: BenchmarkLedger,
  browser: BrowserManager,
  getOwner: () => "main" | `subagent:${string}`,
  /** When set (child session), only challenge-scoped mutations are allowed and uniqueCode is locked to this value. */
  assignedChallenge?: string,
  onChallengeAcquired?: (challenge: ChallengeState) => void | Promise<void>,
  onChallengeReleased?: () => void | Promise<void>,
  persistEvidence?: (reference: string | undefined, uniqueCode: string) => Promise<string>
): ToolDefinition {
  const tool: ToolDefinition = {
    name: "benchmark_control",
    label: "Benchmark control",
    description: "Interface to the TSec benchmark platform and challenge blackboard. Coverage is low-score-first; every attempt is capped at 30 minutes (one notice at 25, checkpoint and submit stay available at the stop, then the environment is released and the challenge requeues at the tail). A revisit can earn one 10-minute extension only for verified progress in its final ten minutes (a newly accepted flag or a stage_transition checkpoint with previously unseen evidence). Unfinished challenges must remain eligible for continued solving until the platform ends the run or the operator stops it; permanent abandonment is not supported. Actions: sync, status, read_memory, acquire, checkpoint, submit, hint (attempt 2+), defer (requeues the challenge and preserves available environments in the final-three revisit stage), reset_environment (requires reason and evidenceRef; closes and releases the target for a fresh acquire/assignment), publish_intel.",
    promptSnippet: "benchmark_control(action, uniqueCode?, flag?, signal?, signalKind?, evidenceRef?, triedFamilies?, ruledOutFamilies?, currentApproach?, nextProbe?, reason?)",
    parameters: Type.Object({
      action: Type.Union([
        Type.Literal("sync"), Type.Literal("status"), Type.Literal("read_memory"), Type.Literal("acquire"),
        Type.Literal("checkpoint"), Type.Literal("submit"), Type.Literal("hint"),
        Type.Literal("defer"), Type.Literal("reset_environment"), Type.Literal("publish_intel")
      ], { description: "The platform action to perform" }),
      uniqueCode: Type.Optional(Type.String({ maxLength: 200, description: "Challenge unique_code (required for acquire/checkpoint/submit/hint/defer/reset_environment)" })),
      flag: Type.Optional(Type.String({ maxLength: 4_096, description: "Flag string to submit (submit action only)" })),
      signal: Type.Optional(Type.String({ maxLength: 2_000, description: "Concise factual observation with evidence and remaining uncertainty. Do not prescribe a current route or next steps to the next worker. Checkpoints never extend the first-attempt timer." })),
      signalKind: Type.Optional(Type.Union([
        Type.Literal("foothold"), Type.Literal("credential"), Type.Literal("privilege_change"),
        Type.Literal("exploit_primitive"), Type.Literal("stage_transition"), Type.Literal("decisive_rule_out"),
        Type.Literal("new_surface"), Type.Literal("note")
      ])),
      evidenceRef: Type.Optional(Type.String({ maxLength: 500, description: "Stable request/artifact/URL/tool evidence reference required for strong non-flag progress" })),
      currentApproach: Type.Optional(Type.String({ maxLength: 300, description: "Current hypothesis being tested; retained as an unverified candidate, not a confirmed fact." })),
      nextProbe: Type.Optional(Type.String({ maxLength: 1_000, description: "Different hypothesis or next check proposed for a later attempt; requires revalidation." })),
      supersedesEvidenceRef: Type.Optional(Type.String({ maxLength: 500, description: "Evidence reference of a previous blackboard observation invalidated or replaced by this checkpoint; explain the correction in signal" })),
      triedFamilies: Type.Optional(Type.Array(Type.String({ maxLength: 100 }), { maxItems: 20, description: "Attack families tried on this challenge" })),
      ruledOutFamilies: Type.Optional(Type.Array(Type.String({ maxLength: 100 }), { maxItems: 20, description: "Attack families ruled out by decisive evidence. Requires signalKind=decisive_rule_out and a nonempty evidenceRef; attempted-but-failed routes belong in triedFamilies" })),
      reason: Type.Optional(Type.String({ maxLength: 1_000, description: "Reason for a temporary defer or explicit environment reset" })),
      scope: Type.Optional(Type.Union([Type.Literal("global"), Type.Literal("target")], { description: "Intel visibility: global or target" })),
      target: Type.Optional(Type.String({ maxLength: 200, description: "Target identifier, hostname, address, or challenge code for target-scoped intel" })),
      intel: Type.Optional(Type.String({ maxLength: 800, description: "Bounded cross-challenge fact such as credentials, foothold, endpoint, or flag-format quirk" })),
      cursor: Type.Optional(Type.Union([Type.Integer({ minimum: 0 }), Type.String({ maxLength: 512 })], { description: "Queue offset for status; opaque nextCursor or 0 for read_memory." })),
      kind: Type.Optional(Type.Union([Type.Literal("overview"), Type.Literal("blackboard"), Type.Literal("attempts")], { description: "Optional read_memory record filter; omit to read every category." }))
    }),
    async execute(_toolCallId: string, params: { action: "sync" | "status" | "read_memory" | "acquire" | "checkpoint" | "submit" | "hint" | "defer" | "reset_environment" | "publish_intel"; uniqueCode?: string; flag?: string; signal?: string; signalKind?: ProgressSignalKind; evidenceRef?: string; supersedesEvidenceRef?: string; triedFamilies?: string[]; ruledOutFamilies?: string[]; currentApproach?: string; nextProbe?: string; reason?: string; scope?: "global" | "target"; target?: string; intel?: string; cursor?: string | number; kind?: BenchmarkMemoryKind }) {
      const challengeScoped = new Set(["acquire", "checkpoint", "submit", "hint", "defer", "reset_environment", "read_memory"]);
      const actionKey = challengeScoped.has(params.action) ? (params.uniqueCode ?? assignedChallenge) : undefined;
      const run = <T>(operation: () => Promise<T>) => actionKey ? ledger.runChallengeAction(actionKey, operation) : operation();
      return run(async () => {
      const action = params.action;
      let uniqueCode = params.uniqueCode;
      const flag = params.flag;
      const signal = params.signal;
      const cursor = params.cursor;
      const owner = getOwner();
      // Child sessions: only challenge-scoped actions are allowed, and
      // uniqueCode is locked to the assigned challenge (no cross-challenge access).
      if (assignedChallenge) {
        const allowed = new Set(["checkpoint", "submit", "hint", "defer", "reset_environment", "publish_intel", "read_memory"]);
        if (!allowed.has(action)) {
          return { content: [{ type: "text" as const, text: `benchmark_control(action="${action}") is not available to SubAgents. Allowed: ${[...allowed].join(", ")}.` }], details: { restricted: true } };
        }
        if (!uniqueCode) uniqueCode = assignedChallenge;
        if (uniqueCode !== assignedChallenge) {
          return { content: [{ type: "text" as const, text: `You are assigned to ${assignedChallenge}; operations on other challenges are not available.` }], details: { restricted: true } };
        }
      }
      try {
        switch (action) {
          case "sync": {
            const syncGuard = ledger.captureSyncGuard();
            let vpn: VpnCheckResult;
            try {
              vpn = await controller.checkVpn();
            } catch (error) {
              if (isBenchmarkError(error) && error.kind === "vpn_check_failed") {
                // Do not leave a previous successful check visible after the
                // configured health endpoint reports a failure.
                await ledger.recordVpnCheck(false, "", true);
              }
              throw error;
            }
            const challenges = await controller.listChallenges();
            // The public API has no VPN-check route. Preserve an explicit
            // checked/unchecked distinction instead of claiming success.
            const vpnChecked = vpn.status !== "unchecked";
            await ledger.syncFromPlatform(challenges, vpn.ok, vpn.client_ip, vpnChecked, syncGuard);
            // Reconcile containers that outlived a completed submit or a prior
            // close timeout. Platform completion alone must not leak one of the
            // three global slots.
            const closings = Object.values(ledger.getState().challenges)
              .filter((challenge) => challenge.status === "closing" && challenge.containerStatus !== "stopped");
            await Promise.all(closings.map((challenge) => ledger.runChallengeAction(challenge.uniqueCode, async () => {
              const current = ledger.getChallenge(challenge.uniqueCode);
              if (!current || current.status !== "closing" || !current.pendingStatus || current.containerStatus === "stopped") return;
              try {
                await controller.closeChallenge(challenge.uniqueCode);
                const afterClose = ledger.getChallenge(challenge.uniqueCode);
                if (afterClose?.status === "closing" && afterClose.pendingStatus) await ledger.confirmClosed(challenge.uniqueCode);
              } catch {
                const afterFailure = ledger.getChallenge(challenge.uniqueCode);
                if (afterFailure?.status === "closing" && afterFailure.containerStatus !== "stopped") {
                  await ledger.markCloseFailed(challenge.uniqueCode);
                }
              }
            })));
            await ledger.maybeAdvancePhase();
            const state = ledger.getState();
            if (!Object.values(state.challenges).some((challenge) => challenge.owner === owner)) await onChallengeReleased?.();
            return {
              content: [{ type: "text" as const, text: [
                vpnChecked ? `VPN: ok (${vpn.client_ip})` : `VPN: not prechecked (BENCHMARK_VPN_URL is not configured; ensure SSLVPN is connected before opening containers)`,
                `Phase: ${state.phase}`,
                `Run elapsed: ${Math.floor(ledger.runElapsedMs() / 60_000)}m`,
                `Score: ${scoreLabel(state)}`,
                `Challenges: ${state.totalChallenges} total, ${state.solvedCount} solved, ${state.totalChallenges - state.solvedCount} unfinished, ${state.activeContainers} active containers`,
                `Available candidates: ${ledger.candidates(5).map((challenge) => challenge.uniqueCode).join(", ") || "(none)"}`
              ].join("\n") }],
              details: { phase: state.phase, score: state.cumulativeScore, total: state.totalChallenges }
            };
          }
          case "read_memory": {
            const page = await readBenchmarkMemory(ledger, uniqueCode, cursor, params.kind);
            return { content: [{ type: "text" as const, text: JSON.stringify(page) }], details: page };
          }
          case "status": {
            const state = ledger.getState();
            if (typeof cursor === "string") throw new Error("status requires a numeric queue cursor.");
            const offset = Math.max(0, Math.floor(cursor ?? 0));
            const queue = ledger.candidates(10, offset);
            const mine = Object.values(state.challenges).filter((challenge) => challenge.owner === owner);
            const lines = [
              `Schedule: ${state.phase} | Run elapsed: ${Math.floor(ledger.runElapsedMs() / 60_000)}m | Score: ${scoreLabel(state)} | Solved: ${state.solvedCount}/${state.totalChallenges} | Containers: ${state.activeContainers}/3`,
              mine.length ? `My challenge: ${mine.map((challenge) => `${challenge.uniqueCode} (${challenge.status}, flags ${challenge.correctFlagCount}/${challenge.flagCount})`).join("; ")}` : "My challenge: (none — acquire one)",
              `SubAgent challenges: ${ledger.activeSubagentCount()}/2 active`,
              `Candidates ${offset}-${offset + queue.length}:`,
              ...queue.map((challenge) => `  ${challenge.uniqueCode} | ${challenge.difficulty} | ${challenge.totalScore}pts | ${challenge.flagCount} flags | ${challenge.status}`)
            ];
            if (mine[0] && ledger.isBudgetExhausted(mine[0].uniqueCode)) {
              lines.push(`\nFIRST_ATTEMPT_COMPLETE on ${mine[0].uniqueCode}. Save a concise blackboard checkpoint and defer immediately; solving tools are now blocked.`);
            }
            const hasMore = queue.length === 10;
            return { content: [{ type: "text" as const, text: lines.join("\n") }], details: { phase: state.phase, nextCursor: hasMore ? offset + 10 : null } };
          }
          case "acquire": {
            if (!uniqueCode) throw new Error("uniqueCode is required for acquire");
            const reusable = ledger.getChallenge(uniqueCode);
            const reuseLiveContainer = hasReusableBenchmarkContainer(reusable);
            // Phase 1: reserve atomically (checks owner, container limit, one-worker-one-challenge).
            await ledger.reserve(uniqueCode, owner);
            let startResult;
            if (reuseLiveContainer) {
              startResult = { unique_code: uniqueCode, container_addr: reusable!.containerAddrs };
            } else {
              try {
                // Phase 2: platform start.
                startResult = await controller.startChallenge(uniqueCode);
              } catch (error) {
                // Rollback the reservation on failure.
                await ledger.releaseReservation(uniqueCode, owner, isBenchmarkError(error) && error.kind === "resource_unavailable"
                  ? { resourceUnavailable: true, reason: error.message }
                  : undefined);
                await ledger.maybeAdvancePhase();
                throw error;
              }
            }
            // Phase 3: confirm and activate.
            const challenge = await ledger.confirmStarted(uniqueCode, startResult.container_addr, owner);
            await onChallengeAcquired?.(challenge);
            // Grant precise browser scope: normalize bare IP:port to http:// URL,
            // use exact-port grant so only this host:port is allowed.
            for (const addr of startResult.container_addr) {
              const normalized = addr.includes("://") ? addr : `http://${addr}/`;
              browser.grantScope(normalized, true);
            }
            return {
              content: [{ type: "text" as const, text: `Acquired ${challenge.uniqueCode} (${challenge.difficulty}, ${challenge.totalScore}pts, ${challenge.flagCount} flags; attempt ${challenge.attemptCount}).\nContainer: ${startResult.container_addr.join(", ")}${reuseLiveContainer ? " (reused existing environment)" : ""}${challenge.attemptCount > 1 && !reuseLiveContainer ? " (fresh instance: cookies, sessions, tunnels and in-memory footholds from previous attempts do not exist; blackboard knowledge, static credentials, artifacts and scripts in work/ remain valid)" : ""}\nDescription: ${challenge.description}${recoveryText(challenge, ledger.isEndgame())}\n${challenge.attemptCount === 1 ? "This first-attempt clock is silent: one warning at 25 minutes, hard stop at 30 minutes." : ledger.isEndgame() ? BENCHMARK_ENDGAME_GUIDANCE : "This revisit runs under the same 30-minute deadline (one extension possible only for verified progress in its final ten minutes). Replay your recorded access recipe before probing anything new; if truly exhausted, checkpoint and defer it for the end."}` }],
              details: { uniqueCode: challenge.uniqueCode, containerAddrs: startResult.container_addr, schedule: scheduleSnapshot(ledger, challenge) }
            };
          }
          case "checkpoint": {
            if (!uniqueCode) throw new Error("uniqueCode is required for checkpoint");
            if (!signal) throw new Error("signal is required for checkpoint");
            const owned = persistEvidence ? await ledger.assertOwned(uniqueCode, owner) : undefined;
            const evidenceRef = persistEvidence ? await persistEvidence(params.evidenceRef, uniqueCode) : params.evidenceRef;
            let supersedesEvidenceRef = params.supersedesEvidenceRef;
            if (persistEvidence && supersedesEvidenceRef && !owned?.blackboard.some((entry) => entry.evidenceRef === supersedesEvidenceRef)) {
              supersedesEvidenceRef = await persistEvidence(supersedesEvidenceRef, uniqueCode);
            }
            const result = await ledger.checkpoint(uniqueCode, signal, params.triedFamilies, params.nextProbe, owner, {
              currentApproach: params.currentApproach,
              signalKind: params.signalKind,
              evidenceRef,
              supersedesEvidenceRef,
              ruledOutFamilies: params.ruledOutFamilies
            });
            return {
              content: [{ type: "text" as const, text: result.extended ? "Challenge blackboard updated with observations and evidence. Verified stage progress extended this revisit by 10 minutes (one extension per attempt)." : "Challenge blackboard updated with observations and evidence. Checkpoints never extend attempt 1; a revisit can earn one 10-minute extension only from a stage_transition with previously unseen evidence." }, { type: "text" as const, text: JSON.stringify({ evidenceRef: evidenceRef ?? null, supersedesEvidenceRef: supersedesEvidenceRef ?? null }) }],
              details: { uniqueCode: uniqueCode, updated: result.updated, extended: result.extended, evidenceRef, supersedesEvidenceRef, schedule: scheduleSnapshot(ledger, result.challenge) }
            };
          }
          case "submit": {
            if (!uniqueCode || !flag) throw new Error("uniqueCode and flag are required for submit");
            const beforeCorrectFlagCount = (await ledger.assertOwned(uniqueCode, owner)).correctFlagCount;
            if (ledger.hasTriedFlag(uniqueCode, flag)) {
              return {
                content: [{ type: "text" as const, text: `This exact flag was already attempted for ${uniqueCode}. Do not resubmit it; sync if the prior response was ambiguous, otherwise pursue a different candidate.` }],
                details: { duplicateLocal: true }
              };
            }
            const pending = pendingSubmission(ledger, uniqueCode, flag);
            // Exhaustion stops automatic replay, not an explicit worker retry.
            if (pending?.exhausted) clearPendingSubmission(ledger, uniqueCode, flag);
            if (pending && !pending.exhausted) return { content: [{ type: "text" as const, text: "This candidate is already queued for confirmation. The runtime will reconcile and retry after backoff; continue other useful work." }], details: { outcomeUnknown: true, queued: true } };
            let submitResult: Awaited<ReturnType<BenchmarkController["submitFlag"]>>;
            let wasDuplicate = false;
            let reconciledAfterAmbiguous = false;
            let retriedAfterAmbiguous = false;
            const snapshotAsSubmitResult = (match: Awaited<ReturnType<BenchmarkController["listChallenges"]>>[number]) => ({
              correct: true,
              awarded: 0,
              cumulative_score: 0,
              correct_flag_count: match.correct_flag_count,
              total_flag_count: match.flag_count,
              matched_flag_index: null,
              unique_code: uniqueCode
            });
            const challengeSnapshot = async () => (await controller.listChallenges())
              .find((challenge) => challenge.unique_code === uniqueCode);
            try {
              submitResult = await controller.submitFlag(uniqueCode, flag);
            } catch (error) {
              if (isAmbiguousMutationError(error)) {
                // A dropped response does not tell us whether the platform saw
                // the flag. First reconcile. If progress is unchanged, retry
                // this exact candidate once: a correct first request becomes a
                // duplicate, while a request that never arrived gets a real
                // answer. A second ambiguous result enters the pending confirmation queue.
                let match: Awaited<ReturnType<BenchmarkController["listChallenges"]>>[number] | undefined;
                try {
                  match = await challengeSnapshot();
                } catch {
                  // A failed readback still permits the single bounded retry.
                }
                if (match && (match.correct_flag_count > beforeCorrectFlagCount || match.is_completed)) {
                  reconciledAfterAmbiguous = true;
                  submitResult = snapshotAsSubmitResult(match);
                } else {
                  try {
                    retriedAfterAmbiguous = true;
                    submitResult = await controller.submitFlag(uniqueCode, flag);
                  } catch (retryError) {
                    if (isBenchmarkError(retryError) && retryError.kind === "duplicate_submit") {
                      wasDuplicate = true;
                      reconciledAfterAmbiguous = true;
                      const duplicateMatch = await challengeSnapshot();
                      if (!duplicateMatch) {
                        return { content: [{ type: "text" as const, text: `The retry for ${uniqueCode} was reported as duplicate, but the challenge is absent from the platform list. Sync and verify state before continuing.` }], details: { error: "challenge_not_found_on_duplicate" } };
                      }
                      submitResult = snapshotAsSubmitResult(duplicateMatch);
                    } else if (isAmbiguousMutationError(retryError)) {
                      enqueuePendingSubmission(ledger, uniqueCode, flag, owner);
                      await ledger.recordSubmissionAttempt(uniqueCode, flag, owner);
                      return {
                        content: [{ type: "text" as const, text: `Submission for ${uniqueCode} remained ambiguous after one bounded retry. The exact candidate is queued for bounded confirmation and retry after backoff. Continue other useful work; do not resubmit it manually.` }],
                        details: { outcomeUnknown: true, retriedOnce: true, queued: true }
                      };
                    } else {
                      throw retryError;
                    }
                  }
                }
              } else if (isBenchmarkError(error) && error.kind === "duplicate_submit") {
                // A duplicate means the flag was already accepted. Sync the
                // platform for authoritative counts. Fail closed if the
                // challenge is not found — a phantom duplicate is suspicious.
                wasDuplicate = true;
                const match = await challengeSnapshot();
                if (!match) {
                  return { content: [{ type: "text" as const, text: `Duplicate submit for ${uniqueCode}, but the challenge is not found on the platform. Sync and verify the challenge state before continuing.` }], details: { error: "challenge_not_found_on_duplicate" } };
                }
                submitResult = snapshotAsSubmitResult(match);
              } else {
                throw error;
              }
            }
            // Ownership was checked before dispatch under this challenge's action
            // lock. Sync may release it while the response is in flight; merge
            // that response, while still rejecting a different non-null owner.
            await ledger.recordSubmission(
              uniqueCode, flag, submitResult.correct,
              wasDuplicate || reconciledAfterAmbiguous ? undefined : submitResult.cumulative_score, submitResult.correct_flag_count, submitResult.matched_flag_index, owner, true
            );
            if (submitResult.correct && submitResult.correct_flag_count >= submitResult.total_flag_count) {
              // Mark solved first (score is real), then attempt close; a close
              // failure doesn't un-solve the challenge but must be surfaced.
              const solved = await ledger.markSolved(uniqueCode, wasDuplicate || reconciledAfterAmbiguous ? undefined : submitResult.cumulative_score, owner, true);
              let closeNote = "Container closed.";
              if (solved.status === "closing") {
                try {
                  await controller.closeChallenge(uniqueCode);
                  await ledger.confirmClosed(uniqueCode);
                } catch {
                  await ledger.markCloseFailed(uniqueCode);
                  closeNote = "Container close FAILED — it may still occupy a platform slot; sync will reconcile.";
                }
              }
              await ledger.maybeAdvancePhase();
              await onChallengeReleased?.();
              return {
                content: [{ type: "text" as const, text: reconciledAfterAmbiguous
                  ? `✓ Platform reconciliation confirms ${uniqueCode} is fully solved. Exact awarded/challenge score is unavailable from the list endpoint. ${closeNote} Acquire the next challenge.`
                  : `✓ CORRECT! Challenge ${uniqueCode} fully solved (+${submitResult.awarded} pts). Challenge score: ${submitResult.cumulative_score}. Run total: ${scoreLabel(ledger.getState())}. ${closeNote} Acquire the next challenge.` }],
                details: { correct: true, solved: true, awarded: submitResult.awarded, retriedAfterAmbiguous }
              };
            }
            if (submitResult.correct) {
              const prefix = reconciledAfterAmbiguous
                ? "Platform reconciliation confirms the ambiguous submission increased progress."
                : wasDuplicate ? "Flag already submitted (duplicate — no penalty, already correct)." : `✓ Flag ${submitResult.matched_flag_index !== null ? submitResult.matched_flag_index + 1 : "?"} correct (+${submitResult.awarded} pts).`;
              const nextInstruction = ledger.isBudgetExhausted(uniqueCode)
                ? "The flag was recorded, but the first-attempt limit has ended. Save final blackboard notes and defer immediately."
                : "Challenge remains active. Continue finding the remaining flags; a correct flag does not extend the first-attempt limit.";
              return {
                content: [{ type: "text" as const, text: `${prefix} Progress: ${submitResult.correct_flag_count}/${submitResult.total_flag_count}. ${nextInstruction}` }],
                details: { correct: true, partial: true, duplicate: wasDuplicate, retriedAfterAmbiguous, schedule: scheduleSnapshot(ledger, ledger.getChallenge(uniqueCode)!) }
              };
            }
            return {
              content: [{ type: "text" as const, text: `✗ Incorrect flag. Progress: ${submitResult.correct_flag_count}/${submitResult.total_flag_count}. Continue testing.` }],
              details: { correct: false }
            };
          }
          case "hint": {
            if (!uniqueCode) throw new Error("uniqueCode is required for hint");
            const state = ledger.getState();
            const challenge = state.challenges[uniqueCode];
            if (!challenge) throw new Error(`Challenge ${uniqueCode} not found`);
            if (challenge.attemptCount < 2) {
              return { content: [{ type: "text" as const, text: `Hints are unavailable on the first attempt. Finish coverage before revisiting ${uniqueCode}.` }], details: { hintBlocked: true } };
            }
            if (challenge.owner && challenge.owner !== owner) throw new Error(`Challenge ${uniqueCode} is owned by ${challenge.owner}, not by ${owner}`);
            if (challenge.hintUsed) {
              return {
                content: [{ type: "text" as const, text: challenge.hintContent ? `Hint for ${uniqueCode} was already purchased: ${challenge.hintContent}` : `A hint request for ${uniqueCode} was already recorded; do not pay for it again.` }],
                details: { uniqueCode, cached: true }
              };
            }
            const hint = await controller.getHint(uniqueCode);
            await ledger.recordHint(uniqueCode, hint.hint, owner);
            return {
              content: [{ type: "text" as const, text: hint.hint ? `Hint for ${uniqueCode} (score deducted): ${hint.hint}` : `No hint available for ${uniqueCode}.` }],
              details: { uniqueCode: uniqueCode }
            };
          }
          case "reset_environment":
          case "defer": {
            if (!uniqueCode) throw new Error("uniqueCode is required for defer");
            const reset = action === "reset_environment";
            if (reset && (!params.reason?.trim() || !params.evidenceRef?.trim())) {
              throw new Error("reset_environment requires a concrete failure reason and evidenceRef; changing worker or hypothesis alone is not an environment failure");
            }
            if (reset && persistEvidence) await ledger.assertOwned(uniqueCode, owner);
            const resetEvidenceRef = reset && persistEvidence ? await persistEvidence(params.evidenceRef, uniqueCode) : params.evidenceRef;
            const reason = reset ? `Environment reset [${resetEvidenceRef}]: ${params.reason}` : params.reason ?? "attempt ended";
            const challenge = await ledger.defer(uniqueCode, reason, params.nextProbe, owner, reset);
            if (challenge.status === "orphaned") {
              await onChallengeReleased?.();
              return { content: [{ type: "text" as const, text: `Attempt ended for ${uniqueCode}. Environment preserved at ${challenge.containerAddrs.join(", ")}; ownership released for handoff. Resume the existing environment and valid partial work. Do not reset simply to change worker or hypothesis.` }], details: { uniqueCode, status: "orphaned", environmentPreserved: true } };
            }
            // Close must be confirmed by the platform — a failed close keeps the
            // container alive and the slot occupied; we surface that honestly.
            try {
              await controller.closeChallenge(uniqueCode);
              await ledger.confirmClosed(uniqueCode);
            } catch {
              await ledger.markCloseFailed(uniqueCode);
              return {
                content: [{ type: "text" as const, text: `Deferred ${uniqueCode}, but the container close FAILED — it is still running on the platform and occupying a slot. The main Agent must run benchmark_control(action="sync") to reconcile it; do not retry defer because ownership has already been released.` }],
                details: { uniqueCode, status: "closing", closeFailed: true }
              };
            } finally {
              await onChallengeReleased?.();
            }
            await ledger.maybeAdvancePhase();
            return {
              content: [{ type: "text" as const, text: `${reset ? "Environment reset requested for" : "Deferred"} ${uniqueCode} (${challenge.deferredReason}). Container closed; evidence is retained. ${reset ? "The current attempt is released. The main Agent must acquire or assign this challenge again to start a fresh environment." : "Acquire the next eligible challenge."}` }],
              details: { uniqueCode: uniqueCode, status: "deferred" }
            };
          }
          case "publish_intel": {
            if (!params.intel) throw new Error("intel is required for publish_intel");
            const scope = params.scope ?? "target";
            const target = scope === "global" ? "*" : (params.target ?? uniqueCode ?? assignedChallenge ?? "");
            const entry = await ledger.publishIntel(scope, target, params.intel);
            return {
              content: [{ type: "text" as const, text: `Shared intelligence recorded for ${entry.scope === "global" ? "all challenges" : entry.target}. It will be included only in relevant future briefs.` }],
              details: { published: true, scope: entry.scope, target: entry.target }
            };
          }
          default:
            return { content: [{ type: "text" as const, text: `Unknown action: ${action}` }], details: {} };
        }
      } catch (error) {
        if (isBenchmarkError(error)) {
          return { content: [{ type: "text" as const, text: friendlyError(error) }] };
        }
        throw error;
      }
      });
    }
  } as ToolDefinition;
  return tool;
}
