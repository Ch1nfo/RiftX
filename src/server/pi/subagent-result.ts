import { textFromContent } from "./text-content";

export type ExtractedAssistantResult = {
  summary?: string;
  error?: string;
};

/**
 * Reads the final assistant result from a persisted session branch.
 *
 * Scans backwards past toolUse entries (the model's text response follows
 * its last tool call, so an earlier toolUse stopReason must not terminate
 * the scan) and picks the most recent assistant entry that actually carries
 * text. A very long thinking phase can fill the context, trigger mid-turn
 * compaction, or leave the last entry as a thinking-only assistant message —
 * in all those cases an earlier text-bearing entry is the correct result.
 */
export function extractLastAssistantResult(branch: readonly unknown[]): ExtractedAssistantResult {
  for (let index = branch.length - 1; index >= 0; index -= 1) {
    const entry = branch[index];
    if (!entry || typeof entry !== "object") continue;
    const value = entry as { type?: unknown; message?: unknown };
    if (value.type !== "message" || !value.message || typeof value.message !== "object") continue;
    const message = value.message as { role?: unknown; content?: unknown; stopReason?: unknown };
    if (message.role !== "assistant") continue;

    // A provider error is terminal: surface it.
    if (message.stopReason === "error") {
      const errorMessage = (message as { errorMessage?: unknown }).errorMessage;
      return { error: typeof errorMessage === "string" && errorMessage.trim() ? errorMessage.trim() : "SubAgent provider request failed." };
    }

    // Aborted: no usable result.
    if (message.stopReason === "aborted") return {};

    // Only "stop" (normal completion) and "length" (output truncated but
    // text exists) carry a valid final response. Everything else — toolUse
    // (preamble text, not the result), unknown/missing stopReason — is
    // skipped to avoid misattributing intermediate output as the result.
    if (message.stopReason !== "stop" && message.stopReason !== "length") continue;

    const text = textFromContent(message.content).trim();
    if (text) return { summary: text };
  }
  return {};
}

/**
 * Build a summary-model transcript from a session branch. Includes both
 * assistant text and toolResult content — the actual evidence lives in
 * tool results, not in the model's plans. Tool results are truncated
 * per-entry and labeled with the tool name; sensitive values inside them
 * are bounded by the same truncation.
 */
export function buildSummaryTranscript(branch: readonly unknown[]): string {
  return branch
    .map((entry) => {
      if (!entry || typeof entry !== "object") return "";
      const value = entry as { type?: unknown; message?: { role?: unknown; content?: unknown; toolName?: unknown; isError?: unknown } };
      if (value.type !== "message" || !value.message) return "";
      const msg = value.message;
      if (msg.role === "assistant") return textFromContent(msg.content, { separator: " " });
      if (msg.role === "toolResult") {
        const toolName = typeof msg.toolName === "string" ? msg.toolName : "tool";
        const isError = msg.isError ? " (error)" : "";
        const text = textFromContent(msg.content, { separator: " " }).slice(0, 500);
        return text ? `[${toolName}${isError}] ${text}` : "";
      }
      return "";
    })
    .filter(Boolean)
    .join("\n");
}
