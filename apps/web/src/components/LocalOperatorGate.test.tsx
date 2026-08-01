import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  api,
  clearLocalOperatorToken,
  localOperatorHeaders,
  RiftXAPIError,
} from "../api/client";
import { LocalOperatorGate } from "./LocalOperatorGate";

afterEach(() => {
  cleanup();
  clearLocalOperatorToken();
  vi.restoreAllMocks();
});

describe("LocalOperatorGate", () => {
  it("unlocks with a valid memory-only token without writing browser storage", async () => {
    const storageWrite = vi.spyOn(Storage.prototype, "setItem");
    const profileRequest = vi.spyOn(api, "getSecurityProfile").mockImplementation(async () => {
      expect(localOperatorHeaders()).toEqual({
        Authorization: "Bearer local-operator-secret",
      });
      return {
        profile: "local_single_operator",
        principal_id: "local-principal:v1:test",
        capabilities: ["local.read"],
        features: {
          gateway: false,
          remote_identity: false,
          route: false,
          traffic_body: false,
          traffic_replay: false,
        },
        tenant_safe: false,
      };
    });

    render(
      <LocalOperatorGate>
        <p>Protected RiftX UI</p>
      </LocalOperatorGate>,
    );
    fireEvent.change(screen.getByLabelText("Local operator token"), {
      target: { value: "local-operator-secret" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Continue" }));

    expect(await screen.findByText("Protected RiftX UI")).toBeInTheDocument();
    expect(profileRequest).toHaveBeenCalledTimes(1);
    expect(storageWrite).not.toHaveBeenCalled();
    expect(localOperatorHeaders()).toEqual({
      Authorization: "Bearer local-operator-secret",
    });
  });

  it("keeps the gate accessible and clears the in-memory token after rejection", async () => {
    vi.spyOn(api, "getSecurityProfile").mockRejectedValue(
      new RiftXAPIError(
        401,
        "local_operator_authentication_failed",
        "The local operator credential is invalid",
      ),
    );

    render(
      <LocalOperatorGate>
        <p>Protected RiftX UI</p>
      </LocalOperatorGate>,
    );
    const input = screen.getByLabelText("Local operator token");
    fireEvent.change(input, { target: { value: "wrong-token" } });
    fireEvent.submit(input.closest("form")!);

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("The local operator credential is invalid");
    expect(alert).toHaveTextContent("local_operator_authentication_failed");
    expect(input).toHaveAttribute("type", "password");
    expect(input).toHaveAttribute("autocomplete", "off");
    expect(screen.queryByText("Protected RiftX UI")).not.toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole("button", { name: "Continue" })).toBeEnabled());
    expect(localOperatorHeaders()).toEqual({});
  });
});
