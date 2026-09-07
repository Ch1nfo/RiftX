import type { AgentSession } from "@mariozechner/pi-coding-agent";

export const PENTEST_REPORT_SKILL_NAME = "pentest-report";

const REPORT_SKILL_MARKER = `<skill name="${PENTEST_REPORT_SKILL_NAME}"`;
const REPORT_NOUN = /(?:渗透测试|安全测试|漏洞|安全评估|评估|测试)?报告|\breport\b/iu;
const REPORT_NEGATION = /(?:不要|不用|无需|不需要|不必|别|禁止|停止|取消|避免).{0,20}(?:报告|\breport\b)|\b(?:do not|don't|dont|no need to|without|stop|avoid)\b.{0,80}\breport\b/iu;
const REPORT_DISCUSSION = /(?:为什么|为何|原因|如何|怎么(?:会|总是)?|是否|会不会).{0,32}(?:生成|撰写|写|制作|输出|保存)?.{0,16}(?:报告|\breport\b)|\b(?:why|how|whether)\b.{0,80}\breport\b/iu;
const CHINESE_DIRECT_REQUEST = /^(?:请)?(?:生成|撰写|写|制作|做|出|输出|导出|保存|整理|形成).{0,24}(?:渗透测试|安全测试|漏洞|安全评估|评估|测试)?报告|(?:请|帮我|麻烦).{0,16}(?:生成|撰写|写|制作|做|出|输出|导出|保存|整理|形成).{0,24}(?:渗透测试|安全测试|漏洞|安全评估|评估|测试)?报告|(?:给我|我要|我想要|我需要).{0,12}(?:一份|一个|正式|完整)?.{0,8}(?:渗透测试|安全测试|漏洞|安全评估|评估|测试)?报告/iu;
const CHINESE_FORMAT_REQUEST = /(?:请|帮我|给我|我要|我需要|^).{0,12}(?:按|按照).{0,24}(?:报告|模板).{0,12}(?:格式|生成|输出)/iu;
const ENGLISH_DIRECT_REQUEST = /^(?:please\s+)?(?:write|generate|create|produce|provide|draft|prepare|export|save)\b.{0,80}\breport\b|\b(?:please|can you|could you)\b.{0,24}(?:write|generate|create|produce|provide|draft|prepare|export|save)\b.{0,80}\breport\b|\b(?:give me|i (?:want|need)(?: you to)?)\b.{0,24}\breport\b/iu;

/** Report instructions are intentionally opt-in; mentioning or discussing reports is not enough. */
export function isExplicitReportRequest(task: string) {
  const normalized = task.trim();
  if (!normalized || !REPORT_NOUN.test(normalized) || REPORT_NEGATION.test(normalized) || REPORT_DISCUSSION.test(normalized)) return false;
  return CHINESE_DIRECT_REQUEST.test(normalized)
    || CHINESE_FORMAT_REQUEST.test(normalized)
    || ENGLISH_DIRECT_REQUEST.test(normalized);
}

function userMessageText(message: unknown) {
  const candidate = message as { role?: unknown; content?: unknown };
  if (candidate?.role !== "user") return "";
  if (typeof candidate.content === "string") return candidate.content;
  if (!Array.isArray(candidate.content)) return "";
  return candidate.content
    .map((part) => part && typeof part === "object" && (part as { type?: unknown }).type === "text"
      ? String((part as { text?: unknown }).text ?? "")
      : "")
    .join("\n");
}

function isReportSkillContext(message: unknown) {
  const candidate = message as { role?: unknown; customType?: unknown; content?: unknown };
  return candidate?.role === "custom"
    && candidate.customType === "riftx_skill_context"
    && typeof candidate.content === "string"
    && candidate.content.includes(REPORT_SKILL_MARKER);
}

/**
 * Keep an explicitly requested report skill active for that Agent turn only.
 * This also neutralizes report skill messages persisted by older RiftX builds.
 */
export function scopeReportSkillContext<T>(messages: readonly T[]): readonly T[] {
  const currentTask = [...messages].reverse().map(userMessageText).find(Boolean) ?? "";
  if (isExplicitReportRequest(currentTask)) return messages;
  const filtered = messages.filter((message) => !isReportSkillContext(message));
  return filtered.length === messages.length ? messages : filtered;
}

export function installReportSkillContextScope(session: AgentSession) {
  const agent = session.agent;
  const originalTransform = agent.transformContext;
  agent.transformContext = async (messages, signal) => {
    const transformed = originalTransform ? await originalTransform(messages, signal) : messages;
    return scopeReportSkillContext(transformed) as typeof transformed;
  };
}
