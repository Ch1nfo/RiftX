import { completeSimple, type Model } from "@mariozechner/pi-ai";
import type { ModelRegistry } from "@mariozechner/pi-coding-agent";
import { textFromModelContent } from "./text-content";

export const TASK_TITLE_PROMPT = `You create concise session titles for RiftX, an authorized Web security testing assistant.

Given the user's latest task, return exactly one short title in the same language as the task. Describe the main goal, not the full instructions. Keep it between 6 and 32 characters when possible. Do not use Markdown, quotes, prefixes, numbering, or a trailing period. Return title text only.`;

export function normalizeSessionTitle(raw: string) {
  const firstLine = raw.trim().split(/\r?\n/).map((line) => line.trim()).find(Boolean) ?? "";
  const title = firstLine.replace(/^[-*#\d.)\s]+/, "").replace(/^([`'\"]+)|([`'\"]+)$/g, "").trim();
  if (!title) throw new Error("模型没有返回有效任务标题");
  return Array.from(title).slice(0, 32).join("");
}

export async function generateSessionTitle(modelRegistry: ModelRegistry, model: Model<any>, task: string, authFailure: "empty" | "throw" = "throw") {
  const auth = await modelRegistry.getApiKeyAndHeaders(model);
  if (!auth.ok) {
    if (authFailure === "empty") return "";
    throw new Error(auth.error);
  }
  const response = await completeSimple(model, {
    systemPrompt: TASK_TITLE_PROMPT,
    messages: [{ role: "user", content: task, timestamp: Date.now() }]
  }, {
    apiKey: auth.apiKey,
    headers: auth.headers,
    maxTokens: 64,
    temperature: 0,
    timeoutMs: 8_000,
    maxRetries: 1
  });
  return normalizeSessionTitle(textFromModelContent(response.content));
}
