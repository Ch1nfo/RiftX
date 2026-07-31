import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook } from "@testing-library/react";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { useRunControl } from "./queries";

const mocks = vi.hoisted(() => ({
  cancelRun: vi.fn(),
}));

vi.mock("../api/client", () => ({
  api: {
    cancelRun: mocks.cancelRun,
  },
}));

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
