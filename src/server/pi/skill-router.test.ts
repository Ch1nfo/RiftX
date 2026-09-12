import assert from "node:assert/strict";
import { mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { activeSkillNamesFromBranch, loadSkillContext, prepareSkillPrompt, rankSkills, updateActiveSkills, type SkillDescriptor } from "./skill-router";

function skill(name: string, description: string): SkillDescriptor {
  return { name, description, filePath: `/skills/${name}/SKILL.md` };
}

test("abstains on generic overlap or incompatible domains even when a skill ranks first", () => {
  const web = skill("web-passwords", "Recover passwords from files in web CTF challenges. Analyze code and find flags.");
  const reverse = skill("reverse-engineering", "Reverse engineering ELF binaries and inspecting password checks.");
  assert.deepEqual(rankSkills("Solve a CTF challenge, analyze code files and find flags", [web]), []);
  const task = "Reverse engineer this ELF file and recover the password; download at https://example.test/artifact";
  assert.deepEqual(rankSkills(task, [web], 1), []);
  assert.deepEqual(rankSkills("逆向分析 ELF 文件，找到密码", [web], 1), []);
  assert.equal(rankSkills(task, [web, reverse], 1)[0]?.name, "reverse-engineering");
  // Mixed tasks may legitimately need a skill from either matching domain.
  const sql = skill("exploit-sqli", "SQL injection testing in Web applications.");
  assert.equal(rankSkills("Reverse the binary and examine embedded SQL injection", [sql], 1)[0]?.name, "exploit-sqli");
  assert.deepEqual(rankSkills("Inspect GraphQL schema", [sql], 1), []);
});

test("explicit no-match clears old active skills while a bare continuation preserves them", async () => {
  const active = new Set(["web-passwords"]);
  const loaded = new Set(["web-passwords"]);
  const skills = [skill("web-passwords", "Recover passwords in Web applications.")];
  updateActiveSkills(active, await prepareSkillPrompt("继续", skills, loaded));
  assert.deepEqual([...active], ["web-passwords"]);
  const rejected = await prepareSkillPrompt("Reverse this ELF and recover the password", skills, loaded);
  assert.equal(rejected.skillContext, "");
  assert.deepEqual(rejected.matched, []);
  assert.deepEqual(rejected.loaded, []);
  updateActiveSkills(active, rejected);
  assert.equal(active.size, 0);
  assert.deepEqual([...loaded], ["web-passwords"], "abstention does not mutate the loaded-file cache");
});

test("a matching but unreadable skill is not marked active", async () => {
  const selected = await prepareSkillPrompt("SQL injection", [{ ...skill("exploit-sqli", "SQL injection checks"), filePath: "/does-not-exist/skill-fixture/SKILL.md" }], new Set());
  assert.deepEqual(selected.matched, []);
  assert.equal(selected.skillContext, "");
});

test("ranks a domain skill from English and Chinese task wording", () => {
  const skills = [
    skill("exploit-sqli", "SQL injection detection and exploitation for URLs, forms, headers, and cookies."),
    skill("recon-dir-scan", "Discover hidden directories and files with path fuzzing."),
    skill("pentest-report", "Generate a structured penetration testing report.")
  ];
  assert.equal(rankSkills("检查登录接口的 SQL 注入", skills, 1)[0]?.name, "exploit-sqli");
  assert.equal(rankSkills("发现站点隐藏目录", skills, 1)[0]?.name, "recon-dir-scan");
  assert.equal(rankSkills("生成渗透测试报告", skills, 1)[0]?.name, "pentest-report");
});

test("loads the report skill only for an explicit report request", async () => {
  const root = await mkdtemp(join(tmpdir(), "riftx-report-skill-"));
  const filePath = join(root, "pentest-report", "SKILL.md");
  try {
    await mkdir(join(root, "pentest-report"), { recursive: true });
    await writeFile(filePath, "---\nname: pentest-report\ndescription: Formal report generation.\ndisable-model-invocation: true\n---\n\nUse the formal report template.\n");
    const descriptor = { ...skill("pentest-report", "Formal report generation."), filePath, disableModelInvocation: true };
    const loaded = new Set<string>();

    assert.deepEqual((await prepareSkillPrompt("总结本次漏洞发现", [descriptor], loaded)).loaded, []);
    assert.deepEqual((await prepareSkillPrompt("输出安全测试结果", [descriptor], loaded)).loaded, []);
    assert.deepEqual((await prepareSkillPrompt("为什么每次都会自动生成报告？", [descriptor], loaded)).loaded, []);
    assert.deepEqual((await prepareSkillPrompt("不要写报告，只给我简短总结", [descriptor], loaded)).loaded, []);

    const first = await prepareSkillPrompt("请生成一份正式渗透测试报告", [descriptor], loaded);
    const second = await prepareSkillPrompt("Generate a formal penetration testing report", [descriptor], loaded);
    assert.deepEqual(first.loaded, ["pentest-report"]);
    assert.deepEqual(second.loaded, ["pentest-report"]);
    assert.match(first.skillContext, /formal report template/i);
    assert.equal(loaded.has("pentest-report"), false);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("does not auto-load a skill disabled for model invocation", () => {
  const matches = rankSkills("test SQL injection", [
    { ...skill("exploit-sqli", "SQL injection testing."), disableModelInvocation: true },
    skill("generic-review", "Review application behavior.")
  ]);
  assert.equal(matches.some((item) => item.name === "exploit-sqli"), false);
});

test("loads skill instructions without changing the external file", async () => {
  const root = await mkdtemp(join(tmpdir(), "riftx-skill-router-"));
  const filePath = join(root, "review", "SKILL.md");
  try {
    await mkdir(join(root, "review"), { recursive: true });
    const original = "---\nname: review\ndescription: Review code.\n---\n\nUse focused checks.\n";
    await writeFile(filePath, original);
    const descriptor = { name: "review", description: "Review code.", filePath, baseDir: join(root, "review") };
    const context = await loadSkillContext(descriptor);
    assert.match(context, /Use focused checks/);
    assert.doesNotMatch(context, /description: Review code/);
    assert.equal(await readFile(filePath, "utf8"), original);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("automatically injects a matching skill once per session", async () => {
  const root = await mkdtemp(join(tmpdir(), "riftx-skill-router-"));
  const filePath = join(root, "sqli", "SKILL.md");
  try {
    await mkdir(join(root, "sqli"), { recursive: true });
    await writeFile(filePath, "---\nname: exploit-sqli\ndescription: SQL injection testing.\n---\n\nUse a minimal SQLi canary.\n");
    const descriptor = { name: "exploit-sqli", description: "SQL injection testing.", filePath };
    const loaded = new Set<string>();
    const first = await prepareSkillPrompt("Test SQL injection", [descriptor], loaded);
    const second = await prepareSkillPrompt("Test SQL injection again", [descriptor], loaded);
    assert.match(first.prompt, /Use a minimal SQLi canary/);
    assert.deepEqual(first.loaded, ["exploit-sqli"]);
    assert.deepEqual(first.matched, ["exploit-sqli"]);
    assert.equal(second.prompt, "Test SQL injection again");
    assert.deepEqual(second.loaded, []);
    assert.deepEqual(second.matched, ["exploit-sqli"]);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("restores active skills from the newest compaction metadata or historic skill context", () => {
  assert.deepEqual(activeSkillNamesFromBranch([
    { type: "custom_message", customType: "riftx_skill_context", content: '<skill name="web-passwords">synthetic</skill>' },
    { type: "custom_message", customType: "riftx_skill_context", content: "" }
  ]), [], "persisted abstention must not revive a previous skill on restart");
  assert.deepEqual(activeSkillNamesFromBranch([
    { type: "custom_message", customType: "riftx_skill_context", content: '<skill name="old-skill">x</skill>' },
    { type: "compaction", details: { riftx: { activeSkills: ["exploit-authz", "api-testing"] } } }
  ]), ["exploit-authz", "api-testing"]);
  assert.deepEqual(activeSkillNamesFromBranch([
    { type: "custom_message", customType: "riftx_skill_context", content: '<skill name="exploit-sqli">x</skill>' }
  ]), ["exploit-sqli"]);
});

test("routes authz, upload, API, and SSRF tasks to the gap-filling skills", () => {
  const skills = [
    skill("exploit-authz", "Broken access control / IDOR 越权测试：水平越权、垂直越权、功能级访问控制。privilege escalation."),
    skill("exploit-file-upload", "文件上传漏洞：扩展名校验绕过、图片马、SVG XSS、webshell 部署。upload bypass."),
    skill("api-testing", "API 安全测试 接口安全测试：REST、GraphQL、JWT、swagger。"),
    skill("exploit-ssrf", "Server-side request forgery SSRF 服务端请求伪造测试。"),
    skill("recon-crawl", "Attack-surface crawling with the crawl tool — BFS link/form collection, JS-bundle API route extraction.")
  ];
  assert.equal(rankSkills("越权测试", skills, 1)[0]?.name, "exploit-authz");
  assert.equal(rankSkills("水平越权对比", skills, 1)[0]?.name, "exploit-authz");
  assert.equal(rankSkills("文件上传绕过", skills, 1)[0]?.name, "exploit-file-upload");
  assert.equal(rankSkills("接口安全测试", skills, 1)[0]?.name, "api-testing");
  assert.equal(rankSkills("API测试", skills, 1)[0]?.name, "api-testing");
  assert.equal(rankSkills("SSRF测试", skills, 1)[0]?.name, "exploit-ssrf");
  assert.equal(rankSkills("爬取网站攻击面", skills, 1)[0]?.name, "recon-crawl");
});

test("object prototype keys in descriptions never crash the router", () => {
  const skills = [
    skill("exploit-proto-pollution", "Prototype pollution 原型污染：__proto__ 与 constructor.prototype 污染链。"),
    skill("api-testing", "API 安全测试。")
  ];
  // "constructor" / "valueOf" would index Object.prototype without the hasOwn guard.
  const top = rankSkills("constructor.prototype 污染怎么测", skills, 1)[0];
  assert.equal(top?.name, "exploit-proto-pollution");
  assert.doesNotThrow(() => rankSkills("valueOf toString", skills, 1));
});

test("short single-concept queries still auto-load their skill", () => {
  const skills = [
    skill("exploit-race", "Race condition 竞态条件测试：优惠券并发、TOCTOU。"),
    skill("exploit-authz", "IDOR 越权测试。"),
    skill("exploit-host-header", "Host 头攻击：密码重置投毒。"),
    skill("security-passwords", "password brute force 密码爆破 词表。")
  ];
  assert.equal(rankSkills("竞态", skills, 1)[0]?.name, "exploit-race");
  assert.equal(rankSkills("IDOR", skills, 1)[0]?.name, "exploit-authz");
  assert.equal(rankSkills("密码爆破", skills, 1)[0]?.name, "security-passwords");
});

test("matches complete Chinese aliases without bridging word boundaries", () => {
  for (const [query, name, description] of [
    ["大模型", "llm-model", "LLM model inspection"],
    ["提示词", "prompt-review", "Prompt analysis"],
    ["反编译", "decompile", "Binary decompilation"],
    ["密码学", "crypto", "Cryptography methods"],
    ["文件包含", "lfi", "Local file include"],
    ["模糊测试", "fuzzing", "Fuzz testing"]
  ]) assert.equal(rankSkills(query, [skill(name, description)])[0]?.name, name, query);
  assert.deepEqual(rankSkills("大，模型", [skill("llm-model", "LLM model inspection")]), []);
  assert.deepEqual(rankSkills("提 API 示词", [skill("prompt-review", "Prompt analysis")]), []);
});
