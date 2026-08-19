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
