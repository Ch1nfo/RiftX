export type PromptMode = "prompt" | "steer" | "followUp";

export const PROMPT_MODES = ["prompt", "steer", "followUp"] as const;

export function isPromptMode(value: unknown): value is PromptMode {
  return typeof value === "string" && (PROMPT_MODES as readonly string[]).includes(value);
}

export function resolvePromptMode(mode: PromptMode, isStreaming: boolean): PromptMode {
  if (!isStreaming) return "prompt";
  return mode === "prompt" ? "steer" : mode;
}

export function isAlreadyProcessingError(message: string) {
  return message.includes("already processing") && message.includes("streamingBehavior");
}
