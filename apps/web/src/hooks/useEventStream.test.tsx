import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render } from "@testing-library/react";
import type { PropsWithChildren } from "react";
import { afterEach, describe, expect, it } from "vitest";

import type { RunEventList } from "../api/types";
import { queryKeys } from "./queries";
import { useEventStream } from "./useEventStream";

class FakeEventSource {
  static latest: FakeEventSource | null = null;
  readonly url: string;
  onmessage: ((event: MessageEvent<string>) => void) | null = null;
  listeners = new Map<string, Array<(event: MessageEvent<string>) => void>>();
  closed = false;

  constructor(url: string | URL) {
    this.url = String(url);
    FakeEventSource.latest = this;
  }

  addEventListener(type: string, listener: EventListenerOrEventListenerObject) {
    const callback = listener as (event: MessageEvent<string>) => void;
    this.listeners.set(type, [...(this.listeners.get(type) ?? []), callback]);
  }

  close() {
    this.closed = true;
  }

  emit(type: string, data: object) {
    const event = new MessageEvent("message", { data: JSON.stringify(data) });
    for (const listener of this.listeners.get(type) ?? []) listener(event);
  }
}

function Probe({ runId = "run-1" }: { runId?: string }) {
  useEventStream(runId);
  return null;
}

afterEach(() => {
  FakeEventSource.latest = null;
});

describe("useEventStream", () => {
  it("resumes from the cached sequence and appends SSE events to query state", () => {
    const original = globalThis.EventSource;
    globalThis.EventSource = FakeEventSource as unknown as typeof EventSource;
    const queryClient = new QueryClient();
    queryClient.setQueryData<RunEventList>(queryKeys.events("run-1"), {
      after_sequence: 0,
      items: [
        {
          id: "event-1",
          run_id: "run-1",
          sequence: 1,
          event_type: "run.created",
          payload: {},
          created_at: "2026-07-29T00:00:00Z",
        },
      ],
    });
    const Wrapper = ({ children }: PropsWithChildren) => (
      <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
    );

    const rendered = render(<Probe />, { wrapper: Wrapper });
    expect(FakeEventSource.latest?.url).toContain("after_sequence=1");

    act(() => {
      FakeEventSource.latest?.emit("user.message_queued", {
        id: "event-2",
        run_id: "run-1",
        sequence: 2,
        event_type: "user.message_queued",
        payload: { message: "continue" },
        created_at: "2026-07-29T00:00:01Z",
      });
    });

    expect(FakeEventSource.latest?.listeners.has("agent.tool_completed")).toBe(true);
    expect(FakeEventSource.latest?.listeners.has("runtime.engine_event")).toBe(true);
    expect(FakeEventSource.latest?.listeners.has("tool.approval_required")).toBe(true);
    expect(FakeEventSource.latest?.listeners.has("terminal.opened")).toBe(true);
    expect(FakeEventSource.latest?.listeners.has("artifact.registered")).toBe(true);
    expect(FakeEventSource.latest?.listeners.has("finding.updated")).toBe(true);
    expect(FakeEventSource.latest?.listeners.has("report.generated")).toBe(true);
    expect(FakeEventSource.latest?.listeners.has("tool.execution_completed")).toBe(false);

    act(() => {
      FakeEventSource.latest?.emit("runtime.engine_event", {
        id: "event-runtime-1",
        run_id: "run-1",
        sequence: 3,
        event_type: "runtime.engine_event",
        payload: {
          cycle_id: "cycle-1",
          event_type: "assistant_delta",
          data: { delta: "hello" },
        },
        created_at: "2026-07-29T00:00:02Z",
      });
    });

    act(() => {
      FakeEventSource.latest?.emit("agent.tool_completed", {
        id: "event-4",
        run_id: "run-1",
        sequence: 4,
        event_type: "agent.tool_completed",
        payload: { tool_name: "nmap" },
        created_at: "2026-07-29T00:00:03Z",
      });
    });

    act(() => {
      FakeEventSource.latest?.emit("terminal.opened", {
        id: "event-5",
        run_id: "run-1",
        sequence: 5,
        event_type: "terminal.opened",
        payload: { session_id: "terminal-1" },
        created_at: "2026-07-29T00:00:04Z",
      });
    });

    act(() => {
      FakeEventSource.latest?.emit("report.generated", {
        id: "event-6",
        run_id: "run-1",
        sequence: 6,
        event_type: "report.generated",
        payload: { report_id: "report-1" },
        created_at: "2026-07-29T00:00:05Z",
      });
    });

    const cached = queryClient.getQueryData<RunEventList>(queryKeys.events("run-1"));
    expect(cached?.items.map((event) => event.sequence)).toEqual([1, 2, 3, 4, 5, 6]);
    rendered.unmount();
    expect(FakeEventSource.latest?.closed).toBe(true);
    globalThis.EventSource = original;
  });

  it("resets the SSE cursor when navigating between runs", () => {
    const original = globalThis.EventSource;
    globalThis.EventSource = FakeEventSource as unknown as typeof EventSource;
    const queryClient = new QueryClient();
    queryClient.setQueryData<RunEventList>(queryKeys.events("run-a"), {
      after_sequence: 0,
      items: [
        {
          id: "event-a-100",
          run_id: "run-a",
          sequence: 100,
          event_type: "run.created",
          payload: {},
          created_at: "2026-07-29T00:00:00Z",
        },
      ],
    });
    queryClient.setQueryData<RunEventList>(queryKeys.events("run-b"), {
      after_sequence: 0,
      items: [
        {
          id: "event-b-2",
          run_id: "run-b",
          sequence: 2,
          event_type: "run.created",
          payload: {},
          created_at: "2026-07-29T00:00:00Z",
        },
      ],
    });
    const Wrapper = ({ children }: PropsWithChildren) => (
      <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
    );

    const rendered = render(<Probe runId="run-a" />, { wrapper: Wrapper });
    expect(FakeEventSource.latest?.url).toContain("after_sequence=100");

    rendered.rerender(<Probe runId="run-b" />);
    expect(FakeEventSource.latest?.url).toContain("after_sequence=2");

    rendered.unmount();
    globalThis.EventSource = original;
  });
});
