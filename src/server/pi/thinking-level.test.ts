import assert from "node:assert/strict";
import test from "node:test";
import { join } from "node:path";
import { pathToFileURL } from "node:url";
import { clampThinkingLevel, completeSimple } from "@mariozechner/pi-ai";
import { THINKING_LEVELS, type ModelProfile } from "@/lib/types";
import { registerProfileModel, sdkThinkingLevel } from "./model-registration";

// Exercise the real SDK registry, clamping and request builders without making a network request.
test("all configured thinking levels survive Pi clamping and reach OpenAI request payloads", async () => {
  const { AuthStorage, ModelRegistry } = await import(
    pathToFileURL(join(process.cwd(), "node_modules/@mariozechner/pi-coding-agent/dist/index.js")).href
  ) as typeof import("@mariozechner/pi-coding-agent");
  const auth = AuthStorage.inMemory();
  const registry = ModelRegistry.inMemory(auth);
  for (const api of ["openai-completions", "openai-responses"] as const) {
    for (const thinkingLevel of THINKING_LEVELS) {
      const profile: ModelProfile = {
        id: "test", name: "test", provider: "openai", model: "gpt-test",
        baseUrl: "https://api.openai.com/v1", apiKey: "test-key", api, transport: "sse",
        contextWindow: 128000, maxTokens: 8192, thinkingLevel
      };
      const model = registerProfileModel(auth, registry, profile, true);
      const level = sdkThinkingLevel(thinkingLevel);
      assert.equal(clampThinkingLevel(model, level), level);
      let payload: { reasoning_effort?: string; reasoning?: { effort: string } } | undefined;
      const result = await completeSimple(model, {
        messages: [{ role: "user", content: "Hello", timestamp: 0 }]
      }, {
        apiKey: "test-key",
        ...(level !== "off" ? { reasoning: level } : {}),
        onPayload(value) {
          payload = value as typeof payload;
          throw new Error("request captured before network");
        }
      });
      assert.match(result.errorMessage ?? "", /request captured before network/);
      assert.ok(payload);
      const effort = api === "openai-completions" ? payload.reasoning_effort : payload.reasoning?.effort;
      assert.equal(effort, thinkingLevel === "off" ? undefined : thinkingLevel, `${api}: ${thinkingLevel}`);
    }
  }
});
