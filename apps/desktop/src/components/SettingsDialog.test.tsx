import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  prepareSettingsReload,
  settingsReloadImpact,
} from "../bridge";
import { SettingsDialog } from "./SettingsDialog";

const { applyMutation } = vi.hoisted(() => ({
  applyMutation: vi.fn(),
}));

vi.mock("../bridge", () => ({
  bridgeError: (error: unknown) => error,
  prepareSettingsReload: vi.fn(),
  settingsReloadImpact: vi.fn(),
}));

vi.mock("./ExtensionDiagnostics", () => ({
  ToolsSettingsView: () => <div>Tools setup content</div>,
  SkillsSettingsView: () => <div>Skills setup content</div>,
}));

vi.mock("./ModelSettingsView", () => ({
  ModelSettingsView: ({
    onBeforeMutation,
  }: {
    onBeforeMutation: () => Promise<boolean>;
  }) => (
    <div>
      Model setup content
      <button
        type="button"
        onClick={() =>
          void onBeforeMutation().then((allowed) => {
            if (allowed) {
              applyMutation();
            }
          })
        }
      >
        Save model settings
      </button>
    </div>
  ),
}));

describe("SettingsDialog onboarding", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(settingsReloadImpact).mockResolvedValue({ activeTurns: [] });
    vi.mocked(prepareSettingsReload).mockResolvedValue({
      runtime: {
        state: "paused",
        reason: "operatorPause",
        updatedAt: 1,
        audit: { state: "healthy", message: null, updatedAt: 1 },
      },
      interruptedEngagementIds: ["engagement-a"],
    });
  });

  it("starts first-time setup with Tools and leads to Model configuration", () => {
    render(
      <SettingsDialog
        open
        setupRequired
        onClose={vi.fn()}
        onError={vi.fn()}
        onRuntimeChanged={vi.fn()}
      />,
    );

    expect(screen.getByText("First-time setup")).toBeInTheDocument();
    expect(
      screen.getByText(/Confirm Tools directories and run Doctor/),
    ).toBeInTheDocument();
    expect(screen.getByText("Tools setup content")).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Tools" })).toHaveAttribute(
      "aria-selected",
      "true",
    );

    fireEvent.click(screen.getByRole("tab", { name: "Model" }));

    expect(screen.getByText("Model setup content")).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Model" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
  });

  it("cancels without mutation or pauses affected turns before applying", async () => {
    vi.mocked(settingsReloadImpact).mockResolvedValue({
      activeTurns: [
        {
          engagementId: "engagement-a",
          engagementName: "Authorized lab",
          profileName: "default",
        },
      ],
    });
    render(
      <SettingsDialog
        open
        settingsLocked
        onClose={vi.fn()}
        onError={vi.fn()}
        onRuntimeChanged={vi.fn()}
      />,
    );

    expect(
      screen.getByText("Active execution requires confirmation"),
    ).toBeInTheDocument();
    fireEvent.click(
      screen.getByRole("button", { name: "Save model settings" }),
    );

    expect(await screen.findByRole("alertdialog")).toHaveTextContent(
      "Authorized lab",
    );
    expect(screen.getByRole("alertdialog")).toHaveTextContent(
      "default · engagement-a",
    );
    expect(prepareSettingsReload).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    await waitFor(() =>
      expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument(),
    );
    expect(applyMutation).not.toHaveBeenCalled();

    fireEvent.click(
      screen.getByRole("button", { name: "Save model settings" }),
    );
    await screen.findByRole("alertdialog");
    fireEvent.click(screen.getByRole("button", { name: "Pause and apply" }));

    await waitFor(() =>
      expect(prepareSettingsReload).toHaveBeenCalledWith(["engagement-a"]),
    );
    await waitFor(() => expect(applyMutation).toHaveBeenCalledOnce());
  });
});
