import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { LocalAuditFinding, LocalAuditJob } from "../api/types";
import { LocalAuditDetailPage } from "./LocalAuditDetailPage";

const mocks = vi.hoisted(() => ({
  audit: null as LocalAuditJob | null,
  cancel: vi.fn(),
  fetchNextPage: vi.fn(),
  finding: null as LocalAuditFinding | null,
  findings: [] as LocalAuditFinding[],
  hasNextPage: false,
  selectedFindingIds: [] as string[],
}));

vi.mock("../hooks/queries", () => ({
  useLocalAudit: () => ({
    isLoading: false,
    error: null,
    data: mocks.audit,
  }),
  useLocalAuditControl: () => ({
    cancel: {
      mutate: mocks.cancel,
      isPending: false,
      error: null,
    },
  }),
  useLocalAuditFindings: () => ({
    isLoading: false,
    isFetchingNextPage: false,
    error: null,
    data: {
      pages: [
        {
          items: mocks.findings,
          total: mocks.hasNextPage ? 1001 : mocks.findings.length,
          limit: 100,
          offset: 0,
        },
      ],
    },
    hasNextPage: mocks.hasNextPage,
    fetchNextPage: mocks.fetchNextPage,
  }),
  useLocalAuditSeveritySummary: () => ({
    isLoading: false,
    error: null,
    data: { critical: 0, high: 1, medium: 0, low: 0, info: 0 },
  }),
  useLocalAuditFinding: (_auditId: string, findingId: string) => {
    mocks.selectedFindingIds.push(findingId);
    return {
      isLoading: false,
      error: null,
      data: findingId ? mocks.finding : undefined,
    };
  },
}));

afterEach(cleanup);

describe("LocalAuditDetailPage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mocks.audit = auditJob("scanning");
    mocks.finding = finding();
    mocks.findings = [];
    mocks.hasNextPage = false;
    mocks.selectedFindingIds = [];
  });

  it("shows scanning progress and can cancel a non-terminal audit", async () => {
    const user = userEvent.setup();
    renderPage();

    expect(screen.getByRole("status")).toHaveTextContent("Scanning sealed snapshot");
    await user.click(screen.getByRole("button", { name: "Cancel audit" }));

    expect(mocks.cancel).toHaveBeenCalledTimes(1);
    expect(screen.queryByText("/Users/operator/private-source")).not.toBeInTheDocument();
  });

  it("selects the first completed Finding and renders redacted evidence", async () => {
    mocks.audit = auditJob("completed");
    mocks.findings = [finding()];
    renderPage();

    await waitFor(() =>
      expect(mocks.selectedFindingIds).toContain("finding/1"),
    );
    expect(screen.getAllByText("Hard-coded credential")).toHaveLength(2);
    expect(screen.getByText("secret.detect@1.0.0")).toBeInTheDocument();
    expect(screen.getAllByText("src/config.ts:12:7")).toHaveLength(2);
    expect(screen.getByText('token = "[REDACTED]"')).toBeInTheDocument();
    expect(screen.getByLabelText("Severity summary")).toHaveTextContent("high1");
    expect(screen.queryByRole("button", { name: "Cancel audit" })).not.toBeInTheDocument();
    expect(screen.queryByText("/Users/operator/private-source")).not.toBeInTheDocument();
  });

  it("loads the next Finding page instead of silently truncating results", async () => {
    const user = userEvent.setup();
    mocks.audit = auditJob("completed");
    mocks.findings = [finding()];
    mocks.hasNextPage = true;
    renderPage();

    await user.click(screen.getByRole("button", { name: /Load more findings/ }));

    expect(mocks.fetchNextPage).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("button", { name: /Load more findings/ })).toHaveTextContent(
      "1 / 1001",
    );
  });

  it.each([
    ["failed", "Local audit failed", "scanner_failed"],
    ["cancelled", "Local audit cancelled", null],
  ] as const)("renders the %s terminal state", (status, title, failureCode) => {
    mocks.audit = {
      ...auditJob(status),
      failure_code: failureCode,
    };

    renderPage();

    expect(screen.getByText(title)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Cancel audit" })).not.toBeInTheDocument();
  });

  it("renders a completed zero-Finding result", () => {
    mocks.audit = auditJob("completed");

    renderPage();

    expect(screen.getByText("No security findings")).toBeInTheDocument();
  });
});

function renderPage() {
  render(
    <QueryClientProvider client={new QueryClient()}>
      <MemoryRouter initialEntries={["/audits/audit%2F1"]}>
        <Routes>
          <Route path="/audits/:auditId" element={<LocalAuditDetailPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

function auditJob(status: LocalAuditJob["status"]): LocalAuditJob {
  return {
    audit_id: "audit/1",
    status,
    cancel_requested: false,
    failure_code: null,
    source_identity_digest: "source-digest",
    snapshot_digest: "snapshot-digest",
    manifest_digest: "manifest-digest",
    inventory_digest: "inventory-digest",
    detector_run_digest: "detector-digest",
    report_digest: status === "completed" ? "a".repeat(64) : null,
    total_files: 9,
    scanned_files: status === "completed" ? 9 : 4,
    finding_count: status === "completed" ? 1 : 0,
    created_at: "2026-08-05T01:00:00Z",
    updated_at: "2026-08-05T01:00:03Z",
    queued_at: "2026-08-05T01:00:01Z",
    started_at: "2026-08-05T01:00:02Z",
    finished_at: status === "completed" ? "2026-08-05T01:00:03Z" : null,
    ...({ source_path: "/Users/operator/private-source" } as object),
  };
}

function finding(): LocalAuditFinding {
  return {
    finding_id: "finding/1",
    rule_id: "secret.detect",
    rule_version: "1.0.0",
    category: "secrets",
    title: "Hard-coded credential",
    severity: "high",
    confidence: 0.98,
    relative_path: "src/config.ts",
    blob_digest: "blob-digest",
    line: 12,
    column: 7,
    end_line: 12,
    end_column: 28,
    evidence_excerpt: 'token = "[REDACTED]"',
  };
}
