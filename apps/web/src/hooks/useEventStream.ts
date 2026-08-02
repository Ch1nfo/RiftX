import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";

import { api, localOperatorHeaders, RiftXAPIError } from "../api/client";
import type { RunEvent, RunEventList } from "../api/types";
import { mergeRunEventLists, queryKeys } from "./queries";

const RECONNECT_MIN_MS = 1_000;
const RECONNECT_MAX_MS = 10_000;

export interface EventStreamState {
  connected: boolean;
  stale: boolean;
  error: Error | null;
  actionUpdateRevision: number;
}

export function useEventStream(runId: string, enabled = true): EventStreamState {
  const queryClient = useQueryClient();
  const lastSequence = useRef(0);
  const [state, setState] = useState<EventStreamState>({
    connected: false,
    stale: false,
    error: null,
    actionUpdateRevision: 0,
  });

  useEffect(() => {
    setState({ connected: false, stale: false, error: null, actionUpdateRevision: 0 });
    if (!enabled || !runId || typeof fetch === "undefined") {
      return undefined;
    }
    const cached = queryClient.getQueryData<RunEventList>(queryKeys.events(runId));
    const normalizedCache = cached ? mergeRunEventLists(undefined, cached) : undefined;
    if (normalizedCache && normalizedCache !== cached) {
      queryClient.setQueryData(queryKeys.events(runId), normalizedCache);
    }
    lastSequence.current = contiguousEventCursor(normalizedCache, runId);
    const pendingEvents = new Map<number, RunEvent>();
    let flushTimer: ReturnType<typeof setTimeout> | null = null;
    let disposed = false;
    let fatal = false;
    let connectedOnce = false;
    let repairPromise: Promise<void> | null = null;
    let reconnectDelay = RECONNECT_MIN_MS;
    let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
    let activeController: AbortController | null = null;

    // Native EventSource only dispatches named SSE events to listeners that
    // already know the exact event name. Reading the wire format directly
    // keeps new stop acknowledgements and future audit event types observable
    // without maintaining a second, inevitably stale event registry here.
    void connect();

    async function connect() {
      if (disposed || fatal) return;
      const controller = new AbortController();
      activeController = controller;
      let nextDelay = reconnectDelay;
      try {
        if (connectedOnce) {
          await reconcileSnapshots();
        } else {
          // The Events snapshot and Action list are fetched independently.
          // Reset once before the first stream cursor is opened so a stale
          // list response cannot land after an Action event already present
          // in the Events snapshot and then wait forever for a replay.
          await resetActionRoot();
        }
        if (disposed || fatal) return;
        const response = await fetch(api.eventStreamUrl(runId, lastSequence.current), {
          cache: "no-store",
          headers: { Accept: "text/event-stream", ...localOperatorHeaders() },
          signal: controller.signal,
        });
        if (response.status === 401 || response.status === 403) {
          fatal = true;
          clearActionRoot();
          setState((current) => ({
            ...current,
            connected: false,
            stale: true,
            error: new RiftXAPIError(
              response.status,
              "event_stream_authorization_failed",
              "Run event stream access was denied",
            ),
          }));
          return;
        }
        if (!response.ok) {
          throw new Error(`Run event stream returned HTTP ${response.status}`);
        }
        if (!response.body) {
          throw new Error("Run event stream did not provide a response body");
        }
        connectedOnce = true;
        reconnectDelay = RECONNECT_MIN_MS;
        nextDelay = reconnectDelay;
        setState((current) => ({
          ...current,
          connected: true,
          stale: hasEventGap(
            queryClient.getQueryData<RunEventList>(queryKeys.events(runId)),
            runId,
          ),
          error: null,
        }));
        await consumeServerSentEvents(response.body, ingest, controller.signal);
      } catch (error) {
        if (disposed || controller.signal.aborted || isAbortError(error)) return;
        if (isAuthorizationError(error)) {
          fatal = true;
          clearActionRoot();
        }
        setState((current) => ({
          ...current,
          connected: false,
          stale: true,
          error: error instanceof Error ? error : new Error("Run event stream failed"),
        }));
        nextDelay = reconnectDelay;
        reconnectDelay = Math.min(reconnectDelay * 2, RECONNECT_MAX_MS);
      } finally {
        if (activeController === controller) activeController = null;
        if (!disposed && !fatal) {
          setState((current) => ({ ...current, connected: false, stale: true }));
          reconnectTimer = setTimeout(() => void connect(), nextDelay);
        }
      }
    }

    function ingest(raw: string) {
      let event: RunEvent;
      try {
        event = JSON.parse(raw) as RunEvent;
      } catch {
        return;
      }
      if (
        disposed ||
        event.run_id !== runId ||
        !Number.isSafeInteger(event.sequence) ||
        event.sequence < 1
      ) {
        return;
      }
      if (event.sequence <= lastSequence.current) {
        return;
      }
      if (!pendingEvents.has(event.sequence)) pendingEvents.set(event.sequence, event);
      if (event.sequence > lastSequence.current + 1) {
        setState((current) => ({ ...current, stale: true }));
      }
      if (flushTimer === null) {
        // Providers may emit one persisted engine event per token. A short
        // batch keeps the live reply responsive without sorting and rendering
        // the entire Run page for every individual delta.
        flushTimer = setTimeout(flush, 32);
      }
    }

    function flush() {
      flushTimer = null;
      if (!pendingEvents.size) return;
      const batch = [...pendingEvents.values()];
      pendingEvents.clear();
      const merged = queryClient.setQueryData<RunEventList>(
        queryKeys.events(runId),
        (current) =>
          mergeRunEventLists(current, {
            after_sequence: current?.after_sequence ?? 0,
            items: batch,
          }),
      );
      lastSequence.current = contiguousEventCursor(merged, runId);
      const gap = hasEventGap(merged, runId);
      setState((current) => ({ ...current, stale: gap }));
      if (gap) void scheduleSnapshotRepair().catch(handleSnapshotError);

      const eventTypes = batch.map((event) => event.event_type);
      const runChanged = eventTypes.some((eventType) =>
        [
          "run.status_changed",
          "run.prepared",
          "run.pause_requested",
          "run.resume_requested",
          "run.cancel_requested",
          "run.cleaned_up",
          "workflow.started",
        ].includes(eventType),
      );
      if (runChanged) {
        void queryClient.invalidateQueries({ queryKey: queryKeys.run(runId) });
        void queryClient.invalidateQueries({ queryKey: ["runs"] });
      }
      if (eventTypes.some((eventType) =>
        eventType.startsWith("agent.tool_") || eventType.startsWith("execution."),
      )) {
        void queryClient.invalidateQueries({ queryKey: queryKeys.executions(runId) });
      }
      if (eventTypes.some((eventType) =>
        eventType === "finding.created" || eventType === "finding.updated",
      )) {
        void queryClient.invalidateQueries({ queryKey: queryKeys.findings(runId) });
      }
      if (eventTypes.includes("artifact.registered")) {
        void queryClient.invalidateQueries({ queryKey: queryKeys.artifacts(runId) });
      }
      if (eventTypes.includes("report.generated")) {
        void queryClient.invalidateQueries({ queryKey: queryKeys.reports(runId) });
      }
      if (eventTypes.some((eventType) =>
        eventType.startsWith("tool.approval") ||
        eventType === "tool.approved" ||
        eventType === "tool.rejected",
      )) {
        void queryClient.invalidateQueries({ queryKey: queryKeys.approvals(runId) });
      }
      invalidateActionSnapshots(batch);
      for (const event of batch) {
        if (event.event_type.startsWith("terminal.")) {
          const sessionId = event.payload.session_id;
          if (typeof sessionId === "string") {
            void queryClient.invalidateQueries({ queryKey: queryKeys.terminal(sessionId) });
          }
        }
      }
    }

    function invalidateActionSnapshots(batch: RunEvent[]) {
      const relevant = batch.filter(isActionChangeEvent);
      if (!relevant.length) return;
      const actionIds = new Set<string>();
      let ambiguous = false;
      for (const event of relevant) {
        const actionId = explicitActionId(event);
        if (actionId) actionIds.add(actionId);
        else ambiguous = true;
      }
      if (ambiguous) {
        void queryClient.invalidateQueries({
          queryKey: queryKeys.actionRoot(runId),
          exact: false,
        });
      } else {
        void queryClient.invalidateQueries({
          queryKey: queryKeys.actions(runId),
          exact: true,
        });
        for (const actionId of actionIds) {
          void queryClient.invalidateQueries({
            queryKey: queryKeys.action(runId, actionId),
            exact: true,
          });
        }
      }
      setState((current) => ({
        ...current,
        actionUpdateRevision: current.actionUpdateRevision + 1,
      }));
    }

    function scheduleSnapshotRepair(): Promise<void> {
      if (!repairPromise) {
        repairPromise = reconcileSnapshots().finally(() => {
          repairPromise = null;
        });
      }
      return repairPromise;
    }

    function handleSnapshotError(error: unknown) {
      if (disposed) return;
      if (isAuthorizationError(error)) {
        fatal = true;
        clearActionRoot();
      }
      // A repair failure means this stream's cursor can no longer be trusted.
      // Abort even for retryable errors so connect() reaches its normal
      // backoff/reconnect path, reruns both snapshot reconciliations, and only
      // clears the surfaced error after a new stream is established.
      activeController?.abort();
      setState((current) => ({
        ...current,
        connected: false,
        stale: true,
        error: error instanceof Error ? error : new Error("Run snapshot repair failed"),
      }));
    }

    async function reconcileSnapshots() {
      await Promise.all([repairEventGap(), resetActionRoot()]);
    }

    async function resetActionRoot() {
      if (disposed) return;
      await queryClient.resetQueries({
        queryKey: queryKeys.actionRoot(runId),
        exact: false,
      });
    }

    function clearActionRoot() {
      queryClient.removeQueries({
        queryKey: queryKeys.actionRoot(runId),
        exact: false,
      });
    }

    async function repairEventGap() {
      let cursor = lastSequence.current;
      while (!disposed) {
        const snapshot = await api.listEvents(runId, cursor);
        if (disposed) return;
        const merged = queryClient.setQueryData<RunEventList>(
          queryKeys.events(runId),
          (current) => mergeRunEventLists(current, snapshot),
        );
        const nextCursor = contiguousEventCursor(merged, runId);
        lastSequence.current = nextCursor;
        const gap = hasEventGap(merged, runId);
        setState((current) => ({ ...current, stale: gap }));
        if (snapshot.items.length < 1_000 || nextCursor <= cursor) return;
        cursor = nextCursor;
      }
    }

    return () => {
      disposed = true;
      activeController?.abort();
      if (reconnectTimer !== null) clearTimeout(reconnectTimer);
      if (flushTimer !== null) clearTimeout(flushTimer);
      pendingEvents.clear();
    };
  }, [enabled, queryClient, runId]);

  return state;
}

