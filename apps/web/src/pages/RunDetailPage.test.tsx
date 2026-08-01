import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { useLayoutEffect } from "react";
import { Link, MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { api, RiftXAPIError } from "../api/client";
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

  it("keeps plan updates out of Conversation and separates host tool-call provenance", async () => {
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
    fireEvent.click(screen.getByRole("tab", { name: /tool calls 1/i }));
    expect(await screen.findByText("nmap -sV 127.0.0.1")).toBeInTheDocument();
    expect(screen.getByText("7.95")).toBeInTheDocument();
    expect(screen.getByText("/usr/bin/nmap")).toBeInTheDocument();
    expect(screen.getByText("Stop confirmed")).toHaveClass("confirmed");
  });

  it("makes a terminal execution without durable stop proof visibly unsafe", async () => {
    mocks.executionStatus = "cancelled";
    mocks.physicalStopConfirmedAt = null;
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

    fireEvent.click(screen.getByRole("tab", { name: /tool calls 1/i }));
    expect(await screen.findByText("Stop unconfirmed")).toHaveClass("unconfirmed");
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
    expect(await screen.findByText("Showing the latest 7 of 7 durable events.")).toBeInTheDocument();
    expect(screen.getAllByText(/assistant_delta/)).toHaveLength(3);
    expect(screen.getByText(/partial result/)).toBeInTheDocument();
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

});
