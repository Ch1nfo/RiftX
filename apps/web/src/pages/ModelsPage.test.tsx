import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { RiftXAPIError } from "../api/client";
import { ModelsPage, editorPayload } from "./ModelsPage";

const mocks = vi.hoisted(() => ({
  useModelProfiles: vi.fn(),
  useModelProfileControl: vi.fn(),
  setDefault: vi.fn(),
  remove: vi.fn(),
  getModelProfile: vi.fn(),
  updateModelProfile: vi.fn(),
  refetch: vi.fn(),
}));

vi.mock("../hooks/queries", () => ({
  useModelProfiles: mocks.useModelProfiles,
  useModelProfileControl: mocks.useModelProfileControl,
}));

vi.mock("../api/client", async (importOriginal) => {
  const original = await importOriginal<typeof import("../api/client")>();
  return {
    ...original,
    api: {
      ...original.api,
      getModelProfile: mocks.getModelProfile,
      updateModelProfile: mocks.updateModelProfile,
    },
  };
});

afterEach(cleanup);

const primary = {
  name: "primary",
  provider: "openai_compatible" as const,
  model: "example-model",
  request_mode: "chat_completions" as const,
  base_url: "https://llm.example.test/v1",
  api_key_env: "RIFTX_MODEL_API_KEY",
  requires_api_key: true,
  timeout_seconds: 120,
  max_retries: 2,
  has_stored_api_key: true,
  api_key_configured: true,
  is_default: true,
  is_effective_default: true,
};

const secondary = {
  ...primary,
  name: "secondary",
  model: "fallback-model",
  is_default: false,
  is_effective_default: false,
};

