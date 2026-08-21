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

export type SubagentStatus = "queued" | "running" | "completed" | "failed" | "cancelled" | "interrupted";

export type SubagentLogEntry = {
  id: string;
  type: "thinking" | "tool" | "text" | "error";
  content: string;
  toolName?: string;
  status?: "running" | "done" | "error";
  createdAt: string;
};

export type SubagentLogPatch = {
  id: string;
  content?: string;
  appendContent?: string;
  status?: "running" | "done" | "error";
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

export type RiftxEvent = {
  type: string;
  sessionId?: string;
  [key: string]: unknown;
};

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

export type AppConfig = {
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
