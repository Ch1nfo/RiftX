import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createRef } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api, RiftXAPIError } from "../api/client";
import type { RunAction, RunActionListItem } from "../api/types";
import { LanguageProvider, languageStorageKey } from "../i18n";
import { ActionInspector, ActionTimeline } from "./RunActionTimeline";

const listItem: RunActionListItem = {
  action_id: "action-1",
  run_id: "run-1",
  session_id: "session-a",
  cycle_id: "cycle-shared",
  step_id: "step-7",
  engine_call_id: "provider-call-1",
  tool_id: "nmap",
  skill_id: null,
  reason: "Identify the authorized service before testing it.",
  target_summary: "example.test:443",
  approval_level: "sensitive",
  approval_id: "approval-1",
  approval_status: "approved",
  approval_actor: "local-principal:v1:operator",
  approval_decided_at: "2026-08-02T09:00:01Z",
  approval_correlation_quality: "exact",
  execution_count: 2,
  attempts: [
    {
      execution_id: "execution-current",
      attempt_group: "retry-2",
      node_id: "runner-current",
      status: "running",
      created_at: "2026-08-02T09:00:02Z",
      started_at: "2026-08-02T09:00:03Z",
      finished_at: null,
      exit_code: null,
      correlation_quality: "exact",
      physical_stop_confirmed_at: null,
      stop_confirmation: "not_applicable",
    },
    {
      execution_id: "execution-latest",
      attempt_group: "retry-1",
      node_id: "runner-latest",
      status: "exited",
      created_at: "2026-08-02T08:59:00Z",
      started_at: "2026-08-02T08:59:01Z",
      finished_at: "2026-08-02T08:59:05Z",
      exit_code: 1,
      correlation_quality: "exact",
      physical_stop_confirmed_at: "2026-08-02T08:59:05Z",
      stop_confirmation: "confirmed",
    },
  ],
  attempt_coverage: { scanned: 2, limit: 100, truncated: false },
  latest_execution_id: "execution-latest",
  latest_execution_status: "exited",
  current_execution_id: "execution-current",
  current_execution_status: "running",
  latest_stop_confirmation: "confirmed",
  current_stop_confirmation: "not_applicable",
  attempt_order_quality: "exact",
  artifact_ids: ["artifact-1"],
  artifact_count: 1,
  artifacts_truncated: false,
  output_size: 0,
  output_available: false,
  finding_count: 2,
  event_count: 4,
  finding_coverage: { scanned: 2, limit: 100, truncated: false },
  event_coverage: { scanned: 4, limit: 200, truncated: false },
  lifecycle: "executing",
  lifecycle_sources: ["execution.status"],
  correlation_quality: "exact",
  partial_reasons: [],
  created_at: "2026-08-02T09:00:00Z",
  updated_at: "2026-08-02T09:00:03Z",
  version: "version-1",
};

const detail: RunAction = {
  action_id: listItem.action_id,
  run_id: listItem.run_id,
  session_id: listItem.session_id,
  cycle_id: listItem.cycle_id,
  step_id: listItem.step_id,
  engine_call_id: listItem.engine_call_id,
  tool_id: listItem.tool_id,
  skill_id: listItem.skill_id,
  reason: listItem.reason,
  target_summary: listItem.target_summary,
  approval_level: listItem.approval_level,
  arguments_summary: { target: "example.test", token: "[REDACTED]" },
  approval: {
    approval_id: "approval-1",
    status: "approved",
    actor: "local-principal:v1:operator",
    decided_at: "2026-08-02T09:00:01Z",
    feedback_summary: "Proceed within the recorded scope.",
    correlation_quality: "exact",
  },
  executions: [
    {
      ...listItem.attempts[0],
      error_summary: null,
    },
  ],
  execution_count: 1,
  attempt_coverage: { scanned: 1, limit: 100, truncated: false },
  latest_execution_id: "execution-current",
  current_execution_id: "execution-current",
  latest_stop_confirmation: "not_applicable",
  current_stop_confirmation: "not_applicable",
  attempt_order_quality: "exact",
  result: {
    truncated: false,
    artifact_ids: ["artifact-1"],
    artifact_count: 1,
    output_size: 0,
    output_available: false,
  },
  evidence: {
    finding_ids: ["finding-1"],
    artifact_ids: ["artifact-1"],
    events: [
      {
        event_id: "event-9",
        sequence: 9,
        event_type: "agent.tool_started",
        created_at: "2026-08-02T09:00:02Z",
      },
    ],
    finding_count: 1,
    event_count: 1,
    finding_coverage: { scanned: 1, limit: 100, truncated: false },
    event_coverage: { scanned: 1, limit: 200, truncated: false },
  },
  lifecycle: "executing",
  lifecycle_sources: ["execution.status"],
  correlation_quality: "exact",
  partial_reasons: [],
  created_at: listItem.created_at,
  updated_at: listItem.updated_at,
  version: listItem.version,
};

