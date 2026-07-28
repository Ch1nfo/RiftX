import type { DesktopBridgeError } from "./models";

export type DesktopErrorAction = "retryConnection" | "openSettings";

export interface DesktopErrorPresentation {
  title: string;
  message: string;
  action: DesktopErrorAction | null;
  actionLabel: string | null;
}

const SAFE_PRESENTATIONS: Record<string, DesktopErrorPresentation> = {
  daemon_start_failed: {
    title: "Daemon could not start",
    message:
      "RiftX could not start its local daemon. Review the model Profile and API key, then retry.",
    action: "retryConnection",
    actionLabel: "Retry connection",
  },
  daemon_unavailable: {
    title: "Daemon connection lost",
    message:
      "RiftX cannot reach its local daemon. Wait for any restart to finish, then retry the connection.",
    action: "retryConnection",
    actionLabel: "Retry connection",
  },
  daemon_stop_failed: {
    title: "Daemon did not stop",
    message:
      "RiftX could not complete the controlled daemon restart. Confirm no task is still running, then retry.",
    action: "retryConnection",
    actionLabel: "Retry connection",
  },
  protocol_mismatch: {
    title: "Desktop update required",
    message:
      "RiftX Desktop and the local daemon use incompatible protocols. Install matching RiftX versions before retrying.",
    action: "retryConnection",
    actionLabel: "Retry connection",
  },
  invalid_daemon_response: {
    title: "Invalid daemon response",
    message:
      "RiftX received an invalid local runtime response. Restart RiftX and export diagnostics if the problem continues.",
    action: "retryConnection",
    actionLabel: "Retry connection",
  },
  response_too_large: {
    title: "Daemon response rejected",
    message:
      "RiftX rejected an oversized local runtime response. Restart RiftX and export diagnostics if the problem continues.",
    action: "retryConnection",
    actionLabel: "Retry connection",
  },
  daemon_error: {
    title: "Daemon request failed",
    message:
      "The local RiftX runtime could not complete the request. Retry the connection or review Settings.",
    action: "retryConnection",
    actionLabel: "Retry connection",
  },
  app_server_error: {
    title: "Runtime request failed",
    message:
      "The local RiftX runtime could not complete the request. Open Settings, run the relevant Doctor or connection test, then retry.",
    action: "openSettings",
    actionLabel: "Open settings",
  },
  provider_connection_failed: {
    title: "Model provider request failed",
    message:
      "The configured model provider request failed. Open Settings and run Test connection for the selected Profile.",
    action: "openSettings",
    actionLabel: "Open settings",
  },
  profile_candidate_failed: {
    title: "Profile validation failed",
    message:
      "RiftX could not validate the candidate model Runtime. Review the Profile endpoint, protocol, model, and API key.",
    action: "openSettings",
    actionLabel: "Open settings",
  },
  profile_candidate_timeout: {
    title: "Profile validation timed out",
    message:
      "The candidate model Runtime did not become ready in time. Review the Profile endpoint and timeout, then retry.",
    action: "openSettings",
    actionLabel: "Open settings",
  },
  credential_store: {
    title: "Credential store unavailable",
    message:
      "RiftX could not access the system credential store. Unlock the OS keyring or credential manager, then retry.",
    action: "openSettings",
    actionLabel: "Open settings",
  },
  config_unavailable: {
    title: "Configuration unavailable",
    message:
      "RiftX could not load its local configuration. Open Settings or repair the RiftX installation before retrying.",
    action: "openSettings",
    actionLabel: "Open settings",
  },
  invalid_config: {
    title: "Configuration is invalid",
    message:
      "RiftX could not use the current local configuration. Review Settings and correct the highlighted values.",
    action: "openSettings",
    actionLabel: "Open settings",
  },
  audit_unavailable: {
    title: "Security audit unavailable",
    message:
      "Controlled execution remains blocked because RiftX cannot write its security audit. Restore local storage access, then retry.",
    action: "retryConnection",
    actionLabel: "Retry connection",
  },
  internal_error: {
    title: "RiftX request failed",
    message:
      "RiftX could not complete the local request. Retry once, then export diagnostics if the problem continues.",
    action: null,
    actionLabel: null,
  },
  state_error: {
    title: "RiftX state unavailable",
    message:
      "RiftX could not read or update the task state. Retry once, then export diagnostics if the problem continues.",
    action: null,
    actionLabel: null,
  },
};

export function presentDesktopError(
  error: DesktopBridgeError,
): DesktopErrorPresentation {
  return (
    SAFE_PRESENTATIONS[error.code] ?? {
      title: error.code.replace(/_/g, " "),
      message: error.message,
      action: null,
      actionLabel: null,
    }
  );
}
