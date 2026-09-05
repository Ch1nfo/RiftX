/** Browser screenshots carry a stable id the chat renders via the screenshot route. */
export function extractScreenshotId(result: unknown) {
  if (!result || typeof result !== "object") return undefined;
  const details = (result as { details?: unknown }).details;
  if (!details || typeof details !== "object") return undefined;
  const screenshotId = (details as { screenshotId?: unknown }).screenshotId;
  return typeof screenshotId === "string" ? screenshotId : undefined;
}

export function summarizeToolResult(result: unknown) {
  if (typeof result === "string") return result;
  if (!result || typeof result !== "object") return JSON.stringify(result ?? "", null, 2);
  const screenshotId = extractScreenshotId(result);
  if (screenshotId) return `Screenshot captured: ${screenshotId}`;
  const content = (result as { content?: unknown[] }).content;
  if (Array.isArray(content)) {
    const text = content
      .map((part) => part && typeof part === "object" && "text" in part ? String((part as { text?: unknown }).text ?? "") : "")
      .filter(Boolean)
      .join("\n")
      .trim();
    if (text) return text;
  }
  return JSON.stringify(result, null, 2);
}
