import { cleanup, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { RiftXAPIError } from "../api/client";
import { ErrorState } from "./ErrorState";

afterEach(cleanup);

describe("ErrorState", () => {
  it("shows per-execution safety-stop disposition without hiding failed nodes", () => {
    render(
      <ErrorState
        error={
          new RiftXAPIError(
            503,
            "execution_cancel_failed",
            "Could not confirm every execution stopped",
            {
              run_id: "run-1",
              execution_ids: [
                "execution-stopped",
                "execution-confirmed-no-status",
                "execution-lost",
              ],
              execution_nodes: {
                "execution-stopped": "local",
                "execution-confirmed-no-status": "local",
                "execution-lost": "remote-1",
              },
              execution_statuses: {
                "execution-stopped": "cancelled",
                "execution-confirmed-no-status": "cancelled",
                "execution-lost": "lost",
              },
              confirmed_execution_ids: [
                "execution-stopped",
                "execution-confirmed-no-status",
              ],
              confirmed_statuses: { "execution-stopped": "cancelled" },
              failed_executions: {
                "execution-lost": "Runner did not acknowledge process termination",
              },
            },
          )
        }
      />,
    );

    expect(screen.getByRole("alert")).toHaveTextContent("Safety stop disposition");
    const rows = screen.getAllByRole("row");
    expect(within(rows[1]).getByText("execution-stopped")).toBeInTheDocument();
    expect(within(rows[1]).getByText("local")).toBeInTheDocument();
    expect(within(rows[1]).getByText("Stopped (cancelled)")).toBeInTheDocument();
    expect(within(rows[2]).getByText("execution-confirmed-no-status")).toBeInTheDocument();
    expect(within(rows[2]).getByText("Stop confirmed")).toBeInTheDocument();
    expect(within(rows[3]).getByText("execution-lost")).toBeInTheDocument();
    expect(within(rows[3]).getByText("remote-1")).toBeInTheDocument();
    expect(within(rows[3]).getByText(/Stop unconfirmed \(lost\)/)).toBeInTheDocument();
    expect(within(rows[3]).getByText(/did not acknowledge/)).toBeInTheDocument();
  });

  it("shows only allowlisted resource dispositions for a failed safety stop", () => {
    render(
      <ErrorState
        error={
          new RiftXAPIError(503, "safety_stop_failed", "Safety stop was not confirmed", {
            run_id: "run-2",
            stop_resources: {
              executions: {
                attempted_ids: ["execution-1"],
                node_ids: { "execution-1": "local" },
                observed_statuses: { "execution-1": "cancelled" },
                confirmed_ids: ["execution-1"],
                confirmed_statuses: { "execution-1": "cancelled" },
                failures: {},
                succeeded: true,
                diagnostics: "do-not-render-diagnostics",
              },
              browser_sessions: {
                attempted_ids: ["browser-1"],
                node_ids: { "browser-1": "remote-browser" },
                observed_statuses: { "browser-1": "open" },
                confirmed_ids: [],
                confirmed_statuses: {},
                failures: { "browser-1": "Browser process did not acknowledge close" },
                succeeded: false,
              },
              target_http_requests: {
                attempted_ids: ["request-1"],
                node_ids: {},
                observed_statuses: { "request-1": "aborted" },
                confirmed_ids: [],
                confirmed_statuses: {},
                failures: {},
                succeeded: true,
              },
              unknown_resource: {
                attempted_ids: ["do-not-render-resource"],
              },
            },
            internal_reason: "do-not-render-internal-reason",
          })
        }
      />,
    );

    const rows = screen.getAllByRole("row");
    expect(rows).toHaveLength(4);
    expect(within(rows[1]).getByText("Execution")).toBeInTheDocument();
    expect(within(rows[1]).getByText("execution-1")).toBeInTheDocument();
    expect(within(rows[1]).getByText("Stopped (cancelled)")).toBeInTheDocument();
    expect(within(rows[2]).getByText("Browser session")).toBeInTheDocument();
    expect(within(rows[2]).getByText("browser-1")).toBeInTheDocument();
    expect(within(rows[2]).getByText("Stop unconfirmed (open)")).toBeInTheDocument();
    expect(within(rows[2]).getByText(/did not acknowledge close/)).toBeInTheDocument();
    expect(within(rows[3]).getByText("Target HTTP request")).toBeInTheDocument();
    expect(within(rows[3]).getByText("request-1")).toBeInTheDocument();
    expect(within(rows[3]).getByText("Stop confirmed")).toBeInTheDocument();
    expect(screen.queryByText("do-not-render-resource")).not.toBeInTheDocument();
    expect(screen.queryByText("do-not-render-diagnostics")).not.toBeInTheDocument();
    expect(screen.queryByText("do-not-render-internal-reason")).not.toBeInTheDocument();
  });

  it("does not dump arbitrary structured error details", () => {
    render(
      <ErrorState
        error={
          new RiftXAPIError(503, "temporal_unavailable", "Temporal unavailable", {
            internal_reason: "do-not-render",
          })
        }
      />,
    );

    expect(screen.getByRole("alert")).toHaveTextContent("Temporal unavailable");
    expect(screen.queryByText("do-not-render")).not.toBeInTheDocument();
  });
});
