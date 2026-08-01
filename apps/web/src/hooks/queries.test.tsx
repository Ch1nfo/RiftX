import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { RiftXAPIError } from "../api/client";
import type { RunActionList, RunEvent, RunEventList } from "../api/types";
import {
  flattenRunActionPages,
  mergeRunEventLists,
  queryKeys,
  useRunAction,
  useRunActions,
  useRunControl,
  useRunEvents,
} from "./queries";

const mocks = vi.hoisted(() => ({
  cancelRun: vi.fn(),
  getRunAction: vi.fn(),
  listRunActions: vi.fn(),
  listEvents: vi.fn(),
}));

vi.mock("../api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/client")>();
  return {
    ...actual,
    api: {
      ...actual.api,
      cancelRun: mocks.cancelRun,
      getRunAction: mocks.getRunAction,
      listRunActions: mocks.listRunActions,
      listEvents: mocks.listEvents,
    },
  };
});

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

  it("appends a small live batch without sorting the full high-cardinality history", () => {
    const previousItems = Array.from({ length: 10_000 }, (_, index) =>
      event(index + 1, "runtime.engine_event"),
    );
    const previous: RunEventList = { after_sequence: 0, items: previousItems };
    const sort = vi.spyOn(Array.prototype, "sort");
    try {
      const merged = mergeRunEventLists(previous, {
        after_sequence: 10_000,
        items: [event(10_001, "run.status_changed")],
      });

      expect(sort).not.toHaveBeenCalled();
      expect(merged.items).toHaveLength(10_001);
      expect(merged.items[0]).toBe(previousItems[0]);
      expect(merged.items.at(-1)?.sequence).toBe(10_001);
    } finally {
      sort.mockRestore();
    }
  });
});

