export type ExecutionMode = "redTeam" | "pentest" | "auto";
export type EnvironmentClass = "lab" | "staging" | "production";
export type EngagementStatus =
  | "draft"
  | "active"
  | "interrupted"
  | "expired"
  | "completed";
export type DaemonRunState = "running" | "paused";
export type DaemonPauseReason = "operatorPause" | "killSwitch";
export type AuditHealthState = "healthy" | "degraded";

export interface AuditHealthStatus {
  state: AuditHealthState;
  message: string | null;
  updatedAt: number;
}

export interface DaemonControlStatus {
  state: DaemonRunState;
  reason: DaemonPauseReason | null;
  updatedAt: number;
  audit: AuditHealthStatus;
}

export interface DesktopDaemonInfo {
  protocolVersion: number;
  daemonVersion: string;
  configPath: string;
  runtime: DaemonControlStatus;
}

export interface LlmSettings {
  defaultProfile: string;
  profiles: LlmProfileSettings[];
  daemonRestartRequired: boolean;
}

export interface ToolsSettings {
  directories: string[];
  daemonRestartRequired: boolean;
}

export interface UpsertLlmProfileInput {
  profileName: string;
  model: string;
  baseUrl: string;
  protocol?: "responses" | "chat_completions";
  makeDefault?: boolean;
  enabled?: boolean;
}

export type LlmProfileState =
  | "unconfigured"
  | "ready"
  | "invalid"
  | "unreachable"
  | "disabled"
  | "in_use";

export interface LlmProfileSummary {
  name: string;
  protocol: "responses" | "chat_completions";
  model: string;
  baseUrl: string;
  isDefault: boolean;
  state: LlmProfileState;
  stateDetail: string;
  configured: boolean;
  runtimeReady: boolean;
}

export interface LlmProfileList {
  defaultProfile: string;
  profiles: LlmProfileSummary[];
}

export interface LlmProfileSettings {
  profileName: string;
  protocol: "responses" | "chat_completions";
  model: string;
  baseUrl: string;
  timeoutSeconds: number;
  reasoningLevel: string;
  contextBudget: number;
  credentialSource: "keyring" | "environment";
  credentialName: string;
  configured: boolean;
  enabled: boolean;
}

export type LlmCheckStatus = "passed" | "failed" | "skipped";

export interface LlmCapabilityCheck {
  status: LlmCheckStatus;
  detail: string;
}

export interface LlmCapabilityMatrix {
  config: LlmCapabilityCheck;
  streamText: LlmCapabilityCheck;
  functionTools: LlmCapabilityCheck;
}

export interface LlmConnectionTestResult {
  profileName: string;
  protocol: string;
  model: string;
  ok: boolean;
  capabilities: LlmCapabilityMatrix;
}

export type DiagnosticLevel = "info" | "warning" | "error";

export interface ExtensionDiagnostic {
  level: DiagnosticLevel;
  code: string;
  path: string | null;
  message: string;
}

export interface ToolInventory {
  roots: string[];
  pathEntries: string[];
  tools: DiscoveredTool[];
  snapshotSha256: string;
  diagnostics: ExtensionDiagnostic[];
}

export interface DiscoveredTool {
  name: string;
  path: string;
  sha256: string;
  metadataPath: string | null;
  metadataSha256: string | null;
  metadata: {
    schemaVersion: number;
    capabilities: string[];
    risk: "low" | "medium" | "high" | "critical" | null;
    helpArgs: string[];
    versionArgs: string[];
    healthCheckArgs: string[];
    inputTargetField: string | null;
    outputFormat: string | null;
    parser: string | null;
    credential: {
      capability: string;
      injection: "stdin" | "environment" | "fileEnvironment";
      environmentVariable: string | null;
      arguments: string[];
      authenticationFailureExitCodes: number[];
    } | null;
  } | null;
  shadowedBy: string | null;
}

export interface SkillCatalog {
  root: string;
  skills: DiscoveredSkill[];
  snapshotSha256: string;
  diagnostics: ExtensionDiagnostic[];
}

export interface DiscoveredSkill {
  name: string;
  description: string;
  path: string;
  source: "builtIn" | "user";
  enabled: boolean;
  sha256: string;
}

export interface NotificationSettings {
  permission: "granted" | "denied" | "prompt" | "promptWithRationale";
}

