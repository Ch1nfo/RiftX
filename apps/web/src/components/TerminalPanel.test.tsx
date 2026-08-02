import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { TerminalPanel } from "./TerminalPanel";

const createTerminal = vi.fn(() => new Promise(() => undefined));

vi.mock("@xterm/xterm", () => ({
  Terminal: class {},
}));

vi.mock("../hooks/queries", async (importOriginal) => {
  const original = await importOriginal<typeof import("../hooks/queries")>();
  return {
    ...original,
    useTerminal: () => ({ isLoading: false, data: undefined, error: null }),
    useTerminalControl: () => ({
      create: { isPending: false, error: null, mutateAsync: createTerminal },
      close: { isPending: false, error: null, mutateAsync: vi.fn() },
    }),
  };
});

describe("TerminalPanel", () => {
  it("offers a host-native shell when the run has no terminal", () => {
    const queryClient = new QueryClient();
    render(
      <QueryClientProvider client={queryClient}>
        <TerminalPanel runId="run-1" />
      </QueryClientProvider>,
    );

    const start = screen.getByRole("button", { name: /start local shell/i });
    expect(screen.getByText(/agent-owned sessions remain read-only/i)).toBeInTheDocument();
    fireEvent.click(start);
    expect(createTerminal).toHaveBeenCalledWith({ owner: "agent" });
  });
});
