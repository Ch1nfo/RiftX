import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef } from "react";

import { api } from "../api/client";
import type { RunEvent, RunEventList } from "../api/types";
import { queryKeys } from "./queries";

const RECONNECT_MIN_MS = 1_000;
const RECONNECT_MAX_MS = 10_000;

export function useEventStream(runId: string, enabled = true) {
  const queryClient = useQueryClient();
  const lastSequence = useRef(0);

  useEffect(() => {
    if (!enabled || !runId || typeof fetch === "undefined") {
      return undefined;
    }
    const cached = queryClient.getQueryData<RunEventList>(queryKeys.events(runId));
    lastSequence.current = Math.max(...(cached?.items.map((item) => item.sequence) ?? [0]));
    const pendingEvents: RunEvent[] = [];
    let flushTimer: ReturnType<typeof setTimeout> | null = null;
    let disposed = false;
    let reconnectDelay = RECONNECT_MIN_MS;
    let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
    let activeController: AbortController | null = null;

    // Native EventSource only dispatches named SSE events to listeners that
    // already know the exact event name. Reading the wire format directly
    // keeps new stop acknowledgements and future audit event types observable
    // without maintaining a second, inevitably stale event registry here.
    void connect();

    async function connect() {
      if (disposed) return;
      const controller = new AbortController();
      activeController = controller;
      let nextDelay = reconnectDelay;
      try {
        const response = await fetch(api.eventStreamUrl(runId, lastSequence.current), {
          cache: "no-store",
          headers: { Accept: "text/event-stream" },
          signal: controller.signal,
        });
        if (!response.ok) {
          throw new Error(`Run event stream returned HTTP ${response.status}`);
        }
        if (!response.body) {
          throw new Error("Run event stream did not provide a response body");
        }
        reconnectDelay = RECONNECT_MIN_MS;
        nextDelay = reconnectDelay;
        await consumeServerSentEvents(response.body, ingest, controller.signal);
      } catch (error) {
        if (disposed || controller.signal.aborted || isAbortError(error)) return;
        nextDelay = reconnectDelay;
        reconnectDelay = Math.min(reconnectDelay * 2, RECONNECT_MAX_MS);
      } finally {
        if (activeController === controller) activeController = null;
        if (!disposed) {
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
      if (event.sequence <= lastSequence.current) {
        return;
      }
      lastSequence.current = event.sequence;
      pendingEvents.push(event);
      if (flushTimer === null) {
        // Providers may emit one persisted engine event per token. A short
        // batch keeps the live reply responsive without sorting and rendering
        // the entire Run page for every individual delta.
        flushTimer = setTimeout(flush, 32);
      }
    }

    function flush() {
      flushTimer = null;
      if (!pendingEvents.length) return;
      const batch = pendingEvents.splice(0, pendingEvents.length);
      queryClient.setQueryData<RunEventList>(queryKeys.events(runId), (current) => {
        const currentItems = current?.items ?? [];
        const currentLastSequence = currentItems.at(-1)?.sequence ?? 0;
        return {
          after_sequence: current?.after_sequence ?? 0,
          // SSE sequences are strictly increasing. Filtering against the
          // latest query snapshot also covers a refetch that races this batch,
          // while appending preserves order without O(n log n) sorting.
          items: [
            ...currentItems,
            ...batch.filter((event) => event.sequence > currentLastSequence),
          ],
        };
      });

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
      for (const event of batch) {
        if (event.event_type.startsWith("terminal.")) {
          const sessionId = event.payload.session_id;
          if (typeof sessionId === "string") {
            void queryClient.invalidateQueries({ queryKey: queryKeys.terminal(sessionId) });
          }
        }
      }
    }

    return () => {
      disposed = true;
      activeController?.abort();
      if (reconnectTimer !== null) clearTimeout(reconnectTimer);
      if (flushTimer !== null) clearTimeout(flushTimer);
      flush();
    };
  }, [enabled, queryClient, runId]);
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