export interface Engagement {
  id: string;
  name: string;
  status: EngagementStatus;
  objective: {
    summary: string;
    successCriteria: string[];
    structuredCriteria: unknown[];
  };
  entryPoints: string[];
  mode: ExecutionMode;
  llmProfile: string;
  autoLimits: {
    maxTurns: number;
    maxToolCalls: number;
    maxWallClockSeconds: number;
    maxSingleCommandSeconds: number;
    maxConsecutiveFailures: number;
    noProgressWindow: number;
    maxModelTokensOrCost: number | null;
  } | null;
  authorization: {
    network: {
      cidrs: string[];
      domains: string[];
      ports: number[];
    };
    identities: unknown[];
    capabilities: string[];
    environment: EnvironmentClass;
    window: {
      startsAt: number | null;
      expiresAt: number | null;
    };
  };
  policyRevision: string;
  threadId: string | null;
  createdAt: number;
  updatedAt: number;
}

export interface CreateEngagementInput {
  name: string;
  objective: string;
  successCriteria: string[];
  entryPoints: string[];
  cidrs: string[];
  domains: string[];
  ports: number[];
  mode: ExecutionMode;
  llmProfile: string;
  environment: EnvironmentClass;
  capabilities: string[];
  identities: unknown[];
  startsAt: number | null;
  expiresAt: number | null;
  confirmation?: string | null;
}

export type CredentialKind =
  | "password"
  | "apiToken"
  | "sshKey"
  | "certificate"
  | "other";

export interface CredentialReference {
  id: string;
  engagementId: string;
  label: string;
  kind: CredentialKind;
  username: string | null;
  domain: string | null;
  createdAt: number;
  configured: boolean;
}

export interface CreateAssessmentCredentialInput {
  engagementId: string;
  label: string;
  kind: CredentialKind;
  username: string | null;
  domain: string | null;
  secret: string;
}

export interface CredentialGrant {
  id: string;
  engagementId: string;
  credentialId: string;
  allowedTargets: {
    cidrs: string[];
    domains: string[];
    ports: number[];
  };
  allowedCapabilities: string[];
  maxUses: number;
  maxFailuresPerIdentity: number;
  startsAt: number | null;
  expiresAt: number;
  createdAt: number;
  revokedAt: number | null;
}

export interface CreateCredentialGrantInput {
  engagementId: string;
  credentialId: string;
  cidrs: string[];
  domains: string[];
  ports: number[];
  capabilities: string[];
  maxUses: number;
  maxFailuresPerIdentity: number;
  startsAt: number | null;
  expiresAt: number;
}

export interface TurnAccepted {
  taskId: string;
  status: string;
}

export interface EngagementEvent {
  engagementId: string;
  kind: string;
  timestamp: number;
  data: unknown;
}

export interface ConversationEntry {
  sequence: number;
  id: string;
  engagementId: string;
  turnId: string | null;
  role: "operator" | "agent";
  kind: "message" | "plan";
  text: string;
  createdAt: number;
}

export interface ConversationPage {
  data: ConversationEntry[];
  nextCursor: string | null;
}

export type ApprovalKind = "command" | "tool";

export type ApprovalDecision = "approve" | "deny";

export type ExecutionParseStatus = "parsed" | "complex" | "empty";
export type ExecutionRisk = "low" | "medium" | "high" | "critical" | "unknown";
export type ExecutionRiskSource =
  | "declared"
  | "missingRisk"
  | "missingMetadata"
  | "unmanaged"
  | "unresolved";

export interface ExecutionExecutable {
  requestedName: string;
  displayArgs: string[];
  resolvedPath: string | null;
  sha256: string | null;
  inventorySha256: string | null;
  inventoryHashMatches: boolean | null;
  risk: ExecutionRisk;
  riskSource: ExecutionRiskSource;
  capabilities: string[];
  managed: boolean;
}

export interface ExecutionIntent {
  engagementId: string;
  threadId: string;
  turnId: string;
  toolCallId: string;
  mode: ExecutionMode;
  displayArgv: string[];
  commandSha256: string;
  argumentSha256: string;
  cwd: string;
  executables: ExecutionExecutable[];
  toolInventorySha256: string;
  risk: ExecutionRisk;
  requestedCapabilities: string[];
  authorizationDeadline: number | null;
  policyRevision: string;
  parseStatus: ExecutionParseStatus;
  bindingSha256: string;
}

export interface PendingApproval {
  id: string;
  engagementId: string;
  policyRevision: string;
  kind: ApprovalKind;
  requestedAt: number;
  command: string | null;
  cwd: string | null;
  reason: string | null;
  executionIntent: ExecutionIntent | null;
}

