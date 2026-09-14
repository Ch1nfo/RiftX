import { captureFence } from "./fencing";
import type { BenchmarkLedger } from "./ledger";
import type { FailureSource } from "./attempt-observation";
import { setTimeout as delay } from "node:timers/promises";
import { BenchmarkController, BenchmarkError } from "./controller";
import { hasPendingSubmissions, retryPendingSubmissions } from "./pending-submissions";
import { ModelRecovery } from "./recovery";
import { benchmarkProfile, positiveInteger } from "./environment";
import { benchmarkMainBusy, benchmarkMainHasWork, queueBenchmarkContinuation } from "./scheduling";

const INITIAL_PROMPT = `Start this benchmark now. First call benchmark_control(action="sync") to verify connectivity and read the challenge queue. Solve the platform-provided challenges and submit observed flags through benchmark_control. Use up to two challenge SubAgents and work on your own challenge concurrently. Continue until the authoritative board is terminal or the platform ends the task. Public research is disabled. No human is available; do not wait for instructions. Follow the benchmark scope and tool rules.`;
const CONTINUE_PROMPT = `The benchmark is still unfinished. Reconcile benchmark_control(action="sync"), inspect the authoritative status, refill available SubAgent slots, and continue solving. A completed assistant turn does not end the benchmark. If all remaining approaches are exhausted, explicitly abandon the corresponding challenges through benchmark_control so the board records the terminal outcome.`;

export function redactRuntimeSecrets(message: string, env: NodeJS.ProcessEnv = process.env) {
  for (const secret of [env.BENCHMARK_TOKEN, env.RIFTX_LLM_API_KEY, env.RIFTX_CHILD_LLM_API_KEY]) {
    if (secret) message = message.split(secret).join("[REDACTED]");
  }
  return message;
}

function log(event: string, fields: Record<string, unknown> = {}) {
  console.log(JSON.stringify({ time: new Date().toISOString(), event, ...fields }));
}

