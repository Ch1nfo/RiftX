import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { api } from "../api/client";
import { queryKeys } from "../hooks/queries";
import { DashboardPage } from "./DashboardPage";

afterEach(() => vi.restoreAllMocks());

describe("DashboardPage", () => {
  it("isolates general and Code Audit Run caches", () => {
    expect(queryKeys.runs(undefined, "general")).toEqual([
      "runs",
      "all",
      "general",
    ]);
    expect(queryKeys.runs(undefined, "code_audit")).toEqual([
      "runs",
      "all",
      "code_audit",
    ]);
  });

  it("hydrates dashboard metrics from the control-plane API", async () => {
    vi.spyOn(api, "listRuns").mockResolvedValue({
      items: [],
      limit: 100,
      offset: 0,
    });
    vi.spyOn(api, "listTools").mockResolvedValue({
      node_id: "local",
      generation: 3,
      source_digest: "abc",
      execution_policy: "registered_only",
      tools: [],
    });
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });

    render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter>
          <DashboardPage />
        </MemoryRouter>
      </QueryClientProvider>,
    );

    expect(await screen.findByText("Active run queue")).toBeInTheDocument();
    expect(screen.getByText("No active runs")).toBeInTheDocument();
    expect(screen.getByText("Local tool health")).toBeInTheDocument();
    expect(screen.getByText("generation 3", { exact: false })).toBeInTheDocument();
    expect(api.listRuns).toHaveBeenCalledWith(undefined, "general");
  });
});
