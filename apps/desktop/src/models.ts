export type ExecutionMode = "native" | "hardened" | "auto";
export type EnvironmentClass = "lab" | "staging" | "production";
export type EngagementStatus = "draft" | "active" | "interrupted" | "completed";
export type DaemonRunState = "running" | "paused";
export type DaemonPauseReason = "operatorPause" | "killSwitch";

export interface DaemonControlStatus {
  state: DaemonRunState;
  reason: DaemonPauseReason | null;
  updatedAt: number;
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

export interface LlmProfileSettings {
  profileName: string;
  model: string;
  baseUrl: string;
  timeoutSeconds: number;
  reasoningLevel: string;
  contextBudget: number;
  credentialSource: "keyring" | "environment";
  credentialName: string;
  configured: boolean;
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
    capabilities: string[];
    risk: "low" | "medium" | "high" | "critical" | null;
    helpArgs: string[];
    versionArgs: string[];
    healthCheckArgs: string[];
    inputTargetField: string | null;
    outputFormat: string | null;
    parser: string | null;
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

export type ApprovalDecision = "approve" | "deny";

export interface PendingApproval {
  id: string;
  engagementId: string;
  policyRevision: string;
  kind: "command";
  requestedAt: number;
  command: string | null;
  cwd: string | null;
  reason: string | null;
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
  toolSnapshot: ToolReportSnapshot;
  skillSnapshot: SkillReportSnapshot;
}

export interface DesktopBridgeError {
  code: string;
  message: string;
}
