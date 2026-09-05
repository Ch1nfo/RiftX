export type PromptMode = "prompt" | "steer" | "followUp";

const PROMPT_MODES = ["prompt", "steer", "followUp"] as const;

export function isPromptMode(value: unknown): value is PromptMode {
  return typeof value === "string" && (PROMPT_MODES as readonly string[]).includes(value);
}

export function resolvePromptMode(mode: PromptMode, isStreaming: boolean): PromptMode {
  if (!isStreaming) return "prompt";
  return mode === "prompt" ? "steer" : mode;
}

/**
 * Prepare one prompt dispatch without caching a follow-up decision across an
 * async boundary. Prompt dispatches resolved while idle are serialized by the
 * caller; streaming steer dispatches do not await preparation. Follow-ups are
 * the exceptional direct path, so their single mode decision must happen only
 * after preparation has finished.
 */
export async function preparePromptDispatch<T>(
  mode: PromptMode,
  isStreaming: () => boolean,
  prepare: () => Promise<T>,
  unprepared: () => T
): Promise<{ mode: PromptMode; prepared: T }> {
  if (mode === "followUp") {
    const prepared = await prepare();
    return { mode: resolvePromptMode(mode, isStreaming()), prepared };
  }
  const resolvedMode = resolvePromptMode(mode, isStreaming());
  return {
    mode: resolvedMode,
    prepared: resolvedMode === "steer" ? unprepared() : await prepare()
  };
}

export function isAlreadyProcessingError(message: string) {
  return message.includes("already processing") && message.includes("streamingBehavior");
}