function contiguousEventCursor(
  value: RunEventList | undefined,
  runId: string,
): number {
  let cursor = value?.after_sequence ?? 0;
  for (const event of value?.items ?? []) {
    if (event.run_id !== runId || event.sequence <= cursor) continue;
    if (event.sequence !== cursor + 1) break;
    cursor = event.sequence;
  }
  return cursor;
}

function hasEventGap(value: RunEventList | undefined, runId: string): boolean {
  const contiguous = contiguousEventCursor(value, runId);
  return (value?.items ?? []).some(
    (event) => event.run_id === runId && event.sequence > contiguous,
  );
}

function isActionChangeEvent(event: RunEvent): boolean {
  return (
    event.event_type.startsWith("agent.tool_") ||
    event.event_type.startsWith("action.") ||
    event.event_type.startsWith("tool.") ||
    event.event_type.startsWith("execution.") ||
    event.event_type.startsWith("target_http.") ||
    event.event_type === "artifact.registered" ||
    event.event_type === "finding.created" ||
    event.event_type === "finding.updated"
  );
}

function explicitActionId(event: RunEvent): string | null {
  for (const key of ["action_id", "tool_call_intent_id"] as const) {
    const value = event.payload[key];
    if (typeof value === "string" && value) return value;
  }
  return null;
}

