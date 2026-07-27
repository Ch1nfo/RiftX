import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { LlmProfileList, LlmSettings } from "../models";
import { ModelSettingsView } from "./ModelSettingsView";
import {
  llmProfiles,
  llmSettings,
  notificationSettings,
  upsertLlmProfile,
} from "../bridge";

vi.mock("../bridge", () => ({
  bridgeError: (error: unknown) => error,
  deleteLlmApiKey: vi.fn(),
  deleteLlmProfile: vi.fn(),
  llmProfiles: vi.fn(),
  llmSettings: vi.fn(),
  notificationSettings: vi.fn(),
  requestNotificationPermission: vi.fn(),
  saveLlmApiKey: vi.fn(),
  setDefaultLlmProfile: vi.fn(),
  testLlmProfile: vi.fn(),
  upsertLlmProfile: vi.fn(),
}));

const settings: LlmSettings = {
  defaultProfile: "default",
  daemonRestartRequired: false,
  profiles: [
    {
      profileName: "default",
      protocol: "responses",
      model: "gpt-test",
      baseUrl: "https://api.example.test/v1",
      timeoutSeconds: 300,
      reasoningLevel: "high",
      contextBudget: 200_000,
      credentialSource: "keyring",
      credentialName: "default",
      configured: true,
      enabled: true,
    },
  ],
};

const runtimeProfiles: LlmProfileList = {
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
};

describe("ModelSettingsView", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(llmSettings).mockResolvedValue(settings);
    vi.mocked(llmProfiles).mockResolvedValue(runtimeProfiles);
    vi.mocked(notificationSettings).mockResolvedValue({
      permission: "prompt",
    });
    vi.mocked(upsertLlmProfile).mockResolvedValue({
      ...settings,
      profiles: [
        {
          ...settings.profiles[0],
          timeoutSeconds: 45,
          reasoningLevel: "medium",
          contextBudget: 128_000,
        },
      ],
    });
  });

  it("shows profile health and saves runtime tuning fields", async () => {
    const onError = vi.fn();
    const onRuntimeChanged = vi.fn();
    render(
      <ModelSettingsView
        onBusyChange={vi.fn()}
        onError={onError}
        onRuntimeChanged={onRuntimeChanged}
      />,
    );

    expect(await screen.findByText("Unreachable")).toBeInTheDocument();
    expect(
      screen.getByText("Connection test could not reach the provider."),
    ).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("Reasoning"), {
      target: { value: "medium" },
    });
    fireEvent.change(screen.getByLabelText("Timeout (seconds)"), {
      target: { value: "45" },
    });
    fireEvent.change(screen.getByLabelText("Context budget"), {
      target: { value: "128000" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save profile" }));

    await waitFor(() =>
      expect(upsertLlmProfile).toHaveBeenCalledWith({
        profileName: "default",
        model: "gpt-test",
        baseUrl: "https://api.example.test/v1",
        protocol: "responses",
        timeoutSeconds: 45,
        reasoningLevel: "medium",
        contextBudget: 128_000,
      }),
    );
    expect(onRuntimeChanged).toHaveBeenCalledWith(true);
    expect(onError).not.toHaveBeenCalled();
  });

  it("blocks invalid timeout and context values before invoking Tauri", async () => {
    render(
      <ModelSettingsView
        onBusyChange={vi.fn()}
        onError={vi.fn()}
        onRuntimeChanged={vi.fn()}
      />,
    );

    await screen.findByText("Unreachable");
    fireEvent.change(screen.getByLabelText("Timeout (seconds)"), {
      target: { value: "0" },
    });

    expect(screen.getByRole("alert")).toHaveTextContent(
      "Timeout must be 1–3600 seconds",
    );
    expect(
      screen.getByRole("button", { name: "Save profile" }),
    ).toBeDisabled();
    expect(upsertLlmProfile).not.toHaveBeenCalled();
  });
});
