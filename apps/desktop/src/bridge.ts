import { invoke } from "@tauri-apps/api/core";
import { listen, type UnlistenFn } from "@tauri-apps/api/event";
import type {
  ActiveTurnStatus,
  AutoRun,
  ApprovalDecision,
  ConversationPage,
  CreateAssessmentCredentialInput,
  CreateCredentialGrantInput,
  CreateEngagementInput,
  CredentialGrant,
  CredentialReference,
  DaemonControlStatus,
  DesktopBridgeError,
  DesktopDaemonInfo,
  Engagement,
  EngagementEvent,
  EngagementReport,
  EngagementStreamStatus,
  ExecutionMode,
  LlmProfileList,
  LlmSettings,
  NotificationSettings,
  PendingApproval,
  SettingsReloadImpact,
  SettingsReloadPreparation,
  SkillCatalog,
  ToolInventory,
  ToolsSettings,
  TurnAccepted,
  UpsertLlmProfileInput,
  LlmConnectionTestResult,
} from "./models";

const ENGAGEMENT_EVENT_NAME = "riftx://engagement-event";
const ENGAGEMENT_STREAM_NAME = "riftx://engagement-stream";
const RUNTIME_STATUS_EVENT_NAME = "riftx://runtime-status";
const RUNTIME_ERROR_EVENT_NAME = "riftx://runtime-error";

export function daemonInfo(): Promise<DesktopDaemonInfo> {
  return desktopInvoke("daemon_info");
}

export function activeTurns(): Promise<ActiveTurnStatus[]> {
  return desktopInvoke("active_turns");
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

export function settingsReloadImpact(): Promise<SettingsReloadImpact> {
  return desktopInvoke("settings_reload_impact");
}

export function prepareSettingsReload(
  expectedEngagementIds: string[],
): Promise<SettingsReloadPreparation> {
  return desktopInvoke("prepare_settings_reload", {
    input: { expectedEngagementIds },
  });
}

export function llmSettings(): Promise<LlmSettings> {
  return desktopInvoke("llm_settings");
}

export function llmProfiles(): Promise<LlmProfileList> {
  return desktopInvoke("llm_profiles");
}

export function getToolsSettings(): Promise<ToolsSettings> {
  return desktopInvoke("get_tools_settings");
}

export function saveToolsSettings(
  directories: string[],
): Promise<ToolsSettings> {
  return desktopInvoke("save_tools_settings", { input: { directories } });
}

export function upsertLlmProfile(
  input: UpsertLlmProfileInput,
): Promise<LlmSettings> {
  return desktopInvoke("upsert_llm_profile", { input });
}

export function deleteLlmProfile(profileName: string): Promise<LlmSettings> {
  return desktopInvoke("delete_llm_profile", {
    input: { profileName },
  });
}

export function setDefaultLlmProfile(
  profileName: string,
): Promise<LlmSettings> {
  return desktopInvoke("set_default_llm_profile", {
    input: { profileName },
  });
}

export function saveLlmApiKey(
  profileName: string,
  apiKey: string,
): Promise<LlmSettings> {
  return desktopInvoke("save_llm_api_key", {
    input: { profileName, apiKey },
  });
}

export function deleteLlmApiKey(profileName: string): Promise<LlmSettings> {
  return desktopInvoke("delete_llm_api_key", { input: { profileName } });
}

export function toolInventory(): Promise<ToolInventory> {
  return desktopInvoke("tool_inventory");
}

export function toolDoctor(): Promise<ToolInventory> {
  return desktopInvoke("tool_doctor");
}

export function skillCatalog(): Promise<SkillCatalog> {
  return desktopInvoke("skill_catalog");
}

export function skillDoctor(): Promise<SkillCatalog> {
  return desktopInvoke("skill_doctor");
}

export function testLlmProfile(
  profileName: string,
): Promise<LlmConnectionTestResult> {
  return desktopInvoke("test_llm_profile", { profileName });
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

export function changeEngagementMode(
  engagementId: string,
  mode: ExecutionMode,
  confirmation: string | null,
): Promise<Engagement> {
  return desktopInvoke("change_engagement_mode", {
    input: { engagementId, mode, confirmation },
  });
}

export function listAssessmentCredentials(
  engagementId: string,
): Promise<CredentialReference[]> {
  return desktopInvoke("list_assessment_credentials", { engagementId });
}

export function createAssessmentCredential(
  input: CreateAssessmentCredentialInput,
): Promise<CredentialReference> {
  return desktopInvoke("create_assessment_credential", { input });
}

export function deleteAssessmentCredential(
  engagementId: string,
  credentialId: string,
): Promise<CredentialReference> {
  return desktopInvoke("delete_assessment_credential", {
    input: { engagementId, credentialId },
  });
}

export function listCredentialGrants(
  engagementId: string,
): Promise<CredentialGrant[]> {
  return desktopInvoke("list_credential_grants", { engagementId });
}

export function createCredentialGrant(
  input: CreateCredentialGrantInput,
): Promise<CredentialGrant> {
  return desktopInvoke("create_credential_grant", { input });
}

export function revokeCredentialGrant(
  engagementId: string,
  grantId: string,
): Promise<CredentialGrant> {
  return desktopInvoke("revoke_credential_grant", {
    input: { engagementId, grantId },
  });
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

export function engagementReportMarkdown(
  engagementId: string,
): Promise<string> {
  return desktopInvoke("engagement_report_markdown", { engagementId });
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

export function autoStatus(engagementId: string): Promise<AutoRun> {
  return desktopInvoke("auto_status", { engagementId });
}

export function pauseAuto(engagementId: string): Promise<AutoRun> {
  return desktopInvoke("pause_auto", { engagementId });
}

export function resumeAuto(engagementId: string): Promise<AutoRun> {
  return desktopInvoke("resume_auto", { engagementId });
}

export function killAuto(engagementId: string): Promise<AutoRun> {
  return desktopInvoke("kill_auto", { engagementId });
}
