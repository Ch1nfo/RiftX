import type { AppConfig } from "@/lib/types";

export type ArchivedRestoreCode = "SESSION_NOT_ARCHIVED" | "SESSION_NOT_IN_WORKSPACE" | "SESSION_NOT_FOUND";
export type ArchivedRestoreBlock = "wrong-workspace" | "missing";

/** Remove every archived marker for one session without mutating the config. */
export function restoredArchiveState(config: Pick<AppConfig, "archivedSessionIds" | "archivedSessions">, id: string) {
  return {
    archivedSessionIds: config.archivedSessionIds.filter((item) => item !== id),
    archivedSessions: config.archivedSessions.filter((item) => item.id !== id)
  };
}

export function classifyArchivedRestore(input: {
  archived: boolean;
  inCurrentWorkspace: boolean;
  sessionFileExists: boolean;
}): { ok: true } | { ok: false; code: ArchivedRestoreCode } {
  if (!input.archived) return { ok: false, code: "SESSION_NOT_ARCHIVED" };
  if (input.inCurrentWorkspace) return { ok: true };
  if (input.sessionFileExists) return { ok: false, code: "SESSION_NOT_IN_WORKSPACE" };
  return { ok: false, code: "SESSION_NOT_FOUND" };
}

export function archivedRestoreBlock(inCurrentWorkspace: boolean, sessionFileExists: boolean): ArchivedRestoreBlock | undefined {
  const decision = classifyArchivedRestore({ archived: true, inCurrentWorkspace, sessionFileExists });
  if (decision.ok) return undefined;
  if (decision.code === "SESSION_NOT_IN_WORKSPACE") return "wrong-workspace";
  if (decision.code === "SESSION_NOT_FOUND") return "missing";
  return undefined;
}

export function archivedRestoreError(code: ArchivedRestoreCode): { message: string; status: number } {
  if (code === "SESSION_NOT_ARCHIVED") return { message: "session is not archived", status: 400 };
  if (code === "SESSION_NOT_IN_WORKSPACE") return { message: "Switch to this session's working directory before restoring", status: 404 };
  return { message: "Archived session data is no longer available", status: 404 };
}
