import { invoke } from "@tauri-apps/api/core";
import type {
  CreateEngagementInput,
  DesktopBridgeError,
  DesktopDaemonInfo,
  Engagement,
  EngagementReport,
  TurnAccepted,
} from "./models";

export function daemonInfo(): Promise<DesktopDaemonInfo> {
  return desktopInvoke("daemon_info");
}

export function listEngagements(): Promise<Engagement[]> {
  return desktopInvoke("list_engagements");
}

export function createEngagement(
  input: CreateEngagementInput,
): Promise<Engagement> {
  return desktopInvoke("create_engagement", { input });
}

export function activateEngagement(engagementId: string): Promise<Engagement> {
  return desktopInvoke("activate_engagement", { engagementId });
}

export function startTurn(
  engagementId: string,
  input: string,
): Promise<TurnAccepted> {
  return desktopInvoke("start_turn", { engagementId, input });
}

export function interruptEngagement(engagementId: string): Promise<Engagement> {
  return desktopInvoke("interrupt_engagement", { engagementId });
}

export function engagementReport(
  engagementId: string,
): Promise<EngagementReport> {
  return desktopInvoke("engagement_report", { engagementId });
}

function desktopInvoke<T>(
  command: string,
  args?: Record<string, unknown>,
): Promise<T> {
  if (!("__TAURI_INTERNALS__" in window)) {
    return Promise.reject({
      code: "desktop_runtime_unavailable",
      message: "Open this interface through the RiftX desktop application.",
    });
  }
  return invoke(command, args);
}

export function bridgeError(error: unknown): DesktopBridgeError {
  if (
    typeof error === "object" &&
    error !== null &&
    "code" in error &&
    "message" in error
  ) {
    return {
      code: String(error.code),
      message: String(error.message),
    };
  }
  return {
    code: "desktop_error",
    message: error instanceof Error ? error.message : String(error),
  };
}
