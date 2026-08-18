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
  input: number;
  output: number;
  cacheRead: number;
  cacheWrite: number;
  remaining: number;
};

export type ApprovalRequest = {
  id: string;
  toolName: "bash" | "write" | "edit";
  input: unknown;
  createdAt: string;
};

export type SessionSummary = {
  id: string;
  path: string;
  name: string;
  firstMessage: string;
  updatedAt: string;
  archived: boolean;
};

export type ArchivedSession = Omit<SessionSummary, "archived">;

export type RiftxEvent = {
  type: string;
  sessionId?: string;
  [key: string]: unknown;
};

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
