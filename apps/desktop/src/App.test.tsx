import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import App from "./App";
import {
  activeTurns,
  autoStatus,
  conversationHistory,
  daemonInfo,
  engagementReport,
  engagementStreamStatus,
  listApprovals,
  listAssessmentCredentials,
  listCredentialGrants,
  listEngagements,
  llmProfiles,
} from "./bridge";
import type {
  AutoRun,
  DaemonControlStatus,
  Engagement,
  EngagementReport,
  PendingApproval,
} from "./models";

const runtimeListeners = vi.hoisted(() => ({
  status: null as ((runtime: DaemonControlStatus) => void) | null,
}));

vi.mock("./bridge", () => ({
  activeTurns: vi.fn(),
  autoStatus: vi.fn(),
  bridgeError: (error: unknown) =>
    typeof error === "object" && error !== null && "code" in error
      ? error
      : { code: "desktop_error", message: String(error) },
  conversationHistory: vi.fn(),
  daemonInfo: vi.fn(),
  engagementReport: vi.fn(),
  engagementStreamStatus: vi.fn(),
  listApprovals: vi.fn(),
  listAssessmentCredentials: vi.fn(),
  listCredentialGrants: vi.fn(),
  listEngagements: vi.fn(),
  llmProfiles: vi.fn(),
  onEngagementEvent: vi.fn().mockResolvedValue(() => undefined),
  onEngagementStream: vi.fn().mockResolvedValue(() => undefined),
  onRuntimeStatus: vi.fn().mockImplementation(async (listener) => {
    runtimeListeners.status = listener;
    return () => undefined;
  }),
  onRuntimeError: vi.fn().mockResolvedValue(() => undefined),
  prepareSettingsReload: vi.fn(),
  settingsReloadImpact: vi.fn(),
}));

const runningRuntime: DaemonControlStatus = {
  state: "running",
  reason: null,
  updatedAt: 1,
  audit: { state: "healthy", message: null, updatedAt: 1 },
};

const engagement: Engagement = {
  id: "engagement-a",
  name: "Authorized lab",
  status: "active",
  objective: {
    summary: "Assess the authorized lab",
    successCriteria: ["Confirm exposure"],
    structuredCriteria: [],
  },
  entryPoints: ["lab.example.test"],
  mode: "auto",
  llmProfile: "default",
  autoLimits: {
    maxTurns: 10,
    maxToolCalls: 20,
    maxWallClockSeconds: 3600,
    maxSingleCommandSeconds: 60,
    maxConsecutiveFailures: 3,
    noProgressWindow: 3,
    maxModelTokensOrCost: null,
  },
  authorization: {
    network: {
      cidrs: [],
      domains: ["lab.example.test"],
      ports: [443],
    },
    identities: [],
    capabilities: [],
    environment: "lab",
    window: { startsAt: null, expiresAt: 4_000_000_000 },
  },
  policyRevision: "policy-a",
  threadId: "thread-a",
  createdAt: 1,
  updatedAt: 1,
};

const report: EngagementReport = {
  engagement,
  assets: [],
  services: [],
  observations: [],
  hypotheses: [],
  executions: [],
  findings: [],
  evidence: [],
  attackPaths: [],
  coverage: [],
  tasks: [],
  artifacts: [],
  approvals: [],
  toolSnapshot: { snapshotSha256: "tools", tools: [] },
  skillSnapshot: { snapshotSha256: "skills", skills: [] },
};

function autoRun(
  state: AutoRun["state"],
  currentSubgoal: string,
): AutoRun {
  return {
    engagementId: engagement.id,
    config: {
      objective: engagement.objective,
      expiresAt: 4_000_000_000,
      limits: engagement.autoLimits!,
    },
    state,
    stopReason: null,
    currentSubgoal,
    turnsStarted: 1,
    turnsCompleted: 0,
    toolCalls: 0,
    consecutiveFailures: 0,
    noProgressTurns: 0,
    lastGoalAssessment: null,
    lastProgressAssessment: null,
    startedAt: 1,
    updatedAt: 1,
  };
}

const pendingApproval: PendingApproval = {
  id: "approval-a",
  engagementId: engagement.id,
  policyRevision: "policy-a",
  kind: "command",
  requestedAt: 1,
  command: "nmap -sV lab.example.test",
  cwd: "/tmp/riftx",
  reason: "Recovered approval after daemon restart",
  executionIntent: null,
};

