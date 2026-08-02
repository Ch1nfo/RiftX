import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { NewRunPage, parseEntryPoints } from "./NewRunPage";

const mocks = vi.hoisted(() => ({
  useCreateRun: vi.fn(),
  useNodes: vi.fn(),
  useModelProfiles: vi.fn(),
  mutateAsync: vi.fn(),
}));

vi.mock("../hooks/queries", () => ({
  useCreateRun: mocks.useCreateRun,
  useNodes: mocks.useNodes,
  useModelProfiles: mocks.useModelProfiles,
}));

afterEach(cleanup);

describe("new run entry point parsing", () => {
  it("converts one KIND=VALUE entry per line", () => {
    expect(
      parseEntryPoints("url=https://example.test\nip=10.10.10.20\n"),
    ).toEqual([
      { kind: "url", value: "https://example.test" },
      { kind: "ip", value: "10.10.10.20" },
    ]);
  });

  it("rejects unsupported entry point kinds", () => {
    expect(() => parseEntryPoints("host=example.test")).toThrow(
      "Unsupported entry point kind",
    );
  });
});

describe("new run model selection", () => {
  beforeEach(() => {
    mocks.mutateAsync.mockReset();
    mocks.mutateAsync.mockResolvedValue({ id: "run-1" });
    mocks.useCreateRun.mockReturnValue({
      mutateAsync: mocks.mutateAsync,
      isPending: false,
      error: null,
    });
    mocks.useNodes.mockReturnValue({
      data: {
        items: [
          {
            id: "local",
            name: "Local",
            platform: "darwin",
            architecture: "arm64",
            status: "online",
          },
        ],
      },
    });
    mocks.useModelProfiles.mockReturnValue({
      isLoading: false,
      error: null,
      data: {
        effective_default_profile: "primary",
        profiles: [
          {
            name: "primary",
            model: "example-model",
            request_mode: "chat_completions",
            is_effective_default: true,
            api_key_configured: true,
          },
          {
            name: "fallback",
            model: "fallback-model",
            request_mode: "responses",
            is_effective_default: false,
            api_key_configured: true,
          },
        ],
      },
    });
  });

  it("sends the selected server-side model profile when creating a Run", async () => {
    const user = userEvent.setup();
    const queryClient = new QueryClient();
    render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={["/runs/new"]}>
          <Routes>
            <Route path="/runs/new" element={<NewRunPage />} />
            <Route path="/runs/:runId" element={<div>Conversation destination</div>} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    );

    await user.type(
      screen.getByLabelText("Objective"),
      "Inspect the staging service",
    );
    await waitFor(() => expect(screen.getByLabelText("Model profile")).toHaveValue("primary"));
    await user.selectOptions(screen.getByLabelText("Model profile"), "fallback");
    await user.click(screen.getByRole("button", { name: /Create and continue to chat/ }));

    expect(mocks.mutateAsync).toHaveBeenCalledWith(
      expect.objectContaining({
        objective: "Inspect the staging service",
        model_profile: "fallback",
      }),
    );
    expect(await screen.findByText("Conversation destination")).toBeInTheDocument();
  });

  it("falls back to the first credential-ready profile", async () => {
    mocks.useModelProfiles.mockReturnValue({
      isLoading: false,
      error: null,
      data: {
        effective_default_profile: "primary",
        profiles: [
          {
            name: "primary",
            model: "broken-model",
            request_mode: "chat_completions",
            is_effective_default: true,
            api_key_configured: false,
          },
          {
            name: "fallback",
            model: "ready-model",
            request_mode: "responses",
            is_effective_default: false,
            api_key_configured: true,
          },
        ],
      },
    });

    render(
      <QueryClientProvider client={new QueryClient()}>
        <MemoryRouter>
          <NewRunPage />
        </MemoryRouter>
      </QueryClientProvider>,
    );

    await waitFor(() =>
      expect(screen.getByLabelText("Model profile")).toHaveValue("fallback"),
    );
    expect(
      screen.getByRole("button", { name: /Create and continue to chat/ }),
    ).toBeEnabled();
  });

  it("does not create a Run when model profiles could not be loaded", () => {
    mocks.useModelProfiles.mockReturnValue({
      isLoading: false,
      error: new Error("registry unavailable"),
      data: undefined,
    });

    render(
      <QueryClientProvider client={new QueryClient()}>
        <MemoryRouter>
          <NewRunPage />
        </MemoryRouter>
      </QueryClientProvider>,
    );

    expect(
      screen.getByRole("button", { name: /Create and continue to chat/ }),
    ).toBeDisabled();
  });

  it("shows entry-point validation errors without sending a create request", async () => {
    const user = userEvent.setup();
    render(
      <QueryClientProvider client={new QueryClient()}>
        <MemoryRouter>
          <NewRunPage />
        </MemoryRouter>
      </QueryClientProvider>,
    );

    await user.type(screen.getByLabelText("Objective"), "Inspect staging");
    await user.type(screen.getByLabelText(/^Entry points/), "host=staging.example.test");
    await waitFor(() => expect(screen.getByLabelText("Model profile")).toHaveValue("primary"));
    await user.click(screen.getByRole("button", { name: /Create and continue to chat/ }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      'Unsupported entry point kind "host"',
    );
    expect(mocks.mutateAsync).not.toHaveBeenCalled();
  });
});