describe("ModelsPage", () => {
  beforeEach(() => {
    mocks.setDefault.mockReset();
    mocks.remove.mockReset();
    mocks.getModelProfile.mockReset();
    mocks.updateModelProfile.mockReset();
    mocks.refetch.mockReset();
    mocks.getModelProfile.mockResolvedValue(primary);
    mocks.updateModelProfile.mockResolvedValue(primary);
    mocks.refetch.mockResolvedValue(undefined);
    mocks.useModelProfiles.mockReturnValue({
      isLoading: false,
      error: null,
      data: {
        generation: 3,
        source_digest: "abcdef",
        default_profile: "primary",
        effective_default_profile: "primary",
        profile_override: null,
        profiles: [primary],
      },
      refetch: mocks.refetch,
    });
    mocks.useModelProfileControl.mockReturnValue({
      setDefault: { mutate: mocks.setDefault, isPending: false, error: null },
      remove: { mutate: mocks.remove, isPending: false, error: null },
    });
  });

  it("shows credential metadata without ever rendering the stored key", async () => {
    const user = userEvent.setup();
    render(<ModelsPage />);

    expect(screen.getByText("Credential configured")).toBeInTheDocument();
    expect(screen.getByText("chat_completions")).toBeInTheDocument();

    await user.type(screen.getByLabelText("Admin token (session only)"), "admin-secret");
    await user.click(screen.getByRole("button", { name: "Edit" }));
    await waitFor(() =>
      expect(mocks.getModelProfile).toHaveBeenCalledWith("primary", "admin-secret"),
    );
    const keyInput = screen.getByLabelText("New stored API key");
    expect(keyInput).toHaveValue("");
    expect(keyInput).toHaveAttribute("type", "password");
    expect(screen.getByLabelText("Base URL")).toBeRequired();
    expect(screen.getByLabelText("Timeout seconds")).toHaveAttribute("max", "600");

    await user.type(keyInput, "replacement-secret");
    await user.click(screen.getByRole("button", { name: "Save profile" }));

    await waitFor(() =>
      expect(mocks.updateModelProfile).toHaveBeenCalledWith(
        "primary",
        expect.objectContaining({
          model: "example-model",
          request_mode: "chat_completions",
          api_key: "replacement-secret",
        }),
        "admin-secret",
      ),
    );
    expect(keyInput).toHaveValue("");
  });

  it("creates a new model profile from the editor", async () => {
    const user = userEvent.setup();
    const created = {
      ...primary,
      name: "local-qwen",
      model: "qwen3",
      base_url: "http://127.0.0.1:8000/v1",
      has_stored_api_key: false,
      is_default: false,
      is_effective_default: false,
    };
    mocks.updateModelProfile.mockResolvedValue(created);

    render(<ModelsPage />);
    await user.type(screen.getByLabelText("Admin token (session only)"), "admin-secret");
    await user.click(screen.getByRole("button", { name: "New profile" }));

    const editor = screen.getByRole("form", { name: "Create model profile" });
    const editorFields = within(editor);
    await user.type(editorFields.getByRole("textbox", { name: /Profile name/ }), "local-qwen");
    await user.type(editorFields.getByRole("textbox", { name: /Model name/ }), "qwen3");
    await user.type(editorFields.getByLabelText("Base URL"), "http://127.0.0.1:8000/v1");
    await user.click(screen.getByRole("button", { name: "Save profile" }));

    await waitFor(() =>
      expect(mocks.updateModelProfile).toHaveBeenCalledWith(
        "local-qwen",
        {
          provider: "openai_compatible",
          model: "qwen3",
          request_mode: "chat_completions",
          base_url: "http://127.0.0.1:8000/v1",
          api_key_env: "RIFTX_MODEL_API_KEY",
          requires_api_key: true,
          timeout_seconds: 120,
          max_retries: 2,
        },
        "admin-secret",
      ),
    );
    expect(mocks.refetch).toHaveBeenCalledTimes(1);
    await waitFor(() => expect(editor).toHaveAccessibleName("Edit model profile local-qwen"));
  });

  it("sends an explicit request to remove the stored API key", async () => {
    const user = userEvent.setup();
    mocks.updateModelProfile.mockResolvedValue({
      ...primary,
      has_stored_api_key: false,
    });

    render(<ModelsPage />);
    await user.type(screen.getByLabelText("Admin token (session only)"), "admin-secret");
    await user.click(screen.getByRole("button", { name: "Edit" }));
    const clearStoredKey = await screen.findByRole("checkbox", {
      name: /Remove the stored API key/,
    });

    await user.click(clearStoredKey);
    expect(clearStoredKey).toBeChecked();
    await user.click(screen.getByRole("button", { name: "Save profile" }));

    await waitFor(() =>
      expect(mocks.updateModelProfile).toHaveBeenCalledWith(
        "primary",
        expect.objectContaining({ clear_stored_api_key: true }),
        "admin-secret",
      ),
    );
    const payload = mocks.updateModelProfile.mock.calls[0]?.[1];
    expect(payload).not.toHaveProperty("api_key");
  });

  it("sets a configured non-default profile as the default", async () => {
    const user = userEvent.setup();
    mocks.useModelProfiles.mockReturnValue({
      isLoading: false,
      error: null,
      data: {
        generation: 3,
        source_digest: "abcdef",
        default_profile: "primary",
        effective_default_profile: "primary",
        profile_override: null,
        profiles: [secondary],
      },
      refetch: mocks.refetch,
    });

    render(<ModelsPage />);
    const setDefault = screen.getByRole("button", { name: "Set default" });
    expect(setDefault).toBeDisabled();

    await user.type(screen.getByLabelText("Admin token (session only)"), "admin-secret");
    expect(setDefault).toBeEnabled();
    await user.click(setDefault);

    expect(mocks.setDefault).toHaveBeenCalledWith("secondary");
  });

  it("confirms and deletes a non-default profile", async () => {
    const user = userEvent.setup();
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
    mocks.useModelProfiles.mockReturnValue({
      isLoading: false,
      error: null,
      data: {
        generation: 3,
        source_digest: "abcdef",
        default_profile: "primary",
        effective_default_profile: "primary",
        profile_override: null,
        profiles: [secondary],
      },
      refetch: mocks.refetch,
    });

    render(<ModelsPage />);
    await user.type(screen.getByLabelText("Admin token (session only)"), "admin-secret");
    await user.click(screen.getByRole("button", { name: "Delete" }));

    expect(confirm).toHaveBeenCalledWith("Delete model profile secondary?");
    expect(mocks.remove).toHaveBeenCalledWith("secondary", expect.any(Object));
    confirm.mockRestore();
  });

  it("renders an unauthorized response from an authenticated profile request", async () => {
    const user = userEvent.setup();
    mocks.getModelProfile.mockRejectedValue(
      new RiftXAPIError(
        401,
        "admin_token_invalid",
        "RIFTX_ADMIN_TOKEN is invalid",
      ),
    );

    render(<ModelsPage />);
    await user.type(screen.getByLabelText("Admin token (session only)"), "wrong-token");
    await user.click(screen.getByRole("button", { name: "Edit" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("RIFTX_ADMIN_TOKEN is invalid");
    expect(alert).toHaveTextContent("admin_token_invalid");
    expect(mocks.getModelProfile).toHaveBeenCalledWith("primary", "wrong-token");
  });

  it("defaults new profiles to chat completions", () => {
    expect(
      editorPayload({
        originalName: null,
        name: "local-model",
        provider: "openai_compatible",
        model: "qwen3",
        requestMode: "chat_completions",
        baseUrl: "http://127.0.0.1:8000/v1",
        apiKeyEnv: "",
        requiresApiKey: false,
        timeoutSeconds: "60",
        maxRetries: "1",
        apiKey: "",
        clearStoredApiKey: false,
        hasStoredApiKey: false,
        apiKeyConfigured: true,
      }).payload,
    ).toMatchObject({
      request_mode: "chat_completions",
      base_url: "http://127.0.0.1:8000/v1",
      requires_api_key: false,
    });
  });

  it("requires an explicit HTTP(S) endpoint for openai_compatible profiles", () => {
    const compatibleEditor = {
      originalName: null,
      name: "local-model",
      provider: "openai_compatible" as const,
      model: "qwen3",
      requestMode: "chat_completions" as const,
      baseUrl: "",
      apiKeyEnv: "",
      requiresApiKey: false,
      timeoutSeconds: "60",
      maxRetries: "1",
      apiKey: "",
      clearStoredApiKey: false,
      hasStoredApiKey: false,
      apiKeyConfigured: true,
    };

    expect(() => editorPayload(compatibleEditor)).toThrow(
      "Base URL is required for openai_compatible providers",
    );
    expect(() =>
      editorPayload({ ...compatibleEditor, baseUrl: "ftp://models.example/v1" }),
    ).toThrow("Base URL must be an absolute HTTP or HTTPS URL");
    expect(() =>
      editorPayload({ ...compatibleEditor, baseUrl: "https://user:secret@models.example/v1" }),
    ).toThrow("Base URL must not contain user information");
    expect(
      editorPayload({ ...compatibleEditor, provider: "openai", baseUrl: "" }).payload.base_url,
    ).toBeNull();
  });

  it("rejects non-finite and over-limit model timeouts", () => {
    const editor = {
      originalName: null,
      name: "local-model",
      provider: "openai_compatible" as const,
      model: "qwen3",
      requestMode: "chat_completions" as const,
      baseUrl: "http://127.0.0.1:8000/v1",
      apiKeyEnv: "",
      requiresApiKey: false,
      timeoutSeconds: "Infinity",
      maxRetries: "1",
      apiKey: "",
      clearStoredApiKey: false,
      hasStoredApiKey: false,
      apiKeyConfigured: true,
    };

    expect(() => editorPayload(editor)).toThrow(
      "Timeout must be a finite number no greater than 600 seconds",
    );
    expect(() => editorPayload({ ...editor, timeoutSeconds: "NaN" })).toThrow(
      "Timeout must be a finite number no greater than 600 seconds",
    );
    expect(() => editorPayload({ ...editor, timeoutSeconds: "600.1" })).toThrow(
      "Timeout must be a finite number no greater than 600 seconds",
    );
    expect(editorPayload({ ...editor, timeoutSeconds: "600" }).payload.timeout_seconds).toBe(600);
  });

  it("rejects environment-variable escalation in remotely managed profiles", () => {
    const baseEditor = {
      originalName: null,
      name: "remote-model",
      provider: "openai_compatible" as const,
      model: "qwen3",
      requestMode: "chat_completions" as const,
      baseUrl: "https://models.example/v1",
      apiKeyEnv: "AWS_SECRET_ACCESS_KEY",
      requiresApiKey: true,
      timeoutSeconds: "60",
      maxRetries: "1",
      apiKey: "",
      clearStoredApiKey: false,
      hasStoredApiKey: false,
      apiKeyConfigured: false,
    };

    expect(() => editorPayload(baseEditor)).toThrow(
      "API key environment variable must start with RIFTX_MODEL_",
    );
    expect(() =>
      editorPayload({
        ...baseEditor,
        apiKeyEnv: "RIFTX_MODEL_API_KEY",
        baseUrl: "https://${AWS_SECRET_ACCESS_KEY}.capture.example/v1",
      }),
    ).toThrow("Managed Base URL must not contain environment references");
  });
});
