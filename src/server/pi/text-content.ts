export function textFromModelContent(content: unknown): string {
  if (typeof content === "string") return content;
  if (!Array.isArray(content)) return JSON.stringify(content ?? "", null, 2);
  return content.map((part) => {
    if (typeof part === "string") return part;
    if (!part || typeof part !== "object") return "";
    if ("text" in part) return String((part as { text?: unknown }).text ?? "");
    if ("thinking" in part) return String((part as { thinking?: unknown }).thinking ?? "");
    return "";
  }).join("");
}

/**
 * Extract readable text from persisted message content (assistant text or
 * tool results): strings pass through, arrays keep string and text-bearing
 * parts. Use `separator` when parts must stay visually delimited (e.g.
 * transcript lines).
 */
export function textFromContent(content: unknown, options?: { separator?: string }): string {
  if (typeof content === "string") return content;
  if (!Array.isArray(content)) return "";
  return content.map((part) => {
    if (typeof part === "string") return part;
    if (!part || typeof part !== "object") return "";
    if ("text" in part) return String((part as { text?: unknown }).text ?? "");
    return "";
  }).join(options?.separator ?? "");
}
