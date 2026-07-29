import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import { RunDetailPage } from "./RunDetailPage";

vi.mock("../hooks/useEventStream", () => ({ useEventStream: vi.fn() }));
vi.mock("../components/TerminalPanel", () => ({ TerminalPanel: () => null }));
vi.mock("../hooks/queries", () => ({
  useRun: () => ({
    isLoading: false,
    error: null,
    data: {
      id: "run-1",
      objective: { description: "Inspect local service" },
      node_id: "local",
      approval_mode: "balanced",
      status: "waiting_approval",
      success_criteria: [],
      scope: { cidrs: [], ips: ["127.0.0.1"], domains: [], url_prefixes: [], exclusions: [] },
      created_at: "2026-07-29T00:00:00Z",
      started_at: "2026-07-29T00:00:01Z",
      workspace_path: "/tmp/run-1",
      temporal_workflow_id: "workflow-run-1",
    },
  }),
  useRunEvents: () => ({ isSuccess: true, isLoading: false, data: { items: [] } }),
  useFindings: () => ({ isLoading: false, data: { items: [] } }),
  useArtifacts: () => ({
    isLoading: false,
    data: {
      items: [
        {
          id: "artifact-1",
          run_id: "run-1",
          execution_id: null,
          name: "scan.txt",
          mime_type: "text/plain",
          sha256: "a".repeat(64),
          size: 128,
          description: "Service scan output",
          created_at: "2026-07-29T00:00:03Z",
          content_url: "/api/v1/artifacts/artifact-1/content",
        },
      ],
    },
  }),
  useApprovals: () => ({
    isLoading: false,
    data: {
      items: [
        {
          id: "approval-1",
          run_id: "run-1",
          tool_call_id: "tool-call-1",
          status: "pending",
          tool_name: "nmap",
          command: ["nmap", "-sV", "127.0.0.1"],
          cwd: "/tmp/run-1",
          target_summary: "ip:127.0.0.1",
          env_diff: {},
          reason: "Identify the local service.",
          decided_by: null,
          created_at: "2026-07-29T00:00:02Z",
          decided_at: null,
        },
      ],
    },
  }),
  useRunControl: () => ({
    pause: { isPending: false, error: null, mutate: vi.fn() },
    resume: { isPending: false, error: null, mutate: vi.fn() },
    cancel: { isPending: false, error: null, mutate: vi.fn() },
    message: { isPending: false, error: null, mutateAsync: vi.fn() },
  }),
  useApprovalControl: () => ({
    approve: { isPending: false, error: null, mutate: vi.fn() },
    reject: { isPending: false, error: null, mutate: vi.fn() },
  }),
  useArtifactControl: () => ({
    register: { isPending: false, error: null, mutateAsync: vi.fn() },
  }),
}));

describe("RunDetailPage approvals", () => {
  it("shows the exact pending command and decision actions", async () => {
    const queryClient = new QueryClient();
    render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={["/runs/run-1"]}>
          <Routes>
            <Route path="/runs/:runId" element={<RunDetailPage />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    );

    expect(screen.getByText(/1 tool call awaiting approval/i)).toBeInTheDocument();
    screen.getByRole("button", { name: /1 tool call awaiting approval/i }).click();
    expect(await screen.findByText("nmap -sV 127.0.0.1")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /approve once/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /approve for run/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /reject/i })).toBeInTheDocument();
  });

  it("shows immutable artifacts and their download link", async () => {
    const queryClient = new QueryClient();
    render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={["/runs/run-1"]}>
          <Routes>
            <Route path="/runs/:runId" element={<RunDetailPage />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    );

    screen.getAllByRole("tab", { name: /artifacts 1/i }).at(-1)?.click();
    expect(await screen.findByText("scan.txt")).toBeInTheDocument();
    expect(screen.getByText("Service scan output")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /download/i })).toHaveAttribute(
      "href",
      "/api/v1/artifacts/artifact-1/content",
    );
  });
});
