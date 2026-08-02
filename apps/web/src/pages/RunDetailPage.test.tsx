import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useLayoutEffect } from "react";
import {
  Link,
  MemoryRouter,
  Route,
  Routes,
  useLocation,
  useNavigate,
} from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import trafficMetadataListFixture from "../../../../tests/fixtures/traffic_metadata_list.json";
import { api, RiftXAPIError } from "../api/client";
import type { GraphViewPage, RunAction, RunActionListItem } from "../api/types";
import { LanguageProvider, languageStorageKey } from "../i18n";
import { RunDetailPage } from "./RunDetailPage";

const mocks = vi.hoisted(() => ({
  updateFinding: vi.fn(),
  generateReports: vi.fn(),
  emergencyStop: vi.fn(),
  approveApproval: vi.fn(),
  rejectApproval: vi.fn(),
  messageMutateAsync: vi.fn(),
  messageRunIds: [] as string[],
  messageError: null as Error | null,
  runStatus: "waiting_approval",
  approvalStatus: "pending" as "pending" | "approved" | "rejected",
  runStartedAt: "2026-07-29T00:00:01Z" as string | null,
  runEvents: [] as Array<Record<string, unknown>>,
  executionStatus: "exited",
  physicalStopConfirmedAt: "2026-07-29T00:00:03Z" as string | null,
  actionItems: [] as RunActionListItem[],
  actionItemsByRun: new Map<string, RunActionListItem[]>(),
  actionDetails: new Map<string, RunAction>(),
  actionListError: null as Error | null,
  actionDetailError: null as Error | null,
  actionListLoading: false,
  actionListIsSuccess: true,
  actionListDataUpdatedAt: 1,
  actionDetailLoading: false,
  actionFetchNextPageError: false,
  actionHasNextPage: false,
  actionFetchingNextPage: false,
  fetchNextActionPage: vi.fn(),
  eventStreamError: null as Error | null,
  eventStreamStale: false,
  actionUpdateRevision: 0,
  trafficWorkspaceModuleLoads: 0,
}));

vi.mock("./RunTrafficWorkspace", async (importOriginal) => {
  mocks.trafficWorkspaceModuleLoads += 1;
  return importOriginal<typeof import("./RunTrafficWorkspace")>();
});

