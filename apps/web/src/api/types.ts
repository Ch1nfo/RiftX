export type RunStatus =
  | "created"
  | "initializing"
  | "ready"
  | "preparing"
  | "running"
  | "waiting_tool"
  | "waiting_approval"
  | "waiting_user"
  | "pausing"
  | "paused"
  | "compacting"
  | "completing"
  | "completed"
  | "failed"
  | "cancelling"
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
  model_profile: string | null;
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
  model_profile?: string;
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

export type ModelProviderKind = "openai" | "openai_compatible";
export type ModelRequestMode = "chat_completions" | "responses";

export interface ModelProfileSummary {
  name: string;
  model: string;
  request_mode: ModelRequestMode;
  api_key_configured: boolean;
  is_default: boolean;
  is_effective_default: boolean;
}

export interface ModelProfile extends ModelProfileSummary {
  provider: ModelProviderKind;
  base_url: string | null;
  api_key_env: string | null;
  requires_api_key: boolean;
  timeout_seconds: number;
  max_retries: number;
  has_stored_api_key: boolean;
}

export interface ModelProfileSummaryList {
  default_profile: string;
  effective_default_profile: string;
  profiles: ModelProfileSummary[];
}

export interface ModelProfileList extends ModelProfileSummaryList {
  generation: number;
  source_digest: string;
  profile_override: string | null;
  profiles: ModelProfile[];
}

export interface UpdateModelProfilePayload {
  provider: ModelProviderKind;
  model: string;
  request_mode: ModelRequestMode;
  base_url?: string | null;
  api_key_env?: string | null;
  requires_api_key: boolean;
  timeout_seconds: number;
  max_retries: number;
  api_key?: string;
  clear_stored_api_key?: boolean;
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

export type NodeStatus = "online" | "offline" | "degraded" | "lost" | "unknown";

export interface Node {
  id: string;
  name: string;
  platform: string;
  architecture: string;
  runner_version: string;
  status: NodeStatus;
  capabilities: string[];
  labels: Record<string, string>;
  shell: string | null;
  working_directory: string | null;
  tool_count: number | null;
  active_execution_ids: string[];
  current_run_ids: string[];
  last_seen_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface NodeList {
  items: Node[];
}

export interface Execution {
  id: string;
  execution_key: string;
  run_id: string;
  session_id: string | null;
  tool_call_id: string | null;
  attempt_group: string | null;
  node_id: string;
  executor_type: "process" | "shell" | "pty";
  argv: string[];
  command_text: string | null;
  tool_id: string | null;
  tool_version: string | null;
  executable_path: string | null;
  cwd: string;
  env_diff: Record<string, string | null>;
  platform_system: string;
  platform_release: string;
  platform_architecture: string;
  status:
    | "created"
    | "queued"
    | "starting"
    | "running"
    | "completed"
    | "exited"
    | "failed"
    | "cancelled"
    | "hard_timeout"
    | "lost";
  pid: number | null;
  process_group_id: number | null;
  containment_id: string | null;
  exit_code: number | null;
  stdout_path: string;
  stderr_path: string;
  process_created_at: string | null;
  started_at: string | null;
  finished_at: string | null;
  physical_stop_confirmed_at: string | null;
}

export interface ExecutionList {
  items: Execution[];
  limit: number;
  offset: number;
}

export type ToolAvailability =
  | "available"
  | "unavailable"
  | "misconfigured"
  | "disabled"
  | "unknown";

export interface ToolVersionProbe {
  command: string[];
  timeout_seconds: number;
}

export interface ToolDefinition {
  id: string;
  enabled: boolean;
  command: string[];
  executor: "process" | "shell" | "pty";
  short_description?: string | null;
  description?: string | null;
  capabilities: string[];
  synonyms?: string[];
  input_schema?: Record<string, unknown> | null;
  version_probe: ToolVersionProbe | null;
  approval_level: "never" | "sensitive" | "always";
  timeout_seconds: number;
  output: { preferred: string | null };
  environment: Record<string, string>;
}

export interface ToolDefinitionSummary extends Omit<ToolDefinition, "environment"> {
  environment_variables: string[];
}

export interface UpdateToolPayload {
  enabled: boolean;
  command: string[];
  executor: ToolDefinition["executor"];
  capabilities: string[];
  version_probe?: { command: string[]; timeout_seconds: number } | null;
  approval: ToolDefinition["approval_level"];
  timeout: number;
  output?: { preferred?: string | null };
  environment?: Record<string, string>;
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

export interface RegisteredToolSummary {
  definition: ToolDefinitionSummary;
  state: ToolState;
}

export interface ToolRegistrySummary {
  node_id: string;
  generation: number;
  source_digest: string;
  execution_policy: "open" | "registered_only";
  tools: RegisteredToolSummary[];
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

export interface CreateFindingPayload {
  title: string;
  severity: Finding["severity"];
  status?: Finding["status"];
  affected_assets?: string[];
  description?: string;
  evidence?: FindingEvidence[];
  reproduction_steps?: string[];
  impact?: string;
  recommendation?: string;
}

export type UpdateFindingPayload = Partial<CreateFindingPayload>;

export interface Artifact {
  id: string;
  run_id: string;
  execution_id: string | null;
  name: string;
  mime_type: string;
  sha256: string;
  size: number;
  description: string;
  created_at: string;
  content_url: string;
}

export interface ArtifactList {
  items: Artifact[];
  limit: number;
  offset: number;
}

export interface RegisterArtifactPayload {
  source_path: string;
  name?: string;
  mime_type?: string;
  description?: string;
  execution_id?: string;
}

export type ReportFormat = "markdown" | "html" | "json";

export interface Report {
  id: string;
  run_id: string;
  format: ReportFormat;
  artifact_id: string;
  finding_ids: string[];
  created_at: string;
  content_url: string;
}

export interface ReportList {
  items: Report[];
  limit: number;
  offset: number;
}

export interface GenerateReportsPayload {
  formats: ReportFormat[];
}

export interface APIErrorEnvelope {
  error: {
    code: string;
    message: string;
    details: Record<string, unknown> | unknown[];
  };
}

export type TerminalStatus = "created" | "open" | "closed" | "lost";
export type TerminalOwner = "agent" | "user" | "shared";

export interface TerminalSession {
  id: string;
  run_id: string;
  execution_id: string;
  status: TerminalStatus;
  owner: TerminalOwner;
  cols: number;
  rows: number;
  argv: string[];
  cwd: string;
  pid: number | null;
  exit_code: number | null;
  execution_status:
    | "created"
    | "starting"
    | "running"
    | "exited"
    | "failed"
    | "cancelled"
    | "lost";
  created_at: string;
  closed_at: string | null;
}

export interface CreateTerminalPayload {
  argv?: string[];
  cwd?: string;
  env?: Record<string, string | null>;
  cols?: number;
  rows?: number;
  owner?: TerminalOwner;
}

export type TerminalWebSocketMessage =
  | { type: "state"; session: TerminalSession }
  | { type: "output"; data: string; cursor: number; next_cursor: number }
  | { type: "error"; code: string; message: string }
  | { type: "pong" };
