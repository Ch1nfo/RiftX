import { describe, expect, it } from "vitest";
import { presentDesktopError } from "./errorPresentation";

describe("presentDesktopError", () => {
  it("redacts local transport details and provides a connection retry", () => {
    expect(
      presentDesktopError({
        code: "daemon_unavailable",
        message:
          "connect /private/tmp/riftx.sock failed; Authorization: Bearer secret",
      }),
    ).toEqual({
      title: "Daemon connection lost",
      message:
        "RiftX cannot reach its local daemon. Wait for any restart to finish, then retry the connection.",
      action: "retryConnection",
      actionLabel: "Retry connection",
    });
  });

  it("routes provider failures to model settings without exposing payloads", () => {
    expect(
      presentDesktopError({
        code: "provider_connection_failed",
        message: '{"error":{"api_key":"secret"}}',
      }),
    ).toEqual({
      title: "Model provider request failed",
      message:
        "The configured model provider request failed. Open Settings and run Test connection for the selected Profile.",
      action: "openSettings",
      actionLabel: "Open settings",
    });
  });

  it("preserves actionable domain errors that are not internal failures", () => {
    expect(
      presentDesktopError({
        code: "invalid_cidr",
        message: "10.0.0.999 is not a valid CIDR",
      }),
    ).toEqual({
      title: "invalid cidr",
      message: "10.0.0.999 is not a valid CIDR",
      action: null,
      actionLabel: null,
    });
  });
});
