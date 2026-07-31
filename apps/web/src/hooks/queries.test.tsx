import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { RunEvent, RunEventList } from "../api/types";
import {
  mergeRunEventLists,
  queryKeys,
  useRunControl,
  useRunEvents,
} from "./queries";

const mocks = vi.hoisted(() => ({
  cancelRun: vi.fn(),
  listEvents: vi.fn(),
}));

vi.mock("../api/client", () => ({
  api: {
    cancelRun: mocks.cancelRun,
    listEvents: mocks.listEvents,
  },
}));

function event(sequence: number, eventType: string): RunEvent {
  return {
    id: `event-${sequence}`,
    run_id: "run-1",
    sequence,
    event_type: eventType,
    payload: {},
    created_at: `2026-07-31T00:00:0${sequence}Z`,
  };
}

describe("useRunEvents", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("does not let an older HTTP refetch erase an SSE acknowledgement", async () => {
    const firstSnapshot: RunEventList = {
      after_sequence: 0,
      items: [event(1, "run.created")],
    };
    let resolveRefetch: ((value: RunEventList) => void) | undefined;
    mocks.listEvents
      .mockResolvedValueOnce(firstSnapshot)
      .mockImplementationOnce(
        () =>
          new Promise<RunEventList>((resolve) => {
            resolveRefetch = resolve;
          }),
      );
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    const wrapper = ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
    );
    const { result } = renderHook(() => useRunEvents("run-1"), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    void result.current.refetch();
    await waitFor(() => expect(resolveRefetch).toBeTypeOf("function"));
    queryClient.setQueryData<RunEventList>(queryKeys.events("run-1"), (current) => ({
      after_sequence: current?.after_sequence ?? 0,
      items: [...(current?.items ?? []), event(2, "terminal.close_acknowledged")],
    }));
    resolveRefetch?.(firstSnapshot);
    await waitFor(() => expect(result.current.isFetching).toBe(false));

    expect(
      queryClient
        .getQueryData<RunEventList>(queryKeys.events("run-1"))
        ?.items.map((item) => item.sequence),
    ).toEqual([1, 2]);
  });

  it("keeps the append-only SSE fast path intact", () => {
    const previous: RunEventList = {
      after_sequence: 0,
      items: [event(1, "run.created")],
    };
    const incoming: RunEventList = {
      after_sequence: 0,
      items: [...previous.items, event(2, "run.cancel_requested")],
    };

    expect(mergeRunEventLists(previous, incoming)).toBe(incoming);
  });
});

describe("useRunControl", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mocks.cancelRun.mockResolvedValue({ accepted: true, run: { id: "run-1" } });
  });

  it("routes emergency stop to full-Run cancellation", async () => {
    const queryClient = new QueryClient({
      defaultOptions: { mutations: { retry: false } },
    });
    const wrapper = ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
    );
    const { result } = renderHook(() => useRunControl("run-1"), { wrapper });

    await act(async () => {
      await result.current.emergencyStop.mutateAsync();
    });

    expect(mocks.cancelRun).toHaveBeenCalledWith("run-1");
  });
});
