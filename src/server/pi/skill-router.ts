import { readFile } from "node:fs/promises";
import { isExplicitReportRequest, PENTEST_REPORT_SKILL_NAME } from "./report-skill";

export type SkillDescriptor = {
  name: string;
  description: string;
  filePath: string;
  baseDir?: string;
  disableModelInvocation?: boolean;
};

type SkillMatch = SkillDescriptor & { score: number; matchedTerms: string[] };

const STOP_WORDS = new Set([
  "a", "an", "and", "are", "as", "at", "check", "for", "from", "in", "is", "of", "on", "or", "test", "testing", "the", "to", "use", "with",
  "一个", "一下", "进行", "检查", "测试", "使用", "需要", "漏洞", "安全", "应用", "网站", "网页", "系统",
  "benchmark", "ctf", "challenge", "flag", "task", "solve", "solving", "find", "all", "tool", "file", "code", "analysis", "analyze",
  "题目", "解题", "找到", "分析", "文件", "工具", "代码"
]);

// Coarse metadata-only compatibility guard, not a semantic relevance guarantee.
// A shared word such as "password" must not send a Web-only skill to an ELF task.
const DOMAIN_TERMS = [
  ["web", "html", "browser", "sqli", "sql injection", "sql注入", "sql 注入", "xss", "ssrf", "idor", "graphql", "csrf", "web应用", "网站", "网页"],
  ["reverse", "reversing", "re", "binary", "elf", "disassembly", "decompile", "decompilation", "ghidra", "ida", "pwn", "逆向", "反编译", "二进制"],
  ["crypto", "cryptography", "rsa", "aes", "密码学"],
  ["forensics", "steganography", "stego", "pcap", "取证", "隐写"]
];

function domains(text: string) {
  const normalized = text.toLowerCase();
  const words = new Set(normalized.match(/[a-z0-9]+/g) ?? []);
  return DOMAIN_TERMS.flatMap((markers, index) => markers.some((marker) => /[^a-z]/.test(marker)
    ? normalized.includes(marker) : words.has(marker)) ? [index] : []);
}

const TERM_ALIASES: Record<string, string[]> = {
  "sql": ["sqli"],
  "sqli": ["sql", "injection"],
  "cross-site": ["xss"],
  "xss": ["cross-site", "script"],
  "lfi": ["file", "include"],
  "注入": ["injection"],
  "跨站脚本": ["xss", "cross-site"],
  "文件包含": ["lfi", "file", "include"],
  "文件下载": ["download", "file"],
  "路径遍历": ["traversal", "path"],
  "目录": ["directory", "dir"],
  "子域": ["subdomain"],
  "域名": ["domain", "dns"],
  "端口": ["port"],
  "指纹": ["fingerprint"],
  "技术栈": ["technology", "fingerprint"],
  "报告": ["report"],
  "密码": ["password"],
  "用户名": ["username"],
  "模糊测试": ["fuzz", "fuzzing"],
  "载荷": ["payload"],
  "提示词": ["prompt"],
  "大模型": ["llm", "model"],
  "越权": ["authz", "idor", "authorization"],
  "权限": ["authz", "authorization", "privilege"],
  "上传": ["upload"],
  "接口": ["api", "endpoint"],
  "api": ["接口"],
  "ssrf": ["server", "side", "forgery"],
  "jwt": ["token", "api"],
  "graphql": ["api"],
  "爬取": ["crawl", "spider"],
  "攻击面": ["attack", "surface", "crawl"],
  // Benchmark domain bridges: challenge descriptions are often Chinese
  // while the benchmark-* skill descriptions are English keyword lists.
  "逆向": ["reverse", "reversing"],
  "反编译": ["decompile", "decompilation"],
  "密码学": ["crypto", "cryptography"],
  "取证": ["forensics"],
  "隐写": ["steganography", "stego"],
  "音频": ["audio"],
  "解码": ["decode", "decoding"],
  "溢出": ["overflow"],
  "逃逸": ["escape", "jail"]
};

function expandAliases(term: string) {
  // hasOwn guard: a description term like "constructor" (prototype-pollution
  // keyword) would otherwise resolve to Object.prototype's inherited function
  // — truthy but not spreadable, crashing rankSkills.
  const aliases = Object.hasOwn(TERM_ALIASES, term) ? TERM_ALIASES[term] : [];
  return [term, ...aliases];
}

function terms(text: string) {
  const normalized = text.toLocaleLowerCase();
  const words = normalized.match(/[a-z0-9]+/g) ?? [];
  // Naive plural stemming: benchmark skill descriptions mix "pyjail"/"pyjails",
  // "puzzle"/"puzzles" \u2014 emitting both forms lets either side match.
  const singulars = words.filter((word) => word.length > 3 && word.endsWith("s")).map((word) => word.slice(0, -1));
  const cjk = normalized.match(/[\u3400-\u9fff]/g) ?? [];
  const bigrams = cjk.slice(0, -1).map((char, index) => `${char}${cjk[index + 1]}`);
  const expanded = [...words, ...singulars, ...cjk, ...bigrams].flatMap(expandAliases);
  return [...new Set(expanded.filter((term) => term.length > 1 && !STOP_WORDS.has(term)
    && !(term.endsWith("s") && STOP_WORDS.has(term.slice(0, -1)))))];
}

function searchableText(skill: SkillDescriptor) {
  return `${skill.name.replace(/[-_]/g, " ")} ${skill.description}`.toLocaleLowerCase();
}

