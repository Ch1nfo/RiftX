export type MergeableMessage = {
  id: string;
  role: "user" | "assistant" | "thinking" | "tool";
  content: string;
  toolName?: string;
  toolCallId?: string;
  status?: string;
  isError?: boolean;
};

// Tool cards only move forward through these states. A refetch snapshot can
// predate a locally observed terminal update (a tool_end arriving over SSE
// while the fetch response was still in flight), so merged tool cards compare
// progress instead of letting one side win unconditionally.
const TOOL_STATUS_RANK: Record<string, number> = { queued: 0, running: 1, done: 2, error: 2, cancelled: 2 };

function toolStatusRank(status?: string) {
  // Settled tools are stored without a live status, so absent means settled —
  // the safe default that never resurrects a spinning card.
  return TOOL_STATUS_RANK[status ?? ""] ?? 2;
}

/** Merge a server snapshot without letting a stale local tool state win. */
export function mergeFetchedMessages<T extends MergeableMessage>(current: T[], fetched: T[]) {
  const next = fetched.map((item) => ({ ...item }));
  const used = new Set<number>();
  const matched = new Array<number>(current.length).fill(-1);
  let lastMatchedPosition = -1;
  current.forEach((local, position) => {
    let index = next.findIndex((item, candidate) => !used.has(candidate) && (item.id === local.id || (item.toolCallId && item.toolCallId === local.toolCallId)));
    if (index < 0 && (local.role === "assistant" || local.role === "thinking")) {
      index = next.findIndex((item, candidate) => !used.has(candidate) && item.role === local.role
        && (item.content.startsWith(local.content) || local.content.startsWith(item.content)));
    }
    if (index < 0 && local.role === "user") {
      // Optimistic echoes carry a client UUID while the server stores a
      // positional id; user content is immutable once sent, so equal content
      // identifies the same message across a reconnect refetch.
      index = next.findIndex((item, candidate) => !used.has(candidate) && item.role === "user" && item.content === local.content);
    }
    if (index < 0) return;
    used.add(index);
    matched[position] = index;
    lastMatchedPosition = position;
  });
  current.forEach((local, position) => {
    const index = matched[position];
    if (index < 0) {
      // Keep only trailing messages — optimistic echoes and in-flight
      // streaming that the snapshot predates. An unmatched local message
      // that sits before a matched one was dropped from the snapshot (e.g.
      // by compaction) and must not be re-appended after it.
      if (position > lastMatchedPosition) next.push(local);
      return;
    }
    const remote = next[index];
    const content = local.content.length >= remote.content.length ? local.content : remote.content;
    next[index] = remote.role === "tool"
      ? toolStatusRank(local.status) > toolStatusRank(remote.status)
        // status is set explicitly: a locally settled card stored without the
        // property must still override a stale remote "running" — a plain
        // spread skips absent keys and would let it through.
        ? { ...remote, ...local, status: local.status }
        : { ...local, ...remote, content }
      : local.content.length >= remote.content.length ? { ...remote, ...local } : { ...local, ...remote };
  });
  return next;
}
