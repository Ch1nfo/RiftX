export const COLLABORATION_PROTOCOL = 1;
export const COLLABORATION_MARKER = "riftx_collaboration";
export const BOARD_CONTEXT_TYPE = "riftx_board_context";
export const BOARD_MESSAGE_TYPE = "riftx_board_message";
export const DEFAULT_BOARD_LIMITS = { tasks: 32, wakes: 60, retries: 2, messages: 256 };
export type BoardLimits = typeof DEFAULT_BOARD_LIMITS;
export type WorkStatus = "proposed" | "ready" | "running" | "awaiting_review" | "done" | "blocked" | "failed" | "cancelled" | "rejected";
export type BoardReference = { type: "finding" | "request" | "screenshot" | "artifact" | "tool"; id: string };
export type BoardWork = {
  id: string; objective: string; acceptance: string; priority: number; dependencies: string[]; assets: string[];
  status: WorkStatus; version: number; createdAt: number; updatedAt: number; author: string;
  owner?: string; attemptId?: string; blockedReason?: string; summary?: string; references: BoardReference[];
  attempts: number; retries: number; retryPending?: boolean; admittedRun?: string; admitted: boolean; paused?: boolean; assignedTo?: string;
};
export type BoardAgent = {
  id: string; name: string; role: "main" | "child"; status: "idle" | "running" | "paused" | "sleeping";
  transcript?: string; model?: string; taskId?: string; lastWakeRevision: number; lastActive: number;
  pendingApprovalCount: number; resourcesReleased?: boolean; executionOpen?: boolean; executionId?: string; attentionSeen?: string;
};
export type BoardAttempt = {
  id: string; taskId: string; agentId: string; epoch: number; startedAt: number; leaseUntil: number;
  status: "running" | "settled" | "uncertain"; finishedAt?: number; reason?: string;
};
export type BoardMessage = {
  id: string; from: string; to: string; kind: "information" | "question" | "answer" | "task_update";
  body: string; taskId?: string; replyTo?: string; createdAt: number; status: "queued" | "in_context" | "replied";
};
export type BoardNote = {
  id: string; author: string; kind: "observation" | "hypothesis" | "ruled_out" | "conflict" | "finding";
  body: string; taskId?: string; assets: string[]; references: BoardReference[]; createdAt: number;
};
export type BoardEvent = { seq: number; type: string; actor: string; entityId?: string; entityVersion?: number; createdAt: number };
export type BoardState = {
  protocol: 1; sessionId: string; revision: number; runId: string; epoch: number;
  status: "running" | "paused" | "waiting_user" | "completed"; reason?: string; stopping?: string[];
  limits: BoardLimits; used: { tasks: number; wakes: number; messages: number }; maxConcurrent: number;
  agents: BoardAgent[]; tasks: BoardWork[]; attempts: BoardAttempt[]; messages: BoardMessage[]; notes: BoardNote[];
  findingVersions: Record<string, string>;
};
export type BoardSnapshot = { mode: "shared"; state: BoardState; events: BoardEvent[]; nextCursor: number; hasMore: boolean }
  | { mode: "legacy" };
export type BoardFence = { epoch: number; attemptId?: string; executionId?: string };
export type BoardOperation = "create" | "propose" | "manage" | "claim" | "update" | "publish" | "message" | "finish"
  | "control" | "agent_control" | "register_agent" | "agent_meta" | "wake" | "settle" | "delivered" | "heartbeat" | "finding" | "begin_run" | "user_turn";
