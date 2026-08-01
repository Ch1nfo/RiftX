import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, waitFor } from "@testing-library/react";
import type { PropsWithChildren } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { clearLocalOperatorToken, setLocalOperatorToken } from "../api/client";
import type { RunEvent, RunEventList } from "../api/types";
import { queryKeys } from "./queries";
import { consumeServerSentEvents, useEventStream } from "./useEventStream";

function Probe({ runId = "run-1" }: { runId?: string }) {
  useEventStream(runId);
  return null;
}

function event(sequence: number, eventType: string, payload: Record<string, unknown> = {}): RunEvent {
  return {
    id: `event-${sequence}`,
    run_id: "run-1",
    sequence,
    event_type: eventType,
    payload,
    created_at: `2026-07-29T00:00:0${sequence}Z`,
  };
}

function sseResponse(events: RunEvent[]): Response {
  const encoder = new TextEncoder();
  return new Response(
    new ReadableStream<Uint8Array>({
      start(controller) {
        for (const item of events) {
          controller.enqueue(
            encoder.encode(
              `id: ${item.sequence}\nevent: ${item.event_type}\ndata: ${JSON.stringify(item)}\n\n`,
            ),
          );
        }
        controller.close();
      },
    }),
    { status: 200, headers: { "Content-Type": "text/event-stream" } },
  );
}

function pendingSseResponse(): Response {
  return new Response(new ReadableStream<Uint8Array>(), {
    status: 200,
    headers: { "Content-Type": "text/event-stream" },
  });
}

function wrapper(queryClient: QueryClient) {
  return ({ children }: PropsWithChildren) => (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  );
}

afterEach(() => {
  clearLocalOperatorToken();
  cleanup();
  vi.unstubAllGlobals();
});

describe("useEventStream", () => {
  it("resumes from cache and ingests named stop events without an event registry", async () => {
    setLocalOperatorToken("stream-memory-secret");
    const queryClient = new QueryClient();
    queryClient.setQueryData<RunEventList>(queryKeys.events("run-1"), {
      after_sequence: 0,
      items: [event(1, "run.created")],
    });
    const streamed = [
      event(2, "target_http.request_cancelled", { execution_key: "request-1" }),
      event(3, "terminal.close_requested", { session_id: "terminal-1" }),
      // A future backend event must remain observable without a Web release.
      event(4, "future.stop_acknowledged", { resource_id: "resource-1" }),
      event(5, "runtime.engine_event", {
        cycle_id: "cycle-1",
        event_type: "assistant_delta",
        data: { delta: "hello" },
      }),
    ];
    const fetchMock = vi.fn().mockResolvedValue(sseResponse(streamed));
    vi.stubGlobal("fetch", fetchMock);

    const rendered = render(<Probe />, { wrapper: wrapper(queryClient) });

    await waitFor(() =>
      expect(
        queryClient.getQueryData<RunEventList>(queryKeys.events("run-1"))?.items,
      ).toHaveLength(5),
    );
    expect(String(fetchMock.mock.calls[0]?.[0])).toContain("after_sequence=1");
    expect(fetchMock).toHaveBeenNthCalledWith(
      1,
      expect.any(String),
      expect.objectContaining({
        headers: expect.objectContaining({
          Authorization: "Bearer stream-memory-secret",
        }),
      }),
    );
    expect(
      queryClient
        .getQueryData<RunEventList>(queryKeys.events("run-1"))
        ?.items.map((item) => item.event_type),
    ).toEqual([
      "run.created",
      "target_http.request_cancelled",
      "terminal.close_requested",
      "future.stop_acknowledged",
      "runtime.engine_event",
    ]);

    rendered.unmount();
  });

  it("resets the stream cursor and aborts the old request when navigating between runs", async () => {
    setLocalOperatorToken("run-switch-memory-secret");
    const queryClient = new QueryClient();
    queryClient.setQueryData<RunEventList>(queryKeys.events("run-a"), {
      after_sequence: 0,
      items: [{ ...event(100, "run.created"), id: "event-a-100", run_id: "run-a" }],
    });
    queryClient.setQueryData<RunEventList>(queryKeys.events("run-b"), {
      after_sequence: 0,
      items: [{ ...event(2, "run.created"), id: "event-b-2", run_id: "run-b" }],
    });
    const signals: AbortSignal[] = [];
    const fetchMock = vi.fn().mockImplementation((_url: string, init: RequestInit) => {
      signals.push(init.signal as AbortSignal);
      return Promise.resolve(pendingSseResponse());
    });
    vi.stubGlobal("fetch", fetchMock);

    const rendered = render(<Probe runId="run-a" />, { wrapper: wrapper(queryClient) });
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    expect(String(fetchMock.mock.calls[0]?.[0])).toContain("after_sequence=100");
    expect(fetchMock).toHaveBeenNthCalledWith(
      1,
      expect.any(String),
      expect.objectContaining({
        headers: expect.objectContaining({
          Authorization: "Bearer run-switch-memory-secret",
        }),
      }),
    );

    rendered.rerender(<Probe runId="run-b" />);
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    expect(signals[0]?.aborted).toBe(true);
    expect(String(fetchMock.mock.calls[1]?.[0])).toContain("after_sequence=2");
    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      expect.any(String),
      expect.objectContaining({
        headers: expect.objectContaining({
          Authorization: "Bearer run-switch-memory-secret",
        }),
      }),
    );

    rendered.unmount();
    expect(signals[1]?.aborted).toBe(true);
  });

  it("parses named SSE records split across transport chunks", async () => {
    const encoder = new TextEncoder();
    const stream = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(encoder.encode("event: brand.new.stop_ack\ndata: {\"sequence\":"));
        controller.enqueue(encoder.encode("7,\"status\":\"stopped\"}\n\n: heartbeat\n\n"));
        controller.close();
      },
    });
    const received: string[] = [];

    await consumeServerSentEvents(stream, (data) => received.push(data), new AbortController().signal);

    expect(received).toEqual(['{"sequence":7,"status":"stopped"}']);
  });
});