function installLocalStorage() {
  const values = new Map<string, string>();
  Object.defineProperty(window, "localStorage", {
    configurable: true,
    value: {
      get length() { return values.size; },
      clear: () => values.clear(),
      getItem: (key: string) => values.get(key) ?? null,
      key: (index: number) => [...values.keys()][index] ?? null,
      removeItem: (key: string) => values.delete(key),
      setItem: (key: string, value: string) => values.set(key, String(value)),
    } satisfies Storage,
  });
}

beforeEach(() => installLocalStorage());

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  window.localStorage.removeItem(languageStorageKey);
});

describe("ActionTimeline", () => {
  it("answers why, what, approval, result, and evidence from list DTOs only", () => {
    render(
      <ActionTimeline
        items={[listItem]}
        loading={false}
        error={null}
        selectedActionId={null}
        hasMore={false}
        loadingMore={false}
        onLoadMore={vi.fn()}
        onSelect={vi.fn()}
      />,
    );

    expect(screen.getByText(listItem.reason)).toBeInTheDocument();
    expect(screen.getByText("example.test:443")).toBeInTheDocument();
    expect(screen.getByText("approved")).toBeInTheDocument();
    expect(screen.getAllByText("executing")).toHaveLength(2);
    expect(screen.getByText("runner-current")).toBeInTheDocument();
    expect(screen.getByText(/2 findings/i)).toBeInTheDocument();
    expect(screen.getByText(/1 artifact/i)).toBeInTheDocument();
    expect(screen.queryByText("[REDACTED]")).not.toBeInTheDocument();
  });

  it("keeps parallel actions in one cycle distinct and supports roving keyboard focus", async () => {
    const second = {
      ...listItem,
      action_id: "action-2",
      engine_call_id: "provider-call-1",
      step_id: "step-8",
      tool_id: "curl",
    };
    const onSelect = vi.fn();
    const user = userEvent.setup();
    render(
      <ActionTimeline
        items={[listItem, second]}
        loading={false}
        error={null}
        selectedActionId={null}
        hasMore={false}
        loadingMore={false}
        onLoadMore={vi.fn()}
        onSelect={onSelect}
      />,
    );

    const firstButton = screen.getByRole("button", { name: /inspect action nmap/i });
    const secondButton = screen.getByRole("button", { name: /inspect action curl/i });
    firstButton.focus();
    await user.keyboard("{ArrowDown}{Enter}");

    expect(secondButton).toHaveFocus();
    expect(onSelect).toHaveBeenCalledWith("action-2", secondButton);
  });

  it("marks partial and truncated coverage instead of guessing a runner", () => {
    render(
      <ActionTimeline
        items={[
          {
            ...listItem,
            current_execution_id: null,
            latest_execution_id: null,
            attempt_order_quality: "ambiguous",
            attempt_coverage: { scanned: 2, limit: 2, truncated: true },
            artifacts_truncated: true,
            correlation_quality: "partial",
            partial_reasons: ["execution_attempt_order_ambiguous"],
          },
        ]}
        loading={false}
        error={null}
        selectedActionId={null}
        hasMore={false}
        loadingMore={false}
        onLoadMore={vi.fn()}
        onSelect={vi.fn()}
      />,
    );

    expect(screen.getByText("Partial action")).toBeInTheDocument();
    expect(screen.getByText("Runner unknown")).toBeInTheDocument();
    expect(screen.getAllByText(/truncated/i).length).toBeGreaterThan(0);
    expect(screen.queryByText("runner-current")).not.toBeInTheDocument();
    expect(screen.getAllByText("Not started")).toHaveLength(2);
  });

  it("does not fall back to an old latest Runner when the claimed current attempt is missing", () => {
    render(
      <ActionTimeline
        items={[
          {
            ...listItem,
            current_execution_id: "execution-not-materialized",
            current_execution_status: null,
          },
        ]}
        loading={false}
        error={null}
        selectedActionId={null}
        hasMore={false}
        loadingMore={false}
        onLoadMore={vi.fn()}
        onSelect={vi.fn()}
      />,
    );

    expect(screen.getByText("Runner unknown")).toBeInTheDocument();
    expect(screen.queryByText("runner-latest")).not.toBeInTheDocument();
    expect(screen.getAllByText("Not started")).toHaveLength(2);
  });

  it("does not infer execution start time for an orphan Action without attempts", () => {
    render(
      <ActionTimeline
        items={[
          {
            ...listItem,
            attempts: [],
            execution_count: 0,
            current_execution_id: null,
            latest_execution_id: null,
            lifecycle: "partial",
            correlation_quality: "partial",
            partial_reasons: ["execution_missing_for_intent_status"],
          },
        ]}
        loading={false}
        error={null}
        selectedActionId={null}
        hasMore={false}
        loadingMore={false}
        onLoadMore={vi.fn()}
        onSelect={vi.fn()}
      />,
    );

    expect(screen.getByText("Runner unknown")).toBeInTheDocument();
    expect(screen.getAllByText("Not started")).toHaveLength(2);
  });

  it("does not call a terminal attempt in progress when its finish time is partial", () => {
    render(
      <ActionTimeline
        items={[
          {
            ...listItem,
            attempts: [
              {
                ...listItem.attempts[0],
                status: "hard_timeout",
                finished_at: null,
              },
            ],
            current_execution_status: "hard_timeout",
          },
        ]}
        loading={false}
        error={null}
        selectedActionId={null}
        hasMore={false}
        loadingMore={false}
        onLoadMore={vi.fn()}
        onSelect={vi.fn()}
      />,
    );

    expect(screen.getByText("Unknown")).toBeInTheDocument();
    expect(screen.queryByText("In progress")).not.toBeInTheDocument();
  });

  it("fails closed on an unknown attempt status with no finish time", () => {
    render(
      <ActionTimeline
        items={[
          {
            ...listItem,
            attempts: [{ ...listItem.attempts[0], status: null, finished_at: null }],
            current_execution_status: null,
            correlation_quality: "partial",
          },
        ]}
        loading={false}
        error={null}
        selectedActionId={null}
        hasMore={false}
        loadingMore={false}
        onLoadMore={vi.fn()}
        onSelect={vi.fn()}
      />,
    );

    expect(screen.getByText("Unknown")).toBeInTheDocument();
    expect(screen.queryByText("In progress")).not.toBeInTheDocument();
  });

  it("states when physical stop is unconfirmed", () => {
    render(
      <ActionTimeline
        items={[
          {
            ...listItem,
            current_stop_confirmation: "unconfirmed",
            attempts: [
              { ...listItem.attempts[0], stop_confirmation: "unconfirmed" },
              listItem.attempts[1],
            ],
          },
        ]}
        loading={false}
        error={null}
        selectedActionId={null}
        hasMore={false}
        loadingMore={false}
        onLoadMore={vi.fn()}
        onSelect={vi.fn()}
      />,
    );

    expect(screen.getAllByText("Stop unconfirmed").length).toBeGreaterThan(0);
  });

  it("exposes explicit stable pagination", () => {
    const onLoadMore = vi.fn();
    render(
      <ActionTimeline
        items={[listItem]}
        loading={false}
        error={null}
        selectedActionId={null}
        hasMore
        loadingMore={false}
        onLoadMore={onLoadMore}
        onSelect={vi.fn()}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Load more actions" }));
    expect(onLoadMore).toHaveBeenCalledOnce();
    expect(screen.getByText("1 actions loaded; more are available.")).toBeInTheDocument();
  });

  it("keeps loaded Actions visible when a later page fails", () => {
    render(
      <ActionTimeline
        items={[listItem]}
        loading={false}
        error={null}
        paginationError={new Error("Next Action page failed")}
        selectedActionId="action-1"
        hasMore
        loadingMore={false}
        onLoadMore={vi.fn()}
        onSelect={vi.fn()}
      />,
    );

    expect(screen.getByText(listItem.reason)).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("Next Action page failed");
    expect(screen.getByRole("button", { name: "Load more actions" })).toBeEnabled();
  });

  it("hides cached Action text when a root refetch is forbidden", () => {
    render(
      <ActionTimeline
        items={[listItem]}
        loading={false}
        error={new RiftXAPIError(403, "local_operator_capability_denied", "Forbidden")}
        selectedActionId="action-1"
        hasMore={false}
        loadingMore={false}
        onLoadMore={vi.fn()}
        onSelect={vi.fn()}
      />,
    );

    expect(screen.getByRole("alert")).toHaveTextContent("Forbidden");
    expect(screen.queryByText(listItem.reason)).not.toBeInTheDocument();
    expect(screen.queryByText(listItem.target_summary!)).not.toBeInTheDocument();
  });

  it("translates failed and cancelled Actions plus terminal execution statuses in Chinese", () => {
    window.localStorage.setItem(languageStorageKey, "zh-CN");
    const failed = {
      ...listItem,
      lifecycle: "failed" as const,
      attempts: [{ ...listItem.attempts[0], status: "exited" as const }],
      current_execution_status: "exited" as const,
    };
    const cancelled = {
      ...listItem,
      action_id: "action-cancelled",
      lifecycle: "cancelled" as const,
      attempts: [{ ...listItem.attempts[0], status: "hard_timeout" as const }],
      current_execution_status: "hard_timeout" as const,
    };
    render(
      <LanguageProvider>
        <ActionTimeline
          items={[failed, cancelled]}
          loading={false}
          error={null}
          selectedActionId={null}
          hasMore={false}
          loadingMore={false}
          onLoadMore={vi.fn()}
          onSelect={vi.fn()}
        />
        <ActionInspector
          actionId="action-1"
          action={{
            ...detail,
            lifecycle: "failed",
            executions: [
              { ...detail.executions[0]!, status: "exited" },
              {
                ...detail.executions[0]!,
                execution_id: "execution-timeout",
                status: "hard_timeout",
              },
              {
                ...detail.executions[0]!,
                execution_id: "execution-queued",
                status: "queued",
              },
              {
                ...detail.executions[0]!,
                execution_id: "execution-starting",
                status: "starting",
              },
            ],
          }}
          loading={false}
          error={null}
          focusOnOpen={false}
          onClose={vi.fn()}
        />
      </LanguageProvider>,
    );

    expect(screen.getAllByText("失败").length).toBeGreaterThan(0);
    expect(screen.getAllByText("已取消").length).toBeGreaterThan(0);
    expect(screen.getByText("已退出")).toBeInTheDocument();
    expect(screen.getByText("强制超时")).toBeInTheDocument();
    expect(screen.getByText("排队中")).toBeInTheDocument();
    expect(screen.getByText("启动中")).toBeInTheDocument();
    expect(screen.getByText("会话")).toBeInTheDocument();
    expect(screen.getByText("轮次")).toBeInTheDocument();
    expect(screen.getByText("步骤")).toBeInTheDocument();
  });
});

describe("ActionInspector", () => {
  it("loads detail on selection and keeps artifacts behind authenticated download", async () => {
    const download = vi.spyOn(api, "downloadAuthenticatedUrl").mockResolvedValue();
    const onClose = vi.fn();
    render(
      <ActionInspector
        actionId="action-1"
        action={detail}
        loading={false}
        error={null}
        onClose={onClose}
      />,
    );

    expect(screen.getByRole("region", { name: "Context Inspector" })).toBeInTheDocument();
    expect(
      screen.getByText((_content, element) =>
        element?.tagName === "PRE" && element.textContent?.includes("[REDACTED]") === true,
      ),
    ).toBeInTheDocument();
    expect(screen.getByText("local-principal:v1:operator")).toBeInTheDocument();
    expect(document.body).not.toHaveTextContent("artifact body secret");

    fireEvent.click(
      screen.getByRole("button", { name: "Download Artifact artifact-1" }),
    );
    await waitFor(() =>
      expect(download).toHaveBeenCalledWith(
        "/api/v1/artifacts/artifact-1/content",
        "artifact-artifact-1",
      ),
    );

    fireEvent.click(screen.getByRole("button", { name: "Close Context Inspector" }));
    expect(onClose).toHaveBeenCalledOnce();
  });

  it("surfaces forbidden detail without rendering stale Action data", () => {
    render(
      <ActionInspector
        actionId="foreign-action"
        action={{ ...detail, action_id: "foreign-action" }}
        loading={false}
        error={new RiftXAPIError(403, "local_operator_capability_denied", "Forbidden")}
        onClose={vi.fn()}
      />,
    );

    expect(screen.getByRole("alert")).toHaveTextContent("Forbidden");
    expect(screen.queryByText(listItem.reason)).not.toBeInTheDocument();
    expect(screen.queryByText("local-principal:v1:operator")).not.toBeInTheDocument();
    expect(document.body).not.toHaveTextContent("[REDACTED]");
  });

  it.each([
    [403, "Artifact forbidden"],
    [404, "Artifact not found"],
  ])("surfaces Artifact download HTTP %s without injecting content", async (status, message) => {
    vi.spyOn(api, "downloadAuthenticatedUrl").mockRejectedValue(
      new RiftXAPIError(status, "artifact_download_failed", message),
    );
    render(
      <ActionInspector
        actionId="action-1"
        action={{ ...detail, result: { ...detail.result, truncated: true } }}
        loading={false}
        error={null}
        focusOnOpen={false}
        onClose={vi.fn()}
      />,
    );

    expect(screen.getByText("Artifact references are truncated")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Download Artifact artifact-1" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(message);
    expect(document.body).not.toHaveTextContent("artifact body secret");
  });

  it("focuses Close on open", async () => {
    const fallback = createRef<HTMLButtonElement>();
    render(
      <>
        <button ref={fallback}>Action trigger</button>
        <ActionInspector
          actionId="action-1"
          action={detail}
          loading={false}
          error={null}
          onClose={vi.fn()}
        />
      </>,
    );

    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Close Context Inspector" })).toHaveFocus(),
    );
  });

  it("ignores an old Artifact download failure after the selection changes", async () => {
    let rejectOld!: (reason: Error) => void;
    vi.spyOn(api, "downloadAuthenticatedUrl").mockImplementationOnce(
      () => new Promise<void>((_resolve, reject) => { rejectOld = reject; }),
    );
    const rendered = render(
      <ActionInspector
        actionId="action-1"
        action={detail}
        loading={false}
        error={null}
        onClose={vi.fn()}
      />,
    );
    fireEvent.click(
      screen.getByRole("button", { name: "Download Artifact artifact-1" }),
    );
    await waitFor(() => expect(rejectOld).toBeTypeOf("function"));

    rendered.rerender(
      <ActionInspector
        actionId="action-2"
        action={{
          ...detail,
          action_id: "action-2",
          reason: "Second Action reason",
          result: { ...detail.result, artifact_ids: [] },
          evidence: { ...detail.evidence, artifact_ids: [] },
        }}
        loading={false}
        error={null}
        onClose={vi.fn()}
      />,
    );
    await act(async () => rejectOld(new Error("Old Artifact forbidden")));

    expect(screen.getByText("Second Action reason")).toBeInTheDocument();
    expect(screen.queryByText("Old Artifact forbidden")).not.toBeInTheDocument();
  });
});
