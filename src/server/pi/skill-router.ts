import { readFile } from "node:fs/promises";

export type SkillDescriptor = {
  name: string;
  description: string;
  filePath: string;
  baseDir?: string;
  disableModelInvocation?: boolean;
};

export type SkillMatch = SkillDescriptor & { score: number; matchedTerms: string[] };

const STOP_WORDS = new Set([
  "a", "an", "and", "are", "as", "at", "check", "for", "from", "in", "is", "of", "on", "or", "test", "testing", "the", "to", "use", "with",
  "一个", "一下", "进行", "检查", "测试", "使用", "需要", "漏洞", "安全", "应用", "网站", "网页", "系统"
]);

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
  "大模型": ["llm", "model"]
};

function expandAliases(term: string) {
  return [term, ...(TERM_ALIASES[term] ?? [])];
}

function terms(text: string) {
  const normalized = text.toLocaleLowerCase();
  const words = normalized.match(/[a-z0-9]+/g) ?? [];
  const cjk = normalized.match(/[\u3400-\u9fff]/g) ?? [];
  const bigrams = cjk.slice(0, -1).map((char, index) => `${char}${cjk[index + 1]}`);
  const expanded = [...words, ...cjk, ...bigrams].flatMap(expandAliases);
  return [...new Set(expanded.filter((term) => term.length > 1 && !STOP_WORDS.has(term)))];
}

function searchableText(skill: SkillDescriptor) {
  return `${skill.name.replace(/[-_]/g, " ")} ${skill.description}`.toLocaleLowerCase();
}

export function rankSkills(task: string, skills: readonly SkillDescriptor[], limit = 3): SkillMatch[] {
  const queryTerms = terms(task);
  if (queryTerms.length === 0) return [];
  return skills
    .filter((skill) => !skill.disableModelInvocation)
    .map((skill) => {
      const nameTerms = terms(skill.name.replace(/[-_]/g, " "));
      const searchableTerms = new Set(terms(searchableText(skill)));
      const matchedTerms = queryTerms.filter((term) => searchableTerms.has(term));
      const score = matchedTerms.reduce((total, term) => total + (nameTerms.includes(term) ? 5 : 2), 0)
        + (matchedTerms.length > 1 && queryTerms.every((term) => searchableTerms.has(term)) ? 3 : 0);
      return { ...skill, score, matchedTerms };
    })
    .filter((skill) => skill.score >= 4)
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

export async function prepareSkillPrompt(task: string, skills: readonly SkillDescriptor[], loadedSkills: Set<string>) {
  if (!task.trim() || task.trimStart().startsWith("/skill:")) return { prompt: task, skillContext: "", loaded: [] as string[] };
  const matches = rankSkills(task, skills, 1);
  const selected = matches.filter((skill) => !loadedSkills.has(skill.name));
  if (selected.length === 0) return { prompt: task, skillContext: "", loaded: [] as string[] };
  const loaded = await Promise.all(selected.map(async (skill) => {
    try {
      return { skill, context: await loadSkillContext(skill) };
    } catch {
      return null;
    }
  }));
  const successful = loaded.filter((item): item is { skill: SkillMatch; context: string } => Boolean(item));
  if (successful.length === 0) return { prompt: task, skillContext: "", loaded: [] as string[] };
  successful.forEach(({ skill }) => loadedSkills.add(skill.name));
  const skillContext = successful.map(({ context }) => context).join("\n\n");
  return {
    prompt: `${skillContext}\n\nUser task:\n${task}`,
    skillContext,
    loaded: successful.map(({ skill }) => skill.name)
  };
}
