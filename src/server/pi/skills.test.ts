import assert from "node:assert/strict";
import { mkdtemp, mkdir, rm, writeFile } from "node:fs/promises";
import { homedir, tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { pathToFileURL } from "node:url";
import { getAppPaths } from "@/server/config-store";

test("loads skills from the RiftX user skills directory", async () => {
  const root = await mkdtemp(join(tmpdir(), "riftx-skills-"));
  const skillDir = join(root, "review-api");
  try {
    const { DefaultResourceLoader, formatSkillsForPrompt } = await import(pathToFileURL(join(process.cwd(), "node_modules/@mariozechner/pi-coding-agent/dist/index.js")).href);
    await mkdir(skillDir, { recursive: true });
    await writeFile(join(skillDir, "SKILL.md"), "---\nname: review-api\ndescription: Review API routes for security issues.\n---\n\nUse focused checks.\n");
    const loader = new DefaultResourceLoader({
      cwd: root,
      agentDir: join(root, "agent"),
      additionalSkillPaths: [root],
      extensionFactories: [],
      noExtensions: true,
      noSkills: true,
      noPromptTemplates: true,
      noThemes: true,
      noContextFiles: true,
      systemPrompt: "RiftX test prompt"
    });
    await loader.reload();
    const { skills, diagnostics } = loader.getSkills();
    assert.equal(diagnostics.length, 0);
    assert.deepEqual((skills as Array<{ name: string }>).map((item) => item.name), ["review-api"]);
    const skill = (skills as Array<{ name: string; filePath: string }>)[0];
    assert.equal(skill.filePath, join(skillDir, "SKILL.md"));
    assert.match(formatSkillsForPrompt(skills), /<name>review-api<\/name>/);
    assert.match(formatSkillsForPrompt(skills), new RegExp(`<location>${skillDir.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}`));
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("uses a platform-native user skill path", () => {
  assert.equal(getAppPaths().skills, join(homedir(), ".riftx", "skills"));
});
