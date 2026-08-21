import type { AgentSession } from "@mariozechner/pi-coding-agent";
import type { ContextUsage } from "@/lib/types";

import { replaceAgentMessages, runAutoCompaction, waitForAgentEvents } from "./pi-internals";

export function shouldCompactBeforeSampling(tokens: number | null | undefined, contextWindow: number, reserveTokens: number) {
  return Number.isFinite(tokens)
    && Number(tokens) > 0
    && Number.isFinite(contextWindow)
    && contextWindow > 0
    && Number(tokens) > Math.max(0, contextWindow - Math.max(0, reserveTokens));
}

export function estimateMessagesContextUsage(messages: readonly unknown[], contextWindow: number): ContextUsage {
  const tokens = messages.reduce<number>((total, message) => total + estimateMessageTokens(message), 0);
  return {
    tokens,
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
  return estimateMessagesContextUsage(session.messages, contextWindow);
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

async function runMidTurnCompaction(session: AgentSession, signal?: AbortSignal) {
  if (signal?.aborted) throw new Error("Mid-turn compaction was cancelled");

  let result: unknown;
  const unsubscribe = session.subscribe((event) => {
    if (event.type === "compaction_end" && event.reason === "threshold") {
      result = event.result;
    }
  });
  const abortCompaction = () => session.abortCompaction();
  if (signal) {
    signal.addEventListener("abort", abortCompaction, { once: true });
  }
  try {
    await runAutoCompaction(session);
  } finally {
    unsubscribe();
    signal?.removeEventListener("abort", abortCompaction);
  }
  if (!result) {
    if (signal?.aborted) throw new Error("Mid-turn compaction was cancelled");
    return false;
  }
  return true;
}

/**
 * Keep Pi's active loop alive while replacing its detached context after a
 * tool turn. The public compact() API aborts the active run, so this uses the
 * SDK's auto-compaction path and keeps the current message array in place.
 */
export function installMidTurnCompaction(session: AgentSession) {
  const agent = session.agent;
  const originalTransform = agent.transformContext;
  let compacting = false;

  agent.transformContext = async (messages, signal) => {
    const transformed = originalTransform ? await originalTransform(messages, signal) : messages;
    if (compacting || signal?.aborted) return transformed;

    const settings = session.settingsManager.getCompactionSettings();
    const contextWindow = session.model?.contextWindow ?? 0;
    const usage = session.getContextUsage();
    if (!settings.enabled || !shouldCompactBeforeSampling(usage?.percent === null ? null : usage?.tokens, contextWindow, settings.reserveTokens)) {
      return transformed;
    }

    // The session file must be settled before compaction reads its branch, but
    // avoid paying this await on ordinary sampling turns far below the limit.
    await waitForAgentEvents(session);
    const settledUsage = session.getContextUsage();
    if (!shouldCompactBeforeSampling(settledUsage?.percent === null ? null : settledUsage?.tokens, contextWindow, settings.reserveTokens)) {
      return transformed;
    }

    compacting = true;
    try {
      const compacted = await runMidTurnCompaction(session, signal);
      if (compacted) replaceAgentMessages(session, messages, session.agent.state.messages);
      return messages;
    } finally {
      compacting = false;
    }
  };
}
