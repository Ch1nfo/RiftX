import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, render, waitFor } from "@testing-library/react";
import { useState, type PropsWithChildren } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { clearLocalOperatorToken, setLocalOperatorToken } from "../api/client";
import type { RunEvent, RunEventList } from "../api/types";
import { queryKeys, useRunActions } from "./queries";
import { consumeServerSentEvents, useEventStream } from "./useEventStream";

function Probe({ runId = "run-1" }: { runId?: string }) {
  const stream = useEventStream(runId);
  return (
    <output data-action-revision={stream.actionUpdateRevision}>
      {stream.error?.message ?? ""}
    </output>
  );
}

function ActionListProbe() {
  const actions = useRunActions("run-1");
  const [selected, setSelected] = useState("action-2");
  return (
    <div>
      <output>{selected}</output>
      {actions.data?.pages.flatMap((page) => page.items).map((item) => (
        <button
          aria-pressed={selected === item.action_id}
          autoFocus={item.action_id === "action-2"}
          key={item.action_id}
          onClick={() => setSelected(item.action_id)}
          type="button"
        >
          {item.action_id}
        </button>
      ))}
    </div>
  );
}

function ActionCacheProbe({ showActions }: { showActions: boolean }) {
  const stream = useEventStream("run-1");
  return (
    <>
      <output data-action-revision={stream.actionUpdateRevision} />
      {showActions ? <ActionListProbe /> : null}
    </>
  );
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
  it("merges cached and streamed out-of-order sequences without dropping a gap", async () => {
    const queryClient = new QueryClient();
    queryClient.setQueryData<RunEventList>(queryKeys.events("run-1"), {
      after_sequence: 0,
      items: [event(1, "run.created"), event(3, "run.prepared")],
    });
    const fetchMock = vi.fn().mockResolvedValue(
      sseResponse([
        event(4, "run.status_changed"),
        event(2, "runtime.cycle_started"),
        event(2, "runtime.cycle_started"),
      ]),
    );
    vi.stubGlobal("fetch", fetchMock);

    const rendered = render(<Probe />, { wrapper: wrapper(queryClient) });

    await waitFor(() =>
      expect(
        queryClient
          .getQueryData<RunEventList>(queryKeys.events("run-1"))
          ?.items.map((item) => item.sequence),
      ).toEqual([1, 2, 3, 4]),
    );
    expect(String(fetchMock.mock.calls[0]?.[0])).toContain("after_sequence=1");
    expect(rendered.container.querySelector("output")).toHaveAttribute(
      "data-action-revision",
      "0",
    );

    rendered.unmount();
  });

  it("repairs a detected gap from the event snapshot and resets the Action snapshot", async () => {
    const queryClient = new QueryClient();
    queryClient.setQueryData<RunEventList>(queryKeys.events("run-1"), {
      after_sequence: 1,
      items: [event(1, "run.created")],
    });
    queryClient.setQueryData(queryKeys.action("run-1", "action-1"), {
      action_id: "action-1",
      run_id: "run-1",
      version: "stale",
    });
    const resetQueries = vi.spyOn(queryClient, "resetQueries");
    const fetchMock = vi.fn().mockImplementation((url: string) => {
      if (String(url).includes("/events/stream")) {
        return Promise.resolve(
          sseResponse([
            event(3, "tool.approval_required", {
              tool_call_intent_id: "action-1",
            }),
          ]),
        );
      }
      if (String(url).includes("/events?")) {
        return Promise.resolve(
          new Response(
            JSON.stringify({
              after_sequence: 1,
              items: [
                event(2, "agent.tool_started", {
                  tool_call_intent_id: "action-1",
                }),
                event(3, "tool.approval_required", {
                  tool_call_intent_id: "action-1",
                }),
              ],
            }),
            { status: 200, headers: { "content-type": "application/json" } },
          ),
        );
      }
      throw new Error(`Unexpected URL ${String(url)}`);
    });
    vi.stubGlobal("fetch", fetchMock);

    const rendered = render(<Probe />, { wrapper: wrapper(queryClient) });

    await waitFor(() =>
      expect(
        queryClient
          .getQueryData<RunEventList>(queryKeys.events("run-1"))
          ?.items.map((item) => item.sequence),
      ).toEqual([1, 2, 3]),
    );
    expect(fetchMock.mock.calls.some(([url]) => String(url).includes("/events?"))).toBe(true);
    expect(resetQueries).toHaveBeenCalledWith(
      expect.objectContaining({
        queryKey: queryKeys.actionRoot("run-1"),
        exact: false,
      }),
    );
    expect(queryClient.getQueryData(queryKeys.action("run-1", "action-1"))).toBeUndefined();

    rendered.unmount();
  });

  it("aborts and reconnects after a retryable repair failure, then clears the error", async () => {
    const queryClient = new QueryClient();
    queryClient.setQueryData<RunEventList>(queryKeys.events("run-1"), {
      after_sequence: 0,
      items: [event(1, "run.created")],
    });
    const encoder = new TextEncoder();
    let firstStreamController!: ReadableStreamDefaultController<Uint8Array>;
    let streamCalls = 0;
    let snapshotCalls = 0;
    const fetchMock = vi.fn().mockImplementation((url: string) => {
      if (String(url).includes("/events/stream")) {
        streamCalls += 1;
        if (streamCalls === 1) {
          return Promise.resolve(
            new Response(
              new ReadableStream<Uint8Array>({
                start(controller) {
                  firstStreamController = controller;
                },
              }),
              { status: 200, headers: { "Content-Type": "text/event-stream" } },
            ),
          );
        }
        return Promise.resolve(pendingSseResponse());
      }
      if (String(url).includes("/events?")) {
        snapshotCalls += 1;
        if (snapshotCalls === 1) {
          return Promise.resolve(
            new Response(
              JSON.stringify({
                detail: { code: "snapshot_failed", message: "Snapshot repair failed" },
              }),
              { status: 500, headers: { "content-type": "application/json" } },
            ),
          );
        }
        return Promise.resolve(
          new Response(
            JSON.stringify({
              after_sequence: 1,
              items: [
                event(2, "agent.tool_started", { tool_call_intent_id: "action-1" }),
                event(3, "action.updated", { action_id: "action-1" }),
              ],
            }),
            { status: 200, headers: { "content-type": "application/json" } },
          ),
        );
      }
      throw new Error(`Unexpected URL ${String(url)}`);
    });
    vi.stubGlobal("fetch", fetchMock);

    const rendered = render(<Probe />, { wrapper: wrapper(queryClient) });
    await waitFor(() => expect(firstStreamController).toBeDefined());
    await act(async () => {
      const gapEvent = event(3, "action.updated", { action_id: "action-1" });
      firstStreamController.enqueue(
        encoder.encode(`data: ${JSON.stringify(gapEvent)}\n\n`),
      );
    });
    await waitFor(() =>
      expect(rendered.container.querySelector("output")?.textContent).toContain(
        "HTTP 500",
      ),
    );

    await new Promise<void>((resolve) => window.setTimeout(resolve, 1_100));
    await waitFor(() => expect(streamCalls).toBe(2));
    expect(rendered.container.querySelector("output")).toHaveTextContent("");
    expect(
      queryClient
        .getQueryData<RunEventList>(queryKeys.events("run-1"))
        ?.items.map((item) => item.sequence),
    ).toEqual([1, 2, 3]);

    rendered.unmount();
  });

  it("stops reconnecting and exposes authorization failures", async () => {
    const queryClient = new QueryClient();
    queryClient.setQueryData(queryKeys.actions("run-1"), {
      pages: [{ items: [{ action_id: "action-secret", reason: "cached secret" }] }],
      pageParams: [null],
    });
    queryClient.setQueryData(queryKeys.action("run-1", "action-secret"), {
      action_id: "action-secret",
      reason: "cached secret",
      approval: { actor: "cached-secret-actor" },
    });
    const fetchMock = vi.fn().mockResolvedValue(
      new Response("", { status: 403 }),
    );
    vi.stubGlobal("fetch", fetchMock);

    const rendered = render(<Probe />, { wrapper: wrapper(queryClient) });

    expect(await rendered.findByText("Run event stream access was denied")).toBeInTheDocument();
    expect(queryClient.getQueryData(queryKeys.actions("run-1"))).toBeUndefined();
    expect(
      queryClient.getQueryData(queryKeys.action("run-1", "action-secret")),
    ).toBeUndefined();
    await new Promise<void>((resolve) => window.setTimeout(resolve, 1_100));
    expect(fetchMock).toHaveBeenCalledTimes(1);

    rendered.unmount();
  });

  it("recalibrates Event and Action snapshots after a successful reconnect", async () => {
    const queryClient = new QueryClient();
    queryClient.setQueryData<RunEventList>(queryKeys.events("run-1"), {
      after_sequence: 0,
      items: [event(1, "run.created")],
    });
    const resetQueries = vi.spyOn(queryClient, "resetQueries");
    let streamCalls = 0;
    const fetchMock = vi.fn().mockImplementation((url: string) => {
      if (String(url).includes("/events/stream")) {
        streamCalls += 1;
        return Promise.resolve(streamCalls === 1 ? sseResponse([]) : pendingSseResponse());
      }
      if (String(url).includes("/events?")) {
        return Promise.resolve(
          new Response(
            JSON.stringify({ after_sequence: 1, items: [event(2, "run.prepared")] }),
            { status: 200, headers: { "content-type": "application/json" } },
          ),
        );
      }
      throw new Error(`Unexpected URL ${String(url)}`);
    });
    vi.stubGlobal("fetch", fetchMock);

    const rendered = render(<Probe />, { wrapper: wrapper(queryClient) });
    await waitFor(() => expect(streamCalls).toBe(1));
    await new Promise<void>((resolve) => window.setTimeout(resolve, 1_100));
    await waitFor(() => expect(streamCalls).toBe(2));

    expect(
      queryClient
        .getQueryData<RunEventList>(queryKeys.events("run-1"))
        ?.items.map((item) => item.sequence),
    ).toEqual([1, 2]);
    expect(resetQueries).toHaveBeenCalledWith(
      expect.objectContaining({
        queryKey: queryKeys.actionRoot("run-1"),
        exact: false,
      }),
    );

    rendered.unmount();
  });

  it("recalibrates Actions before the first stream so a delayed stale list cannot miss a cached Action event", async () => {
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    queryClient.setQueryData<RunEventList>(queryKeys.events("run-1"), {
      after_sequence: 0,
      items: [event(1, "action.created", { action_id: "action-fresh" })],
    });
    let resolveStale!: (response: Response) => void;
    let actionCalls = 0;
    const actionPageResponse = (actionId: string) =>
      new Response(
        JSON.stringify({
          items: [{ action_id: actionId, run_id: "run-1" }],
          limit: 50,
          sort: "created_at_desc",
          has_more: false,
          next_cursor: null,
        }),
        { status: 200, headers: { "content-type": "application/json" } },
      );
    const fetchMock = vi.fn().mockImplementation((url: string) => {
      if (String(url).includes("/runs/run-1/actions")) {
        actionCalls += 1;
        if (actionCalls === 1) {
          return new Promise<Response>((resolve) => {
            resolveStale = resolve;
          });
        }
        return Promise.resolve(actionPageResponse("action-fresh"));
      }
      if (String(url).includes("/events/stream")) {
        return Promise.resolve(pendingSseResponse());
      }
      throw new Error(`Unexpected URL ${String(url)}`);
    });
    vi.stubGlobal("fetch", fetchMock);

    const rendered = render(<ActionCacheProbe showActions />, {
      wrapper: wrapper(queryClient),
    });
    expect(await rendered.findByRole("button", { name: "action-fresh" })).toBeInTheDocument();
    expect(actionCalls).toBeGreaterThanOrEqual(2);
    expect(
      fetchMock.mock.calls.some(([url]) =>
        String(url).includes("/events/stream?after_sequence=1"),
      ),
    ).toBe(true);

    await act(async () => {
      resolveStale(actionPageResponse("action-stale"));
      await Promise.resolve();
    });
    expect(rendered.getByRole("button", { name: "action-fresh" })).toBeInTheDocument();
    expect(rendered.queryByRole("button", { name: "action-stale" })).not.toBeInTheDocument();

    rendered.unmount();
  });

  it("keeps loaded Action pages, selection, and focus while an ordinary Action event refetches", async () => {
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false, staleTime: Number.POSITIVE_INFINITY } },
    });
    const invalidateQueries = vi.spyOn(queryClient, "invalidateQueries");
    let streamController!: ReadableStreamDefaultController<Uint8Array>;
    const encoder = new TextEncoder();
    const fetchMock = vi.fn().mockImplementation((url: string) => {
      if (String(url).includes("/events/stream")) {
        return Promise.resolve(
          new Response(
            new ReadableStream<Uint8Array>({
              start(controller) {
                streamController = controller;
              },
            }),
            { status: 200, headers: { "Content-Type": "text/event-stream" } },
          ),
        );
      }
      if (String(url).includes("/runs/run-1/actions")) {
        return Promise.resolve(pendingSseResponse());
      }
      throw new Error(`Unexpected URL ${String(url)}`);
    });
    vi.stubGlobal("fetch", fetchMock);

    const rendered = render(<ActionCacheProbe showActions={false} />, {
      wrapper: wrapper(queryClient),
    });
    await waitFor(() => expect(streamController).toBeDefined());
    queryClient.setQueryData(queryKeys.actions("run-1"), {
      pages: [
        {
          items: [{ action_id: "action-1", run_id: "run-1" }],
          limit: 1,
          sort: "created_at_desc",
          has_more: true,
          next_cursor: "cursor-2",
        },
        {
          items: [{ action_id: "action-2", run_id: "run-1" }],
          limit: 1,
          sort: "created_at_desc",
          has_more: false,
          next_cursor: null,
        },
      ],
      pageParams: [null, "cursor-2"],
    });
    rendered.rerender(<ActionCacheProbe showActions />);
    const selectedCard = await rendered.findByRole("button", { name: "action-2" });
    selectedCard.focus();
    const streamed = [
      event(1, "tool.approved", { tool_call_intent_id: "action-2" }),
      event(2, "execution.started", { tool_call_intent_id: "action-2" }),
    ];
    await act(async () => {
      for (const item of streamed) {
        streamController.enqueue(
          encoder.encode(`data: ${JSON.stringify(item)}\n\n`),
        );
      }
    });

    await waitFor(() =>
      expect(invalidateQueries).toHaveBeenCalledWith({
        queryKey: queryKeys.actions("run-1"),
        exact: true,
      }),
    );
    expect(invalidateQueries).toHaveBeenCalledWith({
      queryKey: queryKeys.action("run-1", "action-2"),
      exact: true,
    });
    expect(
      queryClient.getQueryData<{ pages: unknown[] }>(queryKeys.actions("run-1"))?.pages,
    ).toHaveLength(2);
    expect(rendered.getByText("action-2", { selector: "output" })).toBeInTheDocument();
    expect(selectedCard).toHaveFocus();
    expect(rendered.container.querySelector("output")).toHaveAttribute(
      "data-action-revision",
      "1",
    );

    rendered.unmount();
  });

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
      after_sequence: 99,
      items: [{ ...event(100, "run.created"), id: "event-a-100", run_id: "run-a" }],
    });
    queryClient.setQueryData<RunEventList>(queryKeys.events("run-b"), {
      after_sequence: 1,
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
