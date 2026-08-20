export type PromptMode = "prompt" | "steer" | "followUp";

export function resolvePromptMode(mode: PromptMode, isStreaming: boolean): PromptMode {
  return mode === "prompt" && isStreaming ? "steer" : mode;
}

export function isAlreadyProcessingError(message: string) {
  return message.includes("already processing") && message.includes("streamingBehavior");
}
