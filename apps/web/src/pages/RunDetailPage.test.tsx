import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { RunDetailPage } from "./RunDetailPage";

const mocks = vi.hoisted(() => ({
  updateFinding: vi.fn(),
  generateReports: vi.fn(),
  runStatus: "waiting_approval",
}));

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
      status: mocks.runStatus,
      success_criteria: [],
      scope: { cidrs: [], ips: ["127.0.0.1"], domains: [], url_prefixes: [], exclusions: [] },
      created_at: "2026-07-29T00:00:00Z",
      started_at: "2026-07-29T00:00:01Z",
      workspace_path: "/tmp/run-1",
      temporal_workflow_id: "workflow-run-1",
    },
  }),
  useRunEvents: () => ({ isSuccess: true, isLoading: false, data: { items: [] } }),
  useFindings: () => ({
    isLoading: false,
    data: {
      items: [
        {
          id: "finding-1",
          run_id: "run-1",
          title: "Exposed service",
          severity: "high",
          status: "draft",
          affected_assets: ["127.0.0.1"],
          description: "Development service is reachable.",
          evidence: [
            {
              artifact_id: "artifact-1",
              execution_id: null,
              description: "Banner capture",
              location: "line:1",
            },
          ],
          reproduction_steps: ["curl localhost"],
          impact: "Metadata exposure",
          recommendation: "Restrict access",
          created_at: "2026-07-29T00:00:03Z",
          updated_at: "2026-07-29T00:00:03Z",
        },
      ],
    },
  }),
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
  useReports: () => ({
    isLoading: false,
    data: {
      items: [
        {
          id: "report-1",
          run_id: "run-1",
          format: "markdown",
          artifact_id: "artifact-report-1",
          finding_ids: ["finding-1"],
          created_at: "2026-07-29T00:00:04Z",
          content_url: "/api/v1/artifacts/artifact-report-1/content",
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
  useFindingControl: () => ({
    update: {
      isPending: false,
      error: null,
      mutateAsync: mocks.updateFinding,
    },
  }),
  useReportControl: () => ({
    generate: {
      isPending: false,
      error: null,
      mutate: mocks.generateReports,
    },
  }),
}));

describe("RunDetailPage approvals", () => {
  beforeEach(() => {
    mocks.runStatus = "waiting_approval";
    vi.clearAllMocks();
  });

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

  it("links evidence to artifacts and saves user edits", async () => {
    mocks.updateFinding.mockResolvedValue({});
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

    screen.getAllByRole("tab", { name: /findings 1/i }).at(-1)?.click();
    expect(await screen.findByText("Banner capture")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /artifact artifact-1/i })).toHaveAttribute(
      "href",
      "/api/v1/artifacts/artifact-1/content",
    );
    screen.getByRole("button", { name: /edit exposed service/i }).click();
    expect(await screen.findByDisplayValue("Exposed service")).toBeInTheDocument();
    screen.getByRole("button", { name: /save finding/i }).click();

    expect(mocks.updateFinding).toHaveBeenCalledWith(
      expect.objectContaining({
        findingId: "finding-1",
        payload: expect.objectContaining({
          title: "Exposed service",
          status: "draft",
          evidence: expect.arrayContaining([
            expect.objectContaining({ artifact_id: "artifact-1" }),
          ]),
        }),
      }),
    );
  });
  it("opens generated reports and can request a fresh report set", async () => {
    mocks.runStatus = "completed";
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

    screen.getAllByRole("tab", { name: /reports 1/i }).at(-1)?.click();
    expect(await screen.findByText("MARKDOWN")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /open report/i })).toHaveAttribute(
      "href",
      "/api/v1/artifacts/artifact-report-1/content",
    );
    screen.getByRole("button", { name: /generate reports/i }).click();
    expect(mocks.generateReports).toHaveBeenCalled();
  });

});
