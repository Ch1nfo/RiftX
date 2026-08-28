import assert from "node:assert/strict";
import { mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { loadSkillContext, prepareSkillPrompt, rankSkills, type SkillDescriptor } from "./skill-router";

function skill(name: string, description: string): SkillDescriptor {
  return { name, description, filePath: `/skills/${name}/SKILL.md` };
}

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
    assert.equal(second.prompt, "Test SQL injection again");
    assert.deepEqual(second.loaded, []);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
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
