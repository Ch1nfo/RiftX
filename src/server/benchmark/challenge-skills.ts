import { prepareSkillPrompt, updateActiveSkills, type SkillDescriptor } from "@/server/pi/skill-router";

/** Challenge descriptions select skills; coordinator messages and briefs do not. */
export function createChallengeSkillSelection(
  skills: readonly SkillDescriptor[],
  active: Set<string>,
  persistContext: (context: string) => void
) {
  return async (description?: string) => {
    active.clear();
    // Always persist the selection, including returning to a previously used
    // skill or choosing none. A loaded-file cache is not selection history.
    const selected = await prepareSkillPrompt(description ?? "", skills, new Set());
    updateActiveSkills(active, { ...selected, resetActiveSkills: true });
    persistContext(selected.skillContext);
  };
}
