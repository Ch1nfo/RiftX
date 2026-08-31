import type { SessionSummary } from "./types";

function timestamp(value: string) {
  const parsed = Date.parse(value);
  return Number.isFinite(parsed) ? parsed : 0;
}

/** Running sessions lead the list; each group remains newest-first and stable on ties. */
export function orderSessionsByActivity(items: SessionSummary[]) {
  return items
    .map((session, index) => ({ session, index }))
    .sort((left, right) => {
      const runningDelta = Number(Boolean(right.session.running)) - Number(Boolean(left.session.running));
      if (runningDelta !== 0) return runningDelta;
      const timeDelta = timestamp(right.session.updatedAt) - timestamp(left.session.updatedAt);
      return timeDelta !== 0 ? timeDelta : left.index - right.index;
    })
    .map(({ session }) => session);
}

export function withSessionActivity(items: SessionSummary[], id: string, running: boolean, touch = false) {
  const updatedAt = touch ? new Date().toISOString() : undefined;
  let changed = false;
  const next = items.map((session) => {
    if (session.id !== id) return session;
    if (!touch && Boolean(session.running) === running) return session;
    changed = true;
    return { ...session, running, ...(updatedAt ? { updatedAt } : {}) };
  });
  return changed ? next : items;
}

export function withRunningSessionIds(items: SessionSummary[], runningIds: Iterable<string>) {
  const running = new Set(runningIds);
  let changed = false;
  const next = items.map((session) => {
    const isRunning = running.has(session.id);
    if (Boolean(session.running) === isRunning) return session;
    changed = true;
    return { ...session, running: isRunning };
  });
  return changed ? next : items;
}
