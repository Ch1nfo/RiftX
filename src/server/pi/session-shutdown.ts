/**
 * Idempotent full shutdown of a live session record: rejects approvals,
 * aborts the main agent and every subagent (including bash), permanently
 * shuts down the browser, then detaches the SDK session. Archive, delete,
 * workspace switch, and stale-record rebuild all go through this single
 * entry point so a running task can never survive its session being torn
 * down.
 *
 * Kept free of SDK imports so the ordering contract is unit-testable. Every
 * cleanup step is best-effort: one failing step is logged and the remaining
 * steps still run, and shutdown never rejects to its caller.
 */

import type { McpServerEntry } from "../mcp/manager";

export type ShutdownTarget = {
  id: string;
  aborting?: boolean;
  abortEpoch?: number;
  abortPromise?: Promise<void>;
  waitingForSubagents?: boolean;
  compacting?: boolean;
  shutdownPromise?: Promise<void>;
  gate: { rejectAll(): void };
  session: {
    abortBash(): void;
    abortCompaction(): void;
    abort(): Promise<unknown>;
    dispose(): void;
  };
  subagents?: { abortAll(): Promise<unknown> };
  /** close cancels in-flight Playwright work but keeps the manager reopenable; shutdown is permanent. */
  browser?: { close(): Promise<unknown>; shutdown(): Promise<unknown> };
  /** MCP connection references; releasing the last one closes the connection. */
  mcpEntries?: readonly McpServerEntry[];
  unsubscribe(): void;
};

export async function shutdownSessionRecord(record: ShutdownTarget) {
  if (record.shutdownPromise) return record.shutdownPromise;
  record.aborting = true;
  record.abortEpoch = (record.abortEpoch ?? 0) + 1;
  record.shutdownPromise = (async () => {
    // Coordinate with an in-flight Stop: let it finish its unwinding first so
    // abort logic and shutdown cleanup never interleave.
    if (record.abortPromise) await record.abortPromise.catch(() => undefined);
    const safe = async (step: string, run: () => unknown) => {
      try {
        await run();
      } catch (error) {
        console.error(`RiftX session ${record.id} shutdown step ${step} failed:`, error);
      }
    };
    const gate = record.gate;
    const session = record.session;
    await safe("rejectApprovals", () => gate.rejectAll());
    await safe("abortBash", () => session.abortBash());
    await safe("abortCompaction", () => session.abortCompaction());
    await safe("abortAgent", () => session.abort());
    if (record.subagents) await safe("abortSubagents", () => record.subagents!.abortAll());
    // The session is being destroyed: the browser must shut down permanently
    // so queued operations reject instead of relaunching resources after
    // cleanup. close() alone would leave the manager reopenable.
    if (record.browser) await safe("shutdownBrowser", () => record.browser!.shutdown());
    if (record.mcpEntries?.length) await safe("releaseMcpServers", async () => {
      // Dynamic import keeps this module free of SDK imports (see header).
      const { releaseMcpServers } = await import("../mcp/manager");
      await releaseMcpServers(record.mcpEntries!);
    });
    await safe("unsubscribe", () => record.unsubscribe());
    await safe("dispose", () => session.dispose());
  })().finally(() => {
    record.aborting = false;
  });
  return record.shutdownPromise;
}

/**
 * User-initiated Stop for a still-live session. Coordinates with shutdown in
 * both orders: an in-flight Stop runs to completion once, and a Stop that
 * arrives while a shutdown is already running simply waits for it instead of
 * starting a second round of abort/close on a record being torn down.
 */
export async function abortSessionRecord(record: ShutdownTarget, emit: (event: { type: "session_state"; state: "idle" } | { type: "done"; aborted: boolean }) => void) {
  if (record.abortPromise) return record.abortPromise;
  if (record.shutdownPromise) {
    await record.shutdownPromise;
    return;
  }
  record.aborting = true;
  record.abortPromise = (async () => {
    record.abortEpoch = (record.abortEpoch ?? 0) + 1;
    record.waitingForSubagents = false;
    record.compacting = false;
    record.gate.rejectAll();
    record.session.abortBash();
    record.session.abortCompaction();
    await Promise.allSettled([
      record.session.abort(),
      record.subagents?.abortAll() ?? Promise.resolve()
    ]);
    // The browser tool only races an abort flag; closing the manager is what
    // genuinely cancels in-flight Playwright operations. Stop uses the
    // reopenable close, not the permanent shutdown: the session record
    // survives a Stop and the next prompt must be able to relaunch the
    // browser. Queued operations are already dropped by their AbortSignals.
    await record.browser?.close().catch(() => undefined);
    emit({ type: "session_state", state: "idle" });
    emit({ type: "done", aborted: true });
  })();
  try {
    await record.abortPromise;
  } finally {
    record.abortPromise = undefined;
    // Keep the aborting flag raised while a shutdown is running, so late
    // subagent completions are not delivered into a disposing session.
    if (!record.shutdownPromise) record.aborting = false;
  }
}
