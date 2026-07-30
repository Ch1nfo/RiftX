import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ToolsPage } from "./ToolsPage";

const mocks = vi.hoisted(() => ({
  useTools: vi.fn(),
  useRefreshTools: vi.fn(),
  useUpdateTool: vi.fn(),
  refreshMutate: vi.fn(),
  updateMutate: vi.fn(),
}));

vi.mock("../hooks/queries", () => ({
  useTools: mocks.useTools,
  useRefreshTools: mocks.useRefreshTools,
  useUpdateTool: mocks.useUpdateTool,
}));

const definition = {
  id: "nmap",
  enabled: true,
  command: ["nmap"],
  executor: "process" as const,
  capabilities: ["port_scan"],
  version_probe: { command: ["nmap", "--version"], timeout_seconds: 5 },
  approval_level: "never" as const,
  timeout_seconds: 1800,
  output: { preferred: "xml" },
  environment: {},
};

describe("ToolsPage", () => {
  beforeEach(() => {
    mocks.refreshMutate.mockReset();
    mocks.updateMutate.mockReset();
    mocks.useTools.mockReturnValue({
      isLoading: false,
      error: null,
      data: {
        node_id: "local",
        generation: 1,
        source_digest: "1234567890abcdef",
        execution_policy: "open",
        tools: [
          {
            definition,
            state: {
              tool_id: "nmap",
              node_id: "local",
              availability: "available",
              resolved_command: "/usr/bin/nmap",
              version: "Nmap 7.95",
              reason: null,
              checked_at: "2026-07-30T00:00:00Z",
            },
          },
        ],
      },
    });
    mocks.useRefreshTools.mockReturnValue({
      mutate: mocks.refreshMutate,
      isPending: false,
      error: null,
    });
    mocks.useUpdateTool.mockReturnValue({
      mutate: mocks.updateMutate,
      isPending: false,
      error: null,
    });
  });

  it("edits, persists, and hot reloads a tool definition", async () => {
    const user = userEvent.setup();
    render(<ToolsPage />);

    await user.click(screen.getByRole("button", { name: "Edit" }));
    expect(screen.getByRole("region", { name: "Edit nmap" })).toBeInTheDocument();
    const capabilities = screen.getByLabelText("Capabilities · comma separated");
    await user.clear(capabilities);
    await user.type(capabilities, "port_scan, service_detection");
    const timeout = screen.getByLabelText("Timeout seconds");
    await user.clear(timeout);
    await user.type(timeout, "900");
    await user.click(screen.getByRole("button", { name: "Save and reload" }));

    expect(mocks.updateMutate).toHaveBeenCalledWith(
      {
        toolId: "nmap",
        payload: expect.objectContaining({
          command: ["nmap"],
          capabilities: ["port_scan", "service_detection"],
          timeout: 900,
          output: { preferred: "xml" },
          version_probe: { command: ["nmap", "--version"], timeout_seconds: 5 },
        }),
      },
      expect.objectContaining({ onSuccess: expect.any(Function) }),
    );
  });
});
