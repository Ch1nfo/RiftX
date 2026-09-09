import { Type } from "@sinclair/typebox";
import type { ToolDefinition } from "@mariozechner/pi-coding-agent";
import { BenchmarkError, type BenchmarkController, type VpnCheckResult } from "../controller";
import { budgetPolicyFor, hasReusableBenchmarkContainer, isPartialChallenge, type BenchmarkLedger, type ChallengeState, type ProgressSignalKind } from "../ledger";
import type { BrowserManager } from "@/browser";
import { reconcileExpiredHandoffs, scheduleHandoffCleanup } from "../handoff";

function friendlyError(error: BenchmarkError): string {
  switch (error.kind) {
    case "vpn_check_failed": return `VPN check failed — connect the benchmark VPN, then benchmark_control(action="sync") again. Detail: ${error.message}`;
    case "invalid_state_max_active": return `Container limit reached (3). benchmark_control(action="defer") or (action="abandon") one running challenge first, then retry.`;
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

function scoreLabel(state: Readonly<ReturnType<BenchmarkLedger["getState"]>>): string {
  return state.scoreExact ? String(state.cumulativeScore) : `${state.cumulativeScore}+ (one or more challenge scores are unavailable from the list endpoint)`;
}

function recoveryText(challenge: ChallengeState): string {
  const attempts = challenge.approachHistory.slice(-4);
  const lines = [
    challenge.lastMeaningfulSignalContent
      ? `Previous meaningful signal: ${challenge.lastMeaningfulSignalContent}`
      : challenge.lastSignalContent ? `Latest checkpoint (did not extend time): ${challenge.lastSignalContent}` : "",
    challenge.triedFamilies.length ? `Already tried: ${challenge.triedFamilies.join(", ")}` : "",
    challenge.nextProbe ? `Saved next probe: ${challenge.nextProbe}` : "",
    challenge.deferredReason ? `Previous defer reason: ${challenge.deferredReason}` : "",
    challenge.hintContent ? `Hint already purchased: ${challenge.hintContent}` : "",
    attempts.length ? `Previous approaches: ${attempts.map((attempt) => `${attempt.attemptNumber}:${attempt.approach}`).join(" | ")}` : "",
    attempts.some((attempt) => attempt.ruledOutFamilies.length) ? `Ruled out: ${[...new Set(attempts.flatMap((attempt) => attempt.ruledOutFamilies))].join(", ")}` : "",
    challenge.attemptCount > 1 ? "STRATEGY RESET: choose a materially different hypothesis; do not repeat prior tools with cosmetic changes." : ""
  ].filter(Boolean);
  return lines.length ? `\nRecovery notes (do not repeat these attempts):\n${lines.join("\n")}` : "";
}

function scheduleSnapshot(ledger: BenchmarkLedger, challenge: ChallengeState) {
  const budget = ledger.budgetFor(challenge.uniqueCode);
  const previousApproaches = challenge.approachHistory.slice(-4).map((attempt) => attempt.approach);
  return {
    phase: ledger.getState().phase,
    attemptNumber: challenge.attemptCount,
    progress: `${challenge.correctFlagCount}/${challenge.flagCount}`,
    elapsedMs: budget?.elapsedMs ?? 0,
    timeSinceProgressMs: budget?.sinceProgressMs ?? 0,
    softDeadlineAt: challenge.lastMeaningfulProgressAt && budget ? challenge.lastMeaningfulProgressAt + budget.policy.noProgressMs : null,
    hardDeadlineAt: challenge.hardDeadlineAt,
    signalExtensionsRemaining: Number.isFinite(budget?.policy.maxSignalExtensions)
      ? Math.max(0, (budget?.policy.maxSignalExtensions ?? 0) - challenge.progressExtensions)
      : null,
    policy: budget?.policy.label ?? "none",
    expired: budget?.expired ?? false,
    recommendedAction: budget?.expired
      ? (budget.workerRotationDue ? "warm handoff to a fresh worker" : "submit, checkpoint new evidence, or defer")
      : "continue the current challenge",
    previousApproaches
  };
}

export function createBenchmarkControlTool(
  controller: BenchmarkController,
  ledger: BenchmarkLedger,
  browser: BrowserManager,
  getOwner: () => "main" | `subagent:${string}`,
  /** When set (child session), only challenge-scoped mutations are allowed and uniqueCode is locked to this value. */
  assignedChallenge?: string,
  onChallengeAcquired?: (challenge: ChallengeState) => void,
  onChallengeReleased?: () => void
): ToolDefinition {
  const tool: ToolDefinition = {
    name: "benchmark_control",
    label: "Benchmark control",
    description: "Interface to the TSec benchmark platform and shared run ledger. Actions: sync, status, acquire, evidence-backed checkpoint, submit, hint (recovery/endgame only), defer/warm-handoff, abandon, publish_intel.",
    promptSnippet: "benchmark_control(action, uniqueCode?, flag?, signal?, signalKind?, evidenceRef?, currentApproach?, ruledOutFamilies?, nextProbe?, reason?)",
    parameters: Type.Object({
      action: Type.Union([
        Type.Literal("sync"), Type.Literal("status"), Type.Literal("acquire"),
        Type.Literal("checkpoint"), Type.Literal("submit"), Type.Literal("hint"),
        Type.Literal("defer"), Type.Literal("abandon"), Type.Literal("publish_intel")
      ], { description: "The platform action to perform" }),
      uniqueCode: Type.Optional(Type.String({ maxLength: 200, description: "Challenge unique_code (required for acquire/checkpoint/submit/hint/defer/abandon)" })),
      flag: Type.Optional(Type.String({ maxLength: 4_096, description: "Flag string to submit (submit action only)" })),
      signal: Type.Optional(Type.String({ maxLength: 2_000, description: "Checkpoint observation. Only evidence-backed progress resets the attempt clock." })),
      signalKind: Type.Optional(Type.Union([
        Type.Literal("foothold"), Type.Literal("credential"), Type.Literal("privilege_change"),
        Type.Literal("exploit_primitive"), Type.Literal("stage_transition"), Type.Literal("decisive_rule_out"),
        Type.Literal("new_surface"), Type.Literal("note")
      ])),
      evidenceRef: Type.Optional(Type.String({ maxLength: 500, description: "Stable request/artifact/URL/tool evidence reference required for strong non-flag progress" })),
      currentApproach: Type.Optional(Type.String({ maxLength: 300, description: "Current attack approach; recovery attempts should choose a materially different one" })),
      triedFamilies: Type.Optional(Type.Array(Type.String({ maxLength: 100 }), { maxItems: 20, description: "Attack families tried on this challenge" })),
      ruledOutFamilies: Type.Optional(Type.Array(Type.String({ maxLength: 100 }), { maxItems: 20, description: "Attack families ruled out by decisive evidence" })),
      nextProbe: Type.Optional(Type.String({ maxLength: 1_000, description: "The exact next action to take" })),
      reason: Type.Optional(Type.String({ maxLength: 1_000, description: "Reason for defer or abandon" })),
      scope: Type.Optional(Type.Union([Type.Literal("global"), Type.Literal("target")], { description: "Intel visibility: global or target" })),
      target: Type.Optional(Type.String({ maxLength: 200, description: "Target identifier, hostname, address, or challenge code for target-scoped intel" })),
      intel: Type.Optional(Type.String({ maxLength: 800, description: "Bounded cross-challenge fact such as credentials, foothold, endpoint, or flag-format quirk" })),
      cursor: Type.Optional(Type.Number({ description: "Pagination offset for status (0-based)" }))
    }),
    async execute(_toolCallId: string, params: { action: "sync" | "status" | "acquire" | "checkpoint" | "submit" | "hint" | "defer" | "abandon" | "publish_intel"; uniqueCode?: string; flag?: string; signal?: string; signalKind?: ProgressSignalKind; evidenceRef?: string; currentApproach?: string; triedFamilies?: string[]; ruledOutFamilies?: string[]; nextProbe?: string; reason?: string; scope?: "global" | "target"; target?: string; intel?: string; cursor?: number }) {
      return ledger.runAction(async () => {
      await reconcileExpiredHandoffs(controller, ledger);
      const action = params.action;
      let uniqueCode = params.uniqueCode;
      const flag = params.flag;
      const signal = params.signal;
      const cursor = params.cursor;
      const owner = getOwner();
      // Child sessions: only checkpoint/submit/defer/abandon are allowed, and
      // uniqueCode is locked to the assigned challenge (no cross-challenge access).
      if (assignedChallenge) {
        const allowed = new Set(["checkpoint", "submit", "defer", "abandon", "publish_intel"]);
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
            await ledger.syncFromPlatform(challenges, vpn.ok, vpn.client_ip, vpnChecked);
            // Reconcile containers that outlived a completed submit or a prior
            // close timeout. Platform completion alone must not leak one of the
            // three global slots.
            const closings = Object.values(ledger.getState().challenges)
              .filter((challenge) => challenge.status === "closing" && challenge.containerStatus !== "stopped");
            for (const challenge of closings) {
              try {
                await controller.closeChallenge(challenge.uniqueCode);
                await ledger.confirmClosed(challenge.uniqueCode);
              } catch {
                await ledger.markCloseFailed(challenge.uniqueCode);
              }
            }
            await ledger.maybeAdvancePhase();
            const state = ledger.getState();
            if (!Object.values(state.challenges).some((challenge) => challenge.owner === owner)) onChallengeReleased?.();
            return {
              content: [{ type: "text" as const, text: [
                vpnChecked ? `VPN: ok (${vpn.client_ip})` : `VPN: not prechecked (BENCHMARK_VPN_URL is not configured; ensure SSLVPN is connected before opening containers)`,
                `Phase: ${state.phase}`,
                `Run elapsed: ${Math.floor(ledger.runElapsedMs() / 60_000)}m`,
                `Score: ${scoreLabel(state)}`,
                `Challenges: ${state.totalChallenges} total, ${state.solvedCount} solved, ${state.exhaustedCount} exhausted, ${state.activeContainers} active containers`,
                `Available candidates: ${ledger.candidates(5).map((challenge) => challenge.uniqueCode).join(", ") || "(none)"}`
              ].join("\n") }],
              details: { phase: state.phase, score: state.cumulativeScore, total: state.totalChallenges }
            };
          }
          case "status": {
            const state = ledger.getState();
            const offset = Math.max(0, Math.floor(cursor ?? 0));
            const queue = ledger.candidates(10, offset);
            const terminalUnsolved = Object.values(state.challenges)
              .filter((challenge) => !challenge.isCompleted && challenge.status === "exhausted")
              .sort((left, right) => left.uniqueCode.localeCompare(right.uniqueCode));
            const terminalPage = terminalUnsolved.slice(offset, offset + 10);
            const mine = Object.values(state.challenges).filter((challenge) => challenge.owner === owner);
            const lines = [
              `Phase: ${state.phase} | Run elapsed: ${Math.floor(ledger.runElapsedMs() / 60_000)}m | Score: ${scoreLabel(state)} | Solved: ${state.solvedCount}/${state.totalChallenges} | Containers: ${state.activeContainers}/3`,
              mine.length ? `My challenge: ${mine.map((challenge) => `${challenge.uniqueCode} (${challenge.status}, flags ${challenge.correctFlagCount}/${challenge.flagCount})`).join("; ")}` : "My challenge: (none — acquire one)",
              `SubAgent challenges: ${ledger.activeSubagentCount()}/2 active`,
              `Candidates ${offset}-${offset + queue.length}:`,
              ...queue.map((challenge) => `  ${challenge.uniqueCode} | ${challenge.difficulty} | ${challenge.totalScore}pts | ${challenge.flagCount} flags | ${challenge.status}`)
            ];
            if (terminalPage.length) {
              lines.push(
                `Unsolved terminal challenges ${offset}-${offset + terminalPage.length} of ${terminalUnsolved.length}:`,
                ...terminalPage.map((challenge) => `  ${challenge.uniqueCode} | flags ${challenge.correctFlagCount}/${challenge.flagCount} | ${challenge.deferredReason || "no viable hypothesis recorded"}`)
              );
            }
            if (mine[0]) {
              const schedule = scheduleSnapshot(ledger, mine[0]);
              lines.splice(2, 0, `Attempt: ${schedule.attemptNumber} | Policy: ${schedule.policy} | Progress: ${schedule.progress} | Elapsed: ${Math.floor(schedule.elapsedMs / 60_000)}m | Since progress: ${Math.floor(schedule.timeSinceProgressMs / 60_000)}m | Extensions left: ${schedule.signalExtensionsRemaining ?? "unbounded"}`);
            }
            if (mine[0] && ledger.isBudgetExhausted(mine[0].uniqueCode)) {
              const budget = ledger.budgetFor(mine[0].uniqueCode)!;
              lines.push(`\n⚠ TIMEBOX_EXPIRED on ${mine[0].uniqueCode} (${budget.policy.label}; ${Math.floor(budget.sinceProgressMs / 60_000)}m without meaningful progress). Submit a confirmed flag, record evidence-backed progress, or defer for a different approach${budget.workerRotationDue ? " with a fresh worker" : ""}.`);
            }
            const hasMore = queue.length === 10 || offset + terminalPage.length < terminalUnsolved.length;
            return { content: [{ type: "text" as const, text: lines.join("\n") }], details: { phase: state.phase, nextCursor: hasMore ? offset + 10 : null } };
          }
          case "acquire": {
            if (!uniqueCode) throw new Error("uniqueCode is required for acquire");
            const reusable = ledger.getChallenge(uniqueCode);
            const reuseLiveContainer = hasReusableBenchmarkContainer(reusable);
            if (reuseLiveContainer && reusable?.status === "handoff_waiting" && owner === "main") {
              return {
                content: [{ type: "text" as const, text: `${uniqueCode} is waiting for a warm handoff. Use assign_benchmark_challenge so a fresh worker receives the preserved container and authenticated browser state; main acquire is intentionally skipped.` }],
                details: { uniqueCode, handoffRequiresSubagent: true }
              };
            }
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
                await ledger.releaseReservation(uniqueCode, owner);
                throw error;
              }
            }
            // Phase 3: confirm and activate.
            const challenge = await ledger.confirmStarted(uniqueCode, startResult.container_addr, owner);
            onChallengeAcquired?.(challenge);
            // Grant precise browser scope: normalize bare IP:port to http:// URL,
            // use exact-port grant so only this host:port is allowed.
            for (const addr of startResult.container_addr) {
              const normalized = addr.includes("://") ? addr : `http://${addr}/`;
              browser.grantScope(normalized, true);
            }
            return {
              content: [{ type: "text" as const, text: `Acquired ${challenge.uniqueCode} (${challenge.difficulty}, ${challenge.totalScore}pts, ${challenge.flagCount} flags; attempt ${challenge.attemptCount}).\nContainer: ${startResult.container_addr.join(", ")}${reuseLiveContainer ? " (warm handoff; existing state preserved)" : ""}\nDescription: ${challenge.description}${recoveryText(challenge)}${ledger.intelForChallenge(challenge, startResult.container_addr).length ? `\nRelevant shared intel:\n${ledger.intelForChallenge(challenge, startResult.container_addr).map((entry) => `- ${entry.target}: ${entry.intel}`).join("\n")}` : ""}\nAttempt policy: ${budgetPolicyFor(ledger.getState()).label}. Record evidence-backed progress with checkpoint.` }],
              details: { uniqueCode: challenge.uniqueCode, containerAddrs: startResult.container_addr, schedule: scheduleSnapshot(ledger, challenge) }
            };
          }
          case "checkpoint": {
            if (!uniqueCode) throw new Error("uniqueCode is required for checkpoint");
            if (!signal) throw new Error("signal is required for checkpoint");
            const result = await ledger.checkpoint(uniqueCode, signal, params.triedFamilies, params.nextProbe, owner, {
              signalKind: params.signalKind,
              evidenceRef: params.evidenceRef,
              currentApproach: params.currentApproach,
              ruledOutFamilies: params.ruledOutFamilies
            });
            const elapsedMin = Math.floor(ledger.signalElapsedMs(uniqueCode) / 60_000);
            return {
              content: [{ type: "text" as const, text: result.extended
                ? `Evidence-backed progress recorded. Attempt clock extended. Next probe: ${result.challenge.nextProbe || "(not set)"}`
                : `Checkpoint saved but the attempt clock was NOT extended (${elapsedMin}m since meaningful progress). A changed description alone is not progress; provide a new evidenceRef with a qualifying signalKind, or switch/defer.` }],
              details: { uniqueCode: uniqueCode, updated: result.updated, extended: result.extended, schedule: scheduleSnapshot(ledger, result.challenge) }
            };
          }
          case "submit": {
            if (!uniqueCode || !flag) throw new Error("uniqueCode and flag are required for submit");
            const before = await ledger.assertOwned(uniqueCode, owner);
            if (ledger.hasTriedFlag(uniqueCode, flag)) {
              return {
                content: [{ type: "text" as const, text: `This exact flag was already attempted for ${uniqueCode}. Do not resubmit it; sync if the prior response was ambiguous, otherwise pursue a different candidate.` }],
                details: { duplicateLocal: true }
              };
            }
            let submitResult;
            let wasDuplicate = false;
            let reconciledAfterTimeout = false;
            try {
              submitResult = await controller.submitFlag(uniqueCode, flag);
            } catch (error) {
              if (isBenchmarkError(error) && error.kind === "timeout") {
                // Never replay an ambiguous flag blindly: a wrong submission
                // may be penalized twice. Compare authoritative progress with
                // the snapshot taken while this owner held the action lock.
                await ledger.recordSubmissionAttempt(uniqueCode, flag, owner);
                let match;
                try {
                  match = (await controller.listChallenges()).find((challenge) => challenge.unique_code === uniqueCode);
                } catch {
                  return {
                    content: [{ type: "text" as const, text: `Submission for ${uniqueCode} timed out and reconciliation also failed. The exact flag has been remembered; do NOT resubmit it. Sync when connectivity returns.` }],
                    details: { outcomeUnknown: true }
                  };
                }
                if (!match || (match.correct_flag_count <= before.correctFlagCount && !match.is_completed)) {
                  return {
                    content: [{ type: "text" as const, text: `Submission for ${uniqueCode} timed out. Platform progress did not prove acceptance, so RiftX did not retry and risk a second penalty. Do NOT resubmit this exact flag; sync later and continue with a different hypothesis.` }],
                    details: { outcomeUnknown: true }
                  };
                }
                reconciledAfterTimeout = true;
                submitResult = {
                  correct: true,
                  awarded: 0,
                  cumulative_score: 0,
                  correct_flag_count: match.correct_flag_count,
                  total_flag_count: match.flag_count,
                  matched_flag_index: null,
                  unique_code: uniqueCode
                };
              } else if (isBenchmarkError(error) && error.kind === "duplicate_submit") {
                // A duplicate means the flag was already accepted. Sync the
                // platform for authoritative counts. Fail closed if the
                // challenge is not found — a phantom duplicate is suspicious.
                wasDuplicate = true;
                const challenges = await controller.listChallenges();
                const match = challenges.find((challenge) => challenge.unique_code === uniqueCode);
                if (!match) {
                  return { content: [{ type: "text" as const, text: `Duplicate submit for ${uniqueCode}, but the challenge is not found on the platform. Sync and verify the challenge state before continuing.` }], details: { error: "challenge_not_found_on_duplicate" } };
                }
                submitResult = {
                  correct: true, // duplicate → the flag was correct previously
                  awarded: 0,
                  cumulative_score: 0, // placeholder; duplicate responses do not expose the exact cumulative score
                  correct_flag_count: match.correct_flag_count,
                  total_flag_count: match.flag_count,
                  matched_flag_index: null,
                  unique_code: uniqueCode
                };
                // If the platform says the challenge is now fully completed,
                // update the ledger immediately and attempt close.
                if (match.is_completed) {
                  await ledger.recordSubmission(uniqueCode, flag, true, undefined, match.correct_flag_count, null, owner);
                  const solved = await ledger.markSolved(uniqueCode, undefined, owner);
                  let closeNote = "Container was already stopped.";
                  if (solved.status === "closing") {
                    try {
                      if (match.container_status !== "stopped") await controller.closeChallenge(uniqueCode);
                      await ledger.confirmClosed(uniqueCode);
                      closeNote = "Container closed.";
                    } catch {
                      await ledger.markCloseFailed(uniqueCode);
                      closeNote = "Container close FAILED — it still occupies a platform slot; sync will reconcile.";
                    }
                  }
                  await ledger.maybeAdvancePhase();
                  onChallengeReleased?.();
                  return {
                    content: [{ type: "text" as const, text: `Flag already submitted (duplicate — challenge ${uniqueCode} is completed: ${match.correct_flag_count}/${match.flag_count}). Exact cumulative score is unavailable from the list endpoint. ${closeNote}` }],
                    details: { correct: true, solved: true, duplicate: true, scoreExact: false }
                  };
                }
              } else {
                throw error;
              }
            }
            await ledger.recordSubmission(
              uniqueCode, flag, submitResult.correct,
              wasDuplicate || reconciledAfterTimeout ? undefined : submitResult.cumulative_score, submitResult.correct_flag_count, submitResult.matched_flag_index, owner
            );
            if (submitResult.correct && submitResult.correct_flag_count >= submitResult.total_flag_count) {
              // Mark solved first (score is real), then attempt close; a close
              // failure doesn't un-solve the challenge but must be surfaced.
              const solved = await ledger.markSolved(uniqueCode, wasDuplicate || reconciledAfterTimeout ? undefined : submitResult.cumulative_score, owner);
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
              onChallengeReleased?.();
              return {
                content: [{ type: "text" as const, text: reconciledAfterTimeout
                  ? `✓ Platform reconciliation confirms ${uniqueCode} is fully solved. Exact awarded/challenge score is unavailable from the list endpoint. ${closeNote} Acquire the next challenge.`
                  : `✓ CORRECT! Challenge ${uniqueCode} fully solved (+${submitResult.awarded} pts). Challenge score: ${submitResult.cumulative_score}. Run total: ${scoreLabel(ledger.getState())}. ${closeNote} Acquire the next challenge.` }],
                details: { correct: true, solved: true, awarded: submitResult.awarded }
              };
            }
            if (submitResult.correct) {
              const prefix = reconciledAfterTimeout
                ? "Platform reconciliation confirms the timed-out flag increased progress."
                : wasDuplicate ? "Flag already submitted (duplicate — no penalty, already correct)." : `✓ Flag ${submitResult.matched_flag_index !== null ? submitResult.matched_flag_index + 1 : "?"} correct (+${submitResult.awarded} pts).`;
              const budget = ledger.budgetFor(uniqueCode);
              const nextInstruction = budget?.expired
                ? "The flag was recorded, but this worker has reached its hard limit. Checkpoint and defer for a fresh approach; recovery will preserve valuable state when eligible."
                : `Challenge remains active and the container is preserved. Momentum window renewed for ${Math.floor((budget?.policy.flagMomentumMs ?? 0) / 60_000)} minutes; keep finding the remaining flags and do not acquire another challenge.`;
              return {
                content: [{ type: "text" as const, text: `${prefix} Progress: ${submitResult.correct_flag_count}/${submitResult.total_flag_count}. ${nextInstruction}` }],
                details: { correct: true, partial: true, duplicate: wasDuplicate, schedule: scheduleSnapshot(ledger, ledger.getChallenge(uniqueCode)!) }
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
            if (state.phase !== "second_pass" && state.phase !== "endgame") {
              return { content: [{ type: "text" as const, text: `Hints are forbidden in pass 1. Finish the first pass, then retry in pass 2.` }], details: { hintBlocked: true } };
            }
            const challenge = state.challenges[uniqueCode];
            if (!challenge) throw new Error(`Challenge ${uniqueCode} not found`);
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
          case "defer": {
            if (!uniqueCode) throw new Error("uniqueCode is required for defer");
            const before = ledger.getChallenge(uniqueCode);
            if (!before) throw new Error(`Challenge ${uniqueCode} not found`);
            const durableSignal = before.lastSignalKind === "foothold" || before.lastSignalKind === "credential"
              || before.lastSignalKind === "privilege_change" || before.lastSignalKind === "stage_transition";
            const preserveContainer = ledger.getState().phase !== "first_pass" && (isPartialChallenge(before) || durableSignal);
            if (preserveContainer) {
              let browserState;
              try { browserState = await browser.exportHandoffState(); } catch { browserState = undefined; }
              await ledger.saveBrowserHandoffState(uniqueCode, browserState, owner);
            }
            const challenge = await ledger.defer(uniqueCode, params.reason ?? "attempt budget exhausted", params.nextProbe as string | undefined, owner, { preserveContainer });
            onChallengeReleased?.();
            if (challenge.status === "handoff_waiting") {
              scheduleHandoffCleanup(controller, ledger, uniqueCode);
              await ledger.maybeAdvancePhase();
              return {
                content: [{ type: "text" as const, text: `Deferred ${uniqueCode} for a warm handoff. Its live container and multi-stage state are preserved for 2 minutes. Assign a fresh worker immediately; the recovery brief will require a materially different approach. If nobody takes it, RiftX closes the container automatically.` }],
                details: { uniqueCode, status: "handoff_waiting", handoffExpiresAt: challenge.handoffExpiresAt }
              };
            }
            // Close must be confirmed by the platform — a failed close keeps the
            // container alive and the slot occupied; we surface that honestly.
            try {
              await controller.closeChallenge(uniqueCode);
              await ledger.confirmClosed(uniqueCode);
            } catch {
              await ledger.markCloseFailed(uniqueCode);
              return {
                content: [{ type: "text" as const, text: `Deferred ${uniqueCode}, but the container close FAILED — it is still running on the platform and occupying a slot. The next sync will reconcile; if max-active blocks new acquires, retry defer or abandon on this challenge.` }],
                details: { uniqueCode, status: "closing", closeFailed: true }
              };
            }
            await ledger.maybeAdvancePhase();
            return {
              content: [{ type: "text" as const, text: `Deferred ${uniqueCode} (${challenge.deferredReason}). Container closed. Recovery state: next probe = ${challenge.nextProbe || "(not set)"}. Acquire the next challenge.` }],
              details: { uniqueCode: uniqueCode, status: "deferred" }
            };
          }
          case "abandon": {
            if (!uniqueCode) throw new Error("uniqueCode is required for abandon");
            if (ledger.getState().phase !== "second_pass" && ledger.getState().phase !== "endgame") {
              return { content: [{ type: "text" as const, text: `abandon is terminal and is allowed only in pass 2. Use defer during pass 1.` }], details: { abandonBlocked: true } };
            }
            const challenge = await ledger.abandon(uniqueCode, params.reason ?? "no viable path", owner);
            onChallengeReleased?.();
            try {
              await controller.closeChallenge(uniqueCode);
              await ledger.confirmClosed(uniqueCode);
            } catch {
              await ledger.markCloseFailed(uniqueCode);
              return {
                content: [{ type: "text" as const, text: `Abandoned ${uniqueCode} logically, but the container close FAILED — it is still running on the platform. Next sync will reconcile.` }],
                details: { uniqueCode, status: "closing", closeFailed: true }
              };
            }
            await ledger.maybeAdvancePhase();
            return {
              content: [{ type: "text" as const, text: `Abandoned ${uniqueCode} (${challenge.deferredReason}). Container closed. This challenge is now terminal (exhausted).` }],
              details: { uniqueCode: uniqueCode, status: "exhausted" }
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
