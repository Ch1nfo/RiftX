function textFromAssistantContent(content: unknown) {
  if (typeof content === "string") return content;
  if (!Array.isArray(content)) return "";
  return content.map((part) => {
    if (!part || typeof part !== "object") return "";
    const value = part as { type?: unknown; text?: unknown };
    return value.type === "text" && typeof value.text === "string" ? value.text : "";
  }).join("");
}

export type ExtractedAssistantResult = {
  summary?: string;
  error?: string;
};

/** Reads the final assistant entry in a persisted session branch. */
export function extractLastAssistantResult(branch: readonly unknown[]): ExtractedAssistantResult {
  for (let index = branch.length - 1; index >= 0; index -= 1) {
    const entry = branch[index];
    if (!entry || typeof entry !== "object") continue;
    const value = entry as { type?: unknown; message?: unknown };
    if (value.type !== "message" || !value.message || typeof value.message !== "object") continue;
    const message = value.message as { role?: unknown; content?: unknown; stopReason?: unknown };
    if (message.role !== "assistant") continue;
    if (message.stopReason === "error") {
      const errorMessage = (message as { errorMessage?: unknown }).errorMessage;
      return { error: typeof errorMessage === "string" && errorMessage.trim() ? errorMessage.trim() : "Child Agent provider request failed." };
    }
    if (message.stopReason === "aborted" || message.stopReason === "toolUse") return {};
    const text = textFromAssistantContent(message.content).trim();
    if (text) return { summary: text };
    return {};
  }
  return {};
}

/** Finds the final non-empty assistant text, if the branch ended with one. */
export function extractLastAssistantText(branch: readonly unknown[]) {
  return extractLastAssistantResult(branch).summary;
}
