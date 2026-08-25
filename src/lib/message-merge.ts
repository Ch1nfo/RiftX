export type MergeableMessage = {
  id: string;
  role: "user" | "assistant" | "thinking" | "tool";
  content: string;
  toolName?: string;
  toolCallId?: string;
  status?: string;
  isError?: boolean;
};

/** Merge a server snapshot without letting a stale local tool state win. */
export function mergeFetchedMessages<T extends MergeableMessage>(current: T[], fetched: T[]) {
  const next = fetched.map((item) => ({ ...item }));
  const used = new Set<number>();
  for (const local of current) {
    let index = next.findIndex((item, candidate) => !used.has(candidate) && (item.id === local.id || (item.toolCallId && item.toolCallId === local.toolCallId)));
    if (index < 0 && (local.role === "assistant" || local.role === "thinking")) {
      index = next.findIndex((item, candidate) => !used.has(candidate) && item.role === local.role
        && (item.content.startsWith(local.content) || local.content.startsWith(item.content)));
    }
    if (index < 0) {
      next.push(local);
      continue;
    }
    used.add(index);
    const remote = next[index];
    const content = local.content.length >= remote.content.length ? local.content : remote.content;
    next[index] = remote.role === "tool"
      ? { ...local, ...remote, content }
      : local.content.length >= remote.content.length ? { ...remote, ...local } : { ...local, ...remote };
  }
  return next;
}
