import { readFile, mkdir, writeFile } from "node:fs/promises";
import { dirname, join } from "node:path";
import { COLLABORATION_MARKER, COLLABORATION_PROTOCOL } from "@/lib/collaboration";
import { BoardError } from "./store";
import { boardPath } from "./integration";

type ProtocolSession = {
  getSessionId(): string;
  getEntries(): Array<{ type: string; customType?: string; data?: unknown }>;
  appendCustomEntry(type: string, data: unknown): unknown;
};
/** Explicit marker survives the SDK's deferred transcript flush on the first turn. */
export async function sessionProtocol(root: string, session: ProtocolSession, fresh: boolean) {
  const file = join(dirname(boardPath(root, session.getSessionId())), "protocol.json");
  if (fresh) {
    const marker = { version: COLLABORATION_PROTOCOL };
    await mkdir(dirname(file), { recursive: true, mode: 0o700 });
    await writeFile(file, JSON.stringify(marker), { flag: "wx", mode: 0o600 });
    session.appendCustomEntry(COLLABORATION_MARKER, marker);
    return true;
  }
  const entry = session.getEntries().find((e) => e.type === "custom" && e.customType === COLLABORATION_MARKER);
  let marker = entry?.data as { version?: unknown } | undefined;
  try { marker = JSON.parse(await readFile(file, "utf8")); }
  catch (error) {
    if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw new BoardError("BOARD_UNAVAILABLE", "Collaboration protocol marker cannot be read", 503);
  }
  if (!marker) return false;
  if (marker.version !== COLLABORATION_PROTOCOL) throw new BoardError("BOARD_UNAVAILABLE", "Unsupported collaboration protocol; execution is paused", 503);
  return true;
}
