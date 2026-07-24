import { invoke } from "@tauri-apps/api/core";
import { listen, type UnlistenFn } from "@tauri-apps/api/event";
import type {
  ApprovalDecision,
  ConversationPage,
  CreateEngagementInput,
  DaemonControlStatus,
  DesktopBridgeError,
  DesktopDaemonInfo,
  Engagement,
  EngagementEvent,
  EngagementReport,
  EngagementStreamStatus,
  LlmSettings,
  NotificationSettings,
  PendingApproval,
  TurnAccepted,
} from "./models";

const ENGAGEMENT_EVENT_NAME = "riftx://engagement-event";
const ENGAGEMENT_STREAM_NAME = "riftx://engagement-stream";
const RUNTIME_STATUS_EVENT_NAME = "riftx://runtime-status";
const RUNTIME_ERROR_EVENT_NAME = "riftx://runtime-error";

export function daemonInfo(): Promise<DesktopDaemonInfo> {
  return desktopInvoke("daemon_info");
}

export function pauseRuntime(): Promise<DaemonControlStatus> {
  return desktopInvoke("pause_runtime");
}

export function resumeRuntime(): Promise<DaemonControlStatus> {
  return desktopInvoke("resume_runtime");
}

export function killRuntime(): Promise<DaemonControlStatus> {
  return desktopInvoke("kill_runtime");
}

export function llmSettings(): Promise<LlmSettings> {
  return desktopInvoke("llm_settings");
}

export function saveLlmApiKey(apiKey: string): Promise<LlmSettings> {
  return desktopInvoke("save_llm_api_key", { input: { apiKey } });
}

export function deleteLlmApiKey(): Promise<LlmSettings> {
  return desktopInvoke("delete_llm_api_key");
}

export function notificationSettings(): Promise<NotificationSettings> {
  return desktopInvoke("notification_settings");
}

export function requestNotificationPermission(): Promise<NotificationSettings> {
  return desktopInvoke("request_notification_permission");
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

export function listApprovals(
  engagementId: string,
): Promise<PendingApproval[]> {
  return desktopInvoke("list_approvals", { engagementId });
}

export function decideApproval(
  approvalId: string,
  decision: ApprovalDecision,
): Promise<void> {
  return desktopInvoke("decide_approval", { approvalId, decision });
}

export function engagementStreamStatus(
  engagementId: string,
): Promise<EngagementStreamStatus> {
  return desktopInvoke("engagement_stream_status", { engagementId });
}

export function onEngagementEvent(
  handler: (event: EngagementEvent) => void,
): Promise<UnlistenFn> {
  if (!isDesktopRuntime()) {
    return Promise.resolve(() => undefined);
  }
  return listen<EngagementEvent>(ENGAGEMENT_EVENT_NAME, (event) =>
    handler(event.payload),
  );
}

export function onEngagementStream(
  handler: (status: EngagementStreamStatus) => void,
): Promise<UnlistenFn> {
  if (!isDesktopRuntime()) {
    return Promise.resolve(() => undefined);
  }
  return listen<EngagementStreamStatus>(ENGAGEMENT_STREAM_NAME, (event) =>
    handler(event.payload),
  );
}

export function onRuntimeStatus(
  handler: (status: DaemonControlStatus) => void,
): Promise<UnlistenFn> {
  if (!isDesktopRuntime()) {
    return Promise.resolve(() => undefined);
  }
  return listen<DaemonControlStatus>(RUNTIME_STATUS_EVENT_NAME, (event) =>
    handler(event.payload),
  );
}

export function onRuntimeError(
  handler: (error: DesktopBridgeError) => void,
): Promise<UnlistenFn> {
  if (!isDesktopRuntime()) {
    return Promise.resolve(() => undefined);
  }
  return listen<DesktopBridgeError>(RUNTIME_ERROR_EVENT_NAME, (event) =>
    handler(event.payload),
  );
}

export function engagementReport(
  engagementId: string,
): Promise<EngagementReport> {
  return desktopInvoke("engagement_report", { engagementId });
}

export function conversationHistory(
  engagementId: string,
  cursor: number | null = null,
): Promise<ConversationPage> {
  return desktopInvoke("conversation_history", { engagementId, cursor });
}

function desktopInvoke<T>(
  command: string,
  args?: Record<string, unknown>,
): Promise<T> {
  if (!isDesktopRuntime()) {
    return Promise.reject({
      code: "desktop_runtime_unavailable",
      message: "Open this interface through the RiftX desktop application.",
    });
  }
  return invoke(command, args);
}

function isDesktopRuntime(): boolean {
  return "__TAURI_INTERNALS__" in window;
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
