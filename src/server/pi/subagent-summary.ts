import { completeSimple, type Model } from "@mariozechner/pi-ai";
import type { ModelRegistry } from "@mariozechner/pi-coding-agent";
import { textFromModelContent } from "./text-content";

const SUMMARY_PROMPT = `You summarize the result of a completed SubAgent task for RiftX, an authorized Web security testing assistant.

Given the SubAgent's session transcript (assistant text and tool output), produce a concise summary the parent Agent can use directly. State: what was checked, the outcome (vulnerability found / no issue / inconclusive), and key evidence or limitations. Keep it under 300 words. Use the same language as the transcript. Return summary text only — no Markdown headers, no preamble.`;

/**
 * Generate a SubAgent result summary from the session transcript using the
 * lightweight title model (thinking off, short output). Used as a fallback
 * when the main model spent its entire output budget on thinking and the
 * session produced no final text response.
 */
export async function generateSubagentSummary(modelRegistry: ModelRegistry, model: Model<any>, transcript: string): Promise<string> {
  const auth = await modelRegistry.getApiKeyAndHeaders(model);
  if (!auth.ok) throw new Error(auth.error);
  const response = await completeSimple(model, {
    systemPrompt: SUMMARY_PROMPT,
    messages: [{ role: "user", content: transcript.slice(-8_000), timestamp: Date.now() }]
  }, {
    apiKey: auth.apiKey,
    headers: auth.headers,
    maxTokens: 512,
    temperature: 0,
    timeoutMs: 15_000,
    maxRetries: 1
  });
  const summary = textFromModelContent(response.content).trim();
  if (!summary) throw new Error("Summary model returned empty output.");
  return summary;
}
