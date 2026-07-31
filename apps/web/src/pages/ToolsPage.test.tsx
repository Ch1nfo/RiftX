import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ToolsPage } from "./ToolsPage";

const mocks = vi.hoisted(() => ({
  useTools: vi.fn(),
  useToolAdminDetails: vi.fn(),
  useRefreshTools: vi.fn(),
  useUpdateTool: vi.fn(),
  adminDetailsMutate: vi.fn(),
  refreshMutate: vi.fn(),
  updateMutate: vi.fn(),
}));

vi.mock("../hooks/queries", () => ({
  useTools: mocks.useTools,
  useToolAdminDetails: mocks.useToolAdminDetails,
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
  environment: { RIFTX_TOOL_TOKEN: "administrator-only-value" },
};

const summaryDefinition = (() => {
  const { environment, ...publicDefinition } = definition;
  return {
    ...publicDefinition,
    environment_variables: Object.keys(environment),
  };
})();

describe("ToolsPage", () => {
  beforeEach(() => {
    mocks.refreshMutate.mockReset();
    mocks.updateMutate.mockReset();
    mocks.adminDetailsMutate.mockReset();
    mocks.adminDetailsMutate.mockImplementation(
      (_toolId: string, options: { onSuccess?: (value: typeof definition) => void }) => {
        options.onSuccess?.(definition);
      },
    );
    mocks.useTools.mockReturnValue({
      isLoading: false,
      error: null,
      data: {
        node_id: "local",
        generation: 1,
        source_digest: "1234567890abcdef",
        execution_policy: "registered_only",
        tools: [
          {
            definition: summaryDefinition,
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
    mocks.useToolAdminDetails.mockReturnValue({
      mutate: mocks.adminDetailsMutate,
      isPending: false,
      error: null,
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

    expect(screen.getByRole("button", { name: "Edit" })).toBeDisabled();
    await user.type(screen.getByLabelText("Admin token (session only)"), "admin-secret");
    expect(mocks.useRefreshTools).toHaveBeenLastCalledWith("local", "admin-secret");
    expect(mocks.useUpdateTool).toHaveBeenLastCalledWith("local", "admin-secret");
    expect(mocks.useToolAdminDetails).toHaveBeenLastCalledWith("local", "admin-secret");
    await user.click(screen.getByRole("button", { name: "Edit" }));
    expect(mocks.adminDetailsMutate).toHaveBeenCalledWith(
      "nmap",
      expect.objectContaining({ onSuccess: expect.any(Function) }),
    );
    expect(screen.getByRole("region", { name: "Edit nmap" })).toBeInTheDocument();
    expect(screen.getByLabelText("Environment diff · JSON object")).toHaveValue(
      JSON.stringify(definition.environment, null, 2),
    );
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
