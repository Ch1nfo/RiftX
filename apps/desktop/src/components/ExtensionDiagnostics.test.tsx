import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ToolsSettingsView } from "./ExtensionDiagnostics";
import {
  getToolsSettings,
  saveToolsSettings,
  toolDoctor,
  toolInventory,
} from "../bridge";

vi.mock("../bridge", () => ({
  bridgeError: (error: unknown) => error,
  getToolsSettings: vi.fn(),
  saveToolsSettings: vi.fn(),
  skillCatalog: vi.fn(),
  skillDoctor: vi.fn(),
  toolDoctor: vi.fn(),
  toolInventory: vi.fn(),
}));

const inventory = {
  roots: ["/tools/primary", "/tools/shared"],
  pathEntries: [],
  tools: [],
  snapshotSha256: "a".repeat(64),
  diagnostics: [],
};

describe("ToolsSettingsView", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(getToolsSettings).mockResolvedValue({
      directories: ["/tools/primary", "/tools/shared"],
      daemonRestartRequired: false,
    });
    vi.mocked(toolInventory).mockResolvedValue(inventory);
    vi.mocked(toolDoctor).mockResolvedValue(inventory);
    vi.mocked(saveToolsSettings).mockImplementation(async (directories) => ({
      directories,
      daemonRestartRequired: false,
    }));
  });

  it("adds, reorders, removes, and saves Tools directories", async () => {
    render(<ToolsSettingsView onError={vi.fn()} />);

    const list = await screen.findByLabelText("Configured Tools directories");
    fireEvent.change(screen.getByLabelText("Tools directory path"), {
      target: { value: "/tools/team" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Add" }));
    fireEvent.click(screen.getByRole("button", { name: "Move /tools/team up" }));
    fireEvent.click(
      screen.getByRole("button", { name: "Remove /tools/primary" }),
    );
    fireEvent.click(screen.getByRole("button", { name: "Save directories" }));

    await waitFor(() =>
      expect(saveToolsSettings).toHaveBeenCalledWith([
        "/tools/team",
        "/tools/shared",
      ]),
    );
    expect(toolDoctor).toHaveBeenCalledOnce();
    expect(
      within(list).getAllByRole("code").map((node) => node.textContent),
    ).toEqual(["/tools/team", "/tools/shared"]);
    expect(
      screen.getByText(/active Engagements keep their snapshot/),
    ).toBeInTheDocument();
  });

  it("retries when the Tools Directory is temporarily unavailable", async () => {
    const onError = vi.fn();
    vi.mocked(toolInventory).mockRejectedValueOnce({
      code: "state_error",
      message: "Tools Directory could not be read.",
    });

    render(<ToolsSettingsView onError={onError} />);

    expect(await screen.findByText("Tools unavailable")).toBeInTheDocument();
    expect(onError).toHaveBeenCalledOnce();
    fireEvent.click(screen.getByRole("button", { name: "Retry loading" }));

    expect(
      await screen.findByLabelText("Configured Tools directories"),
    ).toBeInTheDocument();
    expect(toolInventory).toHaveBeenCalledTimes(2);
  });

  it("uses the platform default when the configured list is empty", async () => {
    vi.mocked(getToolsSettings).mockResolvedValue({
      directories: [],
      daemonRestartRequired: false,
    });

    render(<ToolsSettingsView onError={vi.fn()} />);

    expect(
      await screen.findByText("Platform default Tools Directory will be used."),
    ).toBeInTheDocument();
  });
});
