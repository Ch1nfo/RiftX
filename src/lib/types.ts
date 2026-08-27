export const API_TYPES = [
  "openai-completions",
  "openai-responses",
  "anthropic-messages",
  "google-generative-ai"
] as const;

export const TRANSPORTS = ["auto", "sse", "websocket"] as const;

export type ApiType = (typeof API_TYPES)[number];
export type Transport = (typeof TRANSPORTS)[number];
export type ThinkingLevel = "off" | "minimal" | "low" | "medium" | "high" | "xhigh";
export const APPROVAL_MODES = ["request", "auto", "full"] as const;
export type ApprovalMode = (typeof APPROVAL_MODES)[number];
export const SUBAGENT_AGGRESSIVENESS = ["low", "default", "high"] as const;
export type SubagentAggressiveness = (typeof SUBAGENT_AGGRESSIVENESS)[number];

export type ModelProfile = {
  id: string;
  name: string;
  provider: string;
  model: string;
  apiKey?: string;
  baseUrl: string;
  api: ApiType;
  transport: Transport;
  contextWindow: number;
  maxTokens: number;
  thinkingLevel: ThinkingLevel;
  supportsImages?: boolean;
};

export type ContextUsage = {
  tokens: number;
  contextWindow: number;
  percent: number | null;
  input: number | null;
  output: number | null;
  cacheRead: number | null;
  cacheWrite: number | null;
  remaining: number;
};

export type ApprovalRequest = {
  id: string;
  toolName: "bash" | "write" | "edit" | "browser";
  input: unknown;
  createdAt: string;
  subagentId?: string;
  threadId?: string;
  agentName?: string;
  taskSummary?: string;
};

export type SubagentStatus = "queued" | "running" | "completed" | "empty" | "failed" | "cancelled" | "interrupted";

export type SubagentLogEntry = {
  id: string;
  type: "thinking" | "tool" | "text" | "error";
  content: string;
  toolName?: string;
  status?: "queued" | "running" | "done" | "error";
  createdAt: string;
};

export type SubagentLogPatch = {
  id: string;
  content?: string;
  appendContent?: string;
  status?: "queued" | "running" | "done" | "error";
};

export const SUBAGENT_LOG_LIMITS = {
  entries: 80,
  content: 12000
} as const;

export type SubagentTask = {
  id: string;
  parentSessionId: string;
  threadId: string;
  name: string;
  task: string;
  status: SubagentStatus;
  model: string;
  createdAt: string;
  startedAt?: string;
  finishedAt?: string;
  summary?: string;
  error?: string;
  // Delivery mark for the parent transcript: false means the terminal result
  // has not reached the model yet (persisted so a restart retries it);
  // undefined marks legacy records, treated as already delivered.
  delivered?: boolean;
  pendingApprovalCount: number;
  logs: SubagentLogEntry[];
};

export type SubagentTaskPatch = {
  id: string;
  name?: string;
  threadId?: string;
  model?: string;
  pendingApprovalCount?: number;
  appendLog?: SubagentLogEntry;
  patchLog?: SubagentLogPatch;
};

export type SessionSummary = {
  id: string;
  path: string;
  name: string;
  firstMessage: string;
  updatedAt: string;
  archived: boolean;
  profileId?: string;
  provider?: string;
  model?: string;
  contextWindow?: number;
  usage?: ContextUsage;
};

export type ArchivedSession = Omit<SessionSummary, "archived">;

const RIFTX_EVENT_TYPES = [
  "connected", "finding", "findingPatch", "usage", "session_state", "subagent_snapshot",
  "subagent_queued", "subagent_start", "subagent_done", "subagent_empty", "subagent_failed", "subagent_cancelled",
  "subagent_interrupted", "subagent_update", "approval_required", "approval_evaluated",
  "approval_evaluation_error", "approval_decided", "text_delta", "thinking_delta", "message",
  "tool_start", "tool_status", "tool_update", "tool_end", "done", "error"
] as const;