vi.mock("../hooks/useEventStream", () => ({
  useEventStream: () => ({
    connected: true,
    stale: mocks.eventStreamStale,
    error: mocks.eventStreamError,
    actionUpdateRevision: mocks.actionUpdateRevision,
  }),
}));
vi.mock("../components/TerminalPanel", () => ({ TerminalPanel: () => null }));
vi.mock("../hooks/queries", () => ({
  flattenRunActionPages: (pages: Array<{ items: RunActionListItem[] }> | undefined) =>
    pages?.flatMap((page) => page.items) ?? [],
  useRun: (runId: string) => ({
    isLoading: false,
    error: null,
    data: {
      id: runId,
      engagement_id: runId === "run-1" ? "engagement-1" : "engagement-2",
      objective: { description: "Inspect local service" },
      node_id: "local",
      approval_mode: "balanced",
      model_profile: null,
      status: mocks.runStatus,
      success_criteria: [],
      entry_points: [{ kind: "ip", value: "127.0.0.1", metadata: {} }],
      scope: { cidrs: [], ips: ["127.0.0.1"], domains: [], url_prefixes: [], exclusions: [] },
      created_at: "2026-07-29T00:00:00Z",
      started_at: mocks.runStartedAt,
      workspace_path: "/tmp/run-1",
      temporal_workflow_id: "workflow-run-1",
    },
  }),
  useRunEvents: () => ({
    isSuccess: true,
    isLoading: false,
    data: {
      items: mocks.runEvents.length
        ? mocks.runEvents
        : [
            {
              id: "event-agent-1",
              run_id: "run-1",
              sequence: 1,
              event_type: "agent.plan_updated",
              payload: { plan_summary: "Inspect the service and verify evidence." },
              created_at: "2026-07-29T00:00:02Z",
            },
          ],
    },
  }),
  useRunActions: (runId: string) => ({
    isLoading: mocks.actionListLoading,
    isSuccess: mocks.actionListIsSuccess,
    dataUpdatedAt: mocks.actionListDataUpdatedAt,
    error: mocks.actionListError,
    data: {
      pages: [{ items: mocks.actionItemsByRun.get(runId) ?? mocks.actionItems }],
    },
    hasNextPage: mocks.actionHasNextPage,
    isFetchingNextPage: mocks.actionFetchingNextPage,
    isFetchNextPageError: mocks.actionFetchNextPageError,
    fetchNextPage: mocks.fetchNextActionPage,
  }),
  useRunAction: (runId: string, actionId: string) => ({
    isLoading: mocks.actionDetailLoading,
    error: actionId ? mocks.actionDetailError : null,
    data: mocks.actionDetails.get(`${runId}:${actionId}`),
  }),
  useExecutions: () => ({
    isLoading: false,
    data: {
      items: [
        {
          id: "execution-1",
          execution_key: "run-1:step-1:call-1",
          run_id: "run-1",
          session_id: "session-1",
          tool_call_id: "call-1",
          attempt_group: "initial",
          node_id: "local",
          executor_type: "process",
          argv: ["nmap", "-sV", "127.0.0.1"],
          command_text: null,
          tool_id: "nmap",
          tool_version: "7.95",
          executable_path: "/usr/bin/nmap",
          cwd: "/tmp/run-1",
          env_diff: {},
          platform_system: "linux",
          platform_release: "6.8",
          platform_architecture: "x86_64",
          status: mocks.executionStatus,
          pid: 123,
          process_group_id: 123,
          containment_id: "cgroup-v2:test:execution",
          exit_code: 0,
          stdout_path: "/tmp/stdout.log",
          stderr_path: "/tmp/stderr.log",
          process_created_at: "2026-07-29T00:00:02Z",
          started_at: "2026-07-29T00:00:02Z",
          finished_at: "2026-07-29T00:00:03Z",
          physical_stop_confirmed_at: mocks.physicalStopConfirmedAt,
        },
      ],
    },
  }),
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
          status: mocks.approvalStatus,
          tool_name: "nmap",
          command: ["nmap", "-sV", "127.0.0.1"],
          cwd: "/tmp/run-1",
          target_summary: "ip:127.0.0.1",
          env_diff: {},
          reason: "Identify the local service.",
          decided_by: mocks.approvalStatus === "pending" ? null : "api-user",
          created_at: "2026-07-29T00:00:02Z",
          decided_at:
            mocks.approvalStatus === "pending" ? null : "2026-07-29T00:00:03Z",
        },
      ],
    },
  }),
  useRunControl: (runId: string) => ({
    pause: { isPending: false, error: null, mutate: vi.fn() },
    resume: { isPending: false, error: null, mutate: vi.fn() },
    emergencyStop: { isPending: false, error: null, mutate: mocks.emergencyStop },
    message: {
      isPending: false,
      error: mocks.messageError,
      mutateAsync: (input: { message: string; messageEventId: string }) => {
        mocks.messageRunIds.push(runId);
        return mocks.messageMutateAsync(input);
      },
    },
  }),
  useApprovalControl: () => ({
    approve: { isPending: false, error: null, mutate: mocks.approveApproval },
    reject: { isPending: false, error: null, mutate: mocks.rejectApproval },
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

function deferredVoid() {
  let resolve!: () => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<void>((promiseResolve, promiseReject) => {
    resolve = promiseResolve;
    reject = promiseReject;
  });
  return { promise, reject, resolve };
}

function installLocalStorage() {
  const values = new Map<string, string>();
  const storage: Storage = {
    get length() {
      return values.size;
    },
    clear: () => values.clear(),
    getItem: (key) => values.get(key) ?? null,
    key: (index) => [...values.keys()][index] ?? null,
    removeItem: (key) => values.delete(key),
    setItem: (key, value) => values.set(key, String(value)),
  };
  Object.defineProperty(window, "localStorage", {
    configurable: true,
    value: storage,
  });
}

function SubmitComposerBeforePassiveEffects({ path }: { path: string }) {
  const location = useLocation();
  useLayoutEffect(() => {
    if (location.pathname !== path) return;
    document
      .querySelector<HTMLFormElement>("form.message-composer")
      ?.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
  }, [location.pathname, path]);
  return null;
}

function RunNavigationHarness({ autoSubmitPath }: { autoSubmitPath?: string }) {
  return (
    <>
      <Link to="/runs/run-2">Open Run 2</Link>
      {autoSubmitPath ? <SubmitComposerBeforePassiveEffects path={autoSubmitPath} /> : null}
      <Routes>
        <Route path="/runs/:runId" element={<RunDetailPage />} />
      </Routes>
    </>
  );
}

function ActionHistoryHarness() {
  const location = useLocation();
  const navigate = useNavigate();
  return (
    <>
      <output aria-label="Current location">{`${location.pathname}${location.search}`}</output>
      <button type="button" onClick={() => navigate(-1)}>History back</button>
      <button type="button" onClick={() => navigate(1)}>History forward</button>
      <Routes>
        <Route path="/runs/:runId" element={<RunDetailPage />} />
      </Routes>
    </>
  );
}

function SameRawActionRunHarness() {
  const location = useLocation();
  return (
    <>
      <output aria-label="Current location">{`${location.pathname}${location.search}`}</output>
      <Link to="/runs/run-2?action=action-shared">Open same Action in Run 2</Link>
      <Routes>
        <Route path="/runs/:runId" element={<RunDetailPage />} />
      </Routes>
    </>
  );
}

function actionListItem(
  actionId = "action-1",
  runId = "run-1",
  overrides: Partial<RunActionListItem> = {},
): RunActionListItem {
  return {
    action_id: actionId,
    run_id: runId,
    session_id: `session-${runId}`,
    cycle_id: "cycle-shared",
    step_id: `step-${actionId}`,
    engine_call_id: "provider-call-shared",
    graph_ref: {
      view: "task",
      node_id: `action:${runId}:${actionId}`,
      projection_quality: "exact",
    },
    tool_id: "nmap",
    skill_id: null,
    reason: `Public reason for ${actionId}`,
    target_summary: `target-${actionId}.test:443`,
    approval_level: "sensitive",
    approval_id: `approval-${actionId}`,
    approval_status: "approved",
    approval_actor: "local-principal:v1:operator",
    approval_decided_at: "2026-08-02T09:00:01Z",
    approval_correlation_quality: "exact",
    execution_count: 1,
    attempts: [
      {
        execution_id: `execution-${actionId}`,
        attempt_group: "initial",
        node_id: "runner-1",
        status: "exited",
        created_at: "2026-08-02T09:00:02Z",
        started_at: "2026-08-02T09:00:03Z",
        finished_at: "2026-08-02T09:00:04Z",
        exit_code: 0,
        correlation_quality: "exact",
        physical_stop_confirmed_at: "2026-08-02T09:00:04Z",
        stop_confirmation: "confirmed",
      },
    ],
    attempt_coverage: { scanned: 1, limit: 100, truncated: false },
    latest_execution_id: `execution-${actionId}`,
    latest_execution_status: "exited",
    current_execution_id: `execution-${actionId}`,
    current_execution_status: "exited",
    latest_stop_confirmation: "confirmed",
    current_stop_confirmation: "confirmed",
    attempt_order_quality: "exact",
    artifact_ids: [`artifact-${actionId}`],
    artifact_count: 1,
    artifacts_truncated: false,
    output_size: 0,
    output_available: false,
    finding_count: 1,
    event_count: 1,
    finding_coverage: { scanned: 1, limit: 100, truncated: false },
    event_coverage: { scanned: 1, limit: 200, truncated: false },
    lifecycle: "succeeded",
    lifecycle_sources: ["execution.status"],
    correlation_quality: "exact",
    partial_reasons: [],
    created_at: "2026-08-02T09:00:00Z",
    updated_at: "2026-08-02T09:00:04Z",
    version: `version-${actionId}`,
    ...overrides,
  };
}

function actionDetail(item: RunActionListItem, overrides: Partial<RunAction> = {}): RunAction {
  return {
    action_id: item.action_id,
    run_id: item.run_id,
    session_id: item.session_id,
    cycle_id: item.cycle_id,
    step_id: item.step_id,
    engine_call_id: item.engine_call_id,
    graph_ref: item.graph_ref,
    tool_id: item.tool_id,
    skill_id: item.skill_id,
    reason: item.reason,
    target_summary: item.target_summary,
    approval_level: item.approval_level,
    arguments_summary: { target: item.target_summary, secret: "[REDACTED]" },
    approval: {
      approval_id: item.approval_id!,
      status: item.approval_status,
      actor: item.approval_actor,
      decided_at: item.approval_decided_at,
      feedback_summary: "Approved within scope",
      correlation_quality: "exact",
    },
    executions: item.attempts.map((attempt) => ({ ...attempt, error_summary: null })),
    execution_count: item.execution_count,
    attempt_coverage: item.attempt_coverage,
    latest_execution_id: item.latest_execution_id,
    current_execution_id: item.current_execution_id,
    latest_stop_confirmation: item.latest_stop_confirmation,
    current_stop_confirmation: item.current_stop_confirmation,
    attempt_order_quality: item.attempt_order_quality,
    result: {
      truncated: item.artifacts_truncated,
      artifact_ids: item.artifact_ids,
      artifact_count: item.artifact_count,
      output_size: item.output_size,
      output_available: item.output_available,
    },
    evidence: {
      finding_ids: [`finding-${item.action_id}`],
      artifact_ids: item.artifact_ids,
      events: [],
      finding_count: item.finding_count,
      event_count: item.event_count,
      finding_coverage: item.finding_coverage,
      event_coverage: item.event_coverage,
    },
    lifecycle: item.lifecycle,
    lifecycle_sources: item.lifecycle_sources,
    correlation_quality: item.correlation_quality,
    partial_reasons: item.partial_reasons,
    created_at: item.created_at,
    updated_at: item.updated_at,
    version: item.version,
    ...overrides,
  };
}

function graphPageForAction(
  runId: string,
  engagementId: string,
  actionId: string,
): GraphViewPage {
  return {
    scope: { engagement_id: engagementId, run_id: runId },
    view: "task",
    snapshot: { id: `snapshot-${runId}`, stale: false },
    nodes: [
      {
        id: `action:${runId}:${actionId}`,
        type: "action",
        domain_id: actionId,
        label: `Action ${actionId}`,
        status: "succeeded",
        provenance_refs: ["tool_call_intents"],
        projection_quality: "exact",
        partial_reasons: [],
      },
    ],
    edges: [],
    type_metadata: [
      {
        kind: "node",
        type: "action",
        label: "Server action",
        color: "#16a34a",
      },
    ],
    partial_reasons: [],
    truncated: false,
    has_more: false,
    next_cursor: null,
  };
}

function trafficContracts(
  runId = "run-1",
  engagementId = "engagement-1",
  exchangeId = "exchange-1",
  origin = "https://run-one.example.test",
) {
  const page = structuredClone(trafficMetadataListFixture);
  const item = page.items[0]!;
  page.scope.run_id = runId;
  page.scope.engagement_id = engagementId;
  item.exchange_id = exchangeId;
  item.request_id = exchangeId;
  item.execution_key = `execution:${exchangeId}`;
  item.lineage.run_id = runId;
  item.url_summary.origin = origin;
  return {
    detail: {
      scope: structuredClone(page.scope),
      item: structuredClone(item),
    },
    page,
  };
}

function renderActionRoute(
  initialEntry: string,
  { history = false, language = false } = {},
) {
  const queryClient = new QueryClient();
  const routed = (
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[initialEntry]}>
        {history ? (
          <ActionHistoryHarness />
        ) : (
          <Routes>
            <Route path="/runs/:runId" element={<RunDetailPage />} />
          </Routes>
        )}
      </MemoryRouter>
    </QueryClientProvider>
  );
  return render(language ? <LanguageProvider>{routed}</LanguageProvider> : routed);
}

describe("RunDetailPage approvals", () => {
  beforeEach(() => {
    cleanup();
    vi.restoreAllMocks();
    installLocalStorage();
    window.sessionStorage.clear();
    mocks.runStatus = "waiting_approval";
    mocks.approvalStatus = "pending";
    mocks.runStartedAt = "2026-07-29T00:00:01Z";
    mocks.runEvents = [];
    mocks.executionStatus = "exited";
    mocks.physicalStopConfirmedAt = "2026-07-29T00:00:03Z";
    mocks.actionItems = [];
    mocks.actionItemsByRun.clear();
    mocks.actionDetails.clear();
    mocks.actionListError = null;
    mocks.actionDetailError = null;
    mocks.actionListLoading = false;
    mocks.actionListIsSuccess = true;
    mocks.actionListDataUpdatedAt = 1;
    mocks.actionDetailLoading = false;
    mocks.actionFetchNextPageError = false;
    mocks.actionHasNextPage = false;
    mocks.actionFetchingNextPage = false;
    mocks.eventStreamError = null;
    mocks.eventStreamStale = false;
    mocks.actionUpdateRevision = 0;
    mocks.messageError = null;
    mocks.messageRunIds.length = 0;
    mocks.messageMutateAsync.mockResolvedValue(undefined);
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

  it("does not allow pending approvals to be decided after the Run ends", async () => {
    mocks.runStatus = "failed";
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

    screen.getByRole("button", { name: /1 tool call awaiting approval/i }).click();
    expect(
      await screen.findByText(
        "This Run has ended; the pending approval can no longer be decided.",
      ),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /approve once/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /approve for run/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /reject/i })).not.toBeInTheDocument();
  });

  it.each(["approved", "rejected"] as const)(
    "resynchronizes the immutable saved %s decision through its original endpoint",
    (approvalStatus) => {
      mocks.approvalStatus = approvalStatus;
      mocks.runEvents = [
        {
          id: "event-cycle-yielded",
          run_id: "run-1",
          sequence: 8,
          event_type: "runtime.cycle_yielded",
          payload: {
            cycle_id: "cycle-1",
            yield_reason: "approval_required",
            waiting_object_id: "approval-1",
          },
          created_at: "2026-07-29T00:00:02Z",
        },
      ];
      render(
        <QueryClientProvider client={new QueryClient()}>
          <MemoryRouter initialEntries={["/runs/run-1"]}>
            <Routes>
              <Route path="/runs/:runId" element={<RunDetailPage />} />
            </Routes>
          </MemoryRouter>
        </QueryClientProvider>,
      );

      fireEvent.click(
        screen.getByRole("button", { name: "Resync saved decision" }),
      );

      const expectedMutation =
        approvalStatus === "approved"
          ? mocks.approveApproval
          : mocks.rejectApproval;
      const oppositeMutation =
        approvalStatus === "approved"
          ? mocks.rejectApproval
          : mocks.approveApproval;
      expect(expectedMutation).toHaveBeenCalledWith({ approvalId: "approval-1" });
      expect(oppositeMutation).not.toHaveBeenCalled();
      fireEvent.click(screen.getByRole("tab", { name: "Approvals 0" }));
      expect(screen.queryByRole("button", { name: "Approve once" })).not.toBeInTheDocument();
      expect(screen.queryByRole("button", { name: "Reject" })).not.toBeInTheDocument();
    },
  );

  it.each([
    ["Run is no longer waiting", "running", "approved", "approval-1"],
    ["saved Approval is still pending", "waiting_approval", "pending", "approval-1"],
    ["yield points to another object", "waiting_approval", "approved", "approval-2"],
  ] as const)(
    "does not offer approval resynchronization when %s",
    (_label, runStatus, approvalStatus, waitingObjectId) => {
      mocks.runStatus = runStatus;
      mocks.approvalStatus = approvalStatus;
      mocks.runEvents = [
        {
          id: "event-cycle-yielded",
          run_id: "run-1",
          sequence: 8,
          event_type: "runtime.cycle_yielded",
          payload: {
            cycle_id: "cycle-1",
            yield_reason: "approval_required",
            waiting_object_id: waitingObjectId,
          },
          created_at: "2026-07-29T00:00:02Z",
        },
      ];
      render(
        <QueryClientProvider client={new QueryClient()}>
          <MemoryRouter initialEntries={["/runs/run-1"]}>
            <Routes>
              <Route path="/runs/:runId" element={<RunDetailPage />} />
            </Routes>
          </MemoryRouter>
        </QueryClientProvider>,
      );

      expect(
        screen.queryByRole("button", { name: "Resync saved decision" }),
      ).not.toBeInTheDocument();
    },
  );

  it("uses only the latest durable cycle yield when offering resynchronization", () => {
    mocks.approvalStatus = "approved";
    mocks.runEvents = [
      {
        id: "event-old-yield",
        run_id: "run-1",
        sequence: 8,
        event_type: "runtime.cycle_yielded",
        payload: {
          cycle_id: "cycle-1",
          yield_reason: "approval_required",
          waiting_object_id: "approval-1",
        },
        created_at: "2026-07-29T00:00:02Z",
      },
      {
        id: "event-new-yield",
        run_id: "run-1",
        sequence: 12,
        event_type: "runtime.cycle_yielded",
        payload: {
          cycle_id: "cycle-2",
          yield_reason: "user_input_required",
          waiting_object_id: "input-1",
        },
        created_at: "2026-07-29T00:00:04Z",
      },
    ];
    render(
      <QueryClientProvider client={new QueryClient()}>
        <MemoryRouter initialEntries={["/runs/run-1"]}>
          <Routes>
            <Route path="/runs/:runId" element={<RunDetailPage />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    );

    expect(
      screen.queryByRole("button", { name: "Resync saved decision" }),
    ).not.toBeInTheDocument();
  });

  it("uses the full-Run emergency stop control", () => {
    mocks.runStatus = "running";
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

    fireEvent.click(
      screen.getByRole("button", {
        name: "Emergency stop — cancel the entire Run",
      }),
    );

    expect(mocks.emergencyStop).toHaveBeenCalledOnce();
    expect(screen.queryByText("Cancel execution")).not.toBeInTheDocument();
  });

  it("keeps emergency cleanup available after a Run reaches a terminal status", () => {
    mocks.runStatus = "failed";
    render(
      <QueryClientProvider client={new QueryClient()}>
        <MemoryRouter initialEntries={["/runs/run-1"]}>
          <Routes>
            <Route path="/runs/:runId" element={<RunDetailPage />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    );

    const emergencyStop = screen.getByRole("button", {
      name: "Emergency stop — cancel the entire Run",
    });
    expect(emergencyStop).toBeEnabled();
    expect(screen.getByRole("button", { name: "Pause" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Resume" })).toBeDisabled();

    fireEvent.click(emergencyStop);
    expect(mocks.emergencyStop).toHaveBeenCalledOnce();
  });

  it("shows a message delivery failure instead of silently leaving the conversation idle", () => {
    mocks.runStatus = "waiting_user";
    mocks.runStartedAt = null;
    mocks.messageError = new Error("Temporal is unavailable");
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

    expect(screen.getByRole("alert")).toHaveTextContent("Temporal is unavailable");
  });

  it("keeps the first-instruction draft when delivery fails", async () => {
    mocks.runStatus = "waiting_user";
    mocks.runStartedAt = null;
    mocks.messageMutateAsync.mockRejectedValueOnce(new Error("Temporal is unavailable"));
    render(
      <QueryClientProvider client={new QueryClient()}>
        <MemoryRouter initialEntries={["/runs/run-1"]}>
          <Routes>
            <Route path="/runs/:runId" element={<RunDetailPage />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    );

    const input = screen.getByLabelText("Message to Agent");
    fireEvent.change(input, { target: { value: "Start with passive discovery" } });
    fireEvent.click(screen.getByRole("button", { name: "Send message" }));

    await waitFor(() =>
      expect(mocks.messageMutateAsync).toHaveBeenCalledWith({
        message: "Start with passive discovery",
        messageEventId: expect.any(String),
      }),
    );
    expect(input).toHaveValue("Start with passive discovery");
  });

  it("retries an ambiguous Temporal delivery with the same durable message event", async () => {
    mocks.runStatus = "waiting_user";
    mocks.runStartedAt = null;
    mocks.messageMutateAsync
      .mockRejectedValueOnce(
        new RiftXAPIError(503, "temporal_unavailable", "Temporal is unavailable", {
          message_event_id: "event-message-1",
          retry_same_message: true,
        }),
      )
      .mockResolvedValueOnce(undefined);
    render(
      <QueryClientProvider client={new QueryClient()}>
        <MemoryRouter initialEntries={["/runs/run-1"]}>
          <Routes>
            <Route path="/runs/:runId" element={<RunDetailPage />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    );

    const input = screen.getByLabelText("Message to Agent");
    fireEvent.change(input, { target: { value: "Start with passive discovery" } });
    fireEvent.click(screen.getByRole("button", { name: "Send message" }));
    await waitFor(() => expect(mocks.messageMutateAsync).toHaveBeenCalledTimes(1));

    fireEvent.click(screen.getByRole("button", { name: "Send message" }));
    await waitFor(() => expect(mocks.messageMutateAsync).toHaveBeenCalledTimes(2));

    expect(mocks.messageMutateAsync).toHaveBeenNthCalledWith(2, {
      message: "Start with passive discovery",
      messageEventId: "event-message-1",
    });
    await waitFor(() => expect(input).toHaveValue(""));
  });

  it("restores the same durable message event after a refresh", async () => {
    mocks.runStatus = "waiting_user";
    mocks.runStartedAt = null;
    mocks.messageMutateAsync.mockRejectedValueOnce(
      new RiftXAPIError(503, "temporal_unavailable", "Temporal is unavailable", {
        message_event_id: "event-message-refresh-1",
        retry_same_message: true,
      }),
    );
    const firstMount = render(
      <QueryClientProvider client={new QueryClient()}>
        <MemoryRouter initialEntries={["/runs/run-1"]}>
          <Routes>
            <Route path="/runs/:runId" element={<RunDetailPage />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    );

    const firstInput = screen.getByLabelText("Message to Agent");
    fireEvent.change(firstInput, { target: { value: "Start with passive discovery" } });
    fireEvent.click(screen.getByRole("button", { name: "Send message" }));
    await waitFor(() =>
      expect(window.sessionStorage.getItem("riftx.run-message-retry:run-1")).toContain(
        "event-message-refresh-1",
      ),
    );
    firstMount.unmount();

    mocks.messageMutateAsync.mockResolvedValueOnce(undefined);
    render(
      <QueryClientProvider client={new QueryClient()}>
        <MemoryRouter initialEntries={["/runs/run-1"]}>
          <Routes>
            <Route path="/runs/:runId" element={<RunDetailPage />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    );

    const restoredInput = await screen.findByDisplayValue("Start with passive discovery");
    fireEvent.click(screen.getByRole("button", { name: "Send message" }));
    await waitFor(() => expect(mocks.messageMutateAsync).toHaveBeenCalledTimes(2));
    expect(mocks.messageMutateAsync).toHaveBeenNthCalledWith(2, {
      message: "Start with passive discovery",
      messageEventId: "event-message-refresh-1",
    });
    await waitFor(() => expect(restoredInput).toHaveValue(""));
    expect(window.sessionStorage.getItem("riftx.run-message-retry:run-1")).toBeNull();
  });

  it("never submits Run A's draft through Run B and ignores Run A's late success", async () => {
    mocks.runStatus = "waiting_user";
    mocks.runStartedAt = null;
    const firstRequest = deferredVoid();
    mocks.messageMutateAsync.mockReturnValueOnce(firstRequest.promise);
    render(
      <QueryClientProvider client={new QueryClient()}>
        <MemoryRouter initialEntries={["/runs/run-1"]}>
          <RunNavigationHarness autoSubmitPath="/runs/run-2" />
        </MemoryRouter>
      </QueryClientProvider>,
    );

    const input = screen.getByLabelText("Message to Agent");
    fireEvent.change(input, { target: { value: "Instruction for Run A" } });
    fireEvent.click(screen.getByRole("button", { name: "Send message" }));
    await waitFor(() => expect(mocks.messageMutateAsync).toHaveBeenCalledTimes(1));
    expect(mocks.messageRunIds).toEqual(["run-1"]);

    fireEvent.click(screen.getByRole("link", { name: "Open Run 2" }));
    await waitFor(() => expect(input).toHaveValue(""));
    // The harness submits in a layout effect, before RunDetailPage's passive
    // draft-restoration effect. Run A's transition-state draft must not leak.
    expect(mocks.messageMutateAsync).toHaveBeenCalledTimes(1);

    fireEvent.change(input, { target: { value: "Fresh instruction for Run B" } });
    await act(async () => {
      firstRequest.resolve();
      await firstRequest.promise;
    });

    await waitFor(() => expect(input).toHaveValue("Fresh instruction for Run B"));
    expect(mocks.messageRunIds).toEqual(["run-1"]);
    expect(window.sessionStorage.getItem("riftx.run-message-retry:run-1")).toBeNull();
  });

  it("does not let Run A's late rejection replace Run B's restored retry", async () => {
    mocks.runStatus = "waiting_user";
    mocks.runStartedAt = null;
    window.sessionStorage.setItem(
      "riftx.run-message-retry:run-2",
      JSON.stringify({ message: "Retry instruction for Run B", eventId: "event-run-b" }),
    );
    const firstRequest = deferredVoid();
    mocks.messageMutateAsync.mockReturnValueOnce(firstRequest.promise);
    render(
      <QueryClientProvider client={new QueryClient()}>
        <MemoryRouter initialEntries={["/runs/run-1"]}>
          <RunNavigationHarness />
        </MemoryRouter>
      </QueryClientProvider>,
    );

    const input = screen.getByLabelText("Message to Agent");
    fireEvent.change(input, { target: { value: "Instruction for Run A" } });
    fireEvent.click(screen.getByRole("button", { name: "Send message" }));
    await waitFor(() => expect(mocks.messageMutateAsync).toHaveBeenCalledTimes(1));

    fireEvent.click(screen.getByRole("link", { name: "Open Run 2" }));
    await waitFor(() => expect(input).toHaveValue("Retry instruction for Run B"));
    await act(async () => {
      firstRequest.reject(
        new RiftXAPIError(503, "temporal_unavailable", "Temporal is unavailable", {
          message_event_id: "event-run-a-confirmed",
          retry_same_message: true,
        }),
      );
      await Promise.resolve();
    });

    expect(input).toHaveValue("Retry instruction for Run B");
    expect(window.sessionStorage.getItem("riftx.run-message-retry:run-2")).toContain(
      "event-run-b",
    );
    fireEvent.click(screen.getByRole("button", { name: "Send message" }));
    await waitFor(() => expect(mocks.messageMutateAsync).toHaveBeenCalledTimes(2));
    expect(mocks.messageMutateAsync).toHaveBeenNthCalledWith(2, {
      message: "Retry instruction for Run B",
      messageEventId: "event-run-b",
    });
    expect(mocks.messageRunIds).toEqual(["run-1", "run-2"]);
  });

  it("keeps a same-Run edit when an older message succeeds", async () => {
    mocks.runStatus = "waiting_user";
    mocks.runStartedAt = null;
    const request = deferredVoid();
    mocks.messageMutateAsync.mockReturnValueOnce(request.promise);
    render(
      <QueryClientProvider client={new QueryClient()}>
        <MemoryRouter initialEntries={["/runs/run-1"]}>
          <Routes>
            <Route path="/runs/:runId" element={<RunDetailPage />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    );

    const input = screen.getByLabelText("Message to Agent");
    fireEvent.change(input, { target: { value: "Old instruction" } });
    fireEvent.click(screen.getByRole("button", { name: "Send message" }));
    await waitFor(() => expect(mocks.messageMutateAsync).toHaveBeenCalledTimes(1));
    fireEvent.change(input, { target: { value: "New draft while sending" } });

    await act(async () => {
      request.resolve();
      await request.promise;
    });

    expect(input).toHaveValue("New draft while sending");
    expect(window.sessionStorage.getItem("riftx.run-message-retry:run-1")).toBeNull();
  });

  it("keeps a same-Run edit when an older message is rejected", async () => {
    mocks.runStatus = "waiting_user";
    mocks.runStartedAt = null;
    const request = deferredVoid();
    mocks.messageMutateAsync.mockReturnValueOnce(request.promise);
    render(
      <QueryClientProvider client={new QueryClient()}>
        <MemoryRouter initialEntries={["/runs/run-1"]}>
          <Routes>
            <Route path="/runs/:runId" element={<RunDetailPage />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    );

    const input = screen.getByLabelText("Message to Agent");
    fireEvent.change(input, { target: { value: "Old instruction" } });
    fireEvent.click(screen.getByRole("button", { name: "Send message" }));
    await waitFor(() => expect(mocks.messageMutateAsync).toHaveBeenCalledTimes(1));
    fireEvent.change(input, { target: { value: "New draft after failure" } });

    await act(async () => {
      request.reject(
        new RiftXAPIError(503, "temporal_unavailable", "Temporal is unavailable", {
          message_event_id: "event-old-confirmed",
          retry_same_message: true,
        }),
      );
      await Promise.resolve();
    });

    expect(input).toHaveValue("New draft after failure");
    expect(window.sessionStorage.getItem("riftx.run-message-retry:run-1")).toBeNull();
  });

  it("downloads immutable artifacts through the authenticated client", async () => {
    const download = vi.spyOn(api, "downloadAuthenticatedUrl").mockResolvedValue();
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
    fireEvent.click(screen.getByRole("button", { name: /download/i }));
    expect(download).toHaveBeenCalledWith(
      "/api/v1/artifacts/artifact-1/content",
      "scan.txt",
    );
  });

  it("shows a localized visible error when an authenticated download fails", async () => {
    vi.spyOn(api, "downloadAuthenticatedUrl").mockRejectedValue(
      new RiftXAPIError(401, "local_operator_authentication_failed", "rejected"),
    );
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
    fireEvent.click(await screen.findByRole("button", { name: /download/i }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Download failed. Please try again.");
    expect(alert).toHaveTextContent("local_operator_authentication_failed");
    expect(alert).not.toHaveTextContent("rejected");
  });

  it("explains an oversized authenticated download in Chinese", async () => {
    window.localStorage.setItem(languageStorageKey, "zh-CN");
    vi.spyOn(api, "downloadAuthenticatedUrl").mockRejectedValue(
      new RiftXAPIError(413, "download_too_large", "unlocalized transport message", {
        limit_bytes: 64 * 1024 * 1024,
      }),
    );
    const queryClient = new QueryClient();
    render(
      <LanguageProvider>
        <QueryClientProvider client={queryClient}>
          <MemoryRouter initialEntries={["/runs/run-1"]}>
            <Routes>
              <Route path="/runs/:runId" element={<RunDetailPage />} />
            </Routes>
          </MemoryRouter>
        </QueryClientProvider>
      </LanguageProvider>,
    );

    screen.getAllByRole("tab", { name: /制品 1/ }).at(-1)?.click();
    fireEvent.click(await screen.findByRole("button", { name: "下载" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("下载已阻止：文件超过 64 MiB 安全上限。");
    expect(alert).toHaveTextContent("download_too_large");
    expect(alert).not.toHaveTextContent("下载失败，请重试。");
    expect(alert).not.toHaveTextContent("unlocalized transport message");
  });

  it("keeps plan updates out of Conversation and uses Actions instead of host execution provenance", async () => {
    mocks.actionItems = [actionListItem()];
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

    expect(screen.queryByText("Inspect the service and verify evidence.")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("tab", { name: "Overview" }));
    expect(await screen.findByText("Latest plan")).toBeInTheDocument();
    expect(screen.getByText("Inspect the service and verify evidence.")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("tab", { name: /timeline 1/i }));
    expect(await screen.findByText("Inspect the service and verify evidence.")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("tab", { name: /actions 1/i }));
    expect(await screen.findByText("Public reason for action-1")).toBeInTheDocument();
    expect(screen.getByText("runner-1")).toBeInTheDocument();
    expect(screen.getByText("Stop confirmed")).toBeInTheDocument();
    expect(screen.queryByText("nmap -sV 127.0.0.1")).not.toBeInTheDocument();
    expect(screen.queryByText("/usr/bin/nmap")).not.toBeInTheDocument();
  });

  it("makes a terminal execution without durable stop proof visibly unsafe", async () => {
    mocks.actionItems = [
      actionListItem("action-unsafe", "run-1", {
        lifecycle: "cancelled",
        current_execution_status: "cancelled",
        latest_execution_status: "cancelled",
        current_stop_confirmation: "unconfirmed",
        latest_stop_confirmation: "unconfirmed",
        attempts: [
          {
            ...actionListItem().attempts[0]!,
            status: "cancelled",
            physical_stop_confirmed_at: null,
            stop_confirmation: "unconfirmed",
          },
        ],
      }),
    ];
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

    fireEvent.click(screen.getByRole("tab", { name: /actions 1/i }));
    expect(
      (await screen.findAllByText("Stop unconfirmed")).some((node) =>
        node.classList.contains("stop-unconfirmed"),
      ),
    ).toBe(true);
  });

  it("links evidence to artifacts and saves user edits", async () => {
    mocks.updateFinding.mockResolvedValue({});
    const download = vi.spyOn(api, "downloadAuthenticatedUrl").mockResolvedValue();
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
    fireEvent.click(screen.getByRole("button", { name: /artifact artifact-1/i }));
    expect(download).toHaveBeenCalledWith(
      "/api/v1/artifacts/artifact-1/content",
      undefined,
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
    const download = vi.spyOn(api, "downloadAuthenticatedUrl").mockResolvedValue();
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
    fireEvent.click(screen.getByRole("button", { name: /open report/i }));
    expect(download).toHaveBeenCalledWith(
      "/api/v1/artifacts/artifact-report-1/content",
      undefined,
    );
    screen.getByRole("button", { name: /generate reports/i }).click();
    expect(mocks.generateReports).toHaveBeenCalled();
  });

  it("keeps one assistant bubble across high-level and hidden tool delta events", async () => {
    mocks.runStatus = "running";
    mocks.runEvents = [
      {
        id: "event-19",
        run_id: "run-1",
        sequence: 19,
        event_type: "runtime.engine_event",
        payload: {
          cycle_id: "cycle-1",
          engine_sequence: 9,
          event_type: "assistant_delta",
          data: { delta: "主" },
        },
        created_at: "2026-07-31T02:35:26Z",
      },
      {
        id: "event-20",
        run_id: "run-1",
        sequence: 20,
        event_type: "run.status_changed",
        payload: { from: "preparing", to: "running" },
        created_at: "2026-07-31T02:35:26Z",
      },
      {
        id: "event-21",
        run_id: "run-1",
        sequence: 21,
        event_type: "runtime.engine_event",
        payload: {
          cycle_id: "cycle-1",
          engine_sequence: 10,
          event_type: "tool_call_argument_delta",
          data: { call_id: "call-1", delta: '{"target":"127.0.0.1"}' },
        },
        created_at: "2026-07-31T02:35:26Z",
      },
      {
        id: "event-22",
        run_id: "run-1",
        sequence: 22,
        event_type: "runtime.engine_event",
        payload: {
          cycle_id: "cycle-1",
          engine_sequence: 11,
          event_type: "assistant_delta",
          data: { delta: "代理。" },
        },
        created_at: "2026-07-31T02:35:26Z",
      },
      {
        id: "event-23",
        run_id: "run-1",
        sequence: 23,
        event_type: "runtime.engine_event",
        payload: {
          cycle_id: "cycle-1",
          engine_sequence: 12,
          event_type: "tool_result_delta",
          data: { call_id: "call-1", delta: "partial result" },
        },
        created_at: "2026-07-31T02:35:26Z",
      },
      {
        id: "event-24",
        run_id: "run-1",
        sequence: 24,
        event_type: "runtime.engine_event",
        payload: {
          cycle_id: "cycle-1",
          engine_sequence: 13,
          event_type: "assistant_delta",
          data: { delta: "我现在开始按照" },
        },
        created_at: "2026-07-31T02:35:26Z",
      },
      {
        id: "event-25",
        run_id: "run-1",
        sequence: 25,
        event_type: "agent.message",
        payload: {
          agent_step_id: "cycle-1",
          message: "主代理。我现在开始按照",
        },
        created_at: "2026-07-31T02:35:27Z",
      },
    ];
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

    expect(await screen.findByText("主代理。我现在开始按照")).toBeInTheDocument();
    expect(screen.getAllByText("主代理。我现在开始按照")).toHaveLength(1);
    fireEvent.click(screen.getByRole("tab", { name: /timeline 2/i }));
    expect(await screen.findByText("主代理。我现在开始按照")).toBeInTheDocument();
    expect(screen.getByText("#19–#25")).toBeInTheDocument();
    expect(screen.getByText("Agent response")).toBeInTheDocument();
    expect(screen.queryByText(/assistant_delta/)).not.toBeInTheDocument();
    expect(screen.queryByText(/tool_call_argument_delta/)).not.toBeInTheDocument();
    expect(screen.queryByText(/partial result/)).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("tab", { name: /raw events 7/i }));
    expect(await screen.findByText("Showing latest 7 of 7 loaded durable events.")).toBeInTheDocument();
    expect(screen.getAllByText(/assistant_delta/)).toHaveLength(3);
    expect(screen.getByText(/partial result/)).toBeInTheDocument();
  });

  it("labels the bounded Raw Events window as partial without implying a global total", async () => {
    mocks.runEvents = Array.from({ length: 201 }, (_, index) => ({
      id: `raw-window-${index + 1}`,
      run_id: "run-1",
      sequence: index + 1,
      event_type: "run.status_changed",
      payload: { ordinal: index + 1 },
      created_at: "2026-07-31T02:35:27Z",
    }));
    renderActionRoute("/runs/run-1");

    fireEvent.click(screen.getByRole("tab", { name: /raw events 201/i }));
    expect(
      await screen.findByText(/Showing latest 200 of 201 loaded durable events\./),
    ).toHaveTextContent(
      "This Raw Events window is partial; older loaded events are hidden.",
    );
  });

  it("opens in conversation and waits for a specific first instruction", async () => {
    mocks.runStatus = "created";
    mocks.runStartedAt = null;
    mocks.runEvents = [];
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

    expect(screen.getByRole("tab", { name: "Conversation" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    const context = screen.getByRole("region", {
      name: "Objective and authorized boundary",
    });
    expect(within(context).getByText("Inspect local service")).toBeInTheDocument();
    expect(within(context).getByText("ip=127.0.0.1")).toBeInTheDocument();
    expect(within(context).getByText("127.0.0.1")).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: /timeline 1/i })).toHaveAttribute(
      "aria-selected",
      "false",
    );
    expect(screen.getByText("Waiting for your first instruction")).toBeInTheDocument();
    expect(screen.getByPlaceholderText("Tell the Agent what to do first…")).toBeInTheDocument();
    expect(screen.getByText("Not started")).toBeInTheDocument();
    expect(screen.queryByText("workflow-run-1")).not.toBeInTheDocument();
    expect(screen.queryByText("Timeline is empty")).not.toBeInTheDocument();
  });

  it("opens a URL Action deep link without stealing focus and closes to the Actions tab", async () => {
    const item = actionListItem();
    mocks.actionItems = [item];
    mocks.actionDetails.set("run-1:action-1", actionDetail(item));
    renderActionRoute("/runs/run-1?action=action-1", { history: true });

    expect(screen.getByRole("tab", { name: /actions 1/i })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    expect(await screen.findByText("Approved within scope")).toBeInTheDocument();
    const close = screen.getByRole("button", { name: "Close Context Inspector" });
    expect(close).not.toHaveFocus();
    expect(screen.getByLabelText("Current location")).toHaveTextContent(
      "/runs/run-1?action=action-1",
    );
    expect(window.sessionStorage.length).toBe(0);

    fireEvent.click(close);
    await waitFor(() =>
      expect(screen.getByLabelText("Current location")).toHaveTextContent("/runs/run-1"),
    );
    await waitFor(() =>
      expect(screen.getByRole("tab", { name: /actions 1/i })).toHaveFocus(),
    );
  });

  it("tracks Action history and restores the trigger for the current historical selection", async () => {
    const first = actionListItem("action-1");
    const second = actionListItem("action-2", "run-1", { tool_id: "curl" });
    mocks.actionItems = [first, second];
    mocks.actionDetails.set("run-1:action-1", actionDetail(first));
    mocks.actionDetails.set("run-1:action-2", actionDetail(second));
    renderActionRoute("/runs/run-1", { history: true });

    fireEvent.click(screen.getByRole("tab", { name: /actions 2/i }));
    const firstTrigger = screen.getByRole("button", { name: "Inspect action nmap" });
    fireEvent.click(firstTrigger);
    await waitFor(() =>
      expect(screen.getByLabelText("Current location")).toHaveTextContent(
        "/runs/run-1?action=action-1",
      ),
    );
    fireEvent.click(screen.getByRole("button", { name: "Inspect action curl" }));
    await waitFor(() =>
      expect(screen.getByLabelText("Current location")).toHaveTextContent(
        "/runs/run-1?action=action-2",
      ),
    );

    fireEvent.click(screen.getByRole("button", { name: "History back" }));
    await waitFor(() =>
      expect(screen.getByLabelText("Current location")).toHaveTextContent(
        "/runs/run-1?action=action-1",
      ),
    );
    screen.getByRole("tab", { name: "Overview" }).focus();
    fireEvent.keyDown(document, { key: "Escape" });
    await waitFor(() =>
      expect(screen.getByLabelText("Current location")).toHaveTextContent("/runs/run-1"),
    );
    await waitFor(() => expect(firstTrigger).toHaveFocus());

    fireEvent.click(screen.getByRole("button", { name: "History forward" }));
    await waitFor(() =>
      expect(screen.getByLabelText("Current location")).toHaveTextContent(
        "/runs/run-1?action=action-2",
      ),
    );
    expect(await screen.findByText("step-action-2")).toBeInTheDocument();
  });

  it.each([
    [401, "Action authentication failed"],
    [403, "Action forbidden"],
    [404, "Action not found"],
    [500, "Action service failed"],
  ])("fails closed for Action detail HTTP %s", async (status, message) => {
    const item = actionListItem();
    const stale = actionDetail(item, {
      reason: "CACHED DETAIL SECRET",
      approval: {
        ...actionDetail(item).approval!,
        actor: "CACHED SECRET ACTOR",
      },
    });
    mocks.actionItems = [item];
    mocks.actionDetails.set("run-1:action-1", stale);
    mocks.actionDetailError = new RiftXAPIError(status, "action_detail_failed", message);
    renderActionRoute("/runs/run-1?action=action-1");

    expect(await screen.findAllByText(message)).not.toHaveLength(0);
    expect(screen.queryByText("CACHED DETAIL SECRET")).not.toBeInTheDocument();
    expect(screen.queryByText("CACHED SECRET ACTOR")).not.toBeInTheDocument();
    expect(document.body).not.toHaveTextContent("[REDACTED]");
  });

  it.each([
    [401, "button"],
    [401, "Escape"],
    [403, "button"],
    [403, "Escape"],
  ] as const)(
    "keeps cached Actions hidden after detail HTTP %s and Inspector close via %s",
    async (status, closeMethod) => {
      const item = actionListItem("action-secret", "run-1", {
        reason: "DETAIL AUTH CACHED LIST SECRET",
      });
      mocks.actionItems = [item];
      mocks.actionDetails.set("run-1:action-secret", actionDetail(item));
      mocks.actionDetailError = new RiftXAPIError(
        status,
        "action_detail_authorization_failed",
        "Action detail authorization failed",
      );
      renderActionRoute("/runs/run-1?action=action-secret");

      expect(
        await screen.findAllByText("Action detail authorization failed"),
      ).not.toHaveLength(0);
      expect(screen.getByRole("tab", { name: /actions 0/i })).toBeInTheDocument();
      expect(screen.queryByText("DETAIL AUTH CACHED LIST SECRET")).not.toBeInTheDocument();

      if (closeMethod === "button") {
        fireEvent.click(
          screen.getByRole("button", { name: "Close Context Inspector" }),
        );
      } else {
        fireEvent.keyDown(document, { key: "Escape" });
      }

      await waitFor(() =>
        expect(
          screen.queryByRole("region", { name: "Context Inspector" }),
        ).not.toBeInTheDocument(),
      );
      expect(screen.getByRole("tab", { name: /actions 0/i })).toBeInTheDocument();
      expect(screen.queryByText("DETAIL AUTH CACHED LIST SECRET")).not.toBeInTheDocument();

      mocks.actionListDataUpdatedAt += 1;
      fireEvent.click(screen.getByRole("tab", { name: "Overview" }));
      await waitFor(() =>
        expect(screen.getByRole("tab", { name: /actions 1/i })).toBeInTheDocument(),
      );
      fireEvent.click(screen.getByRole("tab", { name: /actions 1/i }));
      expect(screen.getByText("DETAIL AUTH CACHED LIST SECRET")).toBeInTheDocument();
    },
  );

  it("hides cached Action list and Inspector data after fatal SSE authorization loss", async () => {
    const item = actionListItem("action-secret", "run-1", {
      reason: "SSE CACHED LIST SECRET",
    });
    mocks.actionItems = [item];
    mocks.actionDetails.set(
      "run-1:action-secret",
      actionDetail(item, {
        approval: { ...actionDetail(item).approval!, actor: "SSE CACHED ACTOR SECRET" },
      }),
    );
    mocks.eventStreamError = new RiftXAPIError(
      403,
      "event_stream_authorization_failed",
      "Run event stream access was denied",
    );
    renderActionRoute("/runs/run-1?action=action-secret");

    expect(await screen.findAllByText("Run event stream access was denied")).not.toHaveLength(0);
    expect(screen.queryByText("SSE CACHED LIST SECRET")).not.toBeInTheDocument();
    expect(screen.queryByText("SSE CACHED ACTOR SECRET")).not.toBeInTheDocument();
    expect(document.body).not.toHaveTextContent("[REDACTED]");
  });

  it("hides cached list and Inspector data when next-page authorization is revoked", async () => {
    const item = actionListItem("action-page-secret", "run-1", {
      reason: "PAGINATION CACHED LIST SECRET",
    });
    mocks.actionItems = [item];
    mocks.actionDetails.set(
      "run-1:action-page-secret",
      actionDetail(item, {
        approval: {
          ...actionDetail(item).approval!,
          actor: "PAGINATION CACHED ACTOR SECRET",
        },
      }),
    );
    mocks.actionFetchNextPageError = true;
    mocks.actionListError = new RiftXAPIError(
      403,
      "local_operator_capability_denied",
      "Action pagination forbidden",
    );
    renderActionRoute("/runs/run-1?action=action-page-secret");

    expect(await screen.findAllByText("Action pagination forbidden")).not.toHaveLength(0);
    expect(screen.getByRole("tab", { name: /actions 0/i })).toBeInTheDocument();
    expect(screen.queryByRole("tab", { name: /actions 1/i })).not.toBeInTheDocument();
    expect(screen.queryByText("PAGINATION CACHED LIST SECRET")).not.toBeInTheDocument();
    expect(screen.queryByText("PAGINATION CACHED ACTOR SECRET")).not.toBeInTheDocument();
    expect(document.body).not.toHaveTextContent("[REDACTED]");
  });

  it("keeps cards only for a next-page failure and hides them for a root authorization error", () => {
    const item = actionListItem();
    mocks.actionItems = [item];
    mocks.actionHasNextPage = true;
    mocks.actionFetchNextPageError = true;
    mocks.actionListError = new RiftXAPIError(500, "next_page_failed", "Next page failed");
    const rendered = renderActionRoute("/runs/run-1");
    fireEvent.click(screen.getByRole("tab", { name: /actions 1/i }));

    expect(screen.getByText(item.reason)).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("Next page failed");
    expect(screen.getByRole("button", { name: "Load more actions" })).toBeEnabled();

    mocks.actionFetchNextPageError = false;
    mocks.actionListError = new RiftXAPIError(403, "actions_forbidden", "Actions forbidden");
    rendered.rerender(
      <QueryClientProvider client={new QueryClient()}>
        <MemoryRouter initialEntries={["/runs/run-1"]}>
          <Routes>
            <Route path="/runs/:runId" element={<RunDetailPage />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    );
    expect(screen.getByRole("tab", { name: /actions 0/i })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    expect(screen.getByRole("alert")).toHaveTextContent("Actions forbidden");
    expect(screen.queryByText(item.reason)).not.toBeInTheDocument();
  });

  it("clears a selected Action immediately when switching Runs", async () => {
    const item = actionListItem();
    mocks.actionItemsByRun.set("run-1", [item]);
    mocks.actionItemsByRun.set("run-2", []);
    mocks.actionDetails.set(
      "run-1:action-1",
      actionDetail(item, { reason: "OLD RUN DETAIL SECRET" }),
    );
    const queryClient = new QueryClient();
    render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={["/runs/run-1?action=action-1"]}>
          <RunNavigationHarness />
        </MemoryRouter>
      </QueryClientProvider>,
    );
    expect(await screen.findByText("OLD RUN DETAIL SECRET")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("link", { name: "Open Run 2" }));
    expect(screen.queryByText("OLD RUN DETAIL SECRET")).not.toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "Context Inspector" })).not.toBeInTheDocument();
  });

  it("does not load or request the lazy Traffic workspace before its tab is active", () => {
    const contracts = trafficContracts();
    const list = vi
      .spyOn(api, "listRunTargetHttpExchanges")
      .mockResolvedValue(contracts.page);
    const get = vi
      .spyOn(api, "getRunTargetHttpExchange")
      .mockResolvedValue(contracts.detail);

    renderActionRoute("/runs/run-1");

    expect(mocks.trafficWorkspaceModuleLoads).toBe(0);
    expect(list).not.toHaveBeenCalled();
    expect(get).not.toHaveBeenCalled();
    expect(screen.queryByRole("heading", { name: "Target HTTP traffic" })).not.toBeInTheDocument();
  });

  it("opens a direct Traffic Inspector URL and requests the exact Run-scoped Exchange", async () => {
    const contracts = trafficContracts();
    const list = vi
      .spyOn(api, "listRunTargetHttpExchanges")
      .mockResolvedValue(contracts.page);
    const get = vi
      .spyOn(api, "getRunTargetHttpExchange")
      .mockResolvedValue(contracts.detail);

    renderActionRoute(
      "/runs/run-1?traffic_view=inspector&traffic_exchange=exchange-1",
      { history: true },
    );

    expect(screen.getByRole("tab", { name: "Traffic" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    const inspector = await screen.findByRole("article", {
      name: "Selected Exchange metadata",
    });
    expect(within(inspector).getByText("https://run-one.example.test /…")).toBeInTheDocument();
    expect(list).toHaveBeenCalledWith(
      "run-1",
      { cursor: undefined, limit: 50 },
      expect.any(AbortSignal),
    );
    expect(get).toHaveBeenCalledWith(
      "run-1",
      "exchange-1",
      expect.any(AbortSignal),
    );
  });

  it("does not issue a detail request for an invalid Traffic Exchange URL identity", async () => {
    const contracts = trafficContracts();
    const list = vi
      .spyOn(api, "listRunTargetHttpExchanges")
      .mockResolvedValue(contracts.page);
    const get = vi
      .spyOn(api, "getRunTargetHttpExchange")
      .mockResolvedValue(contracts.detail);

    renderActionRoute(
      "/runs/run-1?traffic_view=inspector&traffic_exchange=%20bad%0Aidentity%20",
    );

    expect(await screen.findByText("Invalid Exchange identity")).toBeInTheDocument();
    expect(list).toHaveBeenCalled();
    expect(get).not.toHaveBeenCalled();
  });

  it("roundtrips Action to Traffic with Back/Forward focus on the active surface", async () => {
    const item = actionListItem("action-traffic");
    mocks.actionItems = [item];
    mocks.actionDetails.set("run-1:action-traffic", actionDetail(item));
    const contracts = trafficContracts();
    vi.spyOn(api, "listRunTargetHttpExchanges").mockResolvedValue(contracts.page);
    vi.spyOn(api, "getRunTargetHttpExchange").mockResolvedValue(contracts.detail);
    renderActionRoute("/runs/run-1?action=action-traffic", { history: true });

    expect(await screen.findByRole("region", { name: "Context Inspector" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("tab", { name: "Traffic" }));

    expect(screen.getByLabelText("Current location")).toHaveTextContent(
      "/runs/run-1?traffic_view=history",
    );
    expect(await screen.findByRole("heading", { name: "Target HTTP traffic" })).toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole("tab", { name: "Traffic" })).toHaveFocus());

    fireEvent.click(screen.getByRole("button", { name: "History back" }));

    expect(await screen.findByRole("region", { name: "Context Inspector" })).toBeInTheDocument();
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Close Context Inspector" })).toHaveFocus(),
    );

    fireEvent.click(screen.getByRole("button", { name: "History forward" }));

    expect(await screen.findByRole("heading", { name: "Target HTTP traffic" })).toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole("tab", { name: "Traffic" })).toHaveFocus());
  });

  it("drops Traffic immediately on a plain Run switch and never requests the new Run", async () => {
    const runOne = trafficContracts(
      "run-1",
      "engagement-1",
      "exchange-run-1",
      "https://run-one-only.example.test",
    );
    const list = vi
      .spyOn(api, "listRunTargetHttpExchanges")
      .mockResolvedValue(runOne.page);
    const get = vi
      .spyOn(api, "getRunTargetHttpExchange")
      .mockResolvedValue(runOne.detail);
    render(
      <QueryClientProvider client={new QueryClient()}>
        <MemoryRouter initialEntries={["/runs/run-1?traffic_view=history"]}>
          <RunNavigationHarness />
        </MemoryRouter>
      </QueryClientProvider>,
    );

    expect(await screen.findByText("https://run-one-only.example.test /…")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("link", { name: "Open Run 2" }));

    expect(screen.queryByText("https://run-one-only.example.test /…")).not.toBeInTheDocument();
    await waitFor(() =>
      expect(screen.getByRole("tab", { name: "Conversation" })).toHaveAttribute(
        "aria-selected",
        "true",
      ),
    );
    expect(list.mock.calls.some(([candidateRunId]) => candidateRunId === "run-2")).toBe(false);
    expect(get).not.toHaveBeenCalled();
  });

  it("loads and requests the Graph workspace only after its Run detail tab is activated", async () => {
    const listRunGraph = vi.spyOn(
      api as typeof api & {
        listRunGraph: (...args: unknown[]) => Promise<unknown>;
      },
      "listRunGraph",
    ).mockResolvedValue({
      scope: { engagement_id: "engagement-1", run_id: "run-1" },
      view: "task",
      snapshot: { id: "snapshot-1", stale: false },
      nodes: [],
      edges: [],
      type_metadata: [],
      partial_reasons: [],
      truncated: false,
      has_more: false,
      next_cursor: null,
    });

    renderActionRoute("/runs/run-1");

    expect(listRunGraph).not.toHaveBeenCalled();
    expect(screen.queryByRole("heading", { name: "Run Graph" })).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("tab", { name: "Graph" }));

    expect(await screen.findByRole("heading", { name: "Run Graph" })).toBeInTheDocument();
    expect(listRunGraph).toHaveBeenCalledTimes(1);
    expect(listRunGraph).toHaveBeenCalledWith(
      "run-1",
      expect.objectContaining({ view: "task" }),
      expect.anything(),
    );
  });

  it("restores the default tab on a plain Run switch without requesting that Run's Graph", async () => {
    const listRunGraph = vi.spyOn(api, "listRunGraph").mockResolvedValue({
      scope: { engagement_id: "engagement-1", run_id: "run-1" },
      view: "task",
      snapshot: { id: "snapshot-1", stale: false },
      nodes: [],
      edges: [],
      type_metadata: [],
      partial_reasons: [],
      truncated: false,
      has_more: false,
      next_cursor: null,
    });
    const queryClient = new QueryClient();
    render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={["/runs/run-1?graph_view=task"]}>
          <RunNavigationHarness />
        </MemoryRouter>
      </QueryClientProvider>,
    );
    expect(await screen.findByRole("heading", { name: "Run Graph" })).toBeInTheDocument();
    await waitFor(() => expect(listRunGraph).toHaveBeenCalledTimes(1));

    fireEvent.click(screen.getByRole("link", { name: "Open Run 2" }));

    await waitFor(() =>
      expect(screen.getByRole("tab", { name: "Conversation" })).toHaveAttribute(
        "aria-selected",
        "true",
      ),
    );
    expect(screen.queryByRole("heading", { name: "Run Graph" })).not.toBeInTheDocument();
    expect(
      listRunGraph.mock.calls.some(([candidateRunId]) => candidateRunId === "run-2"),
    ).toBe(false);
  });

  it("roundtrips Graph to Action to the exact server ref with Back/Forward focus", async () => {
    const actionId = "intent-1";
    const nodeId = `action:run-1:${actionId}`;
    const item = actionListItem(actionId);
    mocks.actionItems = [item];
    mocks.actionDetails.set(`run-1:${actionId}`, actionDetail(item));
    const listRunGraph = vi
      .spyOn(api, "listRunGraph")
      .mockResolvedValue(graphPageForAction("run-1", "engagement-1", actionId));
    const user = userEvent.setup();
    renderActionRoute(
      `/runs/run-1?graph_view=task&graph_focus=${encodeURIComponent(nodeId)}`,
      { history: true },
    );

    const graphList = await screen.findByRole("region", { name: "Complete Graph list" });
    expect(
      within(graphList).getByRole("button", { name: /^Inspect Action intent-1/ }),
    ).toHaveAttribute("aria-current", "true");
    fireEvent.click(
      within(graphList).getByRole("button", { name: `Open Action ${actionId}` }),
    );

    expect(screen.getByLabelText("Current location")).toHaveTextContent(
      `/runs/run-1?action=${actionId}`,
    );
    const inspector = await screen.findByRole("region", { name: "Context Inspector" });
    await waitFor(() =>
      expect(
        within(inspector).getByRole("button", { name: "Close Context Inspector" }),
      ).toHaveFocus(),
    );
    const openGraph = within(inspector).getByRole("button", { name: "Open in Graph" });
    openGraph.focus();
    await user.keyboard("{Enter}");

    expect(screen.getByLabelText("Current location")).toHaveTextContent(
      `/runs/run-1?graph_view=task&graph_focus=${encodeURIComponent(nodeId)}`,
    );
    expect(await screen.findByRole("heading", { name: "Run Graph" })).toBeInTheDocument();
    await waitFor(() =>
      expect(screen.getByRole("tab", { name: "Graph" })).toHaveFocus(),
    );
    await waitFor(() =>
      expect(listRunGraph).toHaveBeenLastCalledWith(
        "run-1",
        expect.objectContaining({ focus: nodeId, view: "task" }),
        expect.anything(),
      ),
    );

    fireEvent.click(screen.getByRole("button", { name: "History back" }));

    expect(await screen.findByRole("region", { name: "Context Inspector" })).toBeInTheDocument();
    expect(screen.getByLabelText("Current location")).toHaveTextContent(
      `/runs/run-1?action=${actionId}`,
    );
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "Close Context Inspector" }),
      ).toHaveFocus(),
    );

    fireEvent.click(screen.getByRole("button", { name: "History forward" }));

    expect(await screen.findByRole("heading", { name: "Run Graph" })).toBeInTheDocument();
    expect(screen.getByLabelText("Current location")).toHaveTextContent(
      `/runs/run-1?graph_view=task&graph_focus=${encodeURIComponent(nodeId)}`,
    );
    await waitFor(() =>
      expect(screen.getByRole("tab", { name: "Graph" })).toHaveFocus(),
    );
  });

  it("cancels stale Graph focus restoration during a rapid browser Back", async () => {
    const item = actionListItem("intent-race");
    mocks.actionItems = [item];
    mocks.actionDetails.set("run-1:intent-race", actionDetail(item));
    vi.spyOn(api, "listRunGraph").mockResolvedValue(
      graphPageForAction("run-1", "engagement-1", "intent-race"),
    );
    renderActionRoute("/runs/run-1?action=intent-race", { history: true });

    const openGraph = await screen.findByRole("button", { name: "Open in Graph" });
    fireEvent.click(openGraph);
    expect(screen.getByLabelText("Current location")).toHaveTextContent(
      "/runs/run-1?graph_view=task&graph_focus=action%3Arun-1%3Aintent-race",
    );
    fireEvent.click(screen.getByRole("button", { name: "History back" }));

    const close = await screen.findByRole("button", { name: "Close Context Inspector" });
    await waitFor(() => expect(close).toHaveFocus());
    await act(async () => {
      await new Promise((resolve) => window.setTimeout(resolve, 0));
    });
    expect(close).toHaveFocus();
  });

  it("clears an Action focus when switching to another Graph semantic view", async () => {
    const item = actionListItem("intent-view");
    mocks.actionItems = [item];
    mocks.actionDetails.set("run-1:intent-view", actionDetail(item));
    const listRunGraph = vi.spyOn(api, "listRunGraph").mockImplementation(
      (_runId, options) =>
        Promise.resolve(
          options.view === "task"
            ? graphPageForAction("run-1", "engagement-1", "intent-view")
            : {
                ...graphPageForAction("run-1", "engagement-1", "intent-view"),
                view: options.view,
                nodes: [],
              },
        ),
    );
    renderActionRoute("/runs/run-1?action=intent-view", { history: true });

    fireEvent.click(await screen.findByRole("button", { name: "Open in Graph" }));
    expect(await screen.findByRole("heading", { name: "Run Graph" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("tab", { name: "Evidence" }));

    await waitFor(() =>
      expect(screen.getByLabelText("Current location")).toHaveTextContent(
        "/runs/run-1?graph_view=evidence",
      ),
    );
    expect(screen.getByLabelText("Current location")).not.toHaveTextContent("graph_focus");
    await waitFor(() =>
      expect(listRunGraph).toHaveBeenLastCalledWith(
        "run-1",
        expect.objectContaining({ focus: undefined, view: "evidence" }),
        expect.anything(),
      ),
    );
  });

  it("clears Graph route state when the pending-approval alert opens Approvals", async () => {
    vi.spyOn(api, "listRunGraph").mockResolvedValue(
      graphPageForAction("run-1", "engagement-1", "intent-approval"),
    );
    renderActionRoute(
      "/runs/run-1?graph_view=task&graph_focus=action%3Arun-1%3Aintent-approval",
      { history: true },
    );

    expect(await screen.findByRole("heading", { name: "Run Graph" })).toBeInTheDocument();
    fireEvent.click(
      screen.getByRole("button", { name: /tool call awaiting approval/i }),
    );

    expect(screen.getByRole("tab", { name: /Approvals 1/i })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    await waitFor(() =>
      expect(screen.getByLabelText("Current location")).toHaveTextContent("/runs/run-1"),
    );
    expect(screen.getByLabelText("Current location")).not.toHaveTextContent("graph_");
  });

  it("keeps the same raw Action ID isolated by Run when opening its server Graph ref", async () => {
    const run1Item = actionListItem("action-shared", "run-1");
    const run2Item = actionListItem("action-shared", "run-2");
    mocks.actionItemsByRun.set("run-1", [run1Item]);
    mocks.actionItemsByRun.set("run-2", [run2Item]);
    mocks.actionDetails.set("run-1:action-shared", actionDetail(run1Item));
    mocks.actionDetails.set("run-2:action-shared", actionDetail(run2Item));
    vi.spyOn(api, "listRunGraph").mockImplementation((runId) =>
      Promise.resolve(
        graphPageForAction(
          runId,
          runId === "run-1" ? "engagement-1" : "engagement-2",
          "action-shared",
        ),
      ),
    );
    const queryClient = new QueryClient();
    render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={["/runs/run-1?action=action-shared"]}>
          <SameRawActionRunHarness />
        </MemoryRouter>
      </QueryClientProvider>,
    );

    expect(await screen.findByRole("button", { name: "Open in Graph" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("link", { name: "Open same Action in Run 2" }));
    const run2Inspector = await screen.findByRole("region", { name: "Context Inspector" });
    fireEvent.click(within(run2Inspector).getByRole("button", { name: "Open in Graph" }));

    expect(screen.getByLabelText("Current location")).toHaveTextContent(
      `/runs/run-2?graph_view=task&graph_focus=${encodeURIComponent("action:run-2:action-shared")}`,
    );
    expect(screen.getByLabelText("Current location")).not.toHaveTextContent(
      "action%3Arun-1%3Aaction-shared",
    );
  });

  it("keeps a legacy Action without graph_ref unsupported and never invents a reverse link", async () => {
    const item = actionListItem();
    mocks.actionItems = [item];
    mocks.actionDetails.set(
      "run-1:action-1",
      { ...actionDetail(item), graph_ref: undefined } as unknown as RunAction,
    );
    renderActionRoute("/runs/run-1?action=action-1");

    const inspector = await screen.findByRole("region", { name: "Context Inspector" });
    expect(within(inspector).getByText(/Action-to-Graph link unsupported/)).toBeInTheDocument();
    expect(within(inspector).queryByRole("link", { name: /graph/i })).not.toBeInTheDocument();
    expect(within(inspector).queryByRole("button", { name: /graph/i })).not.toBeInTheDocument();
  });

  it("implements roving tabs whose controls always reference the live tabpanel", () => {
    renderActionRoute("/runs/run-1");
    const tabs = screen.getAllByRole("tab");
    for (const candidate of tabs) {
      expect(document.getElementById(candidate.getAttribute("aria-controls")!)).not.toBeNull();
    }
    const conversation = screen.getByRole("tab", { name: "Conversation" });
    conversation.focus();
    fireEvent.keyDown(conversation, { key: "ArrowRight" });
    const actionsTab = screen.getByRole("tab", { name: /actions 0/i });
    expect(actionsTab).toHaveFocus();
    expect(actionsTab).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("tabpanel")).toHaveAttribute(
      "aria-labelledby",
      actionsTab.id,
    );
    fireEvent.keyDown(actionsTab, { key: "Home" });
    expect(screen.getByRole("tab", { name: "Overview" })).toHaveFocus();
    fireEvent.keyDown(screen.getByRole("tab", { name: "Overview" }), { key: "End" });
    expect(screen.getByRole("tab", { name: /reports 1/i })).toHaveFocus();
  });

  it("renders the Action deep-link main path and terminal failure semantics in Chinese", async () => {
    window.localStorage.setItem(languageStorageKey, "zh-CN");
    const item = actionListItem("action-zh", "run-1", {
      lifecycle: "failed",
      attempts: [
        {
          ...actionListItem().attempts[0]!,
          execution_id: "execution-zh-timeout",
          status: "hard_timeout",
        },
      ],
      current_execution_id: "execution-zh-timeout",
      latest_execution_id: "execution-zh-timeout",
      current_execution_status: "hard_timeout",
      latest_execution_status: "hard_timeout",
    });
    mocks.actionItems = [item];
    mocks.actionDetails.set("run-1:action-zh", actionDetail(item));
    renderActionRoute("/runs/run-1?action=action-zh", { language: true });

    expect(screen.getByRole("tab", { name: /操作 1/ })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    expect(await screen.findByRole("region", { name: "上下文检查器" })).toBeInTheDocument();
    expect(screen.getAllByText("失败").length).toBeGreaterThan(0);
    expect(screen.getByText("强制超时")).toBeInTheDocument();
    expect(screen.getByText("会话")).toBeInTheDocument();
    expect(screen.getByText("轮次")).toBeInTheDocument();
    expect(screen.getByText("步骤")).toBeInTheDocument();
  });

  it("mutates the mounted live-region text for every Action revision batch", async () => {
    renderActionRoute("/runs/run-1");
    expect(screen.getByRole("status")).toHaveTextContent("");

    mocks.actionUpdateRevision = 1;
    fireEvent.click(screen.getByRole("tab", { name: "Overview" }));
    const firstAnnouncement = await screen.findByRole("status");
    await waitFor(() =>
      expect(firstAnnouncement).toHaveTextContent(
        "Action data updated. Live revision 1.",
      ),
    );
    expect(firstAnnouncement).toHaveAttribute("aria-atomic", "true");

    mocks.actionUpdateRevision = 2;
    fireEvent.click(screen.getByRole("tab", { name: "Conversation" }));
    const secondAnnouncement = await screen.findByRole("status");
    expect(secondAnnouncement).toHaveTextContent(
      "Action data updated. Live revision 2.",
    );
    expect(secondAnnouncement).toBe(firstAnnouncement);
  });

});
