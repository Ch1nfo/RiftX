import { API_TYPES, THINKING_LEVELS, TRANSPORTS, type ModelProfile } from "@/lib/types";

const GATEWAY_SUFFIX = ".tsecbench.gw";
const HOSTS = new Set([
  "api.hunyuan.cloud.tencent.com", "api.lkeap.cloud.tencent.com", "tokenhub.tencentmaas.com",
  "api.deepseek.com", "qianfan.baidubce.com", "ark.cn-beijing.volces.com", "open.bigmodel.cn",
  "api.moonshot.cn", "api.siliconflow.cn", "spark-api-open.xf-yun.com", "api.minimaxi.com",
  "api.stepfun.com", "api.lingyiwanwu.com", "api.baichuan-ai.com", "api.xiaomimimo.com",
  "api.kimi.com", "agent-awd.baidu.com"
]);
export const ENV_PROFILE_ID = "benchmark-env";

/** Preserve the provider's API path; only rewrite scheme and hostname. */
export function modelBaseUrl(input: string, hosted: boolean) {
  const url = new URL(input);
  if (!["http:", "https:"].includes(url.protocol) || url.username || url.password || url.search || url.hash) {
    throw new Error("RIFTX_LLM_BASE_URL must be an http(s) API root without credentials, query or fragment");
  }
  if (hosted) {
    const host = url.hostname.endsWith(GATEWAY_SUFFIX) ? url.hostname.slice(0, -GATEWAY_SUFFIX.length) : url.hostname;
    const aliyun = host === "dashscope.aliyuncs.com" || (host.endsWith(".maas.aliyuncs.com") && host !== "maas.aliyuncs.com");
    if (!HOSTS.has(host) && !(aliyun && /^\/compatible-mode(?:\/|$)/.test(url.pathname))) {
      throw new Error("RIFTX_LLM_BASE_URL is outside the TSec model API whitelist");
    }
    if (url.port) throw new Error("Hosted model API URLs must use the default HTTP/HTTPS port");
    url.protocol = "http:";
    url.hostname = `${host}${GATEWAY_SUFFIX}`;
  }
  return url.toString().replace(/\/$/, "");
}

export function positiveInteger(value: string | undefined, fallback: number, name: string) {
  if (value === undefined || value === "") return fallback;
  const parsed = Number(value);
  if (!Number.isSafeInteger(parsed) || parsed <= 0) throw new Error(`${name} must be a positive integer`);
  return parsed;
}

export function benchmarkProfile(env: Record<string, string | undefined> = process.env, child = false): ModelProfile {
  if (child) {
    const merged = { ...env };
    for (const [key, value] of Object.entries(env)) {
      if (key.startsWith("RIFTX_CHILD_LLM_") && value !== undefined) merged[key.replace("RIFTX_CHILD_LLM_", "RIFTX_LLM_")] = value;
    }
    return { ...benchmarkProfile(merged), id: `${ENV_PROFILE_ID}-child`, name: "Benchmark child environment model" };
  }
  for (const name of ["RIFTX_LLM_BASE_URL", "RIFTX_LLM_MODEL", "RIFTX_LLM_API_KEY"] as const) {
    if (!env[name]?.trim()) throw new Error(`${name} is required`);
  }
  const api = env.RIFTX_LLM_API ?? "openai-completions";
  if (!(API_TYPES as readonly string[]).includes(api)) throw new Error("Unsupported RIFTX_LLM_API");
  const thinking = env.RIFTX_LLM_THINKING ?? "off";
  if (!(THINKING_LEVELS as readonly string[]).includes(thinking)) throw new Error("Invalid RIFTX_LLM_THINKING");
  if (env.RIFTX_HOSTED !== undefined && !["0", "1"].includes(env.RIFTX_HOSTED)) throw new Error("RIFTX_HOSTED must be 0 or 1");
  if (env.RIFTX_LLM_IMAGES !== undefined && !["0", "1"].includes(env.RIFTX_LLM_IMAGES)) throw new Error("RIFTX_LLM_IMAGES must be 0 or 1");
  const transport = env.RIFTX_LLM_TRANSPORT ?? "sse";
  if (!(TRANSPORTS as readonly string[]).includes(transport)) throw new Error("Invalid RIFTX_LLM_TRANSPORT");
  const contextWindow = positiveInteger(env.RIFTX_LLM_CONTEXT_WINDOW, 128000, "RIFTX_LLM_CONTEXT_WINDOW");
  const maxTokens = positiveInteger(env.RIFTX_LLM_MAX_TOKENS, 8192, "RIFTX_LLM_MAX_TOKENS");
  if (maxTokens >= contextWindow) throw new Error("RIFTX_LLM_MAX_TOKENS must be smaller than the context window");
  return {
    id: ENV_PROFILE_ID, name: "Benchmark environment model", provider: env.RIFTX_LLM_PROVIDER?.trim() || "riftx-benchmark",
    model: env.RIFTX_LLM_MODEL!.trim(), apiKey: env.RIFTX_LLM_API_KEY!,
    baseUrl: modelBaseUrl(env.RIFTX_LLM_BASE_URL!, env.RIFTX_HOSTED === "1"),
    api: api as ModelProfile["api"], transport: transport as ModelProfile["transport"], contextWindow, maxTokens,
    thinkingLevel: thinking as ModelProfile["thinkingLevel"], supportsImages: env.RIFTX_LLM_IMAGES === "1"
  };
}

export function benchmarkDefaultApproval(env: Record<string, string | undefined> = process.env) {
  return env.BENCHMARK_BASE_URL && env.BENCHMARK_TOKEN ? "full" as const : "request" as const;
}
