import type { AgentSession } from "@mariozechner/pi-coding-agent";
import type { ContextUsage } from "@/lib/types";
import { isContinuityMessage } from "./continuity-context";

export function estimateMessagesContextUsage(messages: readonly unknown[], contextWindow: number): ContextUsage {
  const tokens = messages.reduce<number>((total, message) => total + estimateMessageTokens(message), 0);
  return {
    tokens,
    source: "estimated",
    contextWindow,
    percent: contextWindow > 0 ? Math.min(100, (tokens / contextWindow) * 100) : null,
    input: null,
    output: null,
    cacheRead: null,
    cacheWrite: null,
    remaining: Math.max(0, contextWindow - tokens)
  };
}

export function estimateCompactedUsage(session: AgentSession, contextWindow: number): ContextUsage {
  const messageTokens = estimateMessagesContextUsage(session.messages, contextWindow).tokens;
  const staticTokens = estimateStaticContextTokens(session);
  const ratio = contextTokenRatio(session);
  const saved = savedContextBudget(session);
  // Persisted fixedTokens represents the static prompt/tool baseline used by
  // file based snapshots. Prefer the larger of the live estimate and persisted
  // value to avoid double counting static context after compaction.
  const fixedTokens = Math.max(staticTokens * ratio, Number.isFinite(saved?.fixedTokens) ? Math.max(0, saved!.fixedTokens!) : 0);
  const currentContinuity = estimateMessagesContextUsage(session.messages.filter(isContinuityMessage), 0).tokens * ratio;
  // compaction_end fires before the async continuity restore. Include its
  // already-budgeted packet during that gap instead of displaying a low value.
  const pendingContinuity = saved?.modelKey === contextModelKey(session) && Number.isFinite(saved.continuityTokens)
    ? Math.max(0, saved.continuityTokens! - currentContinuity) : 0;
  return usageEstimate(Math.ceil(messageTokens * ratio + fixedTokens + pendingContinuity), contextWindow);
}

function estimateMessageTokens(message: unknown) {
  const value = message as { role?: string; content?: unknown; command?: string; output?: string; summary?: string };
  let characters = (value.command ?? "").length + (value.output ?? "").length;
  if (value.role === "compactionSummary" || value.role === "branchSummary") characters += (value.summary ?? "").length;
  if (typeof value.content === "string") characters += value.content.length;
  else if (Array.isArray(value.content)) {
    for (const part of value.content) {
      if (!part || typeof part !== "object") continue;
      const item = part as { type?: string; text?: string; thinking?: string; name?: string; arguments?: unknown };
      if (item.type === "text") characters += item.text?.length ?? 0;
      else if (item.type === "thinking") characters += item.thinking?.length ?? 0;
      else if (item.type === "toolCall") characters += (item.name?.length ?? 0) + JSON.stringify(item.arguments ?? {}).length;
      else if (item.type === "image") characters += 4800;
    }
  }
  return Math.ceil(characters / 4);
}

export function usageEstimate(tokens: number, contextWindow: number): ContextUsage {
  return { tokens, contextWindow, source: "estimated", percent: contextWindow > 0 ? Math.min(100, tokens / contextWindow * 100) : null,
    input: null, output: null, cacheRead: null, cacheWrite: null, remaining: Math.max(0, contextWindow - tokens) };
}

export function estimateStaticContextTokens(session: AgentSession) {
  const { systemPrompt = "", tools = [] } = session.agent.state;
  const definitions = tools.map(({ name, description, parameters }) => ({ name, description, parameters }));
  return 64 + Math.ceil((systemPrompt.length + JSON.stringify(definitions).length) / 4);
}

export function contextModelKey(session: AgentSession) {
  const model = session.model;
  return model ? `${model.provider}/${model.id}/${model.contextWindow}` : "";
}

type Sample = { modelKey: string; ratio: number; inputEstimate: number; assistantKey: string };
const samples = new WeakMap<AgentSession, Sample>();

function latestUsage(session: AgentSession) {
  for (const message of [...session.messages].reverse()) {
    if (message.role === "assistant" && message.stopReason !== "error" && message.stopReason !== "aborted") {
      const input = message.usage.input + message.usage.cacheRead + message.usage.cacheWrite;
      return { input, key: `${message.timestamp}/${JSON.stringify(message.usage)}` };
    }
  }
  return { input: 0, key: "" };
}

/** Calibrate against the actual preceding request, not against history that has
 * since grown or been cut. Model changes discard the old calibration. */
export function contextTokenRatio(session: AgentSession): number {
  const modelKey = contextModelKey(session);
  const sample = samples.get(session);
  if (sample?.modelKey === modelKey) {
    const usage = latestUsage(session);
    if (usage.key !== sample.assistantKey && usage.input > 0 && sample.inputEstimate > 0) {
      sample.ratio = Math.max(1, usage.input / sample.inputEstimate);
      sample.assistantKey = usage.key;
    }
    return sample.ratio;
  }
  const saved = savedContextBudget(session);
  return saved?.modelKey === modelKey && Number.isFinite(saved.tokenRatio) ? Math.max(1, saved.tokenRatio!) : 1;
}

function savedContextBudget(session: AgentSession) {
  const entries = session.sessionManager?.getBranch?.() ?? [];
  const entry = [...entries].reverse().find((entry) => entry.type === "compaction");
  const details = (entry?.type === "compaction" ? entry.details : undefined) as {
    riftx?: { budget?: { modelKey?: string; tokenRatio?: number; fixedTokens?: number; continuityTokens?: number } }
  } | undefined;
  return details?.riftx?.budget;
}

/** Install after other transforms so calibration includes the continuity and
 * skill filtering of the request actually returned to the provider. */
export function installContextUsageTracking(session: AgentSession) {
  const original = session.agent.transformContext;
  session.agent.transformContext = async (messages, signal) => {
    const ratio = contextTokenRatio(session);
    const transformed = original ? await original(messages, signal) : messages;
    if (!signal?.aborted) samples.set(session, {
      modelKey: contextModelKey(session), ratio, assistantKey: latestUsage(session).key,
      inputEstimate: estimateStaticContextTokens(session) + estimateMessagesContextUsage(transformed, 0).tokens
    });
    return transformed;
  };
}