describe("App runtime readiness and recovery", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    runtimeListeners.status = null;
    vi.mocked(daemonInfo).mockResolvedValue({
      protocolVersion: 13,
      daemonVersion: "0.8.0",
      configPath: "/tmp/riftx.toml",
      runtime: runningRuntime,
    });
    vi.mocked(listEngagements).mockResolvedValue([]);
    vi.mocked(llmProfiles).mockResolvedValue({
      defaultProfile: "default",
      profiles: [
        {
          name: "default",
          protocol: "responses",
          model: "gpt-test",
          baseUrl: "https://api.example.test/v1",
          isDefault: true,
          state: "unreachable",
          stateDetail: "Connection test could not reach the provider.",
          configured: true,
          runtimeReady: false,
        },
      ],
    });
    vi.mocked(activeTurns).mockResolvedValue([]);
    vi.mocked(engagementReport).mockResolvedValue(report);
    vi.mocked(listApprovals).mockResolvedValue([]);
    vi.mocked(conversationHistory).mockResolvedValue({
      data: [],
      nextCursor: null,
    });
    vi.mocked(listAssessmentCredentials).mockResolvedValue([]);
    vi.mocked(listCredentialGrants).mockResolvedValue([]);
    vi.mocked(engagementStreamStatus).mockResolvedValue({
      engagementId: engagement.id,
      state: "connected",
      message: null,
    });
    vi.mocked(autoStatus).mockResolvedValue(autoRun("ready", "Initial subgoal"));
  });

  it("shows the authorization warning and blocks task creation", async () => {
    render(<App />);

    expect(await screen.findByText("Finish model setup")).toBeInTheDocument();
    expect(
      screen.getByText(/Use RiftX only on systems you are authorized to test/),
    ).toBeInTheDocument();
    for (const button of screen.getAllByRole("button", { name: "New task" })) {
      expect(button).toBeDisabled();
    }
    expect(screen.getByText("Open settings").closest("button")).toBeEnabled();
  });

  it("redacts daemon transport details and retries the connection", async () => {
    vi.mocked(daemonInfo).mockRejectedValueOnce({
      code: "daemon_unavailable",
      message:
        "connect /private/tmp/riftx.sock failed; Authorization: Bearer secret",
    });

    render(<App />);

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Daemon connection lost");
    expect(alert).toHaveTextContent(
      "RiftX cannot reach its local daemon",
    );
    expect(alert).not.toHaveTextContent("/private/tmp/riftx.sock");
    expect(alert).not.toHaveTextContent("Bearer secret");

    fireEvent.click(
      screen.getByRole("button", { name: "Retry connection" }),
    );

    expect(await screen.findByText("Running")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(daemonInfo).toHaveBeenCalledTimes(2);
  });

  it("keeps the daemon online when profile discovery fails", async () => {
    vi.mocked(llmProfiles).mockRejectedValue({
      code: "profile_status_unavailable",
      message: "Could not load model Profile status.",
    });

    render(<App />);

    expect(await screen.findByText("Running")).toBeInTheDocument();
    expect(screen.queryByText("Daemon offline")).not.toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent(
      "Could not load model Profile status.",
    );
    for (const button of screen.getAllByRole("button", { name: "New task" })) {
      expect(button).toBeDisabled();
    }
  });

  it("reconciles persisted runtime, turn, approval, and Auto state after reconnect", async () => {
    vi.mocked(listEngagements).mockResolvedValue([engagement]);
    vi.mocked(llmProfiles).mockResolvedValue({
      defaultProfile: "default",
      profiles: [
        {
          name: "default",
          protocol: "responses",
          model: "gpt-test",
          baseUrl: "https://api.example.test/v1",
          isDefault: true,
          state: "in_use",
          stateDetail: "Profile is active.",
          configured: true,
          runtimeReady: true,
        },
      ],
    });

    render(<App />);

    expect(await screen.findByText("Initial subgoal")).toBeInTheDocument();
    await waitFor(() => expect(runtimeListeners.status).not.toBeNull());

    const killedRuntime: DaemonControlStatus = {
      ...runningRuntime,
      state: "paused",
      reason: "killSwitch",
      updatedAt: 2,
    };
    vi.mocked(daemonInfo).mockResolvedValue({
      protocolVersion: 13,
      daemonVersion: "1.0.0",
      configPath: "/tmp/riftx.toml",
      runtime: killedRuntime,
    });
    vi.mocked(listApprovals).mockResolvedValue([pendingApproval]);
    vi.mocked(conversationHistory).mockResolvedValue({
      data: [
        {
          sequence: 1,
          id: "message-a",
          engagementId: engagement.id,
          turnId: "turn-a",
          role: "agent",
          kind: "message",
          text: "Recovered conversation after reconnect",
          createdAt: 2,
        },
      ],
      nextCursor: null,
    });
    vi.mocked(autoStatus).mockResolvedValue(
      autoRun("needsInput", "Confirm the recovered target scope"),
    );
    vi.mocked(activeTurns).mockResolvedValue([
      { engagementId: engagement.id, profileName: "default" },
    ]);

    await act(async () => {
      runtimeListeners.status?.(killedRuntime);
    });

    expect(await screen.findByText("Kill Switch")).toBeInTheDocument();
    expect(
      await screen.findByText("Confirm the recovered target scope"),
    ).toBeInTheDocument();
    expect(
      await screen.findByText("Recovered conversation after reconnect"),
    ).toBeInTheDocument();
    expect(
      await screen.findByText("nmap -sV lab.example.test"),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Interrupt execution" }),
    ).toBeInTheDocument();
    expect(screen.getByText("live")).toBeInTheDocument();
  });
});
