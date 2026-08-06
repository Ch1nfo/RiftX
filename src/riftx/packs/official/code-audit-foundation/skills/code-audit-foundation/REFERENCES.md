# Code Audit Foundation References

- The owner-bound Workspace or sealed Audit Snapshot is the only authoritative code source for a Run.
- Code Audit remains read-only; a Skill cannot enable Patch, Worktree, Shell, project execution, or target interaction.
- `controlled_lsp` and `builtin_static` are distinct analysis qualities and must remain visibly distinguishable.
- Confirmed findings require direct source evidence, a verified reasoning path, and replayable impact evidence when the claim depends on runtime behavior.
- Partial Closure must expose uncovered regions and evidence gaps rather than inventing completeness.