export type RiftxEventType = (typeof RIFTX_EVENT_TYPES)[number];
type RiftxEventFields = {
  sessionId?: string;
  delta?: unknown;
  message?: unknown;
  toolResults?: unknown;
  toolName?: string;
  toolCallId?: string;
  toolStatus?: "queued" | "running";
  args?: unknown;
  update?: unknown;
  result?: unknown;
  isError?: boolean;
  error?: string;
  state?: "running" | "retrying" | "compacting" | "waiting_for_subagents" | "idle";
  attempt?: number;
  reason?: string;
  usage?: ContextUsage;
  finding?: Finding;
  findingPatch?: FindingPatch;
  approval?: ApprovalRequest;
  approvalId?: string;
  approved?: boolean;
  task?: SubagentTask;
  taskPatch?: SubagentTaskPatch;
  subagentId?: string;
};

export type RiftxEvent = RiftxEventFields & { type: RiftxEventType };

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value && typeof value === "object" && !Array.isArray(value));
}

export function parseRiftxEvent(value: unknown): RiftxEvent | null {
  if (!isRecord(value) || typeof value.type !== "string" || !RIFTX_EVENT_TYPES.includes(value.type as RiftxEventType)) return null;
  if (value.sessionId !== undefined && typeof value.sessionId !== "string") return null;
  if (value.type === "connected" && typeof value.sessionId !== "string") return null;
  if (value.type === "usage" && !isRecord(value.usage)) return null;
  if (value.type === "finding" && !isRecord(value.finding)) return null;
  if (value.type === "findingPatch" && !isRecord(value.findingPatch)) return null;
  if (value.type === "approval_required" && !isRecord(value.approval)) return null;
  if (value.type === "approval_decided" && typeof value.approvalId !== "string") return null;
  if ((value.type === "tool_start" || value.type === "tool_status") && value.toolStatus !== undefined && !["queued", "running"].includes(value.toolStatus as string)) return null;
  if (value.type.startsWith("subagent_") && value.type !== "subagent_update" && !isRecord(value.task)) return null;
  if (value.type === "subagent_update" && value.task === undefined && value.taskPatch === undefined) return null;
  if (value.type === "session_state" && !["running", "retrying", "compacting", "waiting_for_subagents", "idle"].includes(value.state as string)) return null;
  return value as RiftxEvent;
}

export type FindingConfidence = "confirmed" | "likely" | "suspected" | "not_reproducible";
export type FindingStatus = "open" | "dismissed";
export type FindingSource = "main" | "subagent";

export type FindingEvidence =
  | { type: "quote"; quote: string }
  | { type: "tool"; toolCallId: string; toolName: string; content?: string }
  | { type: "request"; requestRef: string; method?: string; url?: string; status?: number }
  | { type: "screenshot"; screenshotId: string; url?: string };

export type Finding = {
  id: string;
  title: string;
  asset: string;
  confidence: FindingConfidence;
  status: FindingStatus;
  impact: string;
  reproduction: string;
  evidence: FindingEvidence[];
  source: FindingSource;
  subagentId?: string;
  createdAt: string;
  updatedAt: string;
};

export type FindingInput = Pick<Finding, "title" | "asset" | "confidence" | "impact" | "reproduction" | "evidence">;
export type FindingPatch = { id: string; confidence?: FindingConfidence; status?: FindingStatus; updatedAt?: string };

export type WebSearchConfig = {
  /** Optional Tavily key; absent/empty means the keyless DuckDuckGo default. */
  tavilyApiKey?: string;
};

export type AppConfig = {
  webSearch?: WebSearchConfig;
  profiles: ModelProfile[];
  activeProfileId: string;
  childProfileId: string | null;
  childInherit: boolean;
  cwd: string;
  approvalMode: ApprovalMode;
  archivedSessionIds: string[];
  archivedSessions: ArchivedSession[];
  sessionTitles: Record<string, string>;
  maxConcurrentSubagents: number;
  subagentAggressiveness: SubagentAggressiveness;
  systemPromptEnabled: boolean;
  systemPrompt: string;
  browserScope: string[];
  browserIgnoreTlsErrors: boolean;
};

export const DEFAULT_PROFILE: ModelProfile = {
  id: "default",
  name: "Default model",
  provider: "openai",
  model: "gpt-4o-mini",
  baseUrl: "https://api.openai.com/v1",
  api: "openai-completions",
  transport: "auto",
  contextWindow: 128000,
  maxTokens: 16384,
  thinkingLevel: "off"
};
