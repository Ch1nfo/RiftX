export type ExecutionMode = "native" | "hardened" | "auto";
export type EnvironmentClass = "lab" | "staging" | "production";
export type EngagementStatus = "draft" | "active" | "interrupted" | "completed";

export interface DesktopDaemonInfo {
  protocolVersion: number;
  daemonVersion: string;
  configPath: string;
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
}

export interface DesktopBridgeError {
  code: string;
  message: string;
}