export interface EngagementStreamStatus {
  engagementId: string;
  state: "connecting" | "connected" | "disconnected";
  message: string | null;
}

export interface ReportTask {
  id: string;
  kind: string;
  status: string;
  error: string | null;
}

export interface ReportExecution {
  id: string;
  runner: string;
  status: string;
  command?: string;
  exitCode?: number | null;
}

export interface ReportFinding {
  id: string;
  title: string;
  severity: string;
  description: string;
}

export interface ReportEvidence {
  id: string;
  summary: string;
}

export interface ReportArtifact {
  id: string;
  path: string;
  sha256: string;
  sizeBytes: number;
}

export interface ToolReportSnapshot {
  snapshotSha256: string;
  tools: {
    name: string;
    sha256: string;
    metadataSha256: string | null;
    metadataSchemaVersion: number | null;
    capabilities: string[];
    risk: "low" | "medium" | "high" | "critical" | null;
    managed: boolean;
    shadowed: boolean;
  }[];
}

export interface SkillReportSnapshot {
  snapshotSha256: string;
  skills: {
    name: string;
    source: "builtIn" | "user";
    enabled: boolean;
    sha256: string;
  }[];
}

export interface ReportApproval {
  id: string;
  engagementId: string;
  kind: "command" | "tool";
  requestedAt: number;
  decidedAt: number | null;
  requestedDecision: "approve" | "deny" | null;
  outcome: "pending" | "approved" | "denied" | "invalidated" | "cancelled";
  actor: "localOperator" | "system" | null;
  decisionReason:
    | "approved"
    | "operatorDenied"
    | "policyOrBindingChanged"
    | "daemonPaused"
    | "auditUnavailable"
    | "engagementStopped"
    | "turnCompleted"
    | "daemonRestart"
    | "runtimeClosed"
    | null;
  policyRevision: string;
  executionBindingSha256: string;
  commandSha256: string;
  argumentSha256: string;
  displayArgv: string[];
  cwd: string | null;
  executableNames: string[];
}

export interface EngagementReport {
  engagement: Engagement;
  assets: unknown[];
  services: unknown[];
  observations: unknown[];
  hypotheses: unknown[];
  executions: ReportExecution[];
  findings: ReportFinding[];
  evidence: ReportEvidence[];
  attackPaths: unknown[];
  coverage: unknown[];
  tasks: ReportTask[];
  artifacts: ReportArtifact[];
  approvals: ReportApproval[];
  toolSnapshot: ToolReportSnapshot;
  skillSnapshot: SkillReportSnapshot;
}

export interface DesktopBridgeError {
  code: string;
  message: string;
}

export type AutoRunState =
  | "ready"
  | "running"
  | "evaluating"
  | "paused"
  | "needsInput"
  | "succeeded"
  | "expired"
  | "budgetExhausted"
  | "failed"
  | "killed";

export type AutoStopReason =
  | "operatorPause"
  | "authorizationExpired"
  | "turnBudgetExhausted"
  | "toolBudgetExhausted"
  | "wallClockBudgetExhausted"
  | "consecutiveFailures"
  | "noProgress"
  | "auditUnavailable"
  | "providerAuthentication"
  | "providerProtocolError"
  | "daemonRestart"
  | "killSwitch"
  | "unrecoverableError"
  | "scopeNeedsInput"
  | "successCriteriaMet";

export interface AutoRun {
  engagementId: string;
  config: {
    objective: Engagement["objective"];
    expiresAt: number;
    limits: {
      maxTurns: number;
      maxToolCalls: number;
      maxWallClockSeconds: number;
      maxSingleCommandSeconds: number;
      maxConsecutiveFailures: number;
      noProgressWindow: number;
      maxModelTokensOrCost: number | null;
    };
  };
  state: AutoRunState;
  stopReason: AutoStopReason | null;
  currentSubgoal: string | null;
  turnsStarted: number;
  turnsCompleted: number;
  toolCalls: number;
  consecutiveFailures: number;
  noProgressTurns: number;
  lastGoalAssessment: {
    succeeded: boolean;
    criteria: Array<{ criterionId: string; satisfied: boolean }>;
  } | null;
  lastProgressAssessment: {
    progressed: boolean;
    action: "continue" | "replan" | "switchStrategy" | "needsInput";
  } | null;
  startedAt: number | null;
  updatedAt: number;
}
