export function summarizeToolResult(result: unknown) {
  if (typeof result === "string") return result;
  if (!result || typeof result !== "object") return JSON.stringify(result ?? "", null, 2);
  const record = result as { details?: Record<string, unknown>; content?: unknown[] };
  const screenshotId = record.details && typeof record.details === "object" && typeof record.details.screenshotId === "string"
    ? record.details.screenshotId
    : undefined;
  if (screenshotId) return `Screenshot captured: ${screenshotId}`;
  if (Array.isArray(record.content)) {
    const text = record.content
      .map((part) => part && typeof part === "object" && "text" in part ? String((part as { text?: unknown }).text ?? "") : "")
      .filter(Boolean)
      .join("\n")
      .trim();
    if (text) return text;
  }
  return JSON.stringify(result, null, 2);
}