export async function runBenchmark(): Promise<number> {
  benchmarkProfile(); // Fail before touching the platform if the model config is incomplete.
  if (Object.keys(process.env).some((key) => key.startsWith("RIFTX_CHILD_LLM_"))) benchmarkProfile(process.env, true);
  const controller = new BenchmarkController();
  const maxMinutes = process.env.RIFTX_BENCHMARK_MAX_MINUTES
    ? positiveInteger(process.env.RIFTX_BENCHMARK_MAX_MINUTES, 1, "RIFTX_BENCHMARK_MAX_MINUTES") : Infinity;
  const deadline = Date.now() + maxMinutes * 60_000;
  let stopCode: number | undefined;
  let forceExit: ReturnType<typeof setTimeout> | undefined;
  const onSignal = (signal: NodeJS.Signals) => {
    if (stopCode !== undefined) return;
    stopCode = signal === "SIGINT" ? 130 : 143;
    log("stopping", { signal });
    forceExit = setTimeout(() => { log("shutdown_timeout"); process.exit(stopCode); }, 90_000);
  };
  const sigint = () => onSignal("SIGINT");
  const sigterm = () => onSignal("SIGTERM");
  process.on("SIGINT", sigint);
  process.on("SIGTERM", sigterm);
  // Some SDK work has no referenced handles while awaiting a provider response.
  const keepAlive = setInterval(() => undefined, 10_000);
  let sessionId: string | undefined;
  let cleanup: ((id: string) => Promise<void>) | undefined;
  let exitCode = 0;
  let submissionDrain: Promise<void> | undefined;
  let runLedger: BenchmarkLedger | undefined;
  const recordRunFailure = async (source: FailureSource, reason: string) => {
    if (!runLedger) return;
    for (const challenge of Object.values(runLedger.getState().challenges)) {
      if (challenge.currentAttemptStartedAt !== null) await runLedger.recordAttemptIncident(captureFence(challenge, challenge.currentAttemptWorker ?? undefined), source, reason, "runner");
    }
  };
  try {
    let initialChallenges: Awaited<ReturnType<BenchmarkController["listChallenges"]>> | undefined;
    let initialVpn: Awaited<ReturnType<BenchmarkController["checkVpn"]>> | undefined;
    for (let attempt = 1; attempt <= 3; attempt++) {
      if (stopCode !== undefined) return stopCode;
      try {
        initialVpn = await controller.checkVpn();
        initialChallenges = await controller.listChallenges();
        break;
      } catch (error) {
        if (error instanceof BenchmarkError && error.kind === "invalid_state_task_ended") {
          log("platform_ended");
          return 0;
        }
        if (!(error instanceof BenchmarkError) || !["vpn_check_failed", "connection_error", "timeout", "internal_error", "resource_unavailable"].includes(error.kind) || attempt === 3) throw error;
        log("preflight_retry", { attempt, kind: error.kind });
        await delay(5_000);
      }
    }
    if (stopCode !== undefined) return stopCode;
    if (!initialVpn || !initialChallenges?.length) throw new Error("Platform returned no benchmark challenges");
    log("preflight_ok", { challenges: initialChallenges.length, vpn: initialVpn.status });
    const { updateConfig } = await import("@/server/config-store");
    const { createSession, startPromptSession, getBenchmarkRuntime, closeBenchmarkSession } = await import("@/server/pi/session-manager");
    const { sessions } = await import("@/server/pi/session-registry");
    cleanup = closeBenchmarkSession;
    await updateConfig({ approvalMode: "full", cwd: process.cwd(), mcpServers: [], systemPromptEnabled: false });
    sessionId = (await createSession()).id;
    const record = sessions.get(sessionId)!;
    const runtime = getBenchmarkRuntime(sessionId)!;
    runLedger = runtime.ledger;
    await runtime.ledger.syncFromPlatform(initialChallenges, initialVpn.ok, initialVpn.client_ip, initialVpn.status !== "unchecked");
    let toolsCompleted = 0;
    const recovery = new ModelRecovery();
    let inspectedAssistant: unknown;
    let failure: string | undefined;
    record.emitter.on("event", (event: { type: string; toolName?: string; error?: string; isError?: boolean }) => {
      if (event.type === "tool_end") {
        toolsCompleted++;
        if (!event.isError) { failure = undefined; recovery.succeeded(); }
      }
      if (event.type === "error") failure = event.error ?? "Agent failed";
      if (["tool_start", "tool_end", "done", "error"].includes(event.type)) {
        log(event.type, { tool: event.toolName, ...(event.error ? { error: redactRuntimeSecrets(event.error) } : {}) });
      }
    });
    log("session_started", { sessionId, model: record.profile.model, approvalMode: record.gate.approvalMode });
    let firstPrompt = true;
    let lastProbe = Date.now();
    let lastTools = 0;
    let emptyTurns = 0;
    let probeFailures = 0;
    while (stopCode === undefined) {
      if (Date.now() >= deadline) { await recordRunFailure("harness_timeout", "Configured run deadline reached"); exitCode = 124; log("run_deadline"); break; }
      if (Date.now() - lastProbe >= 60_000) {
        try {
          await controller.listChallenges(); // Detect platform expiry even during a long agent turn.
          probeFailures = 0;
        } catch (error) {
          if (error instanceof BenchmarkError && error.kind === "invalid_state_task_ended") { await recordRunFailure("platform_failure", "Platform ended the benchmark run"); log("platform_ended"); break; }
          if (!(error instanceof BenchmarkError) || !["connection_error", "timeout", "internal_error", "resource_unavailable"].includes(error.kind) || ++probeFailures >= 3) throw error;
          log("platform_probe_retry", { attempt: probeFailures });
        }
        lastProbe = Date.now();
        const state = runtime.ledger.getState();
        log("progress", { phase: state.phase, solved: state.solvedCount, exhausted: state.exhaustedCount, score: state.cumulativeScore });
      }
      if (!submissionDrain) {
        submissionDrain = retryPendingSubmissions(controller, runtime.ledger)
          .catch((error) => log("pending_submission_error", { error: redactRuntimeSecrets(error instanceof Error ? error.message : String(error)) }))
          .finally(() => { submissionDrain = undefined; });
      }
      if (benchmarkMainBusy(record)) {
        await delay(1_000);
        continue;
      }
      const lastAssistant = [...record.session.messages].reverse().find((message) => message.role === "assistant");
      if (failure || (lastAssistant !== inspectedAssistant && lastAssistant?.role === "assistant" && ["error", "aborted"].includes(lastAssistant.stopReason))) {
        recovery.failed(failure || (lastAssistant?.role === "assistant" ? lastAssistant.errorMessage || `Model stopped: ${lastAssistant.stopReason}` : "Agent failed"));
        failure = undefined;
      }
      inspectedAssistant = lastAssistant;
      const state = runtime.ledger.getState();
      if (state.phase === "completed" && !hasPendingSubmissions(runtime.ledger)) {
        log("completed", { solved: state.solvedCount, exhausted: state.exhaustedCount, score: state.cumulativeScore });
        break;
      }
      const decision = recovery.decision(Boolean(record.subagents?.hasActiveTasks()) || hasPendingSubmissions(runtime.ledger));
      if (decision.action === "fail") {
        await runtime.ledger.recordAttemptIncident(captureFence(runtime.ledger.budgetForOwner("main")?.challenge), "model_failure", decision.error, "model_recovery");
        throw new Error(decision.error);
      }
      if (decision.action === "wait") { await delay(1_000); continue; }
      if (!firstPrompt && !benchmarkMainHasWork(runtime.ledger)) {
        await delay(1_000);
        continue;
      }
      if (!firstPrompt && decision.action !== "retry") {
        emptyTurns = toolsCompleted === lastTools ? emptyTurns + 1 : 0;
        if (emptyTurns >= 3) {
          recovery.failed("Agent stopped three times without using tools while challenges remain unfinished");
          emptyTurns = 0;
          continue;
        }
      }
      lastTools = toolsCompleted;
      if (decision.action === "retry") {
        if (queueBenchmarkContinuation(record, runtime.ledger, CONTINUE_PROMPT)) {
          recovery.dispatched();
          emptyTurns = 0;
          log("model_recovery", { attempt: decision.attempt + 1 });
        }
      } else if (firstPrompt) {
        try { await startPromptSession(sessionId, INITIAL_PROMPT); }
        catch (error) { recovery.failed(error instanceof Error ? error.message : String(error)); }
      } else queueBenchmarkContinuation(record, runtime.ledger, CONTINUE_PROMPT);
      firstPrompt = false;
      await delay(1_000); // Let prompt completion and child deliveries settle before inspecting idle state.
    }
    exitCode = stopCode ?? exitCode;
  } catch (error) {
    await recordRunFailure(error instanceof BenchmarkError ? "platform_failure" : "environment_failure", error instanceof Error ? error.message : String(error));
    exitCode = stopCode ?? 1;
    log("failed", { error: redactRuntimeSecrets(error instanceof Error ? error.message : String(error)) });
  } finally {
    if (sessionId && cleanup) {
      // Bound cleanup independently of the platform's API retry/poll budgets.
      const shutdownDeadline = setTimeout(() => { log("shutdown_timeout"); process.exit(stopCode ?? 1); }, 90_000);
      try {
        await submissionDrain;
        await cleanup(sessionId);
        log("cleanup_complete");
      } catch (error) {
        exitCode = stopCode ?? 1;
        log("cleanup_failed", { error: redactRuntimeSecrets(error instanceof Error ? error.message : String(error)) });
      } finally { clearTimeout(shutdownDeadline); }
    }
    clearInterval(keepAlive);
    if (forceExit) clearTimeout(forceExit);
    process.off("SIGINT", sigint);
    process.off("SIGTERM", sigterm);
  }
  return exitCode;
}
