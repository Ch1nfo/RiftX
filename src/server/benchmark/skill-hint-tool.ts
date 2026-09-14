import { Type } from "@sinclair/typebox";
import type { ToolDefinition } from "@mariozechner/pi-coding-agent";
import { loadSkillContext, rankSkills, type SkillDescriptor } from "@/server/pi/skill-router";

/** Two full skill documents plus wrappers can easily exceed 10k characters;
 * beyond this the returned guidance stops being readable mid-turn. */
const MAX_RETURNED_CONTENT_CHARS = 12_000;

export function createBenchmarkSkillHintTool(getSkills: () => readonly SkillDescriptor[], getChallenge: () => string): ToolDefinition {
  return {
    name: "benchmark_skill_hint",
    label: "Benchmark skill hint",
    description: "When stuck, search the available local skills for up to two relevant references and return their full contents. This is optional guidance and does not change active skills. If nothing is relevant, continue independently.",
    promptSnippet: "benchmark_skill_hint(query?)",
    parameters: Type.Object({ query: Type.Optional(Type.String({ maxLength: 500, description: "The current obstacle, technique, or phase you want help with" })) }),
    async execute(_id, params: { query?: string }) {
      const challenge = getChallenge();
      const query = params.query?.trim();
      // The obstacle query is ranked on its own terms: the router's domain guard
      // must see the query's domain, not only the challenge's — otherwise a web
      // challenge could never pull a crypto or pwn skill while stuck. Challenge
      // matches only fill the remaining slots.
      const ranked = query
        ? [...rankSkills(query, getSkills(), 2), ...rankSkills(challenge, getSkills(), 2)]
        : rankSkills(challenge, getSkills(), 2);
      const seen = new Set<string>();
      const matches = ranked.filter((skill) => {
        if (seen.has(skill.name)) return false;
        seen.add(skill.name);
        return true;
      }).slice(0, 2);
      if (!matches.length) return { content: [{ type: "text" as const, text: "No skill is particularly relevant to this challenge. Continue independently." }], details: { matched: [] } };
      const loaded = await Promise.all(matches.map(async (skill) => {
        try { return { name: skill.name, context: await loadSkillContext(skill) }; } catch { return null; }
      }));
      const usable = loaded.filter((item): item is { name: string; context: string } => Boolean(item));
      if (!usable.length) return { content: [{ type: "text" as const, text: "No skill is particularly relevant to this challenge. Continue independently." }], details: { matched: [] } };
      const parts: string[] = [];
      const omitted: string[] = [];
      for (const item of usable) {
        if (parts.join("\n\n").length + item.context.length > MAX_RETURNED_CONTENT_CHARS) { omitted.push(item.name); continue; }
        parts.push(item.context);
      }
      const body = parts.join("\n\n") || "No skill is particularly relevant to this challenge. Continue independently.";
      const text = `${body}${omitted.length ? `\n\n[Skill document(s) omitted by the size budget: ${omitted.join(", ")} — re-query with a narrower query.]` : ""}`;
      return { content: [{ type: "text" as const, text }], details: { matched: usable.filter((item) => !omitted.includes(item.name)).map((item) => item.name) } };
    }
  };
}