describe("Run Action queries", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("follows the stable server cursor and de-duplicates page overlap by Action ID", async () => {
    const first = actionPage(["action-2", "action-1"], "cursor-1");
    const second = actionPage(["action-1", "action-0"], null);
    mocks.listRunActions
      .mockResolvedValueOnce(first)
      .mockResolvedValueOnce(second);
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    const wrapper = ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
    );
    const { result } = renderHook(() => useRunActions("run-1"), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    await act(async () => {
      await result.current.fetchNextPage();
    });

    expect(mocks.listRunActions).toHaveBeenNthCalledWith(1, "run-1", undefined, 50);
    expect(mocks.listRunActions).toHaveBeenNthCalledWith(2, "run-1", "cursor-1", 50);
    await waitFor(() => {
      const pages = queryClient.getQueryData<{ pages: RunActionList[] }>(
        queryKeys.actions("run-1"),
      )?.pages;
      expect(flattenRunActionPages(pages)).toEqual([
        expect.objectContaining({ action_id: "action-2" }),
        expect.objectContaining({ action_id: "action-1" }),
        expect.objectContaining({ action_id: "action-0" }),
      ]);
      expect(pages?.at(-1)?.has_more).toBe(false);
    });
  });

  it("isolates a late list response after switching Runs", async () => {
    let resolveRunA!: (page: RunActionList) => void;
    let resolveRunB!: (page: RunActionList) => void;
    mocks.listRunActions.mockImplementation((runId: string) =>
      new Promise<RunActionList>((resolve) => {
        if (runId === "run-a") resolveRunA = resolve;
        else resolveRunB = resolve;
      }),
    );
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    const wrapper = ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
    );
    const { result, rerender } = renderHook(
      ({ runId }) => useRunActions(runId),
      { initialProps: { runId: "run-a" }, wrapper },
    );
    await waitFor(() => expect(resolveRunA).toBeTypeOf("function"));
    rerender({ runId: "run-b" });
    await waitFor(() => expect(resolveRunB).toBeTypeOf("function"));

    resolveRunB(actionPage(["action-b"], null, "run-b"));
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    resolveRunA(actionPage(["action-a"], null, "run-a"));
    await act(async () => Promise.resolve());

    expect(flattenRunActionPages(result.current.data?.pages)[0]?.action_id).toBe("action-b");
    expect(
      flattenRunActionPages(
        queryClient.getQueryData<{ pages: RunActionList[] }>(
          queryKeys.actions("run-a"),
        )?.pages,
      )[0]?.action_id,
    ).toBe("action-a");
  });

  it("keys detail by both Run and Action and does not fetch an empty selection", async () => {
    mocks.getRunAction.mockResolvedValue({ action_id: "action-1", run_id: "run-1" });
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    const wrapper = ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
    );
    const { result, rerender } = renderHook(
      ({ runId, actionId }) => useRunAction(runId, actionId),
      { initialProps: { runId: "run-1", actionId: "" }, wrapper },
    );
    expect(result.current.fetchStatus).toBe("idle");
    expect(mocks.getRunAction).not.toHaveBeenCalled();

    rerender({ runId: "run-1", actionId: "action-1" });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    expect(mocks.getRunAction).toHaveBeenCalledWith("run-1", "action-1");
    expect(queryKeys.action("run-1", "action-1")).not.toEqual(
      queryKeys.action("run-2", "action-1"),
    );
  });

  it.each([401, 403] as const)(
    "surfaces a cached list refetch HTTP %s without waiting for the production retry",
    async (status) => {
      const cached = actionPage(["cached-action"], null);
      mocks.listRunActions.mockResolvedValueOnce(cached);
      const queryClient = productionQueryClient();
      const wrapper = ({ children }: { children: ReactNode }) => (
        <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
      );
      const { result } = renderHook(() => useRunActions("run-1"), { wrapper });
      await waitFor(() => expect(result.current.isSuccess).toBe(true));

      const authorizationError = new RiftXAPIError(
        status,
        "local_operator_capability_denied",
        "Action list authorization failed",
      );
      mocks.listRunActions.mockRejectedValueOnce(authorizationError);
      await act(async () => {
        await result.current.refetch();
      });

      await waitFor(() => expect(result.current.error).toBe(authorizationError));
      expect(mocks.listRunActions).toHaveBeenCalledTimes(2);
      expect(flattenRunActionPages(result.current.data?.pages)).toEqual(
        cached.items,
      );
      queryClient.clear();
    },
  );

  it.each([401, 403] as const)(
    "surfaces a cached detail refetch HTTP %s without waiting for the production retry",
    async (status) => {
      const cached = { action_id: "cached-action", run_id: "run-1" };
      mocks.getRunAction.mockResolvedValueOnce(cached);
      const queryClient = productionQueryClient();
      const wrapper = ({ children }: { children: ReactNode }) => (
        <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
      );
      const { result } = renderHook(
        () => useRunAction("run-1", "cached-action"),
        { wrapper },
      );
      await waitFor(() => expect(result.current.isSuccess).toBe(true));

      const authorizationError = new RiftXAPIError(
        status,
        "authentication_required",
        "Action detail authorization failed",
      );
      mocks.getRunAction.mockRejectedValueOnce(authorizationError);
      await act(async () => {
        await result.current.refetch();
      });

      await waitFor(() => expect(result.current.error).toBe(authorizationError));
      expect(result.current.data).toEqual(cached);
      expect(mocks.getRunAction).toHaveBeenCalledTimes(2);
      queryClient.clear();
    },
  );

  it("surfaces a next-page 403 without retrying or discarding loaded pages", async () => {
    const first = actionPage(["cached-action"], "cursor-1");
    mocks.listRunActions.mockResolvedValueOnce(first);
    const queryClient = productionQueryClient();
    const wrapper = ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
    );
    const { result } = renderHook(() => useRunActions("run-1"), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    const forbidden = new RiftXAPIError(
      403,
      "local_operator_capability_denied",
      "Action pagination forbidden",
    );
    mocks.listRunActions.mockRejectedValueOnce(forbidden);
    await act(async () => {
      await result.current.fetchNextPage();
    });

    await waitFor(() => expect(result.current.error).toBe(forbidden));
    expect(result.current.isFetchNextPageError).toBe(true);
    expect(mocks.listRunActions).toHaveBeenCalledTimes(2);
    expect(flattenRunActionPages(result.current.data?.pages)).toEqual(first.items);
    queryClient.clear();
  });

  it("retains the production single retry for non-authorization failures", async () => {
    const transient = new Error("temporary Action detail failure");
    mocks.getRunAction.mockRejectedValue(transient);
    const queryClient = productionQueryClient(0);
    const wrapper = ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
    );
    const { result } = renderHook(
      () => useRunAction("run-1", "action-1"),
      { wrapper },
    );

    await waitFor(() => expect(result.current.error).toBe(transient));
    expect(mocks.getRunAction).toHaveBeenCalledTimes(2);
    queryClient.clear();
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

function actionPage(
  actionIds: string[],
  nextCursor: string | null,
  runId = "run-1",
): RunActionList {
  return {
    items: actionIds.map((actionId) => ({ action_id: actionId, run_id: runId })),
    limit: 50,
    sort: "created_at_desc",
    has_more: nextCursor !== null,
    next_cursor: nextCursor,
  } as RunActionList;
}

function productionQueryClient(retryDelay = 60_000): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: {
        retry: 1,
        retryDelay,
        staleTime: 2_000,
        refetchOnWindowFocus: false,
      },
    },
  });
}