export function rankSkills(task: string, skills: readonly SkillDescriptor[], limit = 3): SkillMatch[] {
  const queryTerms = terms(task);
  if (queryTerms.length === 0) return [];
  const taskDomains = domains(task);
  // Short single-concept queries ("竞态", "IDOR") can only match 1-2 terms;
  // the full cutoff of 4 would leave them without any auto-loaded skill.
  const cutoff = queryTerms.length > 3 ? 4 : 2;
  return skills
    .filter((skill) => !skill.disableModelInvocation)
    .filter((skill) => {
      const skillDomains = domains(searchableText(skill));
      return taskDomains.length === 0 || skillDomains.length === 0
        || taskDomains.some((domain) => skillDomains.includes(domain));
    })
    .map((skill) => {
      const nameTerms = terms(skill.name.replace(/[-_]/g, " "));
      const searchableTerms = new Set(terms(searchableText(skill)));
      const matchedTerms = queryTerms.filter((term) => searchableTerms.has(term));
      const score = matchedTerms.reduce((total, term) => total + (nameTerms.includes(term) ? 5 : 2), 0)
        + (matchedTerms.length > 1 && queryTerms.every((term) => searchableTerms.has(term)) ? 3 : 0);
      return { ...skill, score, matchedTerms };
    })
    .filter((skill) => skill.score >= cutoff)
    .sort((left, right) => right.score - left.score || left.name.localeCompare(right.name))
    .slice(0, limit);
}

function stripFrontmatter(content: string) {
  return content.replace(/^---\r?\n[\s\S]*?\r?\n---\r?\n?/, "").trim();
}

function escapeXml(value: string) {
  return value.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;").replace(/'/g, "&apos;");
}

export async function loadSkillContext(skill: SkillDescriptor) {
  const raw = await readFile(skill.filePath, "utf8");
  const body = stripFrontmatter(raw);
  const location = escapeXml(skill.filePath);
  const baseDir = escapeXml(skill.baseDir ?? skill.filePath.replace(/[\\/][^\\/]*$/, ""));
  return `<skill name="${escapeXml(skill.name)}" location="${location}">\nReferences are relative to ${baseDir}.\n\n${body}\n</skill>`;
}

function unescapeXml(value: string) {
  return value.replace(/&quot;/g, '"').replace(/&apos;/g, "'").replace(/&gt;/g, ">").replace(/&lt;/g, "<").replace(/&amp;/g, "&");
}

/** Recover active skill names from the newest RiftX compaction metadata or historic skill message. */
export function activeSkillNamesFromBranch(entries: readonly unknown[]) {
  for (let index = entries.length - 1; index >= 0; index -= 1) {
    const entry = entries[index] as { type?: unknown; details?: unknown; customType?: unknown; content?: unknown } | undefined;
    if (entry?.type === "compaction" && entry.details && typeof entry.details === "object") {
      const active = (entry.details as { riftx?: { activeSkills?: unknown } }).riftx?.activeSkills;
      if (Array.isArray(active)) return active.filter((name): name is string => typeof name === "string");
    }
    if (entry?.type === "custom_message" && entry.customType === "riftx_skill_context" && typeof entry.content === "string") {
      return [...entry.content.matchAll(/<skill\s+name="([^"]+)"/g)].map((match) => unescapeXml(match[1]));
    }
  }
  return [];
}

export async function prepareSkillPrompt(task: string, skills: readonly SkillDescriptor[], loadedSkills: Set<string>) {
  // A new selection may explicitly be empty. Only a bare continuation preserves
  // the prior selection; abstention must not keep injecting an unrelated skill.
  if (!task.trim() || task.trimStart().startsWith("/skill:") || /^(?:继续|接着|继续吧|继续执行|continue|go on|keep going|proceed)[.!！。\s]*$/i.test(task.trim())) {
    return { prompt: task, skillContext: "", loaded: [] as string[], matched: [] as string[], resetActiveSkills: false };
  }
  // The report skill is opt-in. It stays hidden from the general model/router
  // catalog and is selected only for an explicit report request.
  const explicitReportSkill = isExplicitReportRequest(task)
    ? skills.find((skill) => skill.name === PENTEST_REPORT_SKILL_NAME)
    : undefined;
  const matches: readonly SkillDescriptor[] = explicitReportSkill ? [explicitReportSkill] : rankSkills(task, skills, 1);
  const matched = matches.map((skill) => skill.name);
  const selected = matches.filter((skill) => !loadedSkills.has(skill.name));
  if (selected.length === 0) return { prompt: task, skillContext: "", loaded: [] as string[], matched, resetActiveSkills: true };
  const loaded = await Promise.all(selected.map(async (skill) => {
    try {
      return { skill, context: await loadSkillContext(skill) };
    } catch {
      return null;
    }
  }));
  const successful = loaded.filter((item): item is { skill: SkillMatch; context: string } => Boolean(item));
  if (successful.length === 0) return { prompt: task, skillContext: "", loaded: [] as string[], matched: [], resetActiveSkills: true };
  // Report guidance is scoped to a single explicit request, so allow it to be
  // injected again for a later explicit report rather than treating it as a
  // permanent session capability.
  successful.forEach(({ skill }) => {
    if (skill.name !== PENTEST_REPORT_SKILL_NAME) loadedSkills.add(skill.name);
  });
  const skillContext = successful.map(({ context }) => context).join("\n\n");
  return {
    prompt: `${skillContext}\n\nUser task:\n${task}`,
    skillContext,
    loaded: successful.map(({ skill }) => skill.name),
    matched: successful.map(({ skill }) => skill.name),
    resetActiveSkills: true
  };
}

export function updateActiveSkills(active: Set<string>, selection: { matched: string[]; resetActiveSkills: boolean }) {
  if (!selection.resetActiveSkills) return;
  active.clear();
  selection.matched.filter((name) => name !== PENTEST_REPORT_SKILL_NAME).forEach((name) => active.add(name));
}