function isAuthorizationError(error: unknown): boolean {
  return error instanceof RiftXAPIError && [401, 403].includes(error.status);
}

export async function consumeServerSentEvents(
  stream: ReadableStream<Uint8Array>,
  onData: (data: string) => void,
  signal: AbortSignal,
): Promise<void> {
  const reader = stream.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  const cancel = () => void reader.cancel();
  signal.addEventListener("abort", cancel, { once: true });

  try {
    while (!signal.aborted) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      buffer = drainSSEBlocks(buffer, onData);
    }
    buffer += decoder.decode();
    buffer = drainSSEBlocks(buffer, onData);
    if (buffer.trim()) dispatchSSEBlock(buffer, onData);
  } finally {
    signal.removeEventListener("abort", cancel);
    reader.releaseLock();
  }
}

function drainSSEBlocks(buffer: string, onData: (data: string) => void): string {
  let remaining = buffer;
  while (true) {
    const boundary = /\r?\n\r?\n/.exec(remaining);
    if (!boundary || boundary.index === undefined) return remaining;
    dispatchSSEBlock(remaining.slice(0, boundary.index), onData);
    remaining = remaining.slice(boundary.index + boundary[0].length);
  }
}

function dispatchSSEBlock(block: string, onData: (data: string) => void) {
  const data: string[] = [];
  for (const line of block.split(/\r?\n/)) {
    if (line === "data") {
      data.push("");
    } else if (line.startsWith("data:")) {
      const value = line.slice(5);
      data.push(value.startsWith(" ") ? value.slice(1) : value);
    }
  }
  if (data.length) onData(data.join("\n"));
}

function isAbortError(error: unknown): boolean {
  return error instanceof DOMException && error.name === "AbortError";
}
