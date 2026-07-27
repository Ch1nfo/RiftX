import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import App from "./App";
import { daemonInfo, listEngagements, llmProfiles } from "./bridge";

vi.mock("./bridge", () => ({
  bridgeError: (error: unknown) =>
    typeof error === "object" && error !== null && "code" in error
      ? error
      : { code: "desktop_error", message: String(error) },
  daemonInfo: vi.fn(),
  listEngagements: vi.fn(),
  llmProfiles: vi.fn(),
  onRuntimeStatus: vi.fn().mockResolvedValue(() => undefined),
  onRuntimeError: vi.fn().mockResolvedValue(() => undefined),
  prepareSettingsReload: vi.fn(),
  settingsReloadImpact: vi.fn(),
}));

describe("App first-run model gate", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(daemonInfo).mockResolvedValue({
      protocolVersion: 13,
      daemonVersion: "0.8.0",
      configPath: "/tmp/riftx.toml",
      runtime: {
        state: "running",
        reason: null,
        updatedAt: 1,
        audit: { state: "healthy", message: null, updatedAt: 1 },
      },
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
});
