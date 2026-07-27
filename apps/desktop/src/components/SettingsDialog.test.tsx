import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { SettingsDialog } from "./SettingsDialog";

vi.mock("./ExtensionDiagnostics", () => ({
  ToolsSettingsView: () => <div>Tools setup content</div>,
  SkillsSettingsView: () => <div>Skills setup content</div>,
}));

vi.mock("./ModelSettingsView", () => ({
  ModelSettingsView: () => (
    <div>
      Model setup content
      <button type="button">Save model settings</button>
    </div>
  ),
}));

describe("SettingsDialog onboarding", () => {
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

  it("locks setting mutations while execution is active", () => {
    render(
      <SettingsDialog
        open
        settingsLocked
        onClose={vi.fn()}
        onError={vi.fn()}
        onRuntimeChanged={vi.fn()}
      />,
    );

    expect(screen.getByRole("alert")).toHaveTextContent(
      "Pause or interrupt the active turn",
    );
    expect(
      screen.getByRole("button", { name: "Save model settings" }),
    ).toBeDisabled();
    expect(screen.getByRole("tab", { name: "Tools" })).toBeEnabled();
  });
});
