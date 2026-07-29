export type RunStatus =
  | "created"
  | "preparing"
  | "running"
  | "waiting_approval"
  | "paused"
  | "completed"
  | "failed"
  | "cancelled";

export type ApprovalMode = "auto" | "balanced" | "manual";

export interface Objective {
  description: string;
}

export interface SuccessCriterion {
  description: string;
  required: boolean;
}

export interface EntryPoint {
  kind: "cidr" | "ip" | "domain" | "url" | "file" | "text";
  value: string;
  metadata: Record<string, unknown>;
}

export interface RunScope {
  cidrs: string[];
  ips: string[];
  domains: string[];
  url_prefixes: string[];
  asset_tags: string[];
  exclusions: string[];
  starts_at: string | null;
  ends_at: string | null;
}

export interface Run {
  id: string;
  engagement_id: string;
  node_id: string;
  objective: Objective;
  success_criteria: SuccessCriterion[];
  entry_points: EntryPoint[];
  scope: RunScope;
  status: RunStatus;
  approval_mode: ApprovalMode;
  workspace_path: string;
  temporal_workflow_id: string | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
}

export interface RunList {
  items: Run[];
  limit: number;
  offset: number;
}

export interface CreateRunPayload {
  objective: string;
  node_id?: string;
  approval_mode?: ApprovalMode;
  success_criteria?: SuccessCriterion[];
  entry_points?: Array<Omit<EntryPoint, "metadata"> & { metadata?: Record<string, unknown> }>;
  scope?: Partial<RunScope>;
  workspace_path?: string;
  engagement?: {
    name: string;
    description?: string;
    authorization_reference?: string;
  };
}

export interface RunEvent {
  id: string;
  run_id: string;
  sequence: number;
  event_type: string;
  payload: Record<string, unknown>;
  created_at: string;
}

export interface RunEventList {
  items: RunEvent[];
  after_sequence: number;
}

export type ApprovalStatus = "pending" | "approved" | "rejected" | "cancelled";

export interface Approval {
  id: string;
  run_id: string;
  tool_call_id: string;
  status: ApprovalStatus;
  tool_name: string;
  command: string[];
  cwd: string;
  target_summary: string;
  env_diff: Record<string, string | null>;
  reason: string;
  decided_by: string | null;
  created_at: string;
  decided_at: string | null;
}

export interface ApprovalList {
  items: Approval[];
}

export interface ApprovalDecisionPayload {
  decided_by?: string;
  reason?: string | null;
  approve_for_run?: boolean;
}

export type ToolAvailability =
  | "available"
  | "unavailable"
  | "misconfigured"
  | "disabled"
  | "unknown";

export interface ToolDefinition {
  id: string;
  enabled: boolean;
  command: string[];
  executor: "process" | "shell" | "pty";
  capabilities: string[];
  approval_level: "never" | "sensitive" | "always";
  timeout_seconds: number;
  environment: Record<string, string>;
}

export interface ToolState {
  tool_id: string;
  node_id: string;
  availability: ToolAvailability;
  resolved_command: string | null;
  version: string | null;
  reason: string | null;
  checked_at: string;
}

export interface RegisteredTool {
  definition: ToolDefinition;
  state: ToolState;
}

export interface ToolRegistrySnapshot {
  node_id: string;
  generation: number;
  source_digest: string;
  execution_policy: "open" | "registered_only";
  tools: RegisteredTool[];
}

export interface FindingEvidence {
  artifact_id: string | null;
  execution_id: string | null;
  description: string;
  location: string | null;
}

export interface Finding {
  id: string;
  run_id: string;
  title: string;
  severity: "info" | "low" | "medium" | "high" | "critical";
  status: "draft" | "confirmed" | "resolved" | "false_positive";
  affected_assets: string[];
  description: string;
  evidence: FindingEvidence[];
  reproduction_steps: string[];
  impact: string;
  recommendation: string;
  created_at: string;
  updated_at: string;
}

export interface FindingList {
  items: Finding[];
  limit: number;
  offset: number;
}

export interface APIErrorEnvelope {
  error: {
    code: string;
    message: string;
    details: Record<string, unknown> | unknown[];
  };
}
